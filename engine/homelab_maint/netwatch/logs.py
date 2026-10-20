"""netwatch.logs: firewall and sshd log lines turned into NetEvents, plus the journalctl reader that feeds them.

Read-only. UFW blocks come from the kernel ring (`journalctl -k`); sshd lines come from the ssh unit's journal or from an
auth.log. Both timestamp styles are accepted: journal `short-iso` lines (`2026-10-17T23:42:55.795927-04:00 ...`) and
syslog lines (`Oct 19 08:12:01 ...`, which carry no year, so the current year is assumed).

Metadata only: addresses, ports, protocol and interface. Never packet contents, never the rest of a log line.
"""
import re
import subprocess
import time
from datetime import datetime
from typing import Iterable

from homelab_maint import core
from homelab_maint.netwatch.model import NetEvent
from homelab_maint.netwatch.store import meta_get, meta_set

_KV = re.compile(r"(\w+)=(\S*)")
_SSHD = re.compile(r"sshd\[\d+\]: (Failed password|Invalid user|Accepted) .*? from (\S+) port \d+")
_SYSLOG = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}:\d{2}:\d{2}) ")
_ISO_HEAD = re.compile(r"^(\S+) ")
_SSHD_PORT = 22


def _iso_ts(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _line_ts(line: str) -> float | None:
    """Epoch of a journal short-iso or syslog line, or None when the line has no readable timestamp."""
    if m := _ISO_HEAD.match(line):
        if ts := _iso_ts(m.group(1)):
            return ts
    if m := _SYSLOG.match(line):
        year = datetime.now().year
        try:
            ts = time.mktime(time.strptime(f"{year} {m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d %H:%M:%S"))
        except ValueError:
            return None
        if ts - time.time() > 86400:   # a December line read in January belongs to last year
            ts = time.mktime(time.strptime(f"{year - 1} {m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d %H:%M:%S"))
        return ts
    return None


def parse_ufw(lines: Iterable[str]) -> list[NetEvent]:
    """`[UFW BLOCK]` kernel lines as fw_block events. dport is 0 when the packet has no port (ICMP)."""
    events: list[NetEvent] = []
    for line in lines:
        if "[UFW BLOCK]" not in line:
            continue
        ts = _line_ts(line)
        if ts is None:
            continue
        fields = dict(_KV.findall(line))
        if "SRC" not in fields or "DST" not in fields:
            continue
        dport = int(fields["DPT"]) if fields.get("DPT", "").isdigit() else 0
        events.append(NetEvent(
            ts=ts, kind="fw_block", src=fields["SRC"], dst=fields["DST"], dport=dport,
            detail=f"PROTO={fields.get('PROTO', '')} IN={fields.get('IN', '')}",
        ))
    return events


def parse_auth(lines: Iterable[str]) -> list[NetEvent]:
    """sshd `Failed password` and `Invalid user` lines as auth_fail, `Accepted` lines as auth_ok. src is the client address."""
    events: list[NetEvent] = []
    for line in lines:
        m = _SSHD.search(line)
        if not m:
            continue
        ts = _line_ts(line)
        if ts is None:
            continue
        kind = "auth_ok" if m.group(1) == "Accepted" else "auth_fail"
        events.append(NetEvent(ts=ts, kind=kind, src=m.group(2), dst="host", dport=_SSHD_PORT, detail="sshd"))
    return events


def read_journal(unit_or_k: str, cursor_key: str, db, sh=core.sh) -> list[str]:
    """New journal lines since the cursor stored under `cursor_key` (or the last 10 minutes on the first run).

    `unit_or_k` is "-k" for the kernel ring, otherwise a unit name. The cursor journalctl prints last is saved in meta; a
    failed journalctl returns no lines and leaves the saved cursor where it was.
    """
    argv = ["journalctl", "-o", "short-iso", "--no-pager", "--show-cursor"]
    argv += ["-k"] if unit_or_k == "-k" else ["-u", unit_or_k]
    cursor = meta_get(db, cursor_key)
    argv += ["--after-cursor", cursor] if cursor else ["--since", "-10min"]
    res = sh(argv)
    if res.returncode != 0:
        return []
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    new_cursor = None
    if lines and lines[-1].startswith("-- cursor: "):
        new_cursor = lines.pop()[len("-- cursor: "):].strip()
    if new_cursor:
        meta_set(db, cursor_key, new_cursor)
    return [ln for ln in lines if not ln.startswith("-- ")]
