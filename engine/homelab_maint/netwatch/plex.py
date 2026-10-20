"""netwatch.plex: Plex Media Server log lines and codec downloads turned into NetEvents.

Read-only. Two rules:
- exploit_attempts: Plex's own "unsupported protocol '" rejection of a request (a companion-proxy subscribe to a `searchOne`
  agent is the known shape; any such rejection is suspicious). Only the request id is kept, never the URL.
- unexplained_codecs: a codec library written to disk that no `CodecManager: obtaining` line in the log accounts for.
  Plex downloads codecs on demand and logs the request first, so a codec file with no matching request was put there
  by something else.

Plex writes local time, so timestamps are parsed with time.mktime.
"""
import re
import time
from pathlib import Path

from homelab_maint.netwatch.model import NetEvent

PLEX_DIR_DEFAULT = Path("/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server")
PLEX_PORT = 32400
_EXPLOIT = re.compile(r"unsupported protocol '")
_STAMP = re.compile(r"^([A-Z][a-z]{2} +\d{1,2}, \d{4} \d{2}:\d{2}:\d{2})\.(\d{3})")
_REQ = re.compile(r"\[(Req#[0-9a-fA-F]+)/")
_CODEC_LOG = re.compile(r"CodecManager: obtaining (decoder|encoder) '([^']+)'")
_CODEC_FILE = re.compile(r"^lib(.+)_(decoder|encoder)\.so$")
CODEC_WINDOW = 5.0


def _ts(line: str) -> float | None:
    """Epoch of a Plex log line from its local-time stamp, or None when the line has none."""
    if not (m := _STAMP.match(line)):
        return None
    try:
        whole = time.mktime(time.strptime(" ".join(m.group(1).split()), "%b %d, %Y %H:%M:%S"))
    except ValueError:
        return None
    return whole + int(m.group(2)) / 1000.0


def line_ts(line: str) -> float | None:
    """Epoch of a Plex log line, or None when it has no readable stamp. Public so callers can move a cursor past a line."""
    return _ts(line)


def exploit_attempts(lines) -> list[NetEvent]:
    """plex_exploit events for exploit-shaped requests. detail is the request id, or "unknown" when the line has none."""
    events: list[NetEvent] = []
    for line in lines:
        if not _EXPLOIT.search(line):
            continue
        ts = _ts(line)
        if ts is None:
            continue
        req = _REQ.search(line)
        events.append(NetEvent(
            ts=ts, kind="plex_exploit", src="unknown", dst="plex", dport=PLEX_PORT,
            detail=req.group(1) if req else "unknown",
        ))
    return events


def unexplained_codecs(codec_dir: Path, lines, since: float) -> list[NetEvent]:
    """codec_unexplained events for codec libraries under `codec_dir` modified after `since` with no matching log request.

    A file is explained when a `CodecManager: obtaining (decoder|encoder) '<x>'` line is logged 0 to 5 seconds before its
    mtime and the file is named `lib<x>_<kind>.so` for that same kind. Files that aren't codec libraries are ignored.

    Callers must pass `lines` read with `plex_log_lines(since, cfg, margin=CODEC_WINDOW)`. A request logged up to
    CODEC_WINDOW seconds before `since` can still explain a codec written after `since`; without the margin that request
    is missing from `lines` and the codec is reported as a false positive.
    """
    requests: list[tuple[float, str]] = []   # (logged at, expected filename)
    for line in lines:
        if not (m := _CODEC_LOG.search(line)):
            continue
        if (ts := _ts(line)) is None:
            continue
        requests.append((ts, f"lib{m.group(2)}_{m.group(1)}.so"))

    events: list[NetEvent] = []
    for path in sorted(Path(codec_dir).rglob("lib*.so")):
        if not _CODEC_FILE.match(path.name):
            continue
        mtime = path.stat().st_mtime
        if mtime <= since:
            continue
        if any(0 <= mtime - logged <= CODEC_WINDOW and name == path.name for logged, name in requests):
            continue
        events.append(NetEvent(
            ts=mtime, kind="codec_unexplained", src="unknown", dst="plex", dport=PLEX_PORT, detail=path.name,
        ))
    return events


def plex_log_lines(since: float, cfg: dict | None = None, margin: float = 0.0) -> list[str]:
    """Lines of the Plex Media Server log stamped at or after `since - margin`. A missing log gives no lines."""
    plex_dir = Path((cfg or {}).get("plex_dir", PLEX_DIR_DEFAULT))
    path = plex_dir / "Logs" / "Plex Media Server.log"
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    with handle:
        out: list[str] = []
        for line in handle:
            line = line.rstrip("\r\n")
            ts = _ts(line)
            if ts is not None and ts >= since - margin:
                out.append(line)
    return out
