"""Health trends and meta-monitoring: smart_trend, alert_path_health, growth_watch, config_drift.

All four tasks are C0: they only read. File formats below were taken from the real host:

* smartd attribute log, /var/lib/smartmontools/attrlog.<MODEL>-<SERIAL>.<bus>.csv, one row per
  smartd poll (~30 min), LOCAL time:
      "2026-10-01 18:57:33;\\t1;100;0;\\t5;100;0;...\\t194;70;214749806622;\\ttemperature;28;"
  = "timestamp;" then "id;norm;raw;" triples, then an optional "temperature;<C>;" pair.
* /var/log/smart-alert.log, written by /usr/local/sbin/smart-alert.sh (ISO timestamps with offset):
      "<ts> ALERT SEND FAILED rc=<n> for <device>: <stderr tail>"   current hook
      "<ts> ALERT SEND FAILED for <message>"                         pre-fix hook (no reason logged)
      "<ts> alert sent for <device>"
* core.audit() lines: {"ts": "2026-10-01T19:04:25-0400", "task": "notify", "action": "send",
  "outcome": "sent" | "failed rc=N <stderr>"} and action "budget-exhausted" (outcome "dropped").
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from ..core import GIB, LOG_DIR, Ctx, Result, append_history, human, ikey, read_history, sh, task

# Paths are module constants so tests can point them at tmp dirs.
HWMON_DIR = Path("/sys/class/hwmon")
BYID_DIR = Path("/dev/disk/by-id")
SMARTD_CONF = Path("/etc/smartd.conf")
MOUNTINFO = Path("/proc/self/mountinfo")
AUDIT_LOG = LOG_DIR / "audit.jsonl"
DOCKER_DAEMON_JSON = Path("/etc/docker/daemon.json")
JOURNALD_MAIN = Path("/etc/systemd/journald.conf")
# lowest -> highest priority; a same-named drop-in in a later dir replaces the earlier one
JOURNALD_DIRS = [Path("/usr/lib/systemd/journald.conf.d"), Path("/run/systemd/journald.conf.d"),
                 Path("/etc/systemd/journald.conf.d")]
DEFAULT_BRIDGE = "/usr/local/sbin/backup-notify-hermes.py"
DEFAULT_HOOK = "/usr/local/sbin/smart-alert.sh"


# --------------------------------------------------------------------------- helpers
def _clip(text, n: int = 140) -> str:
    """Summaries can end up in an SMS: one line, ASCII only, bounded."""
    return re.sub(r"[^\x20-\x7e]", "?", " ".join(str(text).split()))[:n]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A secret needs an explicit '=' or ':' after its key. A bare space is NOT a separator: it turned the
# real Gmail error "Username and Password not accepted" into "Password=<redacted> accepted", which reads
# as success. A password may contain spaces, so it is redacted to the end of the line.
_PASSWORD = re.compile(r"(?i)\b(pass(?:word|wd|phrase)?)[\"']?[ \t]*[=:].*")
_SECRET = re.compile(r"(?i)\b(token|secret|api[_-]?key|authorization)[\"']?[ \t]*[=:][ \t]*"
                     r"(?:(?:bearer|basic)[ \t]+)?\S+")       # "Authorization: Bearer <tok>": eat the scheme too
_FLAG_SECRET = re.compile(r"(?i)(--(?:pass(?:word|wd|phrase)?|token|secret|api[_-]?key))[ \t]+\S+")
_BEARER = re.compile(r"(?i)\bbearer[ \t]+[\w.~+/=-]{8,}")
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_+=]{32,}")   # no '/' or '-': keeps file paths readable


def _scrub(text: str, n: int = 100) -> str:
    """Bridge stderr goes to the dashboard (and maybe an SMS): drop addresses and token-like strings."""
    t = _EMAIL.sub("<addr>", text or "")
    t = _PASSWORD.sub(r"\1=<redacted>", t)
    t = _FLAG_SECRET.sub(r"\1 <redacted>", t)
    t = _SECRET.sub(r"\1=<redacted>", t)
    t = _BEARER.sub("Bearer <redacted>", t)
    t = _UUID.sub("<redacted>", t)
    return _clip(_LONG_TOKEN.sub("<redacted>", t), n)


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    """Last <= max_bytes of a text file as lines; the possibly cut first line is dropped. Raises OSError."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if start and lines else lines


def _when(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _iso(ts_s: str) -> float | None:
    try:
        return datetime.fromisoformat(ts_s).timestamp()   # accepts +HH:MM and +HHMM offsets
    except ValueError:
        return None


def _is_root() -> bool:
    return os.geteuid() == 0


# =========================================================================== smart_trend
_ATTR_NAMES = {5: "realloc", 197: "pending", 198: "offline-unc", 199: "CRC"}
_TAIL_BYTES = 1 << 20      # attrlog grows forever; ~8+ days of rows fit in the last MiB


def _parse_attr_row(line: str):
    """-> (epoch, {attr_id: raw}, temp_c|None) or None for a malformed row."""
    tok = [t.strip() for t in line.split(";")]
    try:
        ts = time.mktime(time.strptime(tok[0], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None
    raw: dict[int, int] = {}
    temp = None
    i = 1
    while i < len(tok):
        if tok[i] == "temperature" and i + 1 < len(tok):
            temp = int(tok[i + 1]) if tok[i + 1].isdigit() else None
            i += 2
        elif tok[i].isdigit() and i + 2 < len(tok) and tok[i + 1].isdigit() and tok[i + 2].isdigit():
            raw[int(tok[i])] = int(tok[i + 2])
            i += 3
        else:
            i += 1
    return ts, raw, temp


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", s)


def _byid_kernel_names() -> dict[str, str]:
    """'MODEL_SERIAL' (normalised) -> sdX from /dev/disk/by-id (world-readable, no smartctl)."""
    out: dict[str, str] = {}
    try:
        for e in os.scandir(BYID_DIR):
            if e.name.startswith("ata-") and "-part" not in e.name and e.is_symlink():
                out[_norm(e.name[4:])] = os.path.basename(os.readlink(e.path))
    except OSError:
        pass
    return out


def _ata_device(path: Path, ctx: Ctx, watch: list[int], kernel: dict[str, str]) -> dict:
    stem = path.name[len("attrlog."):-len(".csv")]
    ident, _, _bus = stem.rpartition(".")
    model, _, serial = (ident or stem).rpartition("-")
    label = f"{(model or ident).replace('_', ' ')[:22]} {serial[-4:]}".strip()
    dev = kernel.get(_norm(ident), label)
    d = {"dev": dev, "model": label, "temp_c": None, "realloc": None, "pending": None,
         "crc": None, "power_on_h": None, "level": "ok", "note": ""}
    try:
        rows = [r for r in map(_parse_attr_row, _tail_lines(path, _TAIL_BYTES)) if r]
    except OSError as exc:
        return {**d, "level": "warn", "note": f"unreadable: {exc.strerror}"}
    if not rows:
        return {**d, "level": "warn", "note": "no parsable rows"}
    last = rows[-1]
    notes: list[str] = []
    g = last[1].get
    d.update(realloc=g(5), pending=g(197), crc=g(199),
             power_on_h=(g(9) & 0xFFFFFFFF) if g(9) is not None else None)
    # smartd logs the drive temperature explicitly; 194's low byte is the fallback (not in older rows)
    d["temp_c"] = last[2] if last[2] is not None else (
        g(194) & 0xFF if g(194) is not None and 0 < (g(194) & 0xFF) < 120 else None)

    age_h = (ctx.now - last[0]) / 3600
    if age_h > float(ctx.opt("stale_hours", 6)):
        notes.append(f"smartd data {age_h:.0f}h old")
    window_s = float(ctx.opt("window_days", 7)) * 86400
    # baseline = the row closest to "7 days before the latest"; a younger file uses its first row
    base = min(rows, key=lambda r: abs(r[0] - (last[0] - window_s)))
    if last[0] - base[0] >= float(ctx.opt("min_span_hours", 24)) * 3600:
        for aid in watch:
            a, b = base[1].get(aid), last[1].get(aid)
            if a is not None and b is not None and b > a:
                notes.append(f"{_ATTR_NAMES.get(aid, f'attr{aid}')} +{b - a}")
    limit = float(ctx.opt("ata_temp_warn_c", 65))   # smartd's own 55 C trigger is noise
    if d["temp_c"] is not None and d["temp_c"] >= limit:
        notes.append(f"{d['temp_c']}C")
    if notes:
        d.update(level="warn", note=", ".join(notes))
    return d


def _nvme_devices(limit_c: float) -> list[dict]:
    """NVMe has no attrlog; the hwmon composite temperature is readable without root."""
    out = []
    try:
        hw = sorted(HWMON_DIR.iterdir())
    except OSError:
        return out
    for h in hw:
        try:
            if (h / "name").read_text().strip() != "nvme":
                continue
            temp = round(int((h / "temp1_input").read_text()) / 1000)
            ctrl = (h / "device").resolve()
        except (OSError, ValueError):
            continue
        try:
            label = f"{' '.join((ctrl / 'model').read_text().split()[:4])} {(ctrl / 'serial').read_text().strip()[-4:]}"
        except OSError:
            label = ctrl.name
        hot = temp >= limit_c
        out.append({"dev": ctrl.name, "model": label, "temp_c": temp, "realloc": None, "pending": None,
                    "crc": None, "power_on_h": None, "level": "warn" if hot else "ok",
                    "note": f"{temp}C" if hot else ""})
    return out


_SMART_NOTE = re.compile(r"(?P<attr>[A-Za-z][\w-]*) \+(?P<n>\d+)|(?P<hot>\d+C)|(?P<stale>smartd data)|(?P<bad>unreadable|no parsable rows)")


def _smart_key(bad: list[dict]) -> str | None:
    """SPEC5 Result.issue_key: per FAILING disk (all of them, not the 3 the summary names) the disk's identity (model + serial tail, not the
    kernel name that can swap between boots), which attribute grew and the DECADE of its growth ("realloc:b1" = +1..9, "b4" = +1000..), plus
    "hot" / "stale" / "unreadable". The temperature and the age of the data never enter: +2 sectors and +8000 sectors are both "warn", so the
    decade is what makes the worse one a new error."""
    parts = []
    for x in bad:
        for m in _SMART_NOTE.finditer(x["note"] or ""):
            what = f"{m['attr']}:b{len(m['n'])}" if m["attr"] else "hot" if m["hot"] else "stale" if m["stale"] else m["bad"].replace(" ", "-")
            parts.append(f"{x['model']}={what}")
    return ikey(smart=parts)


@task("smart_trend", klass="C0", tier="check", title="SMART trend", timeout=60)
def smart_trend(ctx: Ctx) -> Result:
    d = Path(ctx.opt("attrlog_dir", "/var/lib/smartmontools"))
    watch = list(dict.fromkeys([int(a) for a in ctx.opt("crit_attrs", [5, 197, 198])] + [199]))
    try:
        files = sorted(p for p in d.glob("attrlog.*.csv") if not p.name.endswith(".nvme.csv"))
    except OSError:
        files = []
    kernel = _byid_kernel_names()
    devs = [_ata_device(p, ctx, watch, kernel) for p in files] + \
           _nvme_devices(float(ctx.opt("nvme_temp_warn_c", 70)))
    if not devs:
        return Result("warn", _clip(f"no SMART attribute logs in {d} (smartd not logging?)"))
    devs.sort(key=lambda x: (x["level"] == "ok", -(x["temp_c"] or 0)))   # problems first, then hottest
    bad = [x for x in devs if x["level"] != "ok"]
    temps = [x for x in devs if x["temp_c"] is not None]
    hot = max(temps, key=lambda x: x["temp_c"]) if temps else None
    metrics = {"devices": [{k: x[k] for k in ("dev", "model", "temp_c", "realloc", "pending", "crc",
                                              "power_on_h", "level", "note")} for x in devs],
               "n_disks": len(devs), "n_problems": len(bad),
               "hottest_dev": hot["dev"] if hot else None, "hottest_c": hot["temp_c"] if hot else None}
    items = [{"dev": x["dev"], "model": x["model"], "level": x["level"], "temp_c": x["temp_c"],
              "detail": x["note"] or "ok"} for x in devs[:12]]
    if bad:
        txt = "SMART: " + "; ".join(f"{x['dev']} {x['note']}" for x in bad[:3])
        txt += f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
        return Result("warn", _clip(txt), metrics, items, issue_key=_smart_key(bad))
    tail = f", hottest {hot['dev']} {hot['temp_c']}C" if hot else ""
    return Result("ok", _clip(f"SMART ok: {len(devs)} disks, no 5/197/198/CRC growth{tail}"), metrics, items)


# =========================================================================== alert_path_health
def _smart_log_events(lines: list[str]) -> list[tuple]:
    """-> [(kind 'fail'|'ok', epoch, rc|None, err)] from smart-alert.log lines (see module docstring)."""
    ev = []
    for ln in lines:
        ts_s, _, rest = ln.partition(" ")
        ts = _iso(ts_s)
        if ts is None:
            continue
        if rest.startswith("ALERT SEND FAILED"):
            m = re.match(r"ALERT SEND FAILED rc=(\d+)", rest)
            # "rc=N for <device>: <stderr>": a device never contains ': ', the stderr text may
            err = rest.partition(": ")[2] if m else ""
            ev.append(("fail", ts, int(m.group(1)) if m else None, _scrub(err)))
        elif rest.startswith("no bridge at"):
            ev.append(("fail", ts, None, "no bridge installed"))
        elif rest.startswith("alert sent for"):
            ev.append(("ok", ts, None, ""))
    return ev


def _audit_notify_events(lines: list[str]) -> list[tuple]:
    """Same shape as _smart_log_events plus ('drop', ts, None, '') for budget-exhausted records."""
    ev = []
    for ln in lines:
        if '"notify"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        ts = _iso(str(r.get("ts", "")))
        if r.get("task") != "notify" or ts is None:
            continue
        out = str(r.get("outcome", ""))
        if r.get("action") == "send" and out.startswith("failed"):
            m = re.match(r"failed rc=(\d+)\s*(.*)", out)
            ev.append(("fail", ts, int(m.group(1)) if m else None, _scrub(m.group(2) if m else out)))
        elif r.get("action") == "send" and out == "sent":
            ev.append(("ok", ts, None, ""))
        elif r.get("action") == "budget-exhausted":
            ev.append(("drop", ts, None, ""))
    return ev


def _path_state(events: list[tuple], now: float, window_s: float) -> dict:
    """A path is 'broken' when it failed inside the window and has not succeeded since."""
    fails = [e for e in events if e[0] == "fail" and now - e[1] <= window_s]
    last_ok = max((e[1] for e in events if e[0] == "ok"), default=0.0)
    last_fail = max(fails, key=lambda e: e[1], default=None)
    return {"fails": len(fails), "last_ok": last_ok, "last_fail": last_fail,
            "drops": sum(1 for e in events if e[0] == "drop" and now - e[1] <= window_s),
            "broken": bool(last_fail) and last_fail[1] > last_ok}


def _smartd_hook(default: str) -> str | None:
    """The `-M exec <script>` in smartd.conf; None if smartd.conf is readable but has none (mail to a box
    with no MTA is a silent drop). Falls back to the configured path if smartd.conf cannot be read."""
    try:
        text = SMARTD_CONF.read_text()
    except OSError:
        return default
    for ln in text.splitlines():
        if not ln.lstrip().startswith("#"):
            m = re.search(r"-M\s+exec\s+(\S+)", ln)
            if m:
                return m.group(1)
    return None


@task("alert_path_health", klass="C0", tier="check", title="Alert path", timeout=30)
def alert_path_health(ctx: Ctx) -> Result:
    """Meta-monitor: is the channel that would tell us about problems itself working? Sends nothing."""
    now = ctx.now
    window_s = float(ctx.opt("window_hours", 24)) * 3600
    items: list[dict] = []
    level = 0                                   # 0 ok, 1 warn, 2 crit
    problems: list[str] = []

    def add(what: str, lvl: str, detail: str) -> None:
        nonlocal level
        items.append({"what": what, "level": lvl, "detail": _clip(detail, 120)})
        level = max(level, {"ok": 0, "info": 0, "warn": 1, "crit": 2}[lvl])

    # 1. bridge and smartd hook exist and are executable
    bridge = ctx.cfg.get("global", {}).get("bridge", DEFAULT_BRIDGE)
    bridge_ok = os.path.isfile(bridge) and os.access(bridge, os.X_OK)
    add("bridge", "ok" if bridge_ok else "crit", f"{bridge} " + ("executable" if bridge_ok else "missing or not executable"))
    if not bridge_ok:
        problems.append("bridge missing")
    hook = _smartd_hook(ctx.opt("smart_hook", DEFAULT_HOOK))
    hook_ok = bool(hook) and os.path.isfile(hook) and os.access(hook, os.X_OK)
    add("smartd hook", "ok" if hook_ok else "warn",
        f"{hook} executable" if hook_ok else (f"{hook} missing or not executable" if hook else "smartd.conf has no -M exec"))
    if not hook_ok:
        problems.append("smartd hook broken")

    # 2/3. send history: smart-alert.log (the smartd hook) and audit.jsonl (this runner's notifier).
    # A source is 'broken' when it failed inside the window and has not succeeded since.
    def judge(what: str, read) -> dict:
        st = {"fails": 0, "last_ok": 0.0, "last_fail": None, "drops": 0, "broken": False}
        try:
            st = _path_state(read(), now, window_s)
        except FileNotFoundError:
            return st                           # nothing ever logged: nothing to judge
        except OSError as exc:
            add(what, "warn", f"cannot read log: {exc.strerror}")
            problems.append(f"{what} log unreadable")
            return st
        if st["broken"]:
            rc, err = st["last_fail"][2], st["last_fail"][3]
            why = err or "no reason logged (pre-fix hook)"
            add(what, "warn", f"{st['fails']} sends failed in 24h, last {_when(st['last_fail'][1])} rc={rc}: {why}")
            problems.append(f"{st['fails']} {what} sends failed rc={rc}: {why}")
        elif st["fails"]:
            add(what, "ok", f"{st['fails']} failures in 24h, recovered (sent OK at {_when(st['last_ok'])})")
        return st

    log = Path(ctx.opt("smart_log", "/var/log/smart-alert.log"))
    smart = judge("smart hook", lambda: _smart_log_events(_tail_lines(log, 2 << 20)))
    note = judge("notifier", lambda: _audit_notify_events(_tail_lines(AUDIT_LOG, 16 << 20)))
    if note["drops"]:
        add("notifier budget", "info", f"{note['drops']} alerts dropped in 24h (daily budget exhausted)")

    last_err = ""
    for st in (smart, note):
        if st["broken"] and not last_err:
            last_err = st["last_fail"][3]
    metrics = {"bridge_ok": bridge_ok, "hook_ok": hook_ok,
               "smart_fail_24h": smart["fails"], "smart_broken": smart["broken"],
               "smart_last_ok_age_min": round((now - smart["last_ok"]) / 60) if smart["last_ok"] else None,
               "notify_fail_24h": note["fails"], "notify_broken": note["broken"],
               "notify_dropped_24h": note["drops"], "last_error": last_err}
    if problems:
        return Result({1: "warn", 2: "crit"}[level], _clip("alert path: " + "; ".join(problems)), metrics, items)
    healed = smart["fails"] + note["fails"]
    ok_txt = f"alert path ok; last SMART send {_when(smart['last_ok'])}" if smart["last_ok"] else "alert path ok"
    return Result("ok", _clip(ok_txt + (f" ({healed} earlier failures recovered)" if healed else "")), metrics, items)


# =========================================================================== growth_watch
# The intake ledger remembers the size of every file >= 1 MiB (keyed by inode) between runs. Module
# constants so tests can patch them.
LEDGER_MIN_BYTES = 1 << 20
LEDGER_MAX_FILES = 4000          # more large files than this: no intake figure for the path (bounded state)


class _Walk(NamedTuple):
    size: int                    # apparent bytes (sum of st_size of regular files)
    complete: bool               # False: the time budget ran out; size is a partial figure
    unreadable: int              # subdirectories that could not be listed
    files: dict | None           # {inode: (size, mtime)} of files >= LEDGER_MIN_BYTES; None: cut off / too many


def _du(path: str, budget_s: float) -> _Walk:
    """Apparent size (sum of st_size of regular files; symlinks not followed, no device crossing).

    Stops with complete=False once the time budget is spent. The same lstat also feeds the per-file
    ledger of large files (inode, size, mtime) used for the written-bytes figure.
    """
    deadline = time.monotonic() + budget_s
    st0 = os.stat(path)                       # FileNotFoundError => caller reports "missing" (a symlinked root is fine)
    if stat.S_ISREG(st0.st_mode):
        big = {st0.st_ino: (st0.st_size, st0.st_mtime)} if st0.st_size >= LEDGER_MIN_BYTES else {}
        return _Walk(st0.st_size, True, 0, big)
    os.scandir(path).close()                  # PermissionError on the root itself => "no access", not size 0
    root_dev = st0.st_dev
    total = unreadable = n = 0
    files: dict | None = {}
    stack = [path]
    while stack:
        if time.monotonic() >= deadline:
            return _Walk(total, False, unreadable, None)
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    n += 1
                    if n % 2048 == 0 and time.monotonic() >= deadline:
                        return _Walk(total, False, unreadable, None)
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev == root_dev:
                            stack.append(e.path)
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
                        if files is not None and st.st_size >= LEDGER_MIN_BYTES:
                            if len(files) >= LEDGER_MAX_FILES:
                                files = None
                            else:
                                files[st.st_ino] = (st.st_size, st.st_mtime)
        except OSError:
            unreadable += 1
    return _Walk(total, True, unreadable, files)


def _fresh_bytes(files: dict | None, ledger) -> int | None:
    """Bytes WRITTEN since the previous complete walk: growth of files already in the ledger plus the size
    of files that are new (and were written since). Keyed by inode, so a logrotate rename is not new data.
    Deletions never subtract: that is the point, a cleaner must not hide a runaway writer.
    None = no usable ledger yet (first run, state lost) or too many files to track."""
    if files is None or not isinstance(ledger, dict):
        return None
    try:
        prev, prev_t = ledger["sizes"], float(ledger["t"])
        total = 0
        for ino, (size, mtime) in files.items():
            old = prev.get(str(ino))
            # unknown inode: a new file counts in full; an old mtime means it was moved in, not written
            total += max(0, size - old) if old is not None else (size if mtime >= prev_t else 0)
        return total
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _rate_per_day(history: list[dict], path: str, now: float, bytes_now: int, min_span_s: float) -> float | None:
    """(now - sample closest to 24 h ago) per day. None until a sample >= min_span old exists."""
    samples = []
    for r in history:
        try:
            if r.get("path") == path and now - float(r["t"]) >= min_span_s:
                samples.append((float(r["t"]), int(r["bytes"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not samples:
        return None
    t0, b0 = min(samples, key=lambda r: abs((now - r[0]) - 86400))
    return (bytes_now - b0) * 86400 / (now - t0)


def _intake_per_day(history: list[dict], path: str, now: float, min_span_s: float) -> float | None:
    """Bytes written per day over the last 24 h: the sum of the per-run `fresh_bytes` increments divided by
    the time they cover (first increment's `since` to now, so a gap between runs is counted as time).
    None until the increments cover >= min_span."""
    recs = []
    for r in history:
        try:
            if r.get("path") == path and "fresh_bytes" in r and 0 <= now - float(r["t"]) < 86400:
                recs.append((float(r["since"]), int(r["fresh_bytes"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not recs:
        return None
    span = now - min(s for s, _ in recs)
    return sum(b for _, b in recs) * 86400 / span if span >= min_span_s else None


def _short(path: str) -> str:
    parts = path.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 3 else path


@task("growth_watch", klass="C0", tier="check", title="Growth watch", timeout=150)
def growth_watch(ctx: Ctx) -> Result:
    """Catch runaway growth (the Kavita 1 GiB/day debug log) by tracking directory sizes over time.

    Two figures per path, and the larger one is compared with the limit:
      * net: size now minus size ~24 h ago;
      * written: bytes written in the last ~24 h (new files + growth of existing ones). Net alone is
        blind to a cleaner, retention rule or log rotation that removes data as fast as it arrives.
    A path that cannot be measured (missing, no access, walk cut off) is never rated from stale data: it
    is shown as unmeasured, counted in the summary, and warns once it has been blind for
    `unmeasured_warn_hours` (0 disables that).
    """
    specs = [p for p in ctx.opt("paths", []) if isinstance(p, dict) and str(p.get("path", "")).startswith("/")]
    if not specs:
        return Result("skipped", "growth_watch: no paths configured")
    # ONE walk budget for the whole task (it has a 150 s SIGALRM): each path gets an equal share of what is
    # left, so one slow tree cannot starve the others and unused time rolls forward.
    deadline = time.monotonic() + min(float(ctx.opt("walk_budget_s", 90)), 120.0)
    min_span = float(ctx.opt("min_span_hours", 12)) * 3600
    blind_h = float(ctx.opt("unmeasured_warn_hours", 24))
    last = ctx.state.setdefault("last", {})                  # last complete walk: {path: {t, bytes}}
    ledgers = ctx.state.setdefault("ledger", {})             # per-file sizes of that walk: {path: {t, sizes}}
    blind = ctx.state.setdefault("unmeasured_since", {})     # {path: first run it could not be measured}
    wanted = {str(s["path"]) for s in specs}
    for d in (last, ledgers, blind):                         # forget paths that left the config
        for k in [k for k in d if k not in wanted]:
            del d[k]
    history = read_history(2 * 86400, "size")   # read once: the history file also holds big spike samples
    rows: list[dict] = []
    for i, spec in enumerate(specs):
        p = str(spec["path"])
        limit = float(spec.get("warn_gib_per_day", ctx.opt("warn_gib_per_day", 0.5)))
        row = {"path": p, "size": "n/a", "bytes": None, "gib_day": None, "rate": "n/a", "intake": "n/a",
               "peak": None, "driver": "", "limit": f"{limit:g} GiB/d", "level": "info", "note": ""}
        rows.append(row)
        try:
            walk = _du(p, max(0.0, deadline - time.monotonic()) / (len(specs) - i))
            why = None if walk.complete else "walk cut off"
        except OSError as exc:
            why = "missing" if isinstance(exc, FileNotFoundError) else f"no access: {exc.strerror}"
        if why:
            # Never rate a path from a stale value: the cached size would turn "cannot see it" into "flat".
            since = blind.setdefault(p, ctx.now)
            hrs = (ctx.now - since) / 3600
            row["note"] = f"unmeasured: {why}"
            if p in last:
                row["note"] += f" (last full size {human(last[p]['bytes'])}, {(ctx.now - last[p]['t']) / 3600:.0f}h ago)"
            if blind_h > 0 and hrs >= blind_h:
                row.update(level="warn", note=row["note"] + f"; blind {hrs:.0f}h")
            continue
        blind.pop(p, None)
        size = walk.size
        led = ledgers.get(p)
        fresh = _fresh_bytes(walk.files, led)
        rec = {"t": ctx.now, "kind": "size", "path": p, "bytes": size}      # only complete walks are recorded
        if fresh is not None:
            rec.update(fresh_bytes=fresh, since=float(led["t"]))
        append_history(rec)
        history.append(rec)
        last[p] = {"t": ctx.now, "bytes": size}
        if walk.files is None:
            ledgers.pop(p, None)
        else:
            ledgers[p] = {"t": ctx.now, "sizes": {str(k): v[0] for k, v in walk.files.items()}}
        notes = []
        if walk.unreadable:
            notes.append(f"{walk.unreadable} unreadable dirs (run as root for full size)")
        if walk.files is None:
            notes.append(f"written-bytes figure off: more than {LEDGER_MAX_FILES} files >= 1 MiB")
        net = _rate_per_day(history, p, ctx.now, size, min_span)
        wr = _intake_per_day(history, p, ctx.now, min_span)
        row.update(size=human(size), bytes=size, level="ok")
        if net is not None:
            row.update(gib_day=round(net / GIB, 3), rate=f"{net / GIB:+.2f} GiB/d")
        if wr is not None:
            row["intake"] = f"{wr / GIB:.2f} GiB/d"
        peak = max((v for v in (net, wr) if v is not None), default=None)
        if peak is None:
            notes.append("measuring (rate needs history)")
        else:
            row.update(peak=round(peak / GIB, 3),
                       driver=row["rate"] if net is not None and (wr is None or net >= wr) else f"{row['intake']} written")
            if peak / GIB > limit:
                row["level"] = "warn"
        row["note"] = "; ".join(notes)
    meas = [r for r in rows if r["bytes"] is not None]
    unm = [r for r in rows if r["bytes"] is None]
    blind_rows = [r for r in unm if r["level"] == "warn"]
    over = sorted((r for r in meas if r["level"] == "warn"), key=lambda r: -r["peak"])
    unm_txt = f"{len(unm)}/{len(rows)} paths unmeasured (missing/no access/cut off)"
    metrics = {"paths": [{k: r[k] for k in ("path", "size", "rate", "intake", "limit", "level")} for r in rows],
               "n_over": len(over), "n_unmeasured": len(unm), "worst_path": over[0]["path"] if over else None,
               "worst_gib_day": over[0]["peak"] if over else None}
    # warnings first, then paths we are blind on, then the rest by speed
    order = lambda r: (0 if r["level"] == "warn" else 1 if r["bytes"] is None else 2, -(r["peak"] or 0))
    items = [{k: r[k] for k in ("path", "size", "rate", "intake", "limit", "level", "note")}
             for r in sorted(rows, key=order)][:12]
    if over:
        n = 2 if unm else 3                                   # leave room for the unmeasured count
        txt = "growth over limit: " + ", ".join(f"{_short(r['path'])} {r['driver']}" for r in over[:n])
        txt += f" (+{len(over) - n} more)" if len(over) > n else ""
        return Result("warn", _clip(txt + (f"; {unm_txt}" if unm else "")), metrics, items)
    if blind_rows:
        names = ", ".join(_short(r["path"]) for r in blind_rows[:3])
        return Result("warn", _clip(f"growth blind >{blind_h:g}h: {len(blind_rows)}/{len(rows)} paths unmeasured "
                                    f"(missing/no access/cut off): {names}"), metrics, items)
    if not meas:
        return Result("skipped", _clip(f"growth_watch: {unm_txt}"), metrics, items)
    status, tail = ("info", f"; {unm_txt}") if unm else ("ok", "")     # info: visible on the dashboard, never pages
    rated = [r for r in meas if r["peak"] is not None]
    if not rated:
        return Result(status, _clip(f"growth: measuring {len(meas)} paths (rates need 12 h of history){tail}"),
                      metrics, items)
    top = max(rated, key=lambda r: r["peak"])
    return Result(status, _clip(f"growth ok: {len(rated)} paths, fastest {_short(top['path'])} {top['driver']}{tail}"),
                  metrics, items)


# =========================================================================== config_drift
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def _bytes(s: str | None) -> int | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*", s or "", re.I)
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()]) if m else None


def _journald_system_max_use() -> str | None:
    """Effective SystemMaxUse: main file, then drop-ins by name (/etc beats /run beats /usr/lib). None = unset."""
    drop: dict[str, Path] = {}
    for d in JOURNALD_DIRS:
        for p in d.glob("*.conf"):
            drop[p.name] = p
    val = None
    for p in [JOURNALD_MAIN] + [drop[k] for k in sorted(drop)]:
        try:
            text = p.read_text()
        except OSError:
            continue
        sect = ""                                             # sections do not carry over between files
        for ln in map(str.strip, text.splitlines()):
            if not ln or ln[0] in "#;":
                continue
            if ln.startswith("["):
                sect = ln.lower()
            elif sect == "[journal]" and ln.partition("=")[0].strip() == "SystemMaxUse":
                val = ln.partition("=")[2].strip() or None     # an empty assignment resets to the default
    return val


def _drift(what: str, actual: str, expected: str, fix: str) -> dict:
    return {"what": what, "actual": _clip(actual, 100), "expected": _clip(expected, 100), "fix": _clip(fix, 100)}


def _chk_journald(ctx: Ctx) -> list[dict]:
    want = str(ctx.opt("expect_journald_system_max_use", "1G"))
    got = _journald_system_max_use()
    if got is not None and _bytes(got) is not None and _bytes(got) == _bytes(want):
        return []
    return [_drift("journald cap", f"SystemMaxUse={got or 'unset (default 10% of fs)'}", f"SystemMaxUse={want}",
                   "write /etc/systemd/journald.conf.d/10-homelab.conf, then restart systemd-journald")]


_CRON_PATTERNS = ("journalctl --vacuum", "find /tmp", "logrotate")


def _chk_root_cron(ctx: Ctx) -> list[dict] | None:
    if not _is_root():
        return None                                       # `crontab -l -u root` needs root: skip
    r = sh(["crontab", "-l", "-u", "root"], timeout=10)
    if r.returncode not in (0, 1):                        # 1 = "no crontab for root"
        return None
    out = []
    for ln in r.stdout.splitlines():
        flat = " ".join(ln.split())
        if flat and not flat.startswith("#") and any(p in flat for p in _CRON_PATTERNS):
            out.append(_drift("root cron", flat, "line removed (journald cap, tmpfiles and the daily logrotate cover it)",
                              "crontab -e -u root"))
    return out


def _chk_snap(ctx: Ctx) -> list[dict] | None:
    want = ctx.opt("expect_snap_retain", ctx.cfg.get("tasks", {}).get("snap_revisions", {}).get("retain", 2))
    r = sh(["snap", "get", "-d", "system", "refresh.retain"], timeout=15)
    got = None
    if r.returncode == 0:
        m = re.search(r"\{.*\}", r.stdout, re.S)
        try:
            got = json.loads(m.group(0)).get("refresh.retain") if m else None
        except (ValueError, AttributeError):
            return None
        if not m:
            return None                                       # unparsable output: skip rather than guess
    elif "has no" not in r.stderr:
        return None                                       # snap missing or failing: do not guess
    if got is not None and str(got) == str(want):
        return []
    return [_drift("snap retain", f"refresh.retain={got if got is not None else 'unset (snap default 3)'}",
                   f"refresh.retain={want}", f"snap set system refresh.retain={want}")]


def _chk_docker_logs(ctx: Ctx) -> list[dict] | None:
    try:
        cfg = json.loads(DOCKER_DAEMON_JSON.read_text())
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError):
        return [_drift("docker log-opts", "daemon.json unreadable or invalid JSON", "log-opts.max-size set",
                       "fix /etc/docker/daemon.json")]
    out = []
    if cfg.get("log-driver", "json-file") == "json-file" and not (cfg.get("log-opts") or {}).get("max-size"):
        out.append(_drift("docker log-opts", "no log-opts.max-size in daemon.json", "log-opts max-size/max-file set",
                          "add log-opts to /etc/docker/daemon.json; recreate containers"))
    # containers keep the log config they were created with: list those without rotation (read-only inspect)
    ids = sh(["docker", "ps", "-aq"], timeout=15)
    if ids.returncode == 0 and ids.stdout.split():
        r = sh(["docker", "inspect", "--format", "{{.Name}}\t{{.HostConfig.LogConfig.Type}}\t{{json .HostConfig.LogConfig.Config}}"]
               + ids.stdout.split(), timeout=60)
        if r.returncode == 0:
            bare = []
            for ln in r.stdout.splitlines():
                name, _, rest = ln.partition("\t")
                typ, _, conf = rest.partition("\t")
                if typ == "json-file" and '"max-size"' not in conf:
                    bare.append(name.lstrip("/"))
            if bare:
                out.append(_drift("container logs", f"{len(bare)} container(s) without max-size: {', '.join(bare[:3])}",
                                  "every container rotates its log", "recreate with log-opts (or set in compose)"))
    return out


_AGE_UNITS = {"s": 1 / 86400, "m": 1 / 1440, "min": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7}


def _chk_tmpfiles(ctx: Ctx) -> list[dict] | None:
    want = float(ctx.opt("expect_tmp_age_days", 7))
    r = sh(["systemd-tmpfiles", "--cat-config"], timeout=20)
    if r.returncode != 0:
        return None
    age = None
    for ln in r.stdout.splitlines():
        f = ln.split()
        # systemd-tmpfiles ignores a duplicate line for a path, so the first /tmp line is the effective one
        if len(f) >= 2 and f[1] == "/tmp" and f[0].rstrip("!+~^=-") in ("D", "d", "e", "q", "Q"):
            age = f[5] if len(f) > 5 else "-"
            break
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|min|m|h|d|w)", (age or "").lstrip("~"))
    days = float(m.group(1)) * _AGE_UNITS[m.group(2)] if m else None
    if days is not None and days <= want:
        return []
    return [_drift("/tmp age", f"tmpfiles age {age or 'no /tmp rule'}", f"age <= {want:g}d",
                   "add /etc/tmpfiles.d/tmp.conf with the agreed age")]


def _ext4_sources() -> list[tuple[str, str]]:
    """[(device, mountpoint)] for each ext4 filesystem once (bind mounts share a device)."""
    seen: dict[str, str] = {}
    try:
        text = MOUNTINFO.read_text()
    except OSError:
        return []
    for ln in text.splitlines():
        pre, sep, post = ln.partition(" - ")
        f, g = pre.split(), post.split()
        if sep and len(g) >= 2 and g[0] == "ext4" and g[1].startswith("/dev/") and len(f) > 4 and f[3] == "/":
            seen.setdefault(g[1], f[4])
    return list(seen.items())


def _chk_reserved(ctx: Ctx) -> list[dict] | None:
    if not _is_root():
        return None                                       # tune2fs -l needs root
    want = float(ctx.opt("expect_reserved_pct", 1.0))
    out = []
    for dev, mnt in _ext4_sources():
        r = sh(["tune2fs", "-l", dev], timeout=20)
        blocks = re.search(r"^Block count:\s+(\d+)", r.stdout, re.M)
        resv = re.search(r"^Reserved block count:\s+(\d+)", r.stdout, re.M)
        if r.returncode != 0 or not blocks or not resv or int(blocks.group(1)) == 0:
            continue
        pct = 100 * int(resv.group(1)) / int(blocks.group(1))
        if pct > want + 0.05:                             # tolerance: -m 1 floors to 0.99999x %
            out.append(_drift("reserved blocks", f"{pct:.1f}% reserved on {mnt} ({dev})", f"<= {want:g}%",
                              f"tune2fs -m {want:g} {dev}"))
    return out


@task("config_drift", klass="C0", tier="weekly", title="Config drift", timeout=120)
def config_drift(ctx: Ctx) -> Result:
    """Read-only comparison with the agreed policy. Never pages (alert=False); each item names the fix."""
    checks = [("journald", _chk_journald), ("root cron", _chk_root_cron), ("snap", _chk_snap),
              ("docker logs", _chk_docker_logs), ("tmpfiles", _chk_tmpfiles), ("reserved blocks", _chk_reserved)]
    drifts: list[dict] = []
    skipped: list[str] = []
    for name, fn in checks:
        try:
            res = fn(ctx)
        except Exception:  # noqa: BLE001 - one broken probe must not hide the others
            res = None
        if res is None:
            skipped.append(name)
        else:
            drifts += res
    metrics = {"drifts": len(drifts), "skipped": skipped, "kinds": sorted({d["what"] for d in drifts})}
    if not drifts:
        note = f" ({', '.join(skipped)} skipped)" if skipped else ""
        return Result("ok", _clip(f"no config drift{note}"), metrics, alert=False)
    counts: dict[str, int] = {}
    for d in drifts:
        counts[d["what"]] = counts.get(d["what"], 0) + 1
    txt = f"{len(drifts)} config drifts: " + ", ".join(f"{k} x{v}" if v > 1 else k for k, v in counts.items())
    if skipped:
        txt += f" ({', '.join(skipped)} skipped)"
    return Result("info", _clip(txt), metrics, drifts[:12], alert=False)
