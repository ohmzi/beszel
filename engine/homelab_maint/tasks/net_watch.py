"""net_watch: the Network Watch verdict, every 5 minutes. Read-only: it reads the netwatch store, judges the last day's window and
reports. The runner debounces and emails; a finding is a fact about the window, never a block (blocks are the dashboard's, by hand).

It also writes netwatch/findings.json for the publisher. A source that cannot be read is reported stale, never "all fine".
"""
from __future__ import annotations

import dataclasses
import re
import sqlite3

from .. import core
from ..core import Ctx, Result, ikey, task
from ..netwatch import entities, judge, store
from ..netwatch.threat import ThreatIndex

STALE_S = 1800            # a source is stale when its last write is this old, and counts toward the warning once stale this long
DAY_S = 86400
ITEMS_MAX = 12
WHY_MAX = 120
TALKERS_MAX = 120
SUMMARY_MAX = 140
EDGE_SOURCE = "cloudflare"
FLOW_DIRECTIONS = ("in", "out", "svc", "lan")


def _clip(text, n: int) -> str:
    """Summaries can become an SMS: one line, ASCII only, bounded."""
    return re.sub(r"[^\x20-\x7e]", "?", " ".join(str(text).split()))[:n]


def _unreadable(exc: Exception) -> Result:
    return Result("error", f"Network: store unreadable ({type(exc).__name__})", alert=True)


# --------------------------------------------------------------------------- sources and staleness
def _parse_source(value: str) -> tuple[str, float | None]:
    """meta source:<name> is '<status>|<epoch>'. A value that does not parse gives an unknown epoch (stale, fail closed)."""
    status, sep, epoch = value.rpartition("|")
    if not sep:
        return value, None
    try:
        return status, float(epoch)
    except ValueError:
        return status, None


def _is_stale(status: str, epoch: float | None, now: float) -> bool:
    if status == "skipped":
        return False
    if status.startswith("stale") or epoch is None:
        return True
    return now - epoch > STALE_S


def _stale_sources(db, now: float, threat: ThreatIndex) -> list[str]:
    """Sources stale for STALE_S or more, by name. Keeps meta stale_since:<name> (set on the first sight of staleness, removed once ok)."""
    stale_now: dict[str, bool] = {}
    for key, value in db.execute("SELECT key, value FROM meta WHERE key LIKE 'source:%'").fetchall():
        status, epoch = _parse_source(value)
        stale_now[key[len("source:"):]] = _is_stale(status, epoch, now)
    for name in threat.stale_lists(now):
        stale_now[f"threat:{name}"] = True

    counted = []
    for name, stale in sorted(stale_now.items()):
        marker = f"stale_since:{name}"
        if not stale:
            with db:
                db.execute("DELETE FROM meta WHERE key = ?", (marker,))
            continue
        try:
            since = float(store.meta_get(db, marker))
        except (TypeError, ValueError):
            since = now
            store.meta_set(db, marker, repr(now))
        if now - since >= STALE_S:
            counted.append(name)
    return counted


# --------------------------------------------------------------------------- the verdict
def _metrics(db, now: float, findings: list, stale: list[str]) -> dict:
    lo = now - DAY_S
    flows = dict(db.execute("SELECT direction, SUM(conns) FROM flows WHERE bucket >= ? GROUP BY direction", (lo,)).fetchall())
    edge_total = db.execute("SELECT COALESCE(SUM(count), 0) FROM edge WHERE bucket >= ?", (lo,)).fetchone()[0]
    blocked = db.execute("SELECT COUNT(*) FROM blocks WHERE removed IS NULL AND (until IS NULL OR until > ?)", (now,)).fetchone()[0]
    metrics = {f"{d}24": int(flows.get(d) or 0) for d in FLOW_DIRECTIONS}
    metrics.update(edge24=int(edge_total), flags=len(findings), blocked=int(blocked), stale_sources=len(stale),
                   top_talkers=_top_talkers(db, lo))
    return metrics


def _top_talkers(db, lo: float) -> str:
    """'ip CC count; ...' for the three internet addresses with the most edge requests in the window, at most TALKERS_MAX characters."""
    rows = db.execute("SELECT client_ip, country, SUM(count) AS n FROM edge WHERE bucket >= ? GROUP BY client_ip, country ORDER BY n DESC",
                      (lo,)).fetchall()
    entries: list[str] = []
    ips: set[str] = set()
    for ip, country, n in rows:
        if ip in ips:
            continue
        entry = f"{ip} {country} {int(n)}"
        if len(entries) == 3 or len("; ".join(entries + [entry])) > TALKERS_MAX:
            break
        entries.append(entry)
        ips.add(ip)
    return "; ".join(entries)


def _summary(crit: list, warn: list, stale: list[str]) -> str:
    parts = []
    if crit:
        parts.append(f"{len(crit)} crit ({crit[0].rule} {crit[0].src})")
    if warn:
        parts.append(f"{len(warn)} warn")
    if stale:
        parts.append("edge data stale" if stale == [EDGE_SOURCE] else f"{len(stale)} sources stale: {', '.join(stale)}")
    return _clip(f"Network: {', '.join(parts) if parts else 'no findings'}", SUMMARY_MAX)


def _verdict(ctx: Ctx, db, cfg: dict, threat: ThreatIndex) -> Result:
    now = ctx.now
    try:
        topo = entities.load_topology()
    except Exception:  # noqa: BLE001  (fail closed: an unknown topology drops every block and every classification rule)
        topo = entities.Topology.empty_stale()
    findings = judge.judge(db, topo, threat, now, cfg)
    ordered = sorted(findings, key=lambda f: (f.severity != "crit", -f.last))
    stale = _stale_sources(db, now, threat)
    crit = [f for f in ordered if f.severity == "crit"]
    warn = [f for f in ordered if f.severity == "warn"]

    if crit:
        status = "crit"
    elif warn or stale:
        status = "warn"
    else:
        status = "ok"

    items = [{"rule": f.rule, "severity": f.severity, "src": f.src, "target": f.target, "why": f.why[:WHY_MAX],
              "count": f.count, "id": f.id} for f in ordered[:ITEMS_MAX]]
    issue_key = ikey(rules=sorted(f"{f.rule}:{f.src}" for f in findings), stale=stale) if findings or stale else None
    link = f"/network?finding={ordered[0].id}" if ordered else ""

    core.write_json_atomic(core.STATE_DIR / "netwatch" / "findings.json",
                           {"generated": now, "findings": [dataclasses.asdict(f) for f in ordered]}, 0o600)
    return Result(status, _summary(crit, warn, stale), metrics=_metrics(db, now, findings, stale), items=items,
                  issue_key=issue_key, link=link)


@task("net_watch", klass="C0", tier="check", title="Network watch", timeout=60, needs_root=True)
def run(ctx: Ctx) -> Result:
    cfg = ctx.cfg.get("netwatch", {}) or {}
    try:
        db = store.open_db()
    except (OSError, sqlite3.Error) as exc:
        return _unreadable(exc)
    try:
        try:
            threat = ThreatIndex.load(core.STATE_DIR / "netwatch" / "threat", core.CONF_DIR / "netwatch-ioc.toml")
        except (OSError, ValueError) as exc:
            return Result("error", f"Network: threat lists unreadable ({type(exc).__name__})", alert=True)
        return _verdict(ctx, db, cfg, threat)
    except (OSError, sqlite3.Error) as exc:
        return _unreadable(exc)
    finally:
        db.close()
