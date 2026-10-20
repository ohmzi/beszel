"""netwatch.protect: the addresses Network Watch must never block.

Two parts. The hard ranges (entities.HARD_RANGES) are never overridable. A sign-in is an address that signed in successfully
in the last 30 days, from one of three sources:
- edge: a 2xx answer on an `auth_api` path class from a host listed in [netwatch.signin] hosts
- sshd: an `auth_ok` event from the journal
- plex: a player address on the local Plex server's /status/sessions that is a global (internet) address

record_signins() writes those into the signins table; protection() is asked before any block. Nothing here blocks anything.

Plex's token is read from Preferences.xml and sent only in the X-Plex-Token header. It never appears in a log line, an exception
or a return value: plex_session_ips() returns [] on any failure.
"""
import ipaddress
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from homelab_maint.netwatch import entities, plex, store

SIGNIN_S = 30 * 86400          # a sign-in protects its address for 30 days from its last_seen
EDGE_BUCKET_S = 3600           # an edge row is an hour of requests: its last request is somewhere inside that hour
SINCE_DEFAULT_S = 3600         # the first read looks back this far
AUTH_CLASS = "auth_api"        # the path class whose 2xx proves a signed-in session on the listed hosts
PLEX_SESSIONS_URL = "http://127.0.0.1:32400/status/sessions"
PLEX_TIMEOUT_S = 5


def _norm(ip: str):
    """The address object for `ip`, with an IPv4-mapped IPv6 address unwrapped. Raises ValueError for anything else."""
    addr = ipaddress.ip_address(str(ip).strip())
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def _internet(addr) -> bool:
    """True for a globally routable address: the only kind a Plex player can be a sign-in from."""
    return addr.is_global


def _date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def plex_session_ips(cfg: dict, http=urllib.request.urlopen, prefs_path=None) -> list[str]:
    """The `address` of each Player in the local Plex server's /status/sessions. [] on any failure, with the token never logged."""
    try:
        path = Path(prefs_path) if prefs_path is not None else Path((cfg or {}).get("plex_dir", plex.PLEX_DIR_DEFAULT)) / "Preferences.xml"
        token = ET.parse(path).getroot().get("PlexOnlineToken")
        if not token:
            return []
        req = urllib.request.Request(PLEX_SESSIONS_URL, headers={"X-Plex-Token": token, "Accept": "application/xml"})
        with http(req, timeout=PLEX_TIMEOUT_S) as resp:
            root = ET.fromstring(resp.read())
        return [p.get("address") for p in root.iter("Player") if p.get("address")]
    except Exception:       # never let a token, a URL or an XML detail escape: the caller gets no sessions, and no reason
        return []


def _signin_rows(db, now: float, cfg: dict, since: float, plex_sessions) -> list[tuple[str, str, float]]:
    rows: list[tuple[str, str, float]] = []
    hosts = set((cfg.get("signin") or {}).get("hosts") or [])
    if hosts:
        for bucket, host, client_ip, status, path_class in db.execute(
            "SELECT bucket, host, client_ip, status, path_class FROM edge WHERE bucket >= ?", (since,)
        ):
            if host in hosts and path_class == AUTH_CLASS and 200 <= status < 300:
                rows.append((client_ip, f"edge:{host}", min(bucket + EDGE_BUCKET_S, now)))
    for ts, src in db.execute("SELECT ts, src FROM events WHERE kind = 'auth_ok' AND ts >= ?", (since,)):
        rows.append((src, "sshd", ts))
    if plex_sessions is not None:
        for ip in plex_sessions():
            try:
                addr = _norm(ip)
            except ValueError:
                continue
            if _internet(addr):
                rows.append((str(addr), "plex", now))
    out = []
    for ip, source, last_seen in rows:
        try:
            out.append((str(_norm(ip)), source, last_seen))
        except ValueError:
            continue
    return out


def record_signins(db, now: float, cfg: dict, plex_sessions=None) -> int:
    """Write the sign-ins seen since the last call (see the module docstring). Returns the number of rows written.

    A row upserts on (ip, source) and keeps the larger last_seen. The `signins:since` mark moves to `now`.
    """
    raw = store.meta_get(db, "signins:since")
    since = float(raw) if raw else now - SINCE_DEFAULT_S
    rows = _signin_rows(db, now, cfg or {}, since, plex_sessions)
    with db:
        db.executemany(
            "INSERT INTO signins (ip, source, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(ip, source) DO UPDATE SET last_seen = max(signins.last_seen, excluded.last_seen)",
            rows,
        )
    store.meta_set(db, "signins:since", repr(now))
    return len(rows)


def protection(ip: str, db, topo, now: float, cfg: dict | None = None) -> tuple[str, str] | None:
    """Why `ip` must not be blocked: ("hard", "<cidr>"), ("signin", "<source> <YYYY-MM-DD>") or None.

    The hard ranges win. Otherwise the most recent sign-in within 30 days of `now` protects the address.
    """
    addr = _norm(ip)
    for net in entities.HARD_RANGES(topo, cfg):
        if addr in net:
            return ("hard", str(net))
    row = db.execute(
        "SELECT source, last_seen FROM signins WHERE ip = ? AND last_seen >= ? ORDER BY last_seen DESC LIMIT 1",
        (str(addr), now - SIGNIN_S),
    ).fetchone()
    if row is None:
        return None
    return ("signin", f"{row[0]} {_date(row[1])}")
