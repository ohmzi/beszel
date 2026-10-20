"""netwatch.threat: the published bad-address lists and the owner's indicators, as one lookup.

    python3 -m homelab_maint.netwatch.threat refresh      (daily job: netwatch-threat-refresh, see etc/rules.d/80-jobs.toml)
    python3 -m homelab_maint.netwatch.threat lookup IP

refresh() downloads each list into core.STATE_DIR/netwatch/threat/NAME.txt, one CIDR per line. A list is written only after it parses
with at least one entry; any failure keeps the previous file and reports "kept:<reason>". Nothing here blocks anything: the lists only
classify a peer.

ThreatIndex.load() reads those files plus the owner's IOC file (core.CONF_DIR/netwatch-ioc.toml: `ips` and `domains`). lookup() names
the first match: "ioc" first, then the list names sorted.

`fetch` is called like urllib.request.urlopen(request, timeout=...) so tests inject a fake and never download.
"""
import ipaddress
import json
import os
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from homelab_maint import core

USER_AGENT = "homelab-maint-netwatch/1"
TIMEOUT_S = 30
STALE_S = 3 * 86400
IOC = "ioc"

# name -> (URL, format). Spamhaus merged EDROP into DROP in 2024, so DROP's JSON feeds are the sources (NDJSON, one object per line).
LISTS = {
    "spamhaus_drop_v4": ("https://www.spamhaus.org/drop/drop_v4.json", "ndjson"),
    "spamhaus_drop_v6": ("https://www.spamhaus.org/drop/drop_v6.json", "ndjson"),
    "firehol_level1": ("https://iplists.firehol.org/files/firehol_level1.netset", "text"),
    "feodo": ("https://feodotracker.abuse.ch/downloads/ipblocklist.txt", "text"),
}


def threat_dir() -> Path:
    return core.STATE_DIR / "netwatch" / "threat"


def ioc_path() -> Path:
    return core.CONF_DIR / "netwatch-ioc.toml"


def _cidr(text: str) -> str:
    """Canonical CIDR text for an IP or a network; raises ValueError for anything else."""
    return str(ipaddress.ip_network(text, strict=False))


def _text_entries(body: str) -> list[str]:
    """Plain lists: one IP or CIDR per line, `#` starts a comment, anything after the first field is ignored."""
    out = []
    for line in body.splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            out.append(_cidr(fields[0]))
    return out


def _ndjson_entries(body: str) -> list[str]:
    """Spamhaus DROP JSON: one object per line. Data lines carry "cidr"; the metadata line at the end does not and is skipped."""
    out = []
    for line in body.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and "cidr" in obj:
            out.append(_cidr(str(obj["cidr"])))
    return out


_PARSERS = {"ndjson": _ndjson_entries, "text": _text_entries}


def _write_atomic(path: Path, cidrs: list[str], now: float) -> None:
    tmp = path.with_name(f".{path.name}.tmp")          # not *.txt, so a load never reads a half-written file
    tmp.write_text("".join(c + "\n" for c in cidrs))
    os.utime(tmp, (now, now))
    os.replace(tmp, path)


def _refresh_one(name: str, url: str, fmt: str, fetch, directory: Path, now: float) -> str:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with fetch(request, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except Exception as e:                              # the reason is the exception class only: no URL, no body
        return f"kept:fetch:{type(e).__name__}"
    try:
        body = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        cidrs = _PARSERS[fmt](body)
    except (ValueError, TypeError) as e:
        return f"kept:parse:{type(e).__name__}"
    if not cidrs:
        return "kept:empty"
    _write_atomic(directory / f"{name}.txt", cidrs, now)
    return "ok"


def refresh(fetch=urllib.request.urlopen, now=None) -> dict[str, str]:
    """Download every list. Returns list name -> "ok" or "kept:<reason>" (the old file stays in place on any failure)."""
    now = time.time() if now is None else now
    directory = threat_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return {name: _refresh_one(name, url, fmt, fetch, directory, now) for name, (url, fmt) in LISTS.items()}


def _group(cidrs) -> dict[tuple[int, int], set[int]]:
    """(version, prefix length) -> the network addresses of that size, shifted right by the host bits so a lookup is one set test."""
    groups: dict[tuple[int, int], set[int]] = {}
    for text in cidrs:
        net = ipaddress.ip_network(text, strict=False)
        host_bits = net.max_prefixlen - net.prefixlen
        groups.setdefault((net.version, net.prefixlen), set()).add(int(net.network_address) >> host_bits)
    return groups


def _contains(groups: dict[tuple[int, int], set[int]], addr) -> bool:
    for (version, prefixlen), keys in groups.items():
        if version == addr.version and (int(addr) >> (addr.max_prefixlen - prefixlen)) in keys:
            return True
    return False


class ThreatIndex:
    def __init__(self, lists: list[tuple[str, dict]], domains: set[str], mtimes: dict[str, float]):
        self._lists = lists                              # ordered: IOC first, then the list names sorted
        self._mtimes = mtimes
        self.domains = domains

    @classmethod
    def load(cls, directory, ioc: Path) -> "ThreatIndex":
        directory = Path(directory)
        ioc_doc = core.load_toml(Path(ioc))
        ioc_ips = [_cidr(str(x)) for x in ioc_doc.get("ips", [])]
        domains = {str(d).strip().lower().rstrip(".") for d in ioc_doc.get("domains", [])}
        files: list[tuple[str, dict]] = []
        mtimes: dict[str, float] = {}
        if directory.is_dir():
            for path in sorted(directory.glob("*.txt")):
                entries = [_cidr(line.strip()) for line in path.read_text().splitlines() if line.strip()]
                files.append((path.stem, _group(entries)))
                mtimes[path.stem] = path.stat().st_mtime
        lists = [(IOC, _group(ioc_ips))] + sorted(files, key=lambda item: item[0])
        return cls(lists, domains, mtimes)

    def lookup(self, ip: str) -> str | None:
        addr = ipaddress.ip_address(ip.strip())
        for name, groups in self._lists:
            if _contains(groups, addr):
                return name
        return None

    def stale_lists(self, now: float) -> list[str]:
        return sorted(name for name, mtime in self._mtimes.items() if now - mtime > STALE_S)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["refresh"]:
        for name, status in refresh().items():
            print(f"{name}: {status}")
        return 0
    if len(argv) == 2 and argv[0] == "lookup":
        try:
            hit = ThreatIndex.load(threat_dir(), ioc_path()).lookup(argv[1])
        except ValueError:
            print(f"not an IP address: {argv[1]}", file=sys.stderr)
            return 2
        print(hit or "none")
        return 0
    print("usage: python3 -m homelab_maint.netwatch.threat refresh | lookup IP", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
