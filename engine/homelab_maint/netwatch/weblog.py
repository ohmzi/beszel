"""The home.ohmz.cloud access log, enriched for the Website page.

The origin is an nginx container behind the host's Cloudflare Tunnel. Its access log is the combined format, captured by
Docker's json-file driver; this module reads the recent tail with `docker logs`, parses the public requests, resolves the
city and network organization through DB-IP Lite (`webgeo`), parses the User-Agent for device/browser/OS, and matches the
network organization against the owner's target-company list.

It is a **server-side** view. There is no client-side tracker, no cookie and no fingerprint: the page behind the hub login
shows what nginx already recorded, and the User-Agent is the only device signal. A network organization is the owner of the
IP, not a person. Corporate NAT, a VPN, or an e-mail security scanner can be the thing that fetched the page.
"""
from __future__ import annotations

import ipaddress
import os
import re
import time
from datetime import datetime

from .. import core
from . import webgeo

CONTAINER = "omar-iqbal"
ROWS_MAX = 500
UA_MAX = 300
PATH_MAX = 300
REF_MAX = 300

_DOCKER_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+")
_LINE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) (?P<proto>[^"]+)" '
    r'(?P<status>\d{3}) (?P<bytes>\d+) "(?P<referer>[^"]*)" "(?P<ua>[^"]*)"(?: "(?P<xff>[^"]*)")?'
)
_TIME = "%d/%b/%Y:%H:%M:%S %z"

NOTE = (
    "The network organization is the owner of the IP, not a person. A visit can be an e-mail security scanner, "
    "a VPN, or a shared cloud network rather than a recruiter. City is approximate. Device is parsed from the User-Agent."
)
ATTRIBUTION = "IP geolocation by DB-IP (https://db-ip.com)"


def parse_line(line: str) -> dict | None:
    """One nginx combined line (with or without Docker's timestamp prefix), or None."""
    line = _DOCKER_TS.sub("", line.strip(), count=1)
    match = _LINE.match(line)
    if not match:
        return None
    try:
        ts = datetime.strptime(match.group("time"), _TIME).timestamp()
        status = int(match.group("status"))
        size = int(match.group("bytes"))
    except ValueError:
        return None
    referer = match.group("referer")
    return {
        "ts": ts,
        "ip": match.group("ip"),
        "method": match.group("method"),
        "path": match.group("path"),
        "proto": match.group("proto"),
        "status": status,
        "bytes": size,
        "referer": "" if referer == "-" else referer,
        "ua": match.group("ua"),
    }


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_unspecified or addr.is_reserved)


def _is_probe(ua: str) -> bool:
    low = (ua or "").lower()
    return low.startswith("homelab-maint") or "homelab-maint-probe" in low


def parse_ua(ua: str) -> tuple[str, str, str]:
    """(device, browser, os) from a User-Agent. Deliberately coarse; no fingerprinting."""
    low = (ua or "").lower()
    if not low:
        return "", "", ""
    if any(token in low for token in ("bot", "crawler", "spider", "preview", "scanner", "headless")):
        return "Bot", "Bot", "Bot"
    if "iphone" in low:
        device, os_ = "iPhone", "iOS"
    elif "ipad" in low:
        device, os_ = "iPad", "iOS"
    elif "android" in low:
        device, os_ = "Android", "Android"
    elif "windows" in low:
        device, os_ = "Desktop", "Windows"
    elif "mac os x" in low or "macintosh" in low:
        device, os_ = "Desktop", "macOS"
    elif "linux" in low:
        device, os_ = "Desktop", "Linux"
    else:
        device, os_ = "Other", "Other"
    if "edg" in low:
        browser = "Edge"
    elif "chrome" in low or "crios" in low:
        browser = "Chrome"
    elif "firefox" in low or "fxios" in low:
        browser = "Firefox"
    elif "safari" in low:
        browser = "Safari"
    else:
        browser = "Other"
    return device, browser, os_


def load_targets() -> list[dict]:
    try:
        doc = core.load_toml(core.CONF_DIR / "web-targets.toml") or {}
    except Exception:  # noqa: BLE001 - a missing/broken list means no matches, never a failed export
        return []
    rows = doc.get("target") if isinstance(doc, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        match = row.get("match") or []
        if isinstance(match, str):
            match = [match]
        out.append({"name": str(row["name"]), "match": [str(m).casefold() for m in match if str(m).strip()]})
    return out


def match_target(org: str, targets: list[dict]) -> str | None:
    low = (org or "").casefold()
    if not low:
        return None
    for target in targets:
        if any(pattern in low for pattern in target["match"]):
            return target["name"]
    return None


def read_logs(container: str = CONTAINER) -> tuple[list[str], str | None]:
    """The recent Docker log tail for the origin. Returns (lines, error)."""
    if os.environ.get("HOMELAB_MAINT_NO_SYSLOG"):
        # The test harness sets this to keep the host journal clean; it also keeps
        # publish tests from shelling out to Docker. Production never sets it.
        return [], "log reader disabled in tests"
    result = core.sh(["docker", "logs", "--timestamps", "--tail", "5000", container], timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return [], detail[-1] if detail else f"docker logs exited {result.returncode}"
    return (result.stdout or "").splitlines(), None


def build_web(now: float | None = None, logs: list[str] | None = None,
              geo=None, targets: list[dict] | None = None) -> dict:
    """The web.json document. `logs`/`geo`/`targets` are injectable for tests."""
    now = time.time() if now is None else float(now)
    source_error = None
    if logs is None:
        logs, source_error = read_logs()
    rows = []
    seen = set()
    for line in logs:
        row = parse_line(line)
        if not row or _is_probe(row["ua"]) or not _is_public(row["ip"]):
            continue
        key = (row["ts"], row["ip"], row["path"], row["ua"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows = rows[:ROWS_MAX]

    own_geo = geo is None
    if own_geo:
        geo = webgeo.GeoLookup()
    geo_ok = getattr(geo, "conn", None) is not None
    if targets is None:
        targets = load_targets()

    from ..publish import clean  # local: publish imports this module only from a builder

    out = []
    for row in rows:
        geo_row = geo.lookup(row["ip"]) if geo is not None else {}
        org = geo_row.get("org", "")
        device, browser, os_ = parse_ua(row["ua"])
        out.append({
            "ts": int(row["ts"]),
            "ip": clean(row["ip"], 64),
            "country": clean(geo_row.get("country", ""), 8),
            "region": clean(geo_row.get("region", ""), 80),
            "city": clean(geo_row.get("city", ""), 80),
            "asn": int(geo_row.get("asn") or 0),
            "org": clean(org, 120),
            "device": device,
            "browser": browser,
            "os": os_,
            "path": clean(row["path"], PATH_MAX),
            "referer": clean(row["referer"], REF_MAX),
            "status": row["status"],
            "bytes": row["bytes"],
            "target": match_target(org, targets),
        })
    if own_geo:
        geo.close()

    return {
        "generated": now,
        "available": bool(out),
        "rows": out,
        "targets": [t["name"] for t in targets],
        "window": {"from": out[-1]["ts"] if out else None, "to": out[0]["ts"] if out else None},
        "sources": {
            "nginx": {"status": "ok" if out else ("error" if source_error else "empty"), "error": source_error},
            "geo": {"status": "ok" if geo_ok else "missing"},
        },
        "note": NOTE,
        "attribution": ATTRIBUTION,
    }
