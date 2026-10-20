"""network.json: the Network Watch data for the OhmzMaintainer dashboard (the public file behind /network).

`build_network(status, now, db=None, findings_path=None) -> dict` reads the netwatch store and the findings file the net_watch
task wrote, and returns a plain dict. Read-only: nothing here writes the store or a host file, and the Cloudflare token file is
never opened. Every string leaf goes through publish's `clean` (the same redaction every public file gets). The result is
capped at SIZE_CAP bytes of compact JSON: talkers, then services, then the lowest-severity findings are dropped until it fits.

Without the store (STATE_DIR/netwatch.db missing, or unreadable) the result is {"available": False, "generated": now}, and the
store is never created here.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from .. import core
from ..publish import clean
from ..tasks import net_watch
from . import entities, judge, protect, store
from .threat import ThreatIndex

SIZE_CAP = 512 * 1024
DAY_S = 86400
TALKERS_MAX = 50
SERVICES_MAX = 100
HOSTS_PER_TALKER = 5
ROWS_MAX = 200                      # blocks and sign-ins: bounded even though they are small in practice
FLOW_DIRECTIONS = ("in", "out", "svc", "lan")
TEXT_MAX = 200


def _store_path() -> Path:
    return core.STATE_DIR / "netwatch.db"


def _findings_path() -> Path:
    return core.STATE_DIR / "netwatch" / "findings.json"


def _text(v: Any, n: int = TEXT_MAX) -> str:
    return clean(v, n)


def _flat(row: dict) -> dict:
    """A flat copy of one findings row: scrubbed strings, scalars kept, anything nested dropped."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, str):
            out[_text(k, 40)] = _text(v)
        elif v is None or isinstance(v, (bool, int)):
            out[_text(k, 40)] = v
        elif isinstance(v, float) and math.isfinite(v):
            out[_text(k, 40)] = v
    return out


def _directions(db, now: float) -> dict:
    out: dict = {}
    for label, secs in (("24h", DAY_S), ("30d", 30 * DAY_S)):
        totals = dict(db.execute("SELECT direction, SUM(conns) FROM flows WHERE bucket >= ? GROUP BY direction", (now - secs,)).fetchall())
        out[label] = {d: int(totals.get(d) or 0) for d in FLOW_DIRECTIONS}
    return out


def _hosts(db, lo: float, ip: str) -> list[str]:
    rows = db.execute("SELECT DISTINCT host FROM edge WHERE bucket >= ? AND client_ip = ? ORDER BY host LIMIT ?",
                      (lo, ip, HOSTS_PER_TALKER)).fetchall()
    return [_text(h, 120) for (h,) in rows]


def _talkers(db, now: float, topo, cfg: dict, threat: ThreatIndex | None) -> list[dict]:
    lo = now - DAY_S
    rows = db.execute("SELECT client_ip, MAX(country), MAX(asn), SUM(count) AS n FROM edge WHERE bucket >= ? "
                      "GROUP BY client_ip ORDER BY n DESC, client_ip LIMIT ?", (lo, TALKERS_MAX)).fetchall()
    out = []
    for ip, country, asn, n in rows:
        hit = None
        if threat is not None:
            try:
                hit = threat.lookup(ip)
            except ValueError:                          # not an address: the edge row is not one we can classify
                hit = None
        prot = protect.protection(ip, db, topo, now, cfg)
        out.append({
            "ip": _text(ip, 64),
            "country": _text(country or "", 8),
            "asn": int(asn or 0),
            "hosts": _hosts(db, lo, ip),
            "requests": int(n or 0),
            "threat": None if threat is None else ([hit] if hit else []),
            "protection": prot[0] if prot else None,
        })
    return out


def _services(db, now: float) -> list[dict]:
    """Per local entity: the distinct peers that connected in and out over 24 h. A flow's direction says which end is local."""
    peers: dict[str, dict[str, set]] = {}

    def add(entity: str, side: str, peer: str) -> None:
        peers.setdefault(entity, {"in": set(), "out": set()})[side].add(peer)

    rows = db.execute("SELECT direction, src, dst FROM flows WHERE bucket >= ? GROUP BY direction, src, dst", (now - DAY_S,)).fetchall()
    for direction, src, dst in rows:
        if direction == "in":
            add(dst, "in", src)
        elif direction == "out":
            add(src, "out", dst)
        elif direction == "svc":
            add(src, "out", dst)
            add(dst, "in", src)
        elif direction == "lan":
            if dst.startswith(("lan:", "ts:")):
                add(src, "out", dst)
            else:
                add(dst, "in", src)
    ranked = sorted(peers.items(), key=lambda kv: (-(len(kv[1]["in"]) + len(kv[1]["out"])), kv[0]))[:SERVICES_MAX]
    return [{"name": _text(name, 80), "in": len(sides["in"]), "out": len(sides["out"])} for name, sides in ranked]


def _findings(path: Path) -> list[dict]:
    """The open findings net_watch wrote (critical first, so trimming removes the lesser ones), or [] when there is no file."""
    doc = core.read_json(path, None)
    rows = doc.get("findings") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    flat = [_flat(r) for r in rows if isinstance(r, dict)]
    flat.sort(key=lambda f: f.get("severity") != "crit")          # stable: crit first, then the order net_watch gave
    return flat


def _blocks(db, now: float) -> list[dict]:
    rows = db.execute("SELECT id, ip, target, zone, until, permanent, reason FROM blocks "
                      "WHERE removed IS NULL AND (until IS NULL OR until > ?) ORDER BY created DESC LIMIT ?", (now, ROWS_MAX)).fetchall()
    return [{"id": _text(i, 64), "ip": _text(ip, 64), "target": _text(t, 40), "zone": _text(z, 80),
             "until": u, "permanent": bool(p), "reason": _text(r)} for i, ip, t, z, u, p, r in rows]


def _signins(db, now: float) -> list[dict]:
    rows = db.execute("SELECT ip, source, last_seen FROM signins WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?",
                      (now - protect.SIGNIN_S, ROWS_MAX)).fetchall()
    return [{"ip": _text(ip, 64), "source": _text(src, 40), "last_seen": seen} for ip, src, seen in rows]


def _sources(db, now: float) -> tuple[dict, list[str]]:
    """Every source's status and age, and the names that are stale: status 'stale...', an epoch older than STALE_S, an epoch that
    does not parse, or a known source with no row at all (the collector never wrote it). Fail closed: stale is never 'all fine'."""
    out: dict = {}
    seen: set[str] = set()
    for key, value in db.execute("SELECT key, value FROM meta WHERE key LIKE 'source:%'").fetchall():
        name = _text(key[len("source:"):], 40)
        status, epoch = net_watch._parse_source(str(value))
        age = None if epoch is None else max(0, int(now - epoch))
        out[name] = {"status": _text(status, 40), "age_s": age}
        seen.add(name)
    stale = []
    for name in sorted(out):
        status, age = out[name]["status"], out[name]["age_s"]
        if status.startswith("stale") or age is None or age > net_watch.STALE_S:
            stale.append(name)
    for name in net_watch.KNOWN_SOURCES:
        if name not in seen:
            out[name] = {"status": "missing", "age_s": None}
            stale.append(name)
    return out, sorted(set(stale))


def _learning_until(db) -> float | None:
    try:
        return float(store.meta_get(db, "first_run")) + judge.LEARNING_S
    except (TypeError, ValueError):
        return None


def _topology():
    try:
        return entities.load_topology()
    except Exception:  # noqa: BLE001  (an unknown topology: the fallback ranges, never a crash of the export)
        return entities.Topology.empty_stale()


def _threat() -> ThreatIndex | None:
    try:
        return ThreatIndex.load(core.STATE_DIR / "netwatch" / "threat", core.CONF_DIR / "netwatch-ioc.toml")
    except (OSError, ValueError):
        return None


def _size(doc: dict) -> int:
    return len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace"))


def _fit(doc: dict) -> dict:
    """Drop talkers, then services, then the least severe findings (a quarter at a time) until the document is within SIZE_CAP."""
    while _size(doc) > SIZE_CAP:
        if doc["talkers"]:
            del doc["talkers"][-max(1, len(doc["talkers"]) // 4):]
        elif doc["services"]:
            del doc["services"][-max(1, len(doc["services"]) // 4):]
        elif doc["findings"]:
            f = doc["findings"]
            crit = sum(1 for x in f if x.get("severity") == "crit")
            rest = len(f) - crit                       # crit rows sit at the front: trim the non-crit tail first
            if rest:
                del f[len(f) - max(1, rest // 4):]
            else:
                del f[-max(1, len(f) // 4):]
        else:
            break
    return doc


def build_network(status: dict, now: float, db=None, findings_path: Path | None = None) -> dict:
    """The network.json document. `status` is the status.json the publisher read (the store and the findings file are the data)."""
    now = float(now)
    own = None
    if db is None:
        path = _store_path()
        if not path.exists():
            return {"available": False, "generated": now}
        try:                                              # read-only: no DDL, no WAL switch, no chmod, the collector keeps writing
            own = db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            return {"available": False, "generated": now}
    try:
        topo = _topology()
        try:
            cfg = (core.load_config().get("netwatch") or {})
        except Exception:  # noqa: BLE001
            cfg = {}
        threat = _threat()
        sources, stale = _sources(db, now)
        doc = {
            "generated": now,
            "learning_until": _learning_until(db),
            "sources": sources,
            "stale": stale,
            "directions": _directions(db, now),
            "talkers": _talkers(db, now, topo, cfg, threat),
            "services": _services(db, now),
            "findings": _findings(Path(findings_path) if findings_path is not None else _findings_path()),
            "blocks": _blocks(db, now),
            "protected": {"signins": _signins(db, now), "hard_ranges": [str(n) for n in entities.HARD_RANGES(topo, cfg)]},
        }
        return _fit(doc)
    finally:
        if own is not None:
            own.close()
