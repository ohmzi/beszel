"""netwatch.judge: turn the stored flows, edge rows and events into Findings.

judge() looks back over the last WINDOW_S seconds. Events carry an exact ts, so they are compared directly. Flows and edge rows
are hourly buckets with no per-request time, so a bucket counts when it overlaps the window (bucket >= the hour that holds the
window's start). A finding lives across the runner's two-run debounce because the window is wider than the run interval.

A finding is a fact about the window, never a block. `block` is only the suggestion the dashboard offers a logged-in user.
Every finding whose src is an IP goes through protection(): a hard range drops block to None, a sign-in keeps the block and sets
stolen_account. A stale topology drops every block, and skips the rules that depend on classification (new_peer, line_not_meant).

The baseline table is the only thing written. Nothing here changes the firewall, Cloudflare or the host.
"""
import hashlib
import ipaddress
from collections import defaultdict
from dataclasses import dataclass, replace

from homelab_maint.netwatch import entities, plex, protect, store
from homelab_maint.netwatch.entities import Topology, classify
from homelab_maint.netwatch.threat import ThreatIndex

WINDOW_S = 1800            # the look-back for every rule
SSH_WINDOW_S = 600         # sshd failures are counted over 10 minutes
SSH_BURST = 5              # this many failed sshd sign-ins from one IP is a burst
EDGE_AUTH_BURST = 10       # this many edge 401/403 on an auth_* path class from one IP is a burst
THREAT_EDGE_MIN = 20       # more than this many edge requests from a listed IP is more than scanning
LEARNING_S = 7 * 86400     # new_peer is silent this long after the first collector start
HOUR_S = 3600
EXPLAIN_MAX = 3            # candidate addresses listed in a plex_exploit reason


@dataclass(frozen=True)
class Finding:
    id: str
    rule: str
    severity: str  # "warn" | "crit"
    src: str
    target: str
    why: str
    first: float
    last: float
    count: int
    block: str | None  # "host" | "cloudflare" | None
    zone: str | None
    stolen_account: str | None


def _fid(rule: str, src: str, target: str) -> str:
    return hashlib.sha1(f"{rule}|{src}|{target}".encode()).hexdigest()[:16]


def _hour(ts: float) -> int:
    ts = int(ts)
    return ts - ts % HOUR_S


def _ip(text) -> str | None:
    """The normalised address text, or None when `text` is not an IP address (entity names, "unknown")."""
    try:
        addr = ipaddress.ip_address(str(text).strip())
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def _internet(ip: str, topo: Topology, cfg: dict) -> bool:
    return classify(ip, topo, cfg).kind == "internet"


def _net(ip: str) -> str:
    """The /24 (IPv4) or /48 (IPv6) network that holds `ip`, as text."""
    addr = ipaddress.ip_address(ip)
    return str(ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 48}", strict=False))


def _is_host_ip(dst: str, topo: Topology) -> bool:
    try:
        return ipaddress.ip_address(str(dst).strip()) in topo.host_ips
    except ValueError:
        return False


def _add(found: dict, rule: str, severity: str, src: str, target: str, why: str, first: float, last: float, count: int,
         block: str | None = None, zone: str | None = None) -> None:
    """Record a finding, merging repeats of the same (rule, src, target) into one entry with a summed count and a widened span."""
    fid = _fid(rule, src, target)
    old = found.get(fid)
    if old is None:
        found[fid] = Finding(fid, rule, severity, src, target, why, first, last, count, block, zone, None)
    else:
        found[fid] = replace(old, first=min(old.first, first), last=max(old.last, last), count=old.count + count)


# ---- rules ----------------------------------------------------------------------------------------------------------------

def _plex(db, topo: Topology, cfg: dict, lo: float, now: float, found: dict) -> None:
    for ts, detail in db.execute(
        "SELECT ts, detail FROM events WHERE kind = 'plex_exploit' AND ts >= ? AND ts <= ? ORDER BY ts", (lo, now)
    ).fetchall():
        peers = set()
        for (raw,) in db.execute(
            "SELECT DISTINCT src FROM flows WHERE direction = 'in' AND dport = ? AND bucket = ?", (plex.PLEX_PORT, _hour(ts))
        ):
            ip = _ip(raw)
            if ip is not None and _internet(ip, topo, cfg):
                peers.add(ip)
        candidates = sorted(peers)
        if len(candidates) == 1:
            _add(found, "plex_exploit", "crit", candidates[0], "plex",
                 f"plex exploit request {detail}; inferred: the only internet peer on 32400 that hour",
                 ts, ts, 1, block="host")
            continue
        why = f"plex exploit request {detail}; {len(candidates)} internet peers on 32400 that hour"
        if candidates:
            why += ": " + ", ".join(candidates[:EXPLAIN_MAX])
        _add(found, "plex_exploit", "crit", "unknown", "plex", why, ts, ts, 1)


def _codec(db, lo: float, now: float, found: dict) -> None:
    for ts, detail in db.execute(
        "SELECT ts, detail FROM events WHERE kind = 'codec_unexplained' AND ts >= ? AND ts <= ? ORDER BY ts", (lo, now)
    ).fetchall():
        _add(found, "codec_unexplained", "crit", "unknown", detail,
             f"Plex codec library {detail} changed with no matching Plex request", ts, ts, 1)


def _threat(db, topo: Topology, threat: ThreatIndex, cfg: dict, lo: float, now: float, found: dict) -> None:
    edge: dict[str, list] = defaultdict(list)
    for bucket, zone, host, client_ip, status, path_class, count in db.execute(
        "SELECT bucket, zone, host, client_ip, status, path_class, count FROM edge WHERE bucket >= ?", (_hour(lo),)
    ):
        ip = _ip(client_ip)
        if ip is not None:
            edge[ip].append((bucket, zone, host, status, path_class, count))
    inbound: dict[str, list] = defaultdict(list)
    for bucket, src, dst, conns in db.execute(
        "SELECT bucket, src, dst, conns FROM flows WHERE direction = 'in' AND bucket >= ?", (_hour(lo),)
    ):
        ip = _ip(src)
        if ip is not None:
            inbound[ip].append((bucket, dst, conns))

    for ip in sorted(set(edge) | set(inbound)):
        lst = threat.lookup(ip)
        if lst is None or not _internet(ip, topo, cfg):
            continue
        rows = edge.get(ip, [])
        ok_rows = [r for r in rows if 200 <= r[3] < 300 and r[4] != "other"]
        total = sum(r[5] for r in rows)
        edge_hit = total > THREAT_EDGE_MIN or bool(ok_rows)
        flows_in = inbound.get(ip, [])
        if not edge_hit and not flows_in:
            continue

        buckets = [r[0] for r in rows] + [f[0] for f in flows_in]
        first = float(min(buckets))
        last = float(min(max(buckets) + HOUR_S, now))
        parts = []
        if total:
            parts.append(f"{total} edge requests")
        if ok_rows:
            parts.append(f"a 2xx on a {ok_rows[0][4]} path")
        if flows_in:
            parts.append(f"{sum(f[2] for f in flows_in)} inbound connections")
        why = f"{ip} is on the {lst} list: " + ", ".join(parts)
        count = total + sum(f[2] for f in flows_in)

        if edge_hit:
            top = max(rows, key=lambda r: (200 <= r[3] < 300 and r[4] != "other", r[5]))
            _add(found, "threat_active", "warn", ip, top[2], why, first, last, count, block="cloudflare", zone=top[1])
        else:
            dst = min(flows_in, key=lambda f: (-f[2], f[1]))[1]
            _add(found, "threat_active", "warn", ip, dst, why, first, last, count, block="host")


def _auth(db, lo: float, now: float, found: dict) -> None:
    for src, n, first, last in db.execute(
        "SELECT src, COUNT(*), MIN(ts), MAX(ts) FROM events WHERE kind = 'auth_fail' AND ts >= ? AND ts <= ? "
        "GROUP BY src HAVING COUNT(*) >= ?",
        (now - SSH_WINDOW_S, now, SSH_BURST),
    ).fetchall():
        _add(found, "auth_burst", "warn", src, "sshd",
             f"{n} failed sshd sign-ins from {src} in 10 minutes", first, last, n, block="host")

    grouped: dict[tuple[str, str, str], list] = defaultdict(lambda: [0, None, None])
    for bucket, zone, host, client_ip, status, path_class, count in db.execute(
        "SELECT bucket, zone, host, client_ip, status, path_class, count FROM edge WHERE bucket >= ? AND status IN (401, 403)",
        (_hour(lo),),
    ):
        if not path_class.startswith("auth_"):
            continue
        acc = grouped[(client_ip, zone, host)]
        acc[0] += count
        acc[1] = bucket if acc[1] is None else min(acc[1], bucket)
        acc[2] = bucket if acc[2] is None else max(acc[2], bucket)
    for (client_ip, zone, host), (n, first, last) in sorted(grouped.items()):
        if n < EDGE_AUTH_BURST:
            continue
        _add(found, "auth_burst", "warn", client_ip, host,
             f"{n} sign-in attempts refused (401/403) on {host} from {client_ip} in the window",
             float(first), float(min(last + HOUR_S, now)), n, block="cloudflare", zone=zone)


def _line_not_meant(db, topo: Topology, cfg: dict, lo: float, now: float, found: dict) -> None:
    for ts, src, dst, dport in db.execute(
        "SELECT ts, src, dst, dport FROM events WHERE kind = 'fw_block' AND ts >= ? AND ts <= ? ORDER BY ts", (lo, now)
    ).fetchall():
        ip = _ip(src)
        if ip is None or classify(ip, topo, cfg).kind != "container" or not _is_host_ip(dst, topo):
            continue
        _add(found, "line_not_meant", "warn", ip, f"host:{dport}",
             f"container {ip} was refused on host port {dport} (pinhole violation)", ts, ts, 1)


def _new_peer(db, topo: Topology, cfg: dict, lo: float, now: float, found: dict) -> None:
    raw = store.meta_get(db, "first_run")
    try:
        first_run = float(raw) if raw is not None else now
    except ValueError:
        first_run = now
    learning = now < first_run + LEARNING_S
    since = _hour(lo)

    observed: dict[tuple[str, str], str] = {}   # (entity, peer_key) -> why, for a peer seen in this window
    for src, dst, dport in db.execute(
        "SELECT src, dst, dport FROM flows WHERE direction = 'out' AND bucket >= ?", (since,)
    ).fetchall():
        ip = _ip(dst)
        if ip is None:
            continue
        net = _net(ip)
        observed[(src, f"out:{net}:{dport}")] = f"{src} connects out to {net} on port {dport}"
    for src, dst, dport in db.execute(
        "SELECT src, dst, dport FROM flows WHERE direction = 'svc' AND bucket >= ?", (since,)
    ).fetchall():
        if not (src.startswith("container:") and dst.startswith("container:")):
            continue            # the host is involved: never expected to flag
        a, b = topo.compose.get(src[len("container:"):]), topo.compose.get(dst[len("container:"):])
        if a is None or b is None or a == b:
            continue            # not a known container, or the same compose project: always expected
        observed[(src, f"svc:{dst}:{dport}")] = f"{src} talks to {dst} on port {dport}, across compose projects"
    for src, dst, dport in db.execute(
        "SELECT src, dst, dport FROM flows WHERE direction = 'lan' AND bucket >= ?", (since,)
    ).fetchall():
        if not (src.startswith("lan:") or src.startswith("ts:")):
            continue            # inbound only: the device reached this host or container
        observed[(src, f"lan:{dst}:{dport}")] = f"{src} reached {dst} on port {dport}, a port it never used before"

    with db:
        for (entity, key), why in sorted(observed.items()):
            row = db.execute("SELECT first_seen FROM baseline WHERE entity = ? AND peer_key = ?", (entity, key)).fetchone()
            if row is None:
                db.execute("INSERT INTO baseline (entity, peer_key, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                           (entity, key, now, now))
                first = now
            else:
                first = row[0]
                db.execute("UPDATE baseline SET last_seen = ? WHERE entity = ? AND peer_key = ?", (now, entity, key))
            if not learning and first >= lo:
                _add(found, "new_peer", "warn", entity, key, why, first, now, 1)


# ---- the judge ------------------------------------------------------------------------------------------------------------

def judge(db, topo: Topology, threat: ThreatIndex, now: float, cfg: dict) -> list[Finding]:
    """Every finding for the last WINDOW_S seconds, with protection and staleness applied. See the module docstring."""
    cfg = cfg or {}
    lo = now - WINDOW_S
    found: dict[str, Finding] = {}
    _plex(db, topo, cfg, lo, now, found)
    _codec(db, lo, now, found)
    _threat(db, topo, threat, cfg, lo, now, found)
    _auth(db, lo, now, found)
    if not topo.stale:
        _line_not_meant(db, topo, cfg, lo, now, found)
        _new_peer(db, topo, cfg, lo, now, found)

    out = []
    for finding in found.values():
        ip = _ip(finding.src)
        if ip is not None:
            p = protect.protection(ip, db, topo, now, cfg)
            if p is not None and p[0] == "hard":
                finding = replace(finding, block=None, why=f"{finding.why} (never blocked: {p[1]})")
            elif p is not None and p[0] == "signin":
                finding = replace(finding, stolen_account=p[1])
        if topo.stale:
            finding = replace(finding, block=None)
        out.append(finding)
    return out
