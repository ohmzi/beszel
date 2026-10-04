"""Daily and weekly maintenance reports (SPEC3 S5).

    reports.generate("daily" | "weekly", now) -> dict      writes STATE_DIR/public/reports/<id>.json + index.json
    python3 -m homelab_maint.reports daily|weekly|index [--now EPOCH] [--tz NAME] [--print]

Periods are whole LOCAL calendar days, so consecutive reports tile exactly (no gap, no overlap) and the same `now`
always gives the same period (re-running is idempotent):
  daily   yesterday, 00:00 -> 24:00 local.          id "2026-10-01"
  weekly  the 7 complete days ending yesterday.     id "2026-W40" = ISO week of the LAST covered day
A day is 23/24/25 h long around DST changes (or 23.5/24.5 h in Lord Howe); every duration in a report is real elapsed
time, every per-day bucket is the local date. The zone is [tasks.report_*] timezone, else $HOMELAB_MAINT_TZ / $TZ, else
/etc/localtime. Events of "today so far" (e.g. this morning's cleanup) belong to the next report; "right now" facts (open
incidents, current backup ages, pending C2 plans) come from the latest status and are labelled as such.

Inputs are read, never written (all optional, any may be missing or damaged; a missing one is named in `notes` and in the
highlights instead of being reported as "all good"): status.json, history.jsonl (task + disk records), LOG_DIR/audit.jsonl,
public|state incidents.json, spikes.jsonl (one line per tick while a spike is open plus a `closed` line: folded by `id`),
pressure-log.jsonl, changes.jsonl, maintenance-journal.jsonl, metrics-ring.json (SPEC2 raw layout), samples.jsonl,
slo.json, routine.json, schedule.json. Nothing here runs a command or touches the host. Only STATE_DIR/public/reports/ and
STATE_DIR/reports.lock are written; that is why the two tasks are class C0. Every string in the result goes through
publish.clean (or a conservative local copy) so no secret, address or deep path reaches the public page.

HEALTH SCORE (formula version 2, `health_score()`; deterministic, integer 0-100, never increases when a problem grows)
  The period is cut into 15-minute slots, the same unit as health-history.json. A slot is
    crit   if any alerting check recorded crit/error in it (or a daily/weekly task's crit result still stands, see below),
    warn   if the worst was warn,   ok otherwise,
    blind  if no check recorded anything in it.
  Checks with alert=False (docker_df, orphan_report ...; per run when history says so, else per task from status.json) never
  colour a slot, and neither do the report generators themselves (report_daily/weekly: a failing report must not grade
  itself). pressure_state/pressure_response set `alert` per RUN, not per task, so a history record that lacks the flag is
  judged from its own metrics (memory stall/wait/available/swap-in at or past the ladder's level-2 thresholds, or crit) and
  never from the task's latest status. Slots before the first record ever stored are not counted (new install). P = slots
  in the (clipped) period.
  A daily/weekly task's non-ok result stands for its cadence (a day, at most HOLD_CAP_S = 2 days for a week) ONLY once it
  has been seen on 2 consecutive runs, the Notifier's own rule: one timeout of a slow weekly task is one slot, not a week
  of F grades, and it has not paged anyone. A daily/weekly task's `error` counts as warn (a cleanup that timed out is not a
  host outage); a check-tier error is still crit.
      score = floor(100 - D_time - D_incidents - D_backups - D_stale - D_open)       clamped to 0..100, then
      score = min(score, 89) when an alerting check's LAST result in the period is crit and still stands
      D_time      = min(60, 60*crit/P + 25*warn/P + 35*blind/P)             whole period crit -60, warn -25, blind -35
      D_incidents = min(20, sum(sev1 10, sev2 5, sev3 2))                   incidents still open at the end of the period
                                                                             (a group of correlated incidents counts once)
      D_backups   = min(30, 15 per failed backup + 5 if one is stale)       state at the end of the period
      D_stale     = min(15, 3 per check whose newest result was older than its tier allows (45 min / 30 h / 9 d / 40 d))
      D_open      = 3 when any alerting check ended the period failing (warn or crit)   a problem still open at midnight is
                                                                             not the same as one that was fixed at 23:45
  Rounded DOWN: 100 means nothing at all was wrong; one warning slot already shows as 99. Grades: A >= 90, B >= 80, C >= 65,
  D >= 50, else F. Not scored (score null, grade "n/a": a made-up number would be false reassurance) only for a NEW INSTALL
  (history starts inside the period): no data, under 1 hour of it, or under 10 % of the period; between 10 % and 90 %
  coverage that score is `provisional` ("early data"). When history exists from before the period, missing results are a
  MONITORING GAP, never a new install: blind time is always scored (even all of it) and the report says "monitoring gap".
  Examples (nothing open at the end): a quiet week = 100; one warn all day = 75 (C); 6 h of crit in a day = 85 (B); all-day
  crit = 40 (F); all-day blind = 65 (C). The same all-day warn still failing at midnight = 72; a crit in the last slot of the
  day = 89 (B, capped).

ACKNOWLEDGED ISSUES (SPEC5, homelab_maint/acks.py). A history record the runner wrote while the owner had acknowledged that exact error
carries `acked: true`. Such a result is treated like `alert: false` for the SCORE: it colours no slot, opens no episode, costs no D_open /
D_incidents / D_backups point and cannot cap the grade (the owner said "I understand this and I'm okay with it"). It is not hidden: the report
gains an `acknowledged` section (every active acknowledgement with its expiry date, how often it held back an alert, and whether the issue is
failing right now), a highlight says so, and the incidents section lists an acknowledged incident but does not count it as open. The SLO
section is NOT adjusted (slo.json counts the true statuses): an acknowledgement is not an excuse to report better availability.

Report body (see build_report): id, kind, period, headline, health, highlights (<= 8 plain-language lines, most important
first, always covering the overall picture, incidents, spikes and maintenance), actions, incidents, spikes, capacity,
temperature, slo, upcoming, notes, plus pressure, days and digest_text. Deviations from the SPEC3 shape are additive only,
except spikes.handled_without_harm which is true/false/null (null = cannot be verified: unknown is not "handled"; a restart or
stop that was attempted and FAILED is unknown too) and health.score/grade which are null/"n/a" when a new install's data is
too thin to score. health.monitoring_gap is true when blind time passes 5 % of the period or a check stopped reporting: the
lead's glue should send the digest then, even when the score is null.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import statistics
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import core
from .core import GIB, Ctx, Result, human, read_json, task

SCHEMA = 1
FORMULA = 2
KEEP = 60                           # index entries (and files) kept
KEEP_MIN_DAILY = 14                 # when trimming, the newest dailies are kept before any weekly is dropped
SLOT_S = 900                        # 15-minute slots (the check tier's cadence, same as health-history.json)
MAX_FILE = 195_000                  # spec: each public file < 200 KB
MAX_LINE = 64 * 1024
HISTORY_WINDOW = 64 * 1024 * 1024   # newest bytes of history.jsonl considered (core caps the file at 40 MiB)
JSONL_WINDOW = 16 * 1024 * 1024
MAX_HIGHLIGHTS = 8
DIGEST_MAX = 600
LOOKBACK_S = 7 * 86400              # trends and episodes look at least this far back from the end of the period
CONFIRM_LOOKBACK_S = 15 * 86400     # task records go this far back: a weekly result is confirmed by the run a week before it
HOLD_CAP_S = 2 * 86400              # a daily/weekly result keeps counting at most this long after its run

LEVEL = {"ok": 0, "info": 0, "skipped": 0, "warn": 1, "crit": 2, "error": 2}
WORD = ("ok", "warn", "crit")
TIER_STALE_S = {"check": 45 * 60, "daily": 30 * 3600, "weekly": 9 * 86400, "monthly": 40 * 86400}     # payloads.py's limits (+ monthly)
TIER_HOLD_S = {"check": SLOT_S, "daily": 86400, "weekly": 7 * 86400, "monthly": 31 * 86400}           # how long a result stands until the next run
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# health score weights (see module docstring)
W_CRIT, W_WARN, W_BLIND = 60.0, 25.0, 35.0
OPEN_PTS, OPEN_CRIT_CAP = 3.0, 89   # a problem still open at the end of the period; a still-open crit keeps the grade below A
GAP_BLIND_PCT = 5.0                 # blind time over this share of the period is a "monitoring gap"
INC_PTS = {1: 10.0, 2: 5.0, 3: 2.0}
INC_CAP = 20.0
BACKUP_FAIL_PTS, BACKUP_STALE_PTS, BACKUP_CAP = 15.0, 5.0, 30.0
STALE_PTS, STALE_CAP = 3.0, 15.0
GRADES = ((90, "A"), (80, "B"), (65, "C"), (50, "D"))
PROVISIONAL_BELOW = 90.0            # coverage % under which a score is marked provisional

NOT_MAINTENANCE = {"notify", "gate", "acks"}          # (acks: the owner's acknowledgements are not maintenance actions)
ACTION_TITLES = {
    "docker_cache": "Docker build cache pruned", "docker_images": "Unused Docker images removed",
    "apt_clean": "APT package cache cleaned", "snap_revisions": "Old snap revisions removed",
    "retention": "Old files removed by retention rules", "trash": "Trash emptied",
    "gradle_reaper": "Idle Gradle/Kotlin daemons stopped", "caps": "Container memory ceilings applied",
    "c2_candidates": "Approved cleanup applied", "pressure_response": "Pressure response",
    "qos_classes": "Service class weights applied",
    # cleaners v2 (audit actions: apt-purge-stale-nvidia, apt-purge-unused, flatpak-uninstall-unused, npm-cache ... c2-archive-remove)
    "stale_driver_packages": "Stale NVIDIA driver packages purged", "apt_autoremove_unused": "Unused packages purged",
    "flatpak_unused": "Unused Flatpak runtimes removed", "tool_caches": "Tool and app caches cleaned",
    "stale_build_output": "Stale build output removed", "unused_venvs": "Approved venv archive and removal applied",
    "large_cold_files": "Approved cold-file archive applied", "app_cache_trim": "App caches trimmed",
    "log_compress": "Rotated logs compressed", "dangling_images": "Dangling Docker images removed",
    "crash_dumps": "Old crash dumps removed", "apt_cache": "APT package cache cleaned",
    "openwebui_media_prune": "Open WebUI media pruned", "routine_rotate": "Logs rotated",
    # pass 1 leftovers (native ports): the two restarts say so in their title, so a report never lists them as plain cleanups
    "comfyui_idle_reclaim": "ComfyUI restarted to free idle GPU memory", "immich_recycle": "Immich server recycled",
    "docker_containers_prune": "Stopped Docker containers removed",
    "swap_auto_relief": "Swap emptied automatically (pages returned to RAM)",   # wording avoids HARM_RX: it never kills or restarts anything
}
HARM_RX = re.compile(r"restart|\bstop\b|kill|reboot|\boom\b|sigterm|terminate|\bdocker[ -]rm\b|container[ -](?:rm|remove)", re.I)
# ^ an action that can hurt a workload (kill covers sigkill; gradle_reaper audits "sigterm-daemon"; `docker image rm` is not one)
# Tasks the score never counts: the report generators themselves (a failing report must not grade itself).
ALWAYS_INFO = frozenset({"report_daily", "report_weekly"})
# Tasks whose `alert` flag depends on the RUN (pressure_state pages only at memory level >= 2), not on the task: a history
# record without the flag is judged from its own metrics. (key, level-2 threshold from pressure.py's SIGNALS, larger is worse)
RUN_ALERT_TASKS = frozenset({"pressure_state", "pressure_response"})
MEM_ALERT_SIGNALS = (("psi_mem_full60", 3.0, True), ("psi_mem_some60", 20.0, True), ("mem_avail_gib", 12.0, False),
                     ("swap_in_pps", 1000.0, True))
SOFT_RX = re.compile(r"unload|free|cpu-shares|blkio|update|throttle|reclaim|memory-reservation", re.I)


# =========================================================================== small helpers
def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _asc(s: Any, n: int = 240) -> str:
    """One line, printable ASCII, bounded: reports end up in SMS, e-mail and a public page. The text is REDACTED FIRST and
    shortened afterwards: cutting first would leave the front half of a token that straddles the cut (the redaction
    patterns need the whole token). The redactor sees `n` + 240 characters, so no token that starts inside the kept part
    can be longer than what it sees (a longer one shows as a long blob and is redacted as one)."""
    s = _cleaner()(str(s)[: min(n + 240, 800)], 800)
    s = re.sub(r"[^\x20-\x7e]", "?", " ".join(s.split()))
    return s if len(s) <= n else s[: max(n - 2, 1)] + ".."


_RC_TAIL = re.compile(r"(?i)\b(rc=\d+)\b.*")


def _cut_rc(s: str) -> str:
    """Drop what follows an "rc=<n>" marker: that is raw stderr of a command (provider replies, credential file names).
    Same rule as publish._cut_rc."""
    return _RC_TAIL.sub(r"\1", s)


def _opaque() -> frozenset:
    """Checks whose text quotes another tool's error output: publish.OPAQUE_CHECKS (alert_path_health at least)."""
    try:
        from .publish import OPAQUE_CHECKS
        return frozenset(OPAQUE_CHECKS) | {"alert_path_health"}
    except Exception:                               # noqa: BLE001 - publish.py is another stream's file
        return frozenset({"alert_path_health"})


def _check_text(name: str, summary: Any, n: int, entry: dict | None = None) -> str:
    """Free text of one check result for the report. An opaque check (alert_path_health) is never quoted: its summary is
    rebuilt from counters by publish's own helper when available, else left out. Everything else loses the status prefix and
    whatever follows "rc=<n>", then goes through the redactor before it is shortened."""
    if name in _opaque():
        try:
            from .publish import _opaque_summary
            return _asc(re.sub(r"^(ok|info|warn|crit|error|skipped):\s*", "", str(_opaque_summary(entry or {}))), n)
        except Exception:                           # noqa: BLE001
            return ""
    return _asc(_cut_rc(re.sub(r"^(ok|info|warn|crit|error|skipped):\s*", "", str(summary or ""))), n)


def _pct(a: float, b: float) -> float | None:
    return None if not b else round(100.0 * a / b, 1)


def _dur(sec: float) -> str:
    """45 min / 3 h 10 min / 2 d 4 h."""
    m = int(max(sec, 0) // 60)
    if m < 1:
        return "<1 min"
    if m < 90:
        return f"{m} min"
    h, rem = divmod(m, 60)
    if h < 48:
        return f"{h} h" if rem < 10 else f"{h} h {rem} min"
    d, hr = divmod(h, 24)
    return f"{d} d" if hr == 0 else f"{d} d {hr} h"


def _parse_size(s: Any) -> float:
    """'14.9 GiB' -> bytes (0 when it is not a human size)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\s*", str(s or ""), re.I)
    if not m:
        return 0.0
    mult = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": GIB, "TIB": 1024 ** 4}
    return float(m.group(1)) * mult.get(m.group(2).upper(), 0)


def _ls(x: Any) -> list:
    """`x` if it is a list, else [] (a damaged or hand-edited file may put any JSON type anywhere)."""
    return x if isinstance(x, list) else []


def _dc(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# =========================================================================== time zone and periods
def resolve_tz(name: str | None = None) -> tzinfo:
    """Configured zone, else $HOMELAB_MAINT_TZ / $TZ, else the host's /etc/localtime, else a fixed UTC offset."""
    for cand in (name, os.environ.get("HOMELAB_MAINT_TZ"), (os.environ.get("TZ") or "").lstrip(":")):
        if cand:
            try:
                return ZoneInfo(cand)
            except (KeyError, ValueError, OSError):
                pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return ZoneInfo(link.split("zoneinfo/", 1)[1])
    except (KeyError, ValueError, OSError):
        pass
    try:
        return ZoneInfo(Path("/etc/timezone").read_text().strip())
    except (KeyError, ValueError, OSError):
        pass
    return timezone(timedelta(seconds=time.localtime().tm_gmtoff or 0))      # no DST knowledge: last resort


def tz_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or tz.tzname(None) or "local"


def _midnight(d: date, tz: tzinfo) -> float:
    """Epoch of the first instant of local date `d` (fold=0 resolves a midnight that a DST jump skips)."""
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _local_date(t: float, tz: tzinfo) -> date:
    return datetime.fromtimestamp(t, tz).date()


def _fmt_day(d: date) -> str:
    return f"{DOW[d.weekday()]} {d.day} {MON[d.month - 1]}"


def _fmt_t(t: float, tz: tzinfo, day: bool = True) -> str:
    dt = datetime.fromtimestamp(t, tz)
    return (f"{DOW[dt.weekday()]} " if day else "") + f"{dt:%H:%M}"


@dataclass(frozen=True)
class Period:
    kind: str
    id: str
    start: float
    end: float
    days: tuple          # local dates covered, oldest first
    label: str
    tz: str

    @property
    def span(self) -> str:
        return "day" if self.kind == "daily" else "week"

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60.0


def period_for(kind: str, now: float, tz: tzinfo) -> Period:
    if kind not in ("daily", "weekly"):
        raise ValueError(f"unknown report kind {kind!r}")
    today = _local_date(now, tz)
    n = 1 if kind == "daily" else 7
    first, last = today - timedelta(days=n), today - timedelta(days=1)
    if kind == "daily":
        pid, label = last.isoformat(), f"{_fmt_day(last)} {last.year}"
    else:
        y, w, _ = last.isocalendar()
        pid = f"{y}-W{w:02d}"
        label = f"{_fmt_day(first)} - {_fmt_day(last)} {last.year}"
    return Period(kind, pid, _midnight(first, tz), _midnight(today, tz),
                  tuple(first + timedelta(days=i) for i in range(n)), label, tz_name(tz))


def parse_ts(v: Any, tz: tzinfo) -> float | None:
    """Epoch from a number or an ISO-ish string ("2026-10-01T21:45:19-0400"); a naive string is in `tz`."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip())
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()
    return None


# =========================================================================== tolerant readers
def _tail_lines(path: Path, window: int):
    """Lines (bytes) of the last `window` bytes of a file; the cut-off first line and over-long lines are skipped."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - window)
            f.seek(start)
            skip = start > 0
            for line in f:
                if skip:
                    skip = False
                    continue
                if len(line) <= MAX_LINE:
                    yield line
    except OSError:
        return


_T_PREFIX = re.compile(rb'^\{\s*"t":\s*([0-9.eE+-]+)')


def _prefix_t(line: bytes) -> float | None:
    """The epoch in a `{"t": ...` line without parsing the line (None when absent, malformed or not finite)."""
    m = _T_PREFIX.match(line)
    try:
        t = float(m.group(1)) if m else None
    except ValueError:                                      # "1791269.00.0": damaged, not a number
        return None
    return t if t is not None and math.isfinite(t) else None


@dataclass
class Rec:
    """One history `task` record."""
    t: float
    task: str
    status: str
    level: int
    m: dict
    info: bool | None = None      # the run flagged itself informational (history `alert: false`); None = not recorded
    acked: bool = False           # the owner had acknowledged this exact error when it ran (history `acked: true`): counts as informational


def _scan_history(path: Path, since: float, until: float) -> tuple[list[Rec], dict[str, list], float | None] | None:
    """(task records, disk series {mount: [(t, free)]}, time of the very first record) for since <= t < until.
    None when the file does not exist. Sample records (huge) are skipped by prefix; damaged lines are skipped."""
    if not path.exists():
        return None
    recs: list[Rec] = []
    disk: dict[str, list] = defaultdict(list)
    first_t: float | None = None
    try:
        with open(path, "rb") as f:
            for _ in range(20):
                first_t = _prefix_t(f.readline(MAX_LINE))
                if first_t is not None:
                    break
    except OSError:
        pass
    for line in _tail_lines(path, HISTORY_WINDOW):
        t0 = _prefix_t(line)
        if t0 is not None and not since <= t0 < until:
            continue
        if b'"sample"' in line[:64]:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = _num(r.get("t")) if isinstance(r, dict) else None
        if t is None or not since <= t < until:
            continue
        if r.get("kind") == "task" and isinstance(r.get("task"), str):
            st = str(r.get("status") or "ok")
            acked = r.get("acked") is True
            recs.append(Rec(t, r["task"], st, LEVEL.get(st, 0), r.get("metrics") if isinstance(r.get("metrics"), dict) else {},
                            True if acked else (not r["alert"]) if isinstance(r.get("alert"), bool) else None, acked))
        elif r.get("kind") == "disk" and isinstance(r.get("mount"), str) and _num(r.get("free")) is not None:
            disk[r["mount"]].append((t, float(r["free"])))
    recs.sort(key=lambda r: r.t)
    for v in disk.values():
        v.sort()
    return recs, dict(disk), first_t


def _read_jsonl(path: Path, since: float, until: float, tz: tzinfo,
                keys: tuple = ("ts", "t")) -> list[dict] | None:
    """Dict records whose timestamp (first parsable of `keys`) is in [since, until), as `_t`, oldest first.
    None when the file does not exist (unknown), [] when it exists and nothing matched."""
    if not path.exists():
        return None
    out = []
    for line in _tail_lines(path, JSONL_WINDOW):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        t = next((x for x in (parse_ts(r.get(k), tz) for k in keys) if x is not None), None)
        if t is not None and since <= t < until:
            r["_t"] = t
            out.append(r)
    out.sort(key=lambda r: r["_t"])
    return out


def _first_json(*paths: Path) -> Any:
    for p in paths:
        d = read_json(p, None)
        if d is not None:
            return d
    return None


def _outcome_class(raw: Any) -> str:
    low = str(raw or "").strip().lower()
    for key in ("done", "dry-run", "approved", "sent", "dropped"):
        if low == key or low.startswith(key + ":") or (key == "sent" and low.startswith("sent")):
            return key
    if low.startswith("refused"):
        return "refused"
    if low.startswith("failed"):
        return "failed"
    return "other"


def _read_audit(path: Path, since: float, until: float, tz: tzinfo) -> list[dict] | None:
    rows = _read_jsonl(path, since, until, tz, keys=("ts",))
    if rows is None:
        return None
    out = []
    for r in rows:
        tk = str(r.get("task") or "?")[:40]
        if tk == "notify":                          # subject and bridge stderr are free text: only the outcome is kept
            out.append({"t": r["_t"], "task": "notify", "action": "alert", "target": "", "bytes": 0,
                        "o": "sent" if _outcome_class(r.get("outcome")) == "sent" else
                        "dropped" if _outcome_class(r.get("outcome")) == "dropped" else "failed", "raw": ""})
        else:
            out.append({"t": r["_t"], "task": tk, "action": str(r.get("action") or ""), "target": str(r.get("target") or ""),
                        "bytes": int(max(_num(r.get("bytes"), 0) or 0, 0)), "o": _outcome_class(r.get("outcome")),
                        "n": int(min(max(_num(r.get("n"), 1) or 1, 1), 10 ** 6)),         # core.Ctx.flush_dry: one row for n unlisted would-do decisions
                        "raw": _cut_rc(str(r.get("outcome") or ""))})         # stderr after "rc=<n>" is never kept
    return out


# =========================================================================== redaction
_FB_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,10}://)(?:[^\s/?#@]*@)?([^\s/?#]*)([^\s?#]*)(?:[?#]\S*)?")
_FB_KV = re.compile(r"(?i)\b([\w.-]{0,24}(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth(?:orization)?|cookie)[\w.-]{0,24})"
                    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_FB_PREFIXED = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}|"
                          r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*)")
_FB_BLOB = re.compile(r"[A-Za-z0-9_+=-]{32,}")
_FB_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_FB_PHONE = re.compile(r"(?<![\w.+-])(?:\+\d{10,15}(?!\d)|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-]))")
_FB_HOME = re.compile(r"(/(?:home/[^/\s]+|root))((?:/[^/\s]+)+)")


def _fallback_clean(v: Any, n: int = 700) -> str:
    s = " ".join(str(v).split())
    s = _FB_URL.sub(lambda m: m.group(1) + m.group(2) + m.group(3), s)
    s = _FB_KV.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", s)
    s = _FB_PREFIXED.sub("[redacted]", s)
    s = _FB_BLOB.sub("[redacted]", s)
    s = _FB_EMAIL.sub("[redacted]", s)
    s = _FB_PHONE.sub("[redacted]", s)
    s = _FB_HOME.sub(lambda m: m.group(1) + "".join("/" + p for p in m.group(2).split("/")[1:4])
                     + ("/..." if len(m.group(2).split("/")) > 4 else ""), s)
    return s[:n]


def _cleaner() -> Callable[..., str]:
    """publish.clean (the one redaction rulebook for public files) when importable, else a conservative local copy."""
    try:
        from .publish import clean
        clean("probe")
        return clean
    except Exception:                               # noqa: BLE001 - publish.py is another stream's file
        return _fallback_clean


def _scrub(obj: Any, clean: Callable[..., str], depth: int = 0) -> Any:
    """Whole-document safety net: every string through the redactor, ASCII only, finite numbers, JSON types."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        return {str(k): _scrub(v, clean, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, clean, depth + 1) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"[^\x20-\x7e]", "?", clean(obj, 700))
    if isinstance(obj, float):
        return round(obj, 4) if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    return _asc(obj)


# =========================================================================== inputs
@dataclass
class Inputs:
    """Everything build_report reads. None = the source does not exist (unknown); empty = it exists and had nothing."""
    status: dict | None = None
    recs: list[Rec] | None = None                 # task history records, up to the end of the period
    disk: dict[str, list] = field(default_factory=dict)
    first_t: float | None = None                  # first history record ever stored
    audit: list[dict] | None = None               # in the period
    incidents: dict | None = None                 # incidents.json snapshot ({"open": [...], "recent": [...]})
    acks: dict | None = None                      # public/acks.json (acks.py): the acknowledged issues as of now
    spikes: list[dict] | None = None
    pressure_log: list[dict] | None = None
    changes: list[dict] | None = None
    journal: list[dict] | None = None
    ring: dict | None = None                      # metrics-ring.json (SPEC2 raw layout)
    samples: dict[str, list] = field(default_factory=dict)     # {"first": [...], "last": [...]} guard samples (weekly)
    slo: dict | None = None
    routine: dict | None = None
    schedule: dict | None = None
    index: list[dict] = field(default_factory=list)
    paused: bool = False
    opts: dict = field(default_factory=dict)


def _read_sample_edges(path: Path, per: Period, edge_s: float) -> dict[str, list]:
    """Guard samples in the first and last `edge_s` of the period (parse only those lines)."""
    out: dict[str, list] = {"first": [], "last": []}
    if not path.exists():
        return out
    lo, hi = (per.start, per.start + edge_s), (per.end - edge_s, per.end)
    for line in _tail_lines(path, 96 * 1024 * 1024):
        t = _prefix_t(line)
        if t is None:
            continue
        key = "first" if lo[0] <= t < lo[1] else "last" if hi[0] <= t < hi[1] else None
        if key:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("c"), dict) and r["c"]:
                out[key].append(r)
    return out


def gather(per: Period, tz: tzinfo, opts: dict | None = None) -> Inputs:
    """Read every input for `per` (read-only, bounded, tolerant)."""
    S, L = Path(core.STATE_DIR), Path(core.LOG_DIR)
    pub = S / "public"
    inp = Inputs(opts=dict(opts or {}))
    st = read_json(S / "status.json", None)
    inp.status = st if isinstance(st, dict) else None
    since = per.end - max(per.end - per.start, CONFIRM_LOOKBACK_S) - 3600       # task records: a weekly result is confirmed by the run before it
    h = _scan_history(S / "history.jsonl", since, per.end)
    if h is not None:
        inp.recs, inp.disk, inp.first_t = h
    inp.audit = _read_audit(L / "audit.jsonl", per.start, per.end, tz)
    inc = _first_json(pub / "incidents.json", S / "incidents.json")
    inp.incidents = inc if isinstance(inc, dict) else None
    ak = _first_json(pub / "acks.json")
    inp.acks = ak if isinstance(ak, dict) else None
    sp = ("t", "start", "t_start", "started_at", "ts", "start_ts")
    inp.spikes = _read_jsonl(S / "spikes.jsonl", per.start - 3600, per.end, tz, keys=sp)
    inp.pressure_log = _read_jsonl(S / "pressure-log.jsonl", per.start - 3600, per.end, tz)
    inp.changes = _read_jsonl(S / "changes.jsonl", per.start, per.end, tz)
    inp.journal = _read_jsonl(S / "maintenance-journal.jsonl", per.start, per.end, tz)
    ring = read_json(S / "metrics-ring.json", None)
    inp.ring = ring if isinstance(ring, dict) else None
    slo = _first_json(pub / "slo.json", S / "slo.json")
    inp.slo = slo if isinstance(slo, dict) else None
    ro = _first_json(pub / "routine.json", S / "routine.json")
    inp.routine = ro if isinstance(ro, dict) else None
    sc = _first_json(pub / "schedule.json")
    inp.schedule = sc if isinstance(sc, dict) else None
    inp.index = load_index(pub / "reports")
    inp.paused = (Path(core.CONF_DIR) / "PAUSE").exists()
    if per.kind == "weekly":
        inp.samples = _read_sample_edges(S / "samples.jsonl", per, 24 * 3600)
    return inp


# =========================================================================== health score
def health_score(*, period_min: float, crit_min: float = 0, warn_min: float = 0, blind_min: float = 0,
                 open_incidents=(), backups_failed: int = 0, backups_stale: int = 0, stale_checks: int = 0,
                 open_level: int = 0) -> dict:
    """The documented formula (module docstring). Pure, deterministic, bounded 0..100, and non-increasing in every
    argument (each term is a clamped non-decreasing function of its own input; terms are summed and subtracted, and the
    final cap only ever lowers). `open_level` is the worst level among alerting checks whose last result in the period
    still stands: 0 none, 1 warn, 2 crit."""
    P = max(_num(period_min, 0) or 0, 1.0)
    f = lambda x: min(max((_num(x, 0) or 0) / P, 0.0), 1.0)         # noqa: E731
    d_time = min(W_CRIT, W_CRIT * f(crit_min) + W_WARN * f(warn_min) + W_BLIND * f(blind_min))
    d_inc = min(INC_CAP, sum(INC_PTS[_sev(s)] for s in open_incidents))
    d_bk = min(BACKUP_CAP, BACKUP_FAIL_PTS * max(int(backups_failed), 0) + BACKUP_STALE_PTS * max(int(backups_stale), 0))
    d_st = min(STALE_CAP, STALE_PTS * max(int(stale_checks), 0))
    lvl = min(max(int(_num(open_level, 0) or 0), 0), 2)
    d_open = OPEN_PTS if lvl else 0.0
    raw = max(0.0, min(100.0, 100.0 - d_time - d_inc - d_bk - d_st - d_open))
    score = int(raw + 1e-6)                                         # rounded DOWN: 100 means nothing at all was wrong
    if lvl >= 2:
        score = min(score, OPEN_CRIT_CAP)                           # a day that ends on an unresolved critical is never an A
    return {"score": score, "grade": grade_for(score),
            "deductions": {"time": round(d_time, 1), "incidents": round(d_inc, 1), "backups": round(d_bk, 1),
                           "stale_checks": round(d_st, 1), "open": round(d_open, 1)}}


def grade_for(score: int | None) -> str:
    if score is None:
        return "n/a"
    return next((g for lim, g in GRADES if score >= lim), "F")


# =========================================================================== analysis: checks
def _title(tasks: dict, name: str) -> str:
    t = (tasks.get(name) or {}).get("title")
    return str(t) if t else name.replace("_", " ").capitalize()


def _tasks_of(status: dict | None) -> dict[str, dict]:
    t = (status or {}).get("tasks")
    return {str(k): v for k, v in t.items() if isinstance(v, dict)} if isinstance(t, dict) else {}


def _tier(entry: dict) -> str:
    t = entry.get("tier")
    return t if isinstance(t, str) else ""


def _tm(tasks: dict, name: str) -> dict:
    """metrics of one task in status.json, always a dict."""
    return _dc(_dc(tasks.get(name)).get("metrics"))


def _run_alerts(r: Rec) -> bool:
    """Did this run of a run-dependent task (pressure_state) alert, when the history record lacks its `alert` flag?
    pressure_state pages only at MEMORY level >= 2: judge from its own metrics against the ladder's level-2 thresholds. A crit
    can only be memory (io/cpu are capped at level 3). Nothing to judge by = alerting: an unproven "informational" would hide
    the problem. pressure_response: a warn is a failed action or a runaway it could not touch, which pages: alerting."""
    if r.task == "pressure_response" or r.status in ("crit", "error"):
        return True
    seen = False
    for key, lim, bigger_is_worse in MEM_ALERT_SIGNALS:
        v = _num(r.m.get(key))
        if v is None:
            continue
        seen = True
        if v >= lim if bigger_is_worse else v <= lim:
            return True
    return not seen


def _lvl(r: Rec, alert_off: set[str]) -> int:
    """The level a result counts at: informational results count as ok. Order: the report generators never count; the flag
    recorded with the run; for pressure_state/pressure_response (the flag depends on the run) the run's own metrics, unless
    the task is in `alert_off` (then only an explicit ignore_tasks); else the task's flag in status.json."""
    if not r.level or r.task in ALWAYS_INFO:
        return 0
    if r.info is not None:
        off = r.info
    elif r.task in RUN_ALERT_TASKS:
        off = r.task in alert_off or not _run_alerts(r)
    else:
        off = r.task in alert_off
    return 0 if off else r.level


def _cadence(tiers: dict, name: str) -> float:
    """Seconds between runs of a task (15 min when its tier is unknown: it counts like a check)."""
    return TIER_HOLD_S.get(tiers.get(name), SLOT_S)


def _eff_lvl(r: Rec, alert_off: set[str], cadence: float) -> int:
    """_lvl, except that a daily/weekly/monthly task's `error` (a cleanup that timed out) counts as warn, not crit."""
    lv = _lvl(r, alert_off)
    return min(lv, 1) if lv and r.status == "error" and cadence > SLOT_S else lv


def _stands(rs: list[Rec], i: int, alert_off: set[str], cadence: float) -> tuple[float, int]:
    """(seconds, level) that the non-ok result rs[i] counts for, from its own timestamp. A check-tier result is one slot. A
    daily/weekly result counts for its whole cadence (at most HOLD_CAP_S) only when the run before it was ALSO non-ok, the
    Notifier's rule (2 consecutive runs; it pages nobody before that), else it is one slot like any other blip."""
    lv = _eff_lvl(rs[i], alert_off, cadence)
    if cadence <= SLOT_S or i == 0:
        return SLOT_S, lv
    prev = rs[i - 1]
    if _lvl(prev, alert_off) == 0 or rs[i].t - prev.t > 2 * cadence + SLOT_S:
        return SLOT_S, lv
    return min(cadence, HOLD_CAP_S), lv


def _episodes(recs: list[Rec], per: Period, alert_off: set[str] = frozenset(), hold_s: float = SLOT_S) -> list[dict]:
    """Stretches of one task's non-ok results that touch the period (start/end clipped to it). Each non-ok result counts for
    `_stands` (one slot, or its cadence once confirmed; `hold_s` is the task's cadence) and stretches that touch merge, so the
    episode time is the same time the score counts. `open`: the task's newest result is non-ok."""
    out: list[dict] = []
    cur: dict | None = None
    for i, r in enumerate(recs):
        lv = _eff_lvl(r, alert_off, hold_s)
        if lv <= 0:
            continue
        stand, lvs = _stands(recs, i, alert_off, hold_s)
        stop = r.t + stand
        if stand > SLOT_S and i + 1 < len(recs) and recs[i + 1].t < stop:
            stop = recs[i + 1].t                                    # the next run replaces this result
        if cur is not None and r.t <= cur["stop"] + SLOT_S / 2:     # touches the previous stretch (run jitter tolerated)
            cur["stop"], cur["level"], cur["last"] = max(cur["stop"], stop), max(cur["level"], lvs), i
        else:
            if cur is not None:
                out.append(cur)
            cur = {"start": r.t, "stop": stop, "level": lvs, "last": i}
    if cur is not None:
        out.append(cur)
    return [{"start": max(c["start"], per.start), "end": min(c["stop"], per.end), "level": c["level"],
             "before": c["start"] < per.start, "open": c["last"] == len(recs) - 1}
            for c in out if min(c["stop"], per.end) > per.start and c["start"] < per.end]


def _blank_checks(per: Period, first_t: float | None = None, n_recs: int = 0) -> dict:
    """analyse_checks' result when there is nothing to analyse (also the fallback if the analysis itself breaks)."""
    return {"has_data": bool(n_recs), "recs": n_recs, "first_t": first_t, "nominal_slots": max(int((per.end - per.start + SLOT_S - 1) // SLOT_S), 1),
            "eff_start": per.start, "n_slots": 0, "covered": 0, "ok_min": 0, "warn_min": 0, "crit_min": 0,
            "blind_min": 0, "observed_pct": 0.0, "time_ok_pct": None, "worst": "unknown",
            "ok_pct_by_task": {}, "episodes": {}, "by_day": {}, "stale": [], "open": {},
            "new_install": False, "gap": False,
            "backups": {"failed": 0, "stale": 0, "seen": False, "peak_failed": 0}}


def _gap_phrase(chk: dict) -> str | None:
    """'20 h without results' / '2 checks stopped reporting' when monitoring itself had a hole, else None. Blind time over
    GAP_BLIND_PCT of the period counts; so does any check that stopped reporting by the end of it."""
    blind = chk.get("blind_min") or 0
    if blind > 0 and blind >= GAP_BLIND_PCT / 100.0 * (chk.get("n_slots") or 0) * 15:
        return f"{_dur(blind * 60)} without results"
    if chk.get("stale"):
        return f"{_plural(len(chk['stale']), 'check')} stopped reporting"
    return None


def analyse_checks(inp: Inputs, per: Period, tz: tzinfo, alert_off: set[str]) -> dict:
    """Slots, minutes, per-day buckets, per-task ok %, episodes, the checks still failing at the end, and the staleness/backup
    state at the period end.

    `n_slots` is the (clipped) period the score is computed over; `observed_pct` is the share of the NOMINAL period that
    has check results. `new_install` is true only when history STARTS inside the period (those first slots are clipped, not
    blind); history that exists from before the period and has holes is a monitoring gap (`gap`), scored as blind time."""
    recs = inp.recs or []
    in_per = [r for r in recs if per.start <= r.t < per.end]
    res = _blank_checks(per, inp.first_t, len(in_per))
    nominal = res["nominal_slots"]
    predates = inp.first_t is not None and inp.first_t < per.start      # history existed before the period began
    if not in_per and not predates:
        return res
    eff_start = max(per.start, inp.first_t) if inp.first_t else per.start
    s0, s1 = int(eff_start // SLOT_S), int((per.end - 1e-6) // SLOT_S)
    tiers = {n: _tier(e) for n, e in _tasks_of(inp.status).items()}
    cad = lambda n: _cadence(tiers, n)                                   # noqa: E731
    slots: dict[int, int] = {}                       # slot -> worst level of an ALERTING check (informational ones: ok)
    for r in in_per:
        s = int(r.t // SLOT_S)
        slots[s] = max(slots.get(s, 0), _eff_lvl(r, alert_off, cad(r.task)))
    by_task: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        if r.t < per.end:
            by_task[r.task].append(r)
    for n, rs in by_task.items():                    # a confirmed daily/weekly result stands until its next run, not for one slot
        for i, r in enumerate(rs):
            lv = _eff_lvl(r, alert_off, cad(n))
            if not lv:
                continue
            hold, lv = _stands(rs, i, alert_off, cad(n))
            if hold > SLOT_S:
                nxt = i + 1 < len(rs) and rs[i + 1].t < r.t + hold
                stop = min(rs[i + 1].t if nxt else r.t + hold, per.end)
                last = int(stop // SLOT_S) - 1 if nxt else int((stop - 1e-6) // SLOT_S)    # the slot of the next run belongs to the next result
                for sl in range(max(int(r.t // SLOT_S), s0), min(last, s1) + 1):
                    if sl in slots:
                        slots[sl] = max(slots[sl], lv)
    covered = [s for s in slots if s0 <= s <= s1]
    n_slots = s1 - s0 + 1
    crit = sum(1 for s in covered if slots[s] == 2)
    warn = sum(1 for s in covered if slots[s] == 1)
    ok = len(covered) - crit - warn
    res.update(eff_start=eff_start, n_slots=n_slots, covered=len(covered), ok_min=ok * 15, warn_min=warn * 15,
               crit_min=crit * 15, blind_min=(n_slots - len(covered)) * 15,
               observed_pct=round(100.0 * len(covered) / nominal, 1), time_ok_pct=_pct(ok, len(covered)),
               worst=WORD[max((slots[s] for s in covered), default=0)] if covered else "unknown",
               new_install=bool(in_per) and s0 > int(per.start // SLOT_S))
    by_day = {d: {"ok": 0, "warn": 0, "crit": 0} for d in per.days}
    for s in covered:
        d = _local_date(s * SLOT_S, tz)
        if d in by_day:
            by_day[d][WORD[slots[s]]] += 15
    res["by_day"] = by_day
    for n, rs in sorted(by_task.items()):
        mine = [r for r in rs if per.start <= r.t < per.end]
        if mine:
            res["ok_pct_by_task"][n] = _pct(sum(1 for r in mine if r.level == 0), len(mine))
        eps = _episodes(rs, per, alert_off, cad(n))
        if eps:
            res["episodes"][n] = eps
        # still failing at the end of the period: the newest result is non-ok and still counts (a check-tier result is
        # tolerated up to the staleness limit so one late run does not hide it)
        i = len(rs) - 1
        lv = _eff_lvl(rs[i], alert_off, cad(n))
        if lv:
            hold, lv = _stands(rs, i, alert_off, cad(n))
            if per.end - rs[i].t <= (hold if hold > SLOT_S else TIER_STALE_S["check"]):
                res["open"][n] = lv
    # staleness at the END of the period: newest result per known-tier task vs what its tier allows
    stale = []
    for n, e in _tasks_of(inp.status).items():
        thr = TIER_STALE_S.get(_tier(e))
        if not thr:
            continue
        last = max((r.t for r in by_task.get(n, [])), default=None)
        if last is None:
            lr = _num(e.get("last_run"))
            last = lr if lr is not None and lr < per.end else None
        if last is not None and per.end - last > thr:
            stale.append(n)
    res["stale"] = sorted(stale)
    bf = [r for r in by_task.get("backup_freshness", []) if r.t < per.end]
    if bf:
        failed = int(_num(bf[-1].m.get("failed"), 0) or 0)
        peak = max((int(_num(r.m.get("failed"), 0) or 0) for r in bf if r.t >= per.start), default=0)
        if bf[-1].acked:                                  # the owner accepted this exact backup problem: no D_backups points (peak still shows it)
            res["backups"] = {"failed": 0, "stale": 0, "seen": True, "peak_failed": peak}
        else:
            res["backups"] = {"failed": failed, "stale": 1 if bf[-1].level == 1 and not failed else 0, "seen": True, "peak_failed": peak}
    res["gap"] = _gap_phrase(res) is not None
    return res


def _metric_series(recs: list[Rec] | None, task_name: str, key: str, lo: float, hi: float) -> list[tuple[float, float]]:
    out = []
    for r in recs or []:
        if r.task == task_name and lo <= r.t < hi:
            v = _num(r.m.get(key))
            if v is not None:
                out.append((r.t, v))
    return out


def _blank_pressure() -> dict:
    return {"samples": 0, "mem_full60_avg": None, "mem_full60_max": None, "io_some60_avg": None, "io_some60_max": None,
            "mem_available_min_gib": None, "oom_kills": None, "level_max": None, "pressure_state_seen": False,
            "spike_tracking_since": None}


def _gate_series(recs: list[Rec] | None, lo: float, hi: float) -> list[float]:
    """Per pressure_state record the level that matters: `gate_level` (memory and cpu only; the level the scheduler and the routine gate on),
    else `level` for a record written before gate_level existed. An io-only or gpu-only plateau (this host sits at io level 3 most nights)
    is not memory pressure and must not read like it in a report."""
    out = []
    for r in recs or []:
        if r.task == "pressure_state" and lo <= r.t < hi:
            v = _num(r.m.get("gate_level"))
            v = _num(r.m.get("level")) if v is None else v
            if v is not None:
                out.append(v)
    return out


def pressure_summary(inp: Inputs, per: Period) -> dict:
    """Host pressure facts from the 15-min memory_health and pressure_state records."""
    def agg(task_name: str, key: str, fn) -> float | None:
        v = [x for _, x in _metric_series(inp.recs, task_name, key, per.start, per.end)]
        return round(fn(v), 1) if v else None
    lv = _gate_series(inp.recs, per.start, per.end)
    oom = [x for _, x in _metric_series(inp.recs, "memory_health", "oom_kills_delta", per.start, per.end)]
    first_ps = min((r.t for r in inp.recs or [] if r.task == "pressure_state"), default=None)    # "since" = tracking began inside the period
    return {"samples": len(_metric_series(inp.recs, "memory_health", "psi_mem_full60", per.start, per.end)),
            "mem_full60_avg": agg("memory_health", "psi_mem_full60", statistics.fmean),
            "mem_full60_max": agg("memory_health", "psi_mem_full60", max),
            "io_some60_avg": agg("memory_health", "psi_io_some60", statistics.fmean),
            "io_some60_max": agg("memory_health", "psi_io_some60", max),
            "mem_available_min_gib": agg("memory_health", "mem_available_gib", min),
            "oom_kills": int(sum(oom)) if oom else None,
            "level_max": int(max(lv)) if lv else None,
            "pressure_state_seen": bool(lv) or "pressure_state" in _tasks_of(inp.status),
            "spike_tracking_since": first_ps if first_ps is not None and per.start + 2 * 3600 < first_ps < per.end else None}


# =========================================================================== analysis: actions
def _blank_actions() -> dict:
    return {"freed_bytes": 0, "count": 0, "by_task": [], "failed": 0, "refused": 0, "dry_run": 0, "alerts_sent": 0, "alerts_failed": 0,
            "audit_available": False, "would_free": [], "cleaner_modes": {"apply": [], "report": []}, "cleaner_runs": 0, "notable": []}


def summarize_actions(inp: Inputs, per: Period, tz: tzinfo) -> dict:
    status = inp.status or {}
    tasks = _tasks_of(status)
    audit = inp.audit
    maint = [r for r in (audit or []) if r["task"] not in NOT_MAINTENANCE]      # alerts are counted separately
    done = [r for r in maint if r["o"] == "done"]
    by: dict[str, dict] = {}
    for r in done:
        e = by.setdefault(r["task"], {"task": r["task"], "count": 0, "freed": 0})
        e["count"] += 1
        e["freed"] += r["bytes"]
    # status.reclaimed_log holds the same bytes per run: per task take the larger of the two (never double-count).
    logged = [x for x in _ls(status.get("reclaimed_log")) if isinstance(x, dict)
              and per.start <= (_num(x.get("t"), -1) or -1) < per.end]
    for name in {str(x.get("task")) for x in logged}:
        mine = [x for x in logged if str(x.get("task")) == name]
        e = by.setdefault(name, {"task": name, "count": 0, "freed": 0})
        e["freed"] = max(e["freed"], sum(int(max(_num(x.get("bytes"), 0) or 0, 0)) for x in mine))
        if audit is None:                            # no audit trail: the log is the only record of how many runs freed space
            e["count"] = len(mine)
    by_task = sorted(by.values(), key=lambda e: (-e["freed"], -e["count"], e["task"]))
    out = {"freed_bytes": sum(e["freed"] for e in by_task), "count": sum(e["count"] for e in by_task),
           "by_task": by_task, "failed": sum(1 for r in maint if r["o"] == "failed"),
           "refused": sum(1 for r in maint if r["o"] == "refused"),
           "dry_run": sum(r.get("n", 1) for r in maint if r["o"] == "dry-run"),
           "alerts_sent": sum(1 for r in audit or [] if r["task"] == "notify" and r["o"] == "sent"),
           "alerts_failed": sum(1 for r in audit or [] if r["task"] == "notify" and r["o"] == "failed"),
           "audit_available": audit is not None}
    # what the report-mode cleaners say they WOULD free, as of the latest status (not additive across days)
    would = []
    for n, e in sorted(tasks.items()):
        m = e.get("metrics") if isinstance(e.get("metrics"), dict) else {}
        if e.get("klass") == "C1" and m.get("mode") == "report" and (_num(m.get("selected"), 0) or 0) > 0:
            b = _parse_size(m.get("selected_h"))
            if b > 0:
                would.append({"task": n, "bytes": int(b), "human": human(b)})
    out["would_free"] = sorted(would, key=lambda x: -x["bytes"])
    # cleaner modes (to say "report-only" honestly)
    modes = {n: _tm(tasks, n).get("mode") for n, e in tasks.items() if e.get("klass") == "C1"}
    out["cleaner_modes"] = {"apply": sorted(n for n, m in modes.items() if m == "apply"),
                            "report": sorted(n for n, m in modes.items() if m == "report")}
    out["cleaner_runs"] = sum(1 for r in inp.recs or [] if per.start <= r.t < per.end
                              and (tasks.get(r.task) or {}).get("klass") == "C1")
    out["notable"] = _notable(inp, per, tz, done, tasks)
    return out


def _notable(inp: Inputs, per: Period, tz: tzinfo, done: list[dict], tasks: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple] = set()
    for r in inp.journal or []:
        if r.get("title"):
            rows.append({"ts": r["_t"], "title": _asc(r["title"], 120), "detail": _asc(r.get("detail") or "", 300),
                         "source": "journal"})
    for r in inp.changes or []:
        tk = str(r.get("task") or "?")
        seen.add((tk, _local_date(r["_t"], tz)))
        ver = r.get("verified")
        tail = "" if ver is None else (" (verified)" if ver else " (NOT verified after the change)")
        rows.append({"ts": r["_t"], "title": _asc(f"{ACTION_TITLES.get(tk, tk)}: {r.get('kind') or 'change'}", 120),
                     "detail": _asc(str(r.get("detail") or r.get("outcome") or "") + tail, 300), "source": "change"})
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in done:
        groups[(r["task"], _local_date(r["t"], tz))].append(r)
    for (tk, d), rs in groups.items():
        if (tk, d) in seen:
            continue
        b = sum(x["bytes"] for x in rs)
        first = rs[0]
        rows.append({"ts": rs[-1]["t"], "title": ACTION_TITLES.get(tk, tk.replace("_", " ").capitalize()),
                     "detail": _asc(f"{_plural(len(rs), 'action')}" + (f", {human(b)} freed" if b else "")
                                    + (f"; first: {first['action']} {first['target']}".rstrip() if first["target"] or first["action"] else ""), 300),
                     "source": "audit", "bytes": b})
    for r in inp.audit or []:
        if r["o"] == "failed" and r["task"] not in NOT_MAINTENANCE:
            rows.append({"ts": r["t"], "title": _asc(f"FAILED: {r['task']} {r['action']}", 120),
                         "detail": _asc(f"{r['target']} {r['raw']}", 300), "source": "audit"})
    rows.sort(key=lambda x: (-x["ts"], x["title"]))
    return rows[:15]


# =========================================================================== analysis: incidents
def _sev(v: Any) -> int:
    """sev1/sev2/sev3, 1..3, 'crit'/'warn' -> 1..3 (unknown counts as 3, the mildest)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return min(max(int(v), 1), 3) if math.isfinite(v) else 3
    s = str(v or "").lower()
    m = re.search(r"[123]", s)
    if m:
        return int(m.group(0))
    return 2 if "crit" in s else 3


def _first_t(d: dict, keys: tuple, tz: tzinfo) -> float | None:
    return next((x for x in (parse_ts(d.get(k), tz) for k in keys) if x is not None), None)


def _blank_incidents() -> dict:
    return {"opened": 0, "resolved": 0, "open_now": 0, "mttr_s": None, "list": [], "available": False}


def _is_acked_inc(i: dict) -> bool:
    """An open incident the owner acknowledged (incidents.py: status "acknowledged" and an `ack` object)."""
    return i.get("status") == "acknowledged" or isinstance(i.get("ack"), dict)


def summarize_incidents(inp: Inputs, per: Period, tz: tzinfo) -> tuple[dict, list[int]]:
    """(section, severities of incidents still open at the period end). Tolerates both key spellings of the S3 snapshot."""
    snap = inp.incidents
    if not isinstance(snap, dict):
        return _blank_incidents(), []
    allx: dict[str, dict] = {}
    for src in ("open", "recent"):
        for i in _ls(snap.get(src)):
            if isinstance(i, dict):
                allx[str(i.get("id") or f"{src}{len(allx)}")] = {**i, "_src": src}
    rows, open_at_end, mttrs = [], [], []
    opened = resolved = 0
    kids: dict[str, int] = defaultdict(int)          # correlated failures are grouped under one parent: count the group once
    for iid, i in allx.items():
        if isinstance(i.get("parent"), str) and i["parent"] in allx and i["parent"] != iid:
            kids[i["parent"]] += 1
    for iid, i in allx.items():
        if isinstance(i.get("parent"), str) and i["parent"] in allx and i["parent"] != iid:
            continue
        t0 = _first_t(i, ("since", "detected_at", "opened_at", "start", "t"), tz)
        t1 = _first_t(i, ("resolved_at", "ended_at", "end"), tz)
        if t1 is None and i["_src"] == "recent" and t0 is not None and _num(i.get("duration_s")) is not None:
            t1 = t0 + float(i["duration_s"])
        if t0 is None:
            continue
        was_open_at_end = t0 < per.end and (t1 is None or t1 >= per.end)
        touched = per.start <= t0 < per.end or (t1 is not None and per.start <= t1 < per.end) or was_open_at_end
        if per.start <= t0 < per.end:
            opened += 1
        if t1 is not None and per.start <= t1 < per.end:
            resolved += 1
            m = _num(i.get("mttr_s"))
            mttrs.append(m if m is not None else t1 - t0)
        if was_open_at_end and not _is_acked_inc(i):          # an acknowledged incident is listed below but costs no D_incidents points
            open_at_end.append(_sev(i.get("severity")))
        if touched:
            dur = (t1 if t1 is not None and t1 < per.end else per.end) - t0         # as of the end of the period, not of the snapshot
            rows.append({"id": _asc(iid, 40), "title": _asc(i.get("title") or i.get("task") or iid, 100),
                         "severity": f"sev{_sev(i.get('severity'))}", "duration_s": int(max(dur, 0)),
                         "resolved": t1 is not None and t1 < per.end, "related": kids.get(iid, 0), "t": t0})
            if _is_acked_inc(i) and was_open_at_end:
                rows[-1]["acknowledged"] = True
    rows.sort(key=lambda r: (r["resolved"], r["severity"], -r["t"]))
    for r in rows:
        del r["t"]
    live = [i for i in _ls(snap.get("open")) if isinstance(i, dict)]
    sec = {"opened": opened, "resolved": resolved, "open_now": len([1 for i in live if not _is_acked_inc(i)]),
           "mttr_s": int(statistics.fmean(mttrs)) if mttrs else None, "list": rows[:12], "available": True}
    if any(_is_acked_inc(i) for i in live):
        sec["acknowledged_now"] = len([1 for i in live if _is_acked_inc(i)])        # only when there is one: the shape is otherwise unchanged
    return sec, open_at_end


# =========================================================================== analysis: acknowledged issues
def acknowledged_section(inp: Inputs, now: float, tz: tzinfo) -> dict:
    """Every active acknowledgement (public/acks.json as of the report), soonest expiry first: what, until when, how often it held back an
    alert, and whether the error is failing right now. Not an input to the score, and never hidden from the reader."""
    doc = inp.acks
    if not isinstance(doc, dict):
        return {"available": False, "count": 0, "failing": 0, "list": [], "expired_30d": 0}
    rows = []
    for a in _ls(doc.get("acks")):
        until = _num(a.get("until")) if isinstance(a, dict) else None
        if until is None:
            continue
        rows.append({"id": _asc(a.get("id"), 16), "task": _asc(a.get("task"), 48), "title": _asc(a.get("title") or a.get("task"), 100),
                     "severity": "crit" if a.get("severity") == "crit" else "warn", "until": until, "until_label": f"{_fmt_day(_local_date(until, tz))} {_local_date(until, tz).year}",
                     "days_left": max(int(math.ceil((until - now) / 86400)), 0), "by": _asc(a.get("by"), 8),
                     "suppressed": int(_num(a.get("suppressed"), 0) or 0), "active": a.get("active") is True,
                     "summary": _asc(a.get("summary"), 160), "note": _asc(a.get("note"), 160)})
    rows.sort(key=lambda r: (r["until"], r["id"]))
    st = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
    return {"available": True, "count": len(rows), "failing": sum(1 for r in rows if r["active"]), "list": rows[:12],
            "expired_30d": int(_num(st.get("expired_30d"), 0) or 0)}


# =========================================================================== analysis: spikes
def _contrib(c: Any) -> str:
    if isinstance(c, str):
        return _asc(c, 60)
    if isinstance(c, dict):
        name = _asc(c.get("name") or c.get("container") or "?", 40)
        bits = [str(c["class"])] if c.get("class") else []
        g = _num(c.get("anon_gib"))
        if g is not None:
            bits.append(f"{g:.1f} GiB")
        cpu = _num(c.get("cpu_pct"))
        if cpu is not None and cpu >= 1:
            bits.append(f"{cpu:.0f}% cpu")
        return f"{name} ({', '.join(bits)})" if bits else name
    return "?"


RESOURCE = {"mem": "memory", "io": "disk I/O", "cpu": "CPU", "gpu": "GPU memory"}


def _spike_ledger(rows: list[dict]) -> list[dict]:
    """spikes.jsonl holds one line per runner tick while a spike is open plus one `closed` line (same `id`, `t` = start):
    fold them to one record per spike, the closed one winning (a record without `id` is its own spike)."""
    by: dict[Any, dict] = {}
    for i, r in enumerate(rows):
        key = r.get("id") if isinstance(r.get("id"), (str, int)) else ("anon", i)
        old = by.get(key)
        if old is None or str(r.get("state")) == "closed" or str(old.get("state")) != "closed":
            by[key] = r
    return list(by.values())


def _harm_action(a: dict) -> bool:
    """An executed action that can hurt a workload: a restart/stop/kill. Freeing files, images or idle models is not harm."""
    return a.get("rung") in ("L4", "L5") or bool(HARM_RX.search(a["action"]))


def _blank_spikes() -> dict:
    return {"count": 0, "worst_level": None, "list": [], "handled_without_harm": None, "data": "missing", "oom_kills": None}


def summarize_spikes(inp: Inputs, per: Period, tz: tzinfo, pressure: dict) -> dict:
    """Spike events of the period and how they were handled. `handled_without_harm` is true only on evidence:
    spike data exists, nothing killed/restarted/stopped (the ledger's own verdict AND the audit trail agree), no OOM kill.
    Without any way to verify (no audit trail, no pressure log, no verdict in the record) it is None, never True."""
    data = inp.spikes is not None or pressure["pressure_state_seen"]
    events = []
    for r in _spike_ledger(inp.spikes or []):
        t0 = r["_t"]
        if not per.start <= t0 < per.end:
            continue
        end = _first_t(r, ("end", "t_end", "ended_at", "resolved_at"), tz)
        ongoing = str(r.get("state")) == "open" and end is None
        dur = next((_num(r[k]) for k in ("duration_s", "dur_s", "duration") if _num(r.get(k)) is not None), None)
        if dur is None and end is not None:
            dur = end - t0
        level = next((int(_num(r[k], 0) or 0) for k in ("peak_level", "peak", "level", "max_level") if _num(r.get(k)) is not None), None)
        gate = _num(r.get("gate_peak"))                      # memory/cpu peak: 0 = only io or gpu was under pressure (spike manager S5)
        contrib = r.get("contributors") or r.get("top_contributors") or r.get("top") or []
        psi = r.get("psi") if isinstance(r.get("psi"), dict) else {}
        dims = r.get("dims") if isinstance(r.get("dims"), dict) else {}
        lead = max(((_num(v, 0) or 0, k) for k, v in dims.items() if k in RESOURCE), default=(0, None))
        events.append({"t": t0, "level": level, "gate": None if gate is None else int(gate), "duration_s": None if dur is None else int(max(dur, 0)), "ongoing": ongoing,
                       "contributors": [_contrib(c) for c in (contrib if isinstance(contrib, list) else [])[:3]],
                       "resource": RESOURCE.get(lead[1]) if lead[0] > 0 else None,
                       "psi": _num(psi.get("mem_full60", r.get("psi_mem") or r.get("mem_full60") or r.get("peak_psi"))),
                       "ledger_verdict": r.get("nothing_killed") if isinstance(r.get("nothing_killed"), bool) else None,
                       "ledger_oom": int(_num(r.get("oom_kills"), 0) or 0),
                       "ledger_text": r.get("outcome") if isinstance(r.get("outcome"), str) and r.get("outcome") != "in progress" else None})
    actions = [a for a in inp.audit or [] if a["o"] == "done" and a["task"] not in NOT_MAINTENANCE]
    # A restart/stop/kill that FAILED is not "nothing happened": a restart is stop-then-start, and a CLI timeout usually means the
    # daemon went on stopping the container. Such attempts are evidence of possible harm (result unknown), never of none.
    tried = [a for a in inp.audit or [] if a["o"] == "failed" and a["task"] not in NOT_MAINTENANCE and _harm_action(a)]
    for p in inp.pressure_log or []:                # response-ladder rows; only executed ones count as actions
        rung, oc = str(p.get("rung") or ""), _outcome_class(p.get("outcome"))
        row = {"t": p["_t"], "task": "pressure_response", "action": str(p.get("action") or ""), "rung": rung,
               "target": str(p.get("target") or ""), "o": oc}
        if oc == "done" and rung in ("L2", "L3", "L4", "L5"):
            actions.append(row)
        elif oc == "failed" and _harm_action(row):
            tried.append(row)
    harm_any = False
    for e in events:
        lo, hi = e["t"] - 60, e["t"] + (e["duration_s"] or 0) + 600
        near = [a for a in actions if lo <= a["t"] <= hi]
        harm = [a for a in near if _harm_action(a)]
        soft = [a for a in near if a not in harm and (a.get("rung") in ("L2", "L3") or SOFT_RX.search(a["action"]))]
        unsure = [a for a in tried if lo <= a["t"] <= hi]
        e["attempted"] = False
        if harm:
            tgt = ", ".join(sorted({a["target"] for a in harm if a["target"]})[:2]) or "a workload"
            e["outcome"], e["harm"] = f"{harm[0]['action']} ran on {tgt}", True
        elif e["ledger_verdict"] is False or e["ledger_oom"]:
            e["outcome"] = e["ledger_text"] or "a workload was restarted, stopped or OOM-killed"
            e["harm"] = True
        elif unsure:
            a = unsure[0]
            tgt = ", ".join(sorted({x["target"] for x in unsure if x["target"]})[:2]) or "a workload"
            verb = {"L4": "restart", "L5": "stop"}.get(a.get("rung")) or a["action"] or "stop/restart"
            e["outcome"], e["harm"], e["attempted"] = f"{verb} attempted on {tgt}, result unknown (the command failed)", None, True
        elif inp.audit is None:                      # the audit trail is the complete record of what this tool did: without it nothing is certified
            e["outcome"] = f"unverified: {e['ledger_text']} (no audit trail to check it against)" if e["ledger_text"] else "outcome unknown (no audit trail)"
            e["harm"] = None
        elif e["ledger_text"]:
            e["outcome"], e["harm"] = e["ledger_text"], False
        elif soft:
            e["outcome"], e["harm"] = "idle resources reclaimed or batch work throttled; nothing killed or restarted", False
        else:
            e["outcome"], e["harm"] = "ended without intervention; nothing killed or restarted", False
        if e["ongoing"]:
            e["outcome"] = "still in progress at the end of the period" + (f"; {e['outcome']}" if e["harm"] or e["attempted"] else "")
        harm_any = harm_any or bool(e["harm"])
    oom = pressure["oom_kills"] or 0
    if oom:
        harm_any = True
    judged = data and inp.audit is not None and all(e["harm"] is not None for e in events)
    handled = (not harm_any) if judged else (False if harm_any else None)
    lvls = [e["level"] if e["gate"] is None else e["gate"] for e in events if e["level"] is not None]      # the worst that MATTERED: io/gpu-only = 0
    events.sort(key=lambda e: -e["t"])
    return {"count": len(events), "worst_level": max(lvls) if lvls else (pressure["level_max"] or 0 if data else None),
            "list": [{"t": e["t"], "level": e["level"], "gate_level": e["gate"], "duration_s": e["duration_s"], "contributors": e["contributors"],
                      "outcome": _asc(e["outcome"], 160), "psi_mem": e["psi"], "resource": e["resource"], "ongoing": e["ongoing"],
                      "harm": e["harm"], "attempted": e["attempted"]}
                     for e in events[:20]],
            "handled_without_harm": handled, "data": "ok" if data else "missing", "oom_kills": pressure["oom_kills"]}


# =========================================================================== analysis: capacity
def _theil_sen(pts: list[tuple[float, float]]) -> float | None:
    """Median pairwise slope (units/s) of hourly medians; None with < 6 hours of points or < 6 h span (fallback copy of
    the disk_forecast helper)."""
    by_h: dict[int, list[float]] = defaultdict(list)
    for t, v in pts:
        by_h[int(t // 3600)].append(v)
    hp = [(h * 3600.0 + 1800, statistics.median(v)) for h, v in sorted(by_h.items())]
    if len(hp) < 6 or hp[-1][0] - hp[0][0] < 6 * 3600:
        return None
    if len(hp) > 200:                               # bound the O(n^2) pair count (a week of hours is 168)
        hp = hp[:: math.ceil(len(hp) / 200)]
    sl = [(b[1] - a[1]) / (b[0] - a[0]) for i, a in enumerate(hp) for b in hp[i + 1:] if b[0] > a[0]]
    return statistics.median(sl) if sl else None


def _fill_rate(samples: list[tuple[float, float]], free_now: float, end: float, capacity: float | None) -> tuple[float | None, float | None]:
    """(bytes/s the disk is filling at, days until full) with the SAME method as the disk_forecast check: hourly medians,
    one-off jumps (a bulk copy, a big delete) removed, and the rate is the slower of the whole-window and the last-48-h
    trends, so consumption that already stopped is not forecast. The rate is negative when free space is growing and
    None when there is too little history (< 6 samples hours spanning >= 6 h). Falls back to a plain robust slope (and no
    forecast date) if the helpers of the other module are not importable."""
    try:
        from .tasks import checks_basic as cb
        pts = cb._destep(cb._hourly(sorted(p for p in samples if p[0] >= end - LOOKBACK_S)), capacity, 3.0)
        s_long = cb._robust_slope(pts)
        s_recent = cb._robust_slope([p for p in pts if p[0] >= end - 48 * 3600])
        days = cb._days_until_full(samples, free_now, end, LOOKBACK_S, capacity=capacity)
    except Exception:                               # noqa: BLE001 - private helpers of another stream: degrade, never fail
        s_long = _theil_sen(samples)
        return (None if s_long is None else -s_long), None
    if s_long is None or s_recent is None:
        return None, None
    return min(-s_long, -s_recent), days


def _median_window(vals: list[tuple[float, float]], lo: float, hi: float) -> float | None:
    v = [x for t, x in vals if lo <= t < hi]
    return statistics.median(v) if len(v) >= 3 else None


def _blank_capacity() -> dict:
    return {"mounts": [], "memory_baseline_gib": {"metric": "container_anon", "window_h": 0, "start": None, "end": None, "drift": None},
            "memory_growers": [], "recommendations": []}


def capacity_section(inp: Inputs, per: Period) -> dict:
    tasks = _tasks_of(inp.status)
    rows_status = {str(r.get("mount")): r for r in _ls(_tm(tasks, "disk_forecast").get("mounts"))
                   if isinstance(r, dict) and r.get("mount")}
    mounts = []
    for mount, series in inp.disk.items():
        pts = [(t, f) for t, f in series if t < per.end and t >= per.end - LOOKBACK_S - 3600]
        if not pts:
            continue
        free = pts[-1][1]
        sr = rows_status.get(mount, {})
        pct = _num(sr.get("used_pct"))
        cap = free / (1 - pct / 100.0) if pct is not None and 0 <= pct < 99.5 and free > 0 else None
        rate, days = _fill_rate(pts, free, per.end, cap)
        if days is None and rate is not None and rate > 0 and _num(sr.get("days")) is not None:
            days = _num(sr.get("days"))              # the check's own forecast, when this module could not compute one
        note = ("collecting data (needs 6 h of history)" if rate is None else
                "free space is stable or growing" if rate <= 0 else
                "filling, no full-disk date within 10 years" if days is None else "filling")
        mounts.append({"mount": mount, "free_b": int(free), "free_h": human(free), "used_pct": pct,
                       "days_to_full": None if days is None else round(days, 1),
                       "trend_gib_per_day": None if rate is None else round(rate * 86400 / GIB, 2) + 0.0,       # + 0.0: no "-0.0"
                       "level": sr.get("level") if sr.get("level") in ("ok", "warn", "crit") else None,
                       "note": note, "samples": len(pts)})
    mounts.sort(key=lambda m: (m["days_to_full"] is None, m["days_to_full"] or 0,
                               m["used_pct"] is None, -(m["used_pct"] or 0), m["mount"]))
    # memory baseline: container anonymous memory (what leaks), start vs end of the period
    anon = _metric_series(inp.recs, "spike_sampler", "anon_total_gib", per.start, per.end)
    edge = (6 if per.kind == "daily" else 24) * 3600
    a, b = _median_window(anon, per.start, per.start + edge), _median_window(anon, per.end - edge, per.end)
    base = {"metric": "container_anon", "window_h": edge // 3600, "start": None if a is None else round(a, 1),
            "end": None if b is None else round(b, 1), "drift": None if a is None or b is None else round(b - a, 1)}
    section = {"mounts": mounts[:12], "memory_baseline_gib": base, "memory_growers": _growers(inp), "recommendations": []}
    return section


def _growers(inp: Inputs) -> list[dict]:
    """Containers whose median anonymous memory grew most between the first and last day of the period (weekly)."""
    first, last = inp.samples.get("first") or [], inp.samples.get("last") or []
    if len(first) < 3 or len(last) < 3:
        return []
    def med(samples: list[dict], name: str) -> float | None:
        v = [s["c"][name]["anon"] for s in samples if isinstance(s["c"].get(name), dict) and _num(s["c"][name].get("anon")) is not None]
        return statistics.median(v) if len(v) >= 3 else None
    out = []
    for name in {n for s in last for n in s["c"]}:
        a, b = med(first, name), med(last, name)
        if a is not None and b is not None and (b - a) >= 0.25 * GIB:
            out.append({"name": _asc(name, 40), "start_gib": round(a / GIB, 2), "end_gib": round(b / GIB, 2),
                        "delta_gib": round((b - a) / GIB, 2)})
    return sorted(out, key=lambda x: -x["delta_gib"])[:3]


# =========================================================================== analysis: temperature, slo, upcoming
def _blank_temperature(per: Period) -> dict:
    h0, h1 = int(per.start // 3600), int(math.ceil(per.end / 3600)) - 1
    return {"cpu_avg": None, "cpu_max": None, "gpu_avg": None, "gpu_max": None, "ram_avg": None, "ram_max": None, "nvme_avg": None,
            "nvme_max": None, "hours_covered": 0, "hours_expected": h1 - h0 + 1, "fan_note": "no temperature data in this period"}


def _thin(temp: dict) -> bool:
    """True when the ring covers under 90% of the period's hours. The 168-hour ring cannot hold the first hours of a week that
    ended this morning (the weekly report runs after midnight), so a few missing hours are normal and not worth a caveat."""
    return temp["hours_covered"] < 0.9 * temp["hours_expected"]


def temperature_section(ring: dict | None, per: Period) -> dict:
    """Averages and maxima over the period's hours from the raw 7-day ring (SPEC2 layout); None where there is no data."""
    keys = ("cpu_temp", "gpu_temp", "ram_temp", "nvme_temp", "cpu_fan_rpm", "case_fan_rpm", "gpu_fan_pct")
    h0, h1 = int(per.start // 3600), int(math.ceil(per.end / 3600)) - 1
    s: dict[str, float] = defaultdict(float)
    c: dict[str, float] = defaultdict(float)
    mx: dict[str, float] = {}
    hours = 0
    for slot in _ls(_dc(ring).get("hours")):
        if not isinstance(slot, dict) or not isinstance(slot.get("sum"), dict) or not isinstance(slot.get("cnt"), dict):
            continue
        h = _num(slot.get("h"))
        if h is None or not h0 <= h <= h1 or not (_num(slot.get("n"), 0) or 0) > 0:
            continue
        hours += 1
        for k in keys:
            n = _num(slot["cnt"].get(k), 0) or 0
            if n > 0:
                s[k] += _num(slot["sum"].get(k), 0) or 0
                c[k] += n
                m = _num(_dc(slot.get("max")).get(k))
                if m is not None:
                    mx[k] = max(mx.get(k, m), m)
    avg = lambda k: round(s[k] / c[k], 1) if c.get(k) else None          # noqa: E731
    out = {"cpu_avg": avg("cpu_temp"), "cpu_max": mx.get("cpu_temp"), "gpu_avg": avg("gpu_temp"), "gpu_max": mx.get("gpu_temp"),
           "ram_avg": avg("ram_temp"), "ram_max": mx.get("ram_temp"), "nvme_avg": avg("nvme_temp"), "nvme_max": mx.get("nvme_temp"),
           "hours_covered": hours, "hours_expected": h1 - h0 + 1}
    bits = []
    if avg("cpu_fan_rpm") is not None:
        bits.append(f"CPU fan {avg('cpu_fan_rpm'):.0f} rpm avg (max {mx.get('cpu_fan_rpm', 0):.0f})")
    if avg("case_fan_rpm") is not None:
        bits.append(f"case fans {avg('case_fan_rpm'):.0f} rpm avg")
    if "gpu_fan_pct" in mx:
        bits.append(f"GPU fan up to {mx['gpu_fan_pct']:.0f}%")
    hot = [f"{n} peaked at {v:.0f} C" for n, v, lim in (("CPU", out["cpu_max"], 85), ("GPU", out["gpu_max"], 85),
                                                         ("NVMe", out["nvme_max"], 70)) if v is not None and v >= lim]
    out["fan_note"] = _asc("; ".join(bits + hot) or ("no fan data" if hours else "no temperature data in this period"), 200)
    return out


def slo_section(inp: Inputs) -> list[dict]:
    rows = []
    for o in _ls(_dc(inp.slo).get("objectives")):
        if isinstance(o, dict) and o.get("name"):
            rows.append({"name": _asc(o["name"], 60), "availability_pct": _num(o.get("availability_pct")),
                         "status": _asc(o.get("status") or "unknown", 20)})
    return rows[:10]


def upcoming_section(inp: Inputs, now: float, tz: tzinfo) -> list[dict]:
    """Next scheduled items: routine.json calendar first, else schedule.json timers; plus a pending C2 plan."""
    out: list[dict] = []
    today = _local_date(now, tz)
    for day in _ls(_dc(inp.routine).get("calendar")):
        try:
            d = date.fromisoformat(str(day.get("date")))
        except (ValueError, AttributeError):
            continue
        if d < today:
            continue
        for it in _ls(day.get("items")):
            if isinstance(it, dict) and it.get("title"):
                out.append({"when": f"{_fmt_day(d)} {it.get('time') or ''}".strip(), "what": _asc(it["title"], 100), "_k": (d, str(it.get("time") or ""))})
    if not out:
        for tm in _ls(_dc(inp.schedule).get("timers")):
            nxt = _num(tm.get("next")) if isinstance(tm, dict) else None
            if nxt is not None and nxt >= now:
                out.append({"when": f"{_fmt_t(nxt, tz)}", "what": _asc(tm.get("title") or tm.get("unit") or "?", 100),
                            "_k": (_local_date(nxt, tz), f"{datetime.fromtimestamp(nxt, tz):%H:%M}")})
    out.sort(key=lambda x: x["_k"])
    out = [{"when": x["when"], "what": x["what"]} for x in out][:10]
    for n, e in sorted(_tasks_of(inp.status).items()):
        plan = e.get("plan")
        if e.get("klass") == "C2" and isinstance(plan, dict) and _ls(plan.get("items")) and e.get("plan_hash"):
            out.append({"when": "needs your approval",
                        "what": _asc(f"{n}: {len(plan['items'])} cleanup candidate(s), {human(_num(plan.get('total_bytes'), 0) or 0)}; "
                                     f"review with: homelab-maint plan {n}", 160)})
    return out[:12]


# =========================================================================== recommendations
def recommendations(inp: Inputs, per: Period, cap: dict, actions: dict, chk: dict, pressure: dict, temp: dict) -> list[str]:
    tasks = _tasks_of(inp.status)
    rec: list[str] = []
    dm = _tm(tasks, "docker_df")
    for m in cap["mounts"]:
        pct = m["used_pct"]
        if m["level"] in ("warn", "crit") or (m["days_to_full"] is not None and m["days_to_full"] <= 30) or (pct is not None and pct >= 85):
            name = "Root disk (/)" if m["mount"] == "/" else m["mount"]
            s = f"{name}: {m['free_h']} free" + (f" ({pct:.0f}% used)" if pct is not None else "")
            if m["trend_gib_per_day"] and m["trend_gib_per_day"] > 0:
                s += f", growing {m['trend_gib_per_day']:.1f} GiB/day"
            if m["days_to_full"] is not None:
                s += f", about {m['days_to_full']:.0f} days to full"
            if m["mount"] == "/" and (dm.get("safe_reclaim_h") or dm.get("build_cache_h")):
                s += f". Docker holds {dm.get('build_cache_h', '?')} build cache and {dm.get('images_reclaim_h', '?')} reclaimable images"
                if "docker_cache" in actions["cleaner_modes"]["report"]:
                    s += " (docker_cache is report-only; set mode = apply to prune)"
            rec.append(_asc(s + ".", 260))
    gw = _tm(tasks, "growth_watch")
    if (_num(gw.get("n_over"), 0) or 0) > 0:
        rec.append(_asc(f"Runaway growth: {gw.get('worst_path') or 'a watched path'} is growing {gw.get('worst_gib_day')} GiB/day; "
                        "find out what writes there before the disk fills.", 220))
    b = cap["memory_baseline_gib"]
    if b["drift"] is not None and b["drift"] >= 2 and b["start"] and b["drift"] >= 0.2 * b["start"]:
        gr = ", ".join(f"{g['name']} +{g['delta_gib']:.1f} GiB" for g in cap["memory_growers"])
        rec.append(_asc(f"Container memory baseline rose from {b['start']} to {b['end']} GiB (+{b['drift']})"
                        + (f"; biggest growers: {gr}" if gr else "") + ". Check for a leak; the caps task can bound a container.", 260))
    if actions["would_free"] and "docker_cache" not in " ".join(rec):
        tot = sum(w["bytes"] for w in actions["would_free"])
        if tot >= GIB:
            rec.append(_asc(f"Cleanup is report-only: {human(tot)} would be freed ({', '.join(f'{w['task']} {w['human']}' for w in actions['would_free'][:3])}). "
                            "Review `homelab-maint status`, then set mode = \"apply\" per task in maint.toml.", 260))
    for n, e in sorted(tasks.items()):
        plan = e.get("plan")
        if e.get("klass") == "C2" and isinstance(plan, dict) and _ls(plan.get("items")) and e.get("plan_hash"):
            rec.append(_asc(f"{_plural(len(plan['items']), 'cleanup candidate')} ({human(_num(plan.get('total_bytes'), 0) or 0)}) await approval: "
                            f"homelab-maint plan {n}.", 200))
    if actions["alerts_failed"]:
        rec.append(_asc(f"{_plural(actions['alerts_failed'], 'alert')} failed to send: fix the alert bridge or you will not be paged.", 200))
    if (pressure["io_some60_avg"] or 0) >= 30:
        rec.append(_asc(f"Disk I/O pressure averaged {pressure['io_some60_avg']:.0f}% (peak {pressure['io_some60_max'] or 0:.0f}%): something keeps the "
                        "disks busy; start with `iotop -oPa` and `docker stats`.", 260))
    for label, key, lim in (("CPU", "cpu_max", 85), ("GPU", "gpu_max", 85), ("NVMe", "nvme_max", 70)):
        if temp[key] is not None and temp[key] >= lim:
            rec.append(f"{label} peaked at {temp[key]:.0f} C (limit {lim} C): check airflow and the fan curve.")
    sm = _tm(tasks, "smart_trend")
    bad = [d.get("dev") for d in _ls(sm.get("devices")) if isinstance(d, dict) and (d.get("level") not in (None, "ok") or (_num(d.get("pending"), 0) or 0) > 0)]
    if bad:
        rec.append(_asc(f"SMART shows trouble on {', '.join(map(str, bad[:3]))}: verify backups cover that disk and plan a replacement.", 200))
    if chk["stale"]:
        rec.append(_asc(f"{_plural(len(chk['stale']), 'check')} stopped reporting ({', '.join(chk['stale'][:3])}): run `homelab-maint doctor`.", 200))
    return rec[:8]


# =========================================================================== highlights, headline, digest
@dataclass
class Facts:
    """Everything the prose generators read, computed once by build_report."""
    inp: Inputs
    per: Period
    tz: tzinfo
    tasks: dict
    chk: dict
    health: dict
    actions: dict
    incidents: dict
    spikes: dict
    capacity: dict
    temp: dict
    pressure: dict
    slo: list
    episodes: list[dict]
    acknowledged: dict = field(default_factory=dict)


def _mount_name(m: str) -> str:
    return "Root disk (/)" if m == "/" else m


def episode_lines(chk: dict, tasks: dict, per: Period, tz: tzinfo) -> list[dict]:
    """One entry per problem check, worst first (a problem still open at the end of the period before a fixed one of the same
    severity): how long, when, and whether it is still going."""
    out = []
    eff = max(per.end - chk["eff_start"], 1)
    still = chk.get("open") or {}
    for n, eps in chk["episodes"].items():
        tot = sum(e["end"] - e["start"] for e in eps)
        lvl = max(e["level"] for e in eps)
        word = "critical" if lvl >= 2 else "warning"
        title = _title(tasks, n)
        longest = max(eps, key=lambda e: e["end"] - e["start"])
        if len(eps) == 1 and tot >= 0.95 * eff:
            s = f"{title}: {word} for the whole {per.span} ({_dur(tot)})"
        elif len(eps) == 1:
            e = eps[0]
            s = f"{title}: {word} for {_dur(tot)}" + (f", from {_fmt_t(e['start'], tz)}" if not e["before"] else ", carried over from before the period")
            s += "" if e["open"] else f", ok again at {_fmt_t(e['end'], tz)}"
        else:
            s = (f"{title}: {word} in {len(eps)} episodes, {_dur(tot)} in total "
                 f"(longest {_dur(longest['end'] - longest['start'])}, from {_fmt_t(longest['start'], tz)})")
        if n in still:
            s += f"; still open at the end of the {per.span}"
        cur = tasks.get(n) or {}
        if any(e["open"] for e in eps) and cur.get("status") in ("warn", "crit", "error"):
            summ = _check_text(n, cur.get("summary"), 110, cur)         # redacted before it is cut; opaque checks are not quoted
            s += f"; at the latest check: {summ}" if summ else "; still failing at the latest check"
        elif any(e["open"] for e in eps):
            s += "; recovered since the period ended"
        out.append({"task": n, "prio": 0.0 if lvl >= 2 else 1.0, "text": _asc(s + ".", 320), "total_s": tot, "open": n in still,
                    "short": f"{title} {word} {_dur(tot)}" + (", still open" if n in still else "")})
    out.sort(key=lambda e: (e["prio"], not e["open"], -e["total_s"], e["task"]))
    return out[:4]


LEVEL_NAMES = ("normal", "annotate", "reclaim", "slow batch", "restart", "emergency")      # pressure.py's ladder, rung 0-5


def _spike_line(sp: dict, tz: tzinfo) -> str:
    """'2 load spikes; worst: level 3 (slow batch) at Fri 14:10 for 9 min, memory pressure, mostly from tunarr-host-net. Nothing ...'"""
    ev = sp["list"]
    worst = max(ev, key=lambda e: (e["level"] or 0, e["duration_s"] or 0, e["t"]))
    lv = worst["level"]
    names = [c.split(" (")[0] for e in ev for c in e["contributors"][:1]]
    top = max(sorted(set(names)), key=names.count) if names else None
    bits = [f"{'worst' if sp['count'] > 1 else 'peak'}: level {lv}" + (f" ({LEVEL_NAMES[lv]})" if lv is not None and 0 <= lv < len(LEVEL_NAMES) else "")
            + f" at {_fmt_t(worst['t'], tz)}"
            + (f" for {_dur(worst['duration_s'])}" if worst["duration_s"] is not None else "") + (", still going" if worst["ongoing"] else "")]
    if worst.get("resource"):
        bits.append(f"{worst['resource']} pressure")
    if top:
        bits.append(f"mostly from {top}")
    hh = sp["handled_without_harm"]
    hurt = next((e["outcome"].rstrip(".") for e in ev if e.get("harm")), None)
    tried = next((e["outcome"].rstrip(".") for e in ev if e.get("attempted")), None)
    tail = ("Nothing was killed or restarted." if hh else
            f"A workload was affected: {hurt}." if hh is False and hurt else "A workload was affected; see the spike outcomes." if hh is False
            else f"A restart or stop was attempted and failed ({tried}); check that workload." if tried
            else "The outcome could not be verified (no audit trail).")
    return f"{_plural(sp['count'], 'load spike')}; {', '.join(bits)}. {tail}"


def _pick(H: list[tuple[float, str, str]]) -> list[tuple[float, str, str]]:
    """The <= MAX_HIGHLIGHTS lines to show. A weekly note always says something about the overall picture, spikes and
    maintenance (and about the acknowledged issues, when there are any: the owner asked to see them), even in a week crowded with problems, so those categories are reserved a slot before the rest compete
    by priority (lowest number = most important). The result is ordered by priority."""
    H = sorted(H, key=lambda x: (x[0], x[1], x[2]))
    reserved = []
    for cat in ("overall", "incident", "spike", "maint", "ack"):                  # (ack: only when something is acknowledged)
        first = next((x for x in H if x[1] == cat), None)
        if first:
            reserved.append(first)
    rest = [x for x in H if x not in reserved]
    chosen = reserved + rest[: max(MAX_HIGHLIGHTS - len(reserved), 0)]
    return sorted(chosen, key=lambda x: (x[0], x[1], x[2]))


def build_highlights(f: Facts) -> list[str]:
    """<= 8 plain sentences, most important first. Every claim is tied to data; a missing input is said, never skipped."""
    H: list[tuple[float, str, str]] = []
    add = lambda prio, cat, text: H.append((prio, cat, text))                                       # noqa: E731
    per, tz, chk, a, inc, sp, cap, tmp, pr = f.per, f.tz, f.chk, f.actions, f.incidents, f.spikes, f.capacity, f.temp, f.pressure
    span = per.span
    # --- overall -------------------------------------------------------------------------------------------------
    gap = _gap_phrase(chk)
    if not chk["has_data"] and not chk["gap"]:
        add(-1, "overall", f"No health-check results were recorded for the {span}: homelab-maint was not running or its history is missing.")
    elif f.health["score"] is None:
        add(-1, "overall", f"Only about {_dur(chk['covered'] * SLOT_S)} of check history exists for the {span} ({chk['observed_pct']:.0f}% of it): "
                           "too little to give a score, still collecting data.")
    elif not chk["has_data"]:
        add(-1, "overall", f"Monitoring gap: no check reported anything during the {span} although history exists from before it, "
                           "so the state of the host is unknown, not healthy.")
    else:
        w, c, ok = chk["warn_min"], chk["crit_min"], chk["ok_min"]
        tot = max(ok + w + c, 1)
        if w == c == 0:
            line = f"All checks were ok for {chk['time_ok_pct']:.0f}% of the observed {span}; no warnings or critical results."
        else:
            line = f"Checks were healthy {chk['time_ok_pct']:.0f}% of the observed {span} (warning {100 * w / tot:.0f}%, critical {100 * c / tot:.0f}%)."
        if chk["observed_pct"] < 95:
            bits = []
            if chk["new_install"]:
                bits.append(f"history starts {_fmt_t(chk['first_t'], tz)}")
            if chk["blind_min"]:
                bits.append(f"{_dur(chk['blind_min'] * 60)} with no check result" + (" after that" if chk["new_install"] else ""))
            line += f" Checks cover {chk['observed_pct']:.0f}% of the {span}" + (f" ({'; '.join(bits)})." if bits else ".")
        if gap:
            line = f"Monitoring gap: {gap}, so that time is unknown, not healthy. " + line
        add(-1, "overall", line)
    # --- problems: episodes, incidents, harm -----------------------------------------------------------------------
    for e in f.episodes[:3]:
        add(e["prio"], "episode", e["text"])
    if inc["available"]:
        open_rows = [r for r in inc["list"] if not r["resolved"]]
        if inc["opened"] or inc["resolved"] or open_rows:
            s = f"{_plural(inc['opened'], 'incident')} opened, {inc['resolved']} resolved"
            if inc["mttr_s"] is not None:
                s += f"; mean time to resolve {_dur(inc['mttr_s'])}"
            if open_rows:
                s += "; still open: " + "; ".join(f"{r['severity']} {r['title']} ({_dur(r['duration_s'])}" + (f", {r['related']} related check(s)" if r.get("related") else "") + ")"
                                     for r in open_rows[:2])
            add(0.2 if open_rows else 3.5, "incident", s + ".")
        else:
            add(3.4, "incident", f"No incidents were opened during the {span}.")
    else:
        add(2.5, "incident", "No incident ledger was found, so incident counts are unavailable; the check results above are all this report can say.")
    ak = f.acknowledged
    if ak.get("count"):
        names = "; ".join(f"{r['title']} until {r['until_label']}" for r in ak["list"][:3])
        add(2.9, "ack", f"{_plural(ak['count'], 'acknowledged issue')} you accepted {'is' if ak['count'] == 1 else 'are'} not counted as problems ({names}"
                        + (f", +{ak['count'] - 3} more" if ak["count"] > 3 else "") + ")"
                        + (f"; {ak['failing']} still failing." if ak["failing"] else "; none is failing now."))
    if pr["oom_kills"]:
        add(0.5, "harm", f"The kernel OOM-killed {_plural(pr['oom_kills'], 'process', 'processes')}: a workload was lost to memory pressure.")
    if chk["stale"]:
        add(0.8, "harm", f"{_plural(len(chk['stale']), 'check')} had stopped reporting by the end of the {span} ({', '.join(chk['stale'][:3])}"
            + (f" and {len(chk['stale']) - 3} more" if len(chk["stale"]) > 3 else "") + f"): {'its' if len(chk['stale']) == 1 else 'their'} state is unknown, not healthy.")
    if a["alerts_failed"]:
        add(0.6, "harm", f"{_plural(a['alerts_failed'], 'alert')} could not be delivered, so you may not have been paged.")
    if a["alerts_sent"]:
        add(3.6, "alerts", f"{_plural(a['alerts_sent'], 'alert')} {'was' if a['alerts_sent'] == 1 else 'were'} sent to you.")
    if a["failed"]:
        add(1.5, "harm", f"{_plural(a['failed'], 'maintenance action')} failed; the notable list has the first errors.")
    # --- spikes ---------------------------------------------------------------------------------------------------
    if sp["data"] == "missing":
        bits = []
        if pr["mem_full60_max"] is not None:
            bits.append(f"memory stall peaked at {pr['mem_full60_max']:.1f}%")
        if pr["mem_available_min_gib"] is not None:
            bits.append(f"available memory bottomed at {pr['mem_available_min_gib']:.0f} GiB")
        add(2.6, "spike", "Spike tracking has no data (pressure_state has not run), so spikes cannot be reported"
            + (f"; the 15-minute checks show {' and '.join(bits)}." if bits else "."))
    elif sp["count"]:
        add(3, "spike", _spike_line(sp, tz))
    else:
        since = pr.get("spike_tracking_since")
        add(3.5, "spike", "No load spikes were recorded" + (f" (peak pressure level {sp['worst_level']})" if sp["worst_level"] else "")
            + (f", but spike tracking only started {_fmt_t(since, tz)}." if since else "."))
    if (pr["io_some60_avg"] or 0) >= 30:
        memo = pr["mem_full60_max"]
        add(3.2, "io", f"Disk I/O pressure was high: tasks waited on disk {pr['io_some60_avg']:.0f}% of the time on average (peak {pr['io_some60_max']:.0f}%)"
            + (f" while memory stall peaked at {memo:.1f}%" + ("; this is disk saturation, not a RAM shortage" if memo < 5 else "") if memo is not None else "") + ".")
    # --- maintenance -----------------------------------------------------------------------------------------------
    modes = a["cleaner_modes"]
    if not a["audit_available"]:
        add(2.7, "maint", "No audit log was found, so maintenance actions cannot be listed.")
    elif a["freed_bytes"]:
        top = ", ".join(f"{ACTION_TITLES.get(e['task'], e['task']).lower()} {human(e['freed'])}" for e in a["by_task"] if e["freed"])
        add(4, "maint", f"Maintenance freed {human(a['freed_bytes'])} in {_plural(a['count'], 'action')} ({top[:120]}).")
    elif a["count"]:
        add(4, "maint", f"Maintenance made {_plural(a['count'], 'change')}; none freed space.")
    elif a["cleaner_runs"] == 0:
        add(3.8, "maint", f"The cleanup tasks did not run during the {span} (no daily-tier results recorded).")
    elif modes["report"] and not modes["apply"]:
        tot = sum(w["bytes"] for w in a["would_free"])
        add(4, "maint", "Cleanup is in report mode: nothing was deleted" + (f"; {human(tot)} is reclaimable right now." if tot else "."))
    else:
        add(4.5, "maint", "The cleanup tasks ran and found nothing to remove.")
    logged = [n for n in a["notable"] if n.get("source") in ("journal", "change")]
    if logged:
        add(4.2, "changes", "Logged changes: " + "; ".join(_asc(n["title"], 60) for n in logged[:3]) + (f" (+{len(logged) - 3} more)." if len(logged) > 3 else "."))
    # --- backups (state at the latest check; skipped when a backup episode is already reported above) ----------------
    if not any(e["task"] == "backup_freshness" for e in f.episodes):
        rows = [r for r in _ls(_tm(f.tasks, "backup_freshness").get("backups")) if isinstance(r, dict)]
        bad = [r for r in rows if str(r.get("result", "ok")).lower() != "ok" or r.get("level") not in (None, "ok")]
        if bad:
            failed = any(str(r.get("result", "ok")).lower() != "ok" for r in bad)
            add(0.7 if failed else 1.3, "backup", "Backups need attention at the latest check: "
                + "; ".join(f"{r.get('name', '?')} {r.get('result') if failed else 'is stale'} ({r.get('age') or '?'} old)" for r in bad[:3]) + ".")
        elif rows:
            add(5, "backup", "Backups are current at the latest check (age: "
                + ", ".join(f"{str(r.get('name', '?')).replace('backup-', '')} {r.get('age') or '?'}" for r in rows[:4]) + ").")
        elif chk["backups"]["seen"]:
            add(5, "backup", "No backup failure was recorded; the latest status has no per-backup detail.")
        else:
            add(2.8, "backup", "No backup check results were recorded, so backup state is unknown.")
    # --- capacity: the tightest mount always, a second one only when it is a problem ------------------------------
    for i, m in enumerate(cap["mounts"][:2]):
        calm = m["level"] in (None, "ok")
        if i and calm:
            continue
        s = f"{_mount_name(m['mount'])}: {m['free_h']} free" + (f" ({m['used_pct']:.0f}% used)" if m["used_pct"] is not None else "")
        if m["days_to_full"] is not None:
            s += f", filling about {m['trend_gib_per_day'] or 0:.1f} GiB/day, roughly {m['days_to_full']:.0f} days to full"
        elif m["trend_gib_per_day"] is None:
            s += "; no fill-up forecast yet (needs 6 h of history)"
        elif m["trend_gib_per_day"] > 0:
            s += f"; filling {m['trend_gib_per_day']:.1f} GiB/day, but no full-disk date within 10 years"
        else:
            s += "; free space is stable or growing"
        add(5.5 if calm else 1.2, "cap", s + ".")
    b = cap["memory_baseline_gib"]
    if b["drift"] is not None and abs(b["drift"]) >= 1:
        add(5.8, "cap2", f"Container memory baseline moved from {b['start']} to {b['end']} GiB ({b['drift']:+.1f}) over the {span}.")
    # --- temperatures ----------------------------------------------------------------------------------------------
    if tmp["hours_covered"] == 0:
        add(5.9, "temp", "No temperature data for this period (the 1-minute sampler recorded no hours), so thermals were not checked.")
    else:
        t = [f"{lbl} {tmp[av]:.0f} C avg / {tmp[mx]:.0f} C max" for lbl, av, mx in
             (("CPU", "cpu_avg", "cpu_max"), ("GPU", "gpu_avg", "gpu_max"), ("NVMe", "nvme_avg", "nvme_max")) if tmp[av] is not None]
        part = f" (only {tmp['hours_covered']} of {tmp['hours_expected']} h recorded)" if _thin(tmp) else ""
        hot = any(tmp[k] is not None and tmp[k] >= lim for k, lim in (("cpu_max", 85), ("gpu_max", 85), ("nvme_max", 70)))
        add(1.4 if hot else 5.9, "temp", ("Temperatures: " + ", ".join(t) if t else "Temperature sensors returned no readings") + part + ".")
    bad_slo = [s for s in f.slo if s["status"] in ("at_risk", "breached")]
    if bad_slo:
        add(1.6, "slo", "Error budget: " + ", ".join(f"{s['name']} {s['status']} ({s['availability_pct']}%)" for s in bad_slo[:3]) + ".")
    return [_asc(t, 320) for _, _c, t in _pick(H)]


def make_headline(f: Facts) -> str:
    h = f.health
    if not f.chk["has_data"] and not f.chk["gap"]:
        return _asc(f"No data recorded for {f.per.label}", 80)
    if h["score"] is None:
        return _asc(f"Collecting data: {_dur(f.chk['covered'] * SLOT_S)} of history for {f.per.label}", 80)
    open_rows = [r for r in f.incidents["list"] if not r["resolved"]]
    problem = f"open incident: {open_rows[0]['title']}" if open_rows else f.episodes[0]["short"] if f.episodes else None
    gap = _gap_phrase(f.chk)             # a hole in monitoring is never "all checks ok"
    core_ = (f"monitoring gap: {gap}" + (f"; {problem}" if problem else "")) if gap else (problem or "all checks ok")
    tail = []
    if f.spikes["count"] and f.spikes["handled_without_harm"]:
        tail.append(f"{_plural(f.spikes['count'], 'spike')} handled")
    if f.actions["freed_bytes"]:
        tail.append(f"{human(f.actions['freed_bytes'])} freed")
    return _asc(f"{h['grade']} ({h['score']}{', early data' if h['provisional'] else ''}): {core_}" + (f"; {', '.join(tail)}" if tail else ""), 80)


def make_digest(f: Facts, recs: list[str]) -> str:
    """<= 600 ASCII chars for one SMS/e-mail; the lead sends it (this module sends nothing)."""
    h, sp, inc, a = f.health, f.spikes, f.incidents, f.actions
    score = "not scored yet (collecting data)" if h["score"] is None else f"{h['grade']} ({h['score']})"
    parts = [f"homelab-maint {f.per.kind} {f.per.id} ({f.per.label}): health {score}" + (", provisional." if h["provisional"] else ".")]
    gap = _gap_phrase(f.chk)
    if gap:
        parts.append(f"MONITORING GAP: {gap}.")
    if f.episodes:
        parts.append("Problems: " + " | ".join(e["short"] for e in f.episodes[:2]) + ".")
    elif not gap:                                   # "no problems" next to a monitoring gap would read as reassurance
        parts.append("No problem episodes.")
    if inc["available"]:
        parts.append(f"Incidents: {inc['opened']} opened, {inc['open_now']} open now.")
    if sp["data"] == "ok":
        parts.append(f"Spikes: {sp['count']}" + (", nothing killed." if sp["count"] and sp["handled_without_harm"] else
                                                  ", a restart/stop failed: check that workload." if any(e.get("attempted") for e in sp["list"]) else "."))
    parts.append(f"Freed {human(a['freed_bytes'])}." if a["freed_bytes"] else "Freed nothing.")
    if f.capacity["mounts"]:
        m = f.capacity["mounts"][0]
        parts.append(f"{_mount_name(m['mount'])} {m['free_h']} free" + (f", ~{m['days_to_full']:.0f} d to full." if m["days_to_full"] is not None else "."))
    if recs:
        parts.append("Do next: " + recs[0])
    text = _asc(" ".join(parts), 4000)
    return text if len(text) <= DIGEST_MAX else text[: DIGEST_MAX - 2].rsplit(" ", 1)[0] + ".."


# =========================================================================== build the report
MIN_SLOTS_FOR_SCORE = 4             # under one hour of check results there is nothing honest to score
MIN_COVERAGE_PCT = 10.0             # ... and neither when they cover under a tenth of the period (a new install's first week)


def build_report(kind: str, now: float, inp: Inputs, tz: tzinfo) -> dict:
    """Pure function of the inputs. A damaged optional input degrades its section and is named in `notes`."""
    per = period_for(kind, now, tz)
    notes: list[str] = []
    tasks = _tasks_of(inp.status)
    # alert=False per task (status.json); pressure_state/pressure_response are judged per run (see _lvl), so only an explicit
    # ignore_tasks puts them here
    alert_off = {n for n, e in tasks.items() if e.get("alert") is False and n not in RUN_ALERT_TASKS} | set(inp.opts.get("ignore_tasks") or ())

    def safe(name: str, fn: Callable[[], Any], fallback: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:                    # noqa: BLE001 - one bad section must not cost the whole report
            notes.append(f"Section {name} unavailable ({type(exc).__name__}).")
            return fallback()

    # the fallbacks are static blanks, never a second call into the code that just failed
    chk = safe("checks", lambda: analyse_checks(inp, per, tz, alert_off), lambda: _blank_checks(per))
    pressure = safe("pressure", lambda: pressure_summary(inp, per), _blank_pressure)
    actions = safe("actions", lambda: summarize_actions(inp, per, tz), _blank_actions)
    incidents, open_sev = safe("incidents", lambda: summarize_incidents(inp, per, tz), lambda: (_blank_incidents(), []))
    acknowledged = safe("acknowledged", lambda: acknowledged_section(inp, now, tz),
                        lambda: {"available": False, "count": 0, "failing": 0, "list": [], "expired_30d": 0})
    spikes = safe("spikes", lambda: summarize_spikes(inp, per, tz, pressure), _blank_spikes)
    capacity = safe("capacity", lambda: capacity_section(inp, per), _blank_capacity)
    temp = safe("temperature", lambda: temperature_section(inp.ring, per), lambda: _blank_temperature(per))
    slo = safe("slo", lambda: slo_section(inp), list)
    upcoming = safe("upcoming", lambda: upcoming_section(inp, now, tz), list)
    capacity["recommendations"] = safe("recommendations", lambda: recommendations(inp, per, capacity, actions, chk, pressure, temp), list)

    # --- health ---------------------------------------------------------------------------------------------------
    # The "collecting data" gate is for a NEW INSTALL only (history starts inside the period). History that exists from before
    # the period and has holes is a monitoring gap: it is scored (blind time), never reported as "not enough data".
    gap = _gap_phrase(chk)
    young = chk["new_install"] and not (chk["covered"] >= MIN_SLOTS_FOR_SCORE and chk["observed_pct"] >= MIN_COVERAGE_PCT)
    scored = (chk["has_data"] or chk["gap"]) and not young
    hs = (health_score(period_min=chk["n_slots"] * 15, crit_min=chk["crit_min"], warn_min=chk["warn_min"], blind_min=chk["blind_min"],
                       open_incidents=open_sev, backups_failed=chk["backups"]["failed"], backups_stale=chk["backups"]["stale"],
                       stale_checks=len(chk["stale"]), open_level=max(chk["open"].values(), default=0))
          if scored else {"score": None, "grade": "n/a", "deductions": {}})
    health = {"score": hs["score"], "grade": hs["grade"], "worst_status": chk["worst"], "time_ok_pct": chk["time_ok_pct"],
              "checks_ok_pct_by_task": chk["ok_pct_by_task"], "coverage_pct": chk["observed_pct"],
              "provisional": bool(scored and chk["new_install"] and chk["observed_pct"] < PROVISIONAL_BELOW),
              "monitoring_gap": bool(chk["gap"]),
              "minutes": {"ok": chk["ok_min"], "warn": chk["warn_min"], "crit": chk["crit_min"], "blind": chk["blind_min"]},
              "deductions": hs["deductions"], "formula": FORMULA}
    prev = next((e for e in inp.index if e.get("kind") == kind and e.get("id") != per.id
                 and (_num(e.get("period_end"), 0) or 0) <= per.start + 1), None)
    if prev and isinstance(prev.get("health"), dict) and prev["health"].get("score") is not None:
        health["previous"] = {"id": prev.get("id"), "score": prev["health"]["score"], "grade": prev["health"].get("grade")}
    episodes = episode_lines(chk, tasks, per, tz) if chk["has_data"] or chk["gap"] else []

    # --- notes: data quality and caveats --------------------------------------------------------------------------
    if not chk["has_data"] and not chk["gap"]:
        notes.append("No history records fall inside this period, so no score is computed.")
    elif not chk["has_data"]:
        notes.append("Monitoring gap: no check result was recorded in this period although history exists from before it; "
                     "the whole period is scored as unknown, not as healthy.")
    elif not scored:
        notes.append(f"Too little check history to score the {per.span}: about {_dur(chk['covered'] * SLOT_S)}, {chk['observed_pct']:.0f}% of the period "
                     f"(a score needs at least 1 hour and {MIN_COVERAGE_PCT:.0f}%). Still collecting data.")
    elif health["provisional"]:
        notes.append(f"Provisional score: check results cover {chk['observed_pct']:.0f}% of the period"
                     + (f" (history starts {_fmt_t(chk['first_t'], tz)})." if chk["first_t"] and chk["first_t"] > per.start else "."))
    if gap and chk["has_data"]:
        notes.append(_asc(f"Monitoring gap: {gap}. Time without a check result is scored as unknown (35% weight), not as healthy.", 200))
    for label, absent in (("history.jsonl", inp.recs is None), ("status.json", inp.status is None), ("audit log", inp.audit is None),
                          ("incident ledger", inp.incidents is None), ("metrics ring (temperatures)", inp.ring is None)):
        if absent:
            notes.append(f"Missing input: {label}.")
    if actions["cleaner_modes"]["report"] and not actions["cleaner_modes"]["apply"]:
        notes.append("All cleanup tasks are in report mode (mode = \"report\"): they list what they would remove and delete nothing.")
    if inp.paused:
        notes.append("The kill switch (PAUSE) is present: automatic maintenance is paused.")
    for n in sorted(alert_off):
        e = tasks.get(n) or {}
        if e.get("status") in ("warn", "crit"):
            notes.append(_asc(f"{_title(tasks, n)} is informational (never pages): " + _check_text(n, e.get("summary"), 160, e), 200))
    if any(m["trend_gib_per_day"] is None for m in capacity["mounts"][:2]):
        notes.append("Disk trends need 6 h of history before a fill-up date can be given.")
    if temp["hours_covered"] and _thin(temp):
        notes.append(f"Temperatures cover {temp['hours_covered']} of {temp['hours_expected']} h (the sampler ring keeps 7 days).")

    # --- per-day table (a day is 23/24/25 h around DST; `hours` says which) ---------------------------------------
    freed_by_day: dict[date, int] = defaultdict(int)
    for r in inp.audit or []:
        if r["o"] == "done" and r["task"] not in NOT_MAINTENANCE:
            freed_by_day[_local_date(r["t"], tz)] += r["bytes"]
    days = []
    for d in per.days:
        v = chk["by_day"].get(d) or {"ok": 0, "warn": 0, "crit": 0}
        worst = "unknown" if not (v["ok"] + v["warn"] + v["crit"]) else "crit" if v["crit"] else "warn" if v["warn"] else "ok"
        days.append({"day": d.isoformat(), "dow": DOW[d.weekday()], "worst": worst, "warn_min": v["warn"], "crit_min": v["crit"],
                     "freed_bytes": freed_by_day.get(d, 0),
                     "hours": round((_midnight(d + timedelta(days=1), tz) - _midnight(d, tz)) / 3600, 2)})

    facts = Facts(inp, per, tz, tasks, chk, health, actions, incidents, spikes, capacity, temp, pressure, slo, episodes, acknowledged)
    highlights = safe("highlights", lambda: build_highlights(facts), list)
    public_actions = {k: v for k, v in actions.items() if k not in ("cleaner_modes", "cleaner_runs")}
    notes = [n for n in notes if n.startswith("Section ")] + [n for n in notes if not n.startswith("Section ")]     # a broken section is never cut
    return {"schema": SCHEMA, "id": per.id, "kind": kind, "generated_at": now,
            "period": {"start": per.start, "end": per.end, "label": per.label, "tz": per.tz, "days": len(per.days),
                       "hours": round((per.end - per.start) / 3600, 2)},
            "headline": make_headline(facts), "health": health, "highlights": highlights, "actions": public_actions,
            "incidents": incidents, "acknowledged": acknowledged, "spikes": spikes, "capacity": capacity, "temperature": temp, "slo": slo,
            "upcoming": upcoming, "notes": notes[:12], "pressure": pressure, "days": days,
            "digest_text": make_digest(facts, capacity["recommendations"])}


# =========================================================================== output, index, retention
_ID_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{4}-W\d{2})\.json$")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rep-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _encode(doc: dict) -> bytes:
    """Compact UTF-8 JSON under the 195 KB cap; the longest lists are halved until it fits."""
    for _ in range(40):
        data = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()
        if len(data) <= MAX_FILE:
            return data
        lists = [(len(v), o, k) for o in (doc, doc.get("actions", {}), doc.get("capacity", {})) for k, v in o.items()
                 if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        _, o, k = max(lists, key=lambda x: x[0])
        del o[k][max(len(o[k]) // 2, 1):]
    raise ValueError("report does not fit in the size cap")


def _index_entry(doc: dict) -> dict | None:
    try:
        h = doc["health"]
        return {"id": doc["id"], "kind": doc["kind"], "period_start": doc["period"]["start"], "period_end": doc["period"]["end"],
                "generated_at": doc["generated_at"], "headline": doc.get("headline", ""),
                "health": {"score": h.get("score"), "grade": h.get("grade"), "worst_status": h.get("worst_status")}}
    except (KeyError, TypeError):
        return None


def load_index(rdir: Path) -> list[dict]:
    """The index rebuilt from the report files (the files are the truth: a lost or damaged index.json heals itself)."""
    out = []
    try:
        names = sorted(p.name for p in rdir.iterdir() if _ID_FILE.match(p.name))
    except OSError:
        return []
    for n in names:
        e = _index_entry(read_json(rdir / n, None) or {})
        if e and f"{e['id']}.json" == n:
            out.append(e)
    out.sort(key=lambda e: (-(e["period_end"] or 0), e["kind"] != "weekly", e["id"]))
    return out


def trim_index(entries: list[dict], keep: int = KEEP) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). The oldest dailies go first, down to the newest KEEP_MIN_DAILY; only then the oldest weeklies:
    weekly history outlives daily history inside the same 60 entries."""
    kept = sorted(entries, key=lambda e: (-(e["period_end"] or 0), e["id"]))
    dropped: list[dict] = []
    while len(kept) > keep:
        dailies = [e for e in kept if e["kind"] == "daily"]
        weeklies = [e for e in kept if e["kind"] != "daily"]
        victim = dailies[-1] if (len(dailies) > KEEP_MIN_DAILY or not weeklies) else weeklies[-1]
        kept.remove(victim)
        dropped.append(victim)
    return kept, dropped


def write_report(doc: dict, keep: int = KEEP) -> list[str]:
    """Write <id>.json, then rebuild index.json from the files and drop what retention evicts. Serialised by a flock
    (the daily and weekly tiers can run at the same time)."""
    pub = Path(core.STATE_DIR) / "public"
    rdir = pub / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    for d in (pub, rdir):
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
    data = _encode(doc)
    with open(Path(core.STATE_DIR) / "reports.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        _atomic_write(rdir / f"{doc['id']}.json", data)
        kept, dropped = trim_index(load_index(rdir), keep)
        for e in dropped:
            try:
                (rdir / f"{e['id']}.json").unlink()
            except OSError:
                pass
        _atomic_write(rdir / "index.json", json.dumps(kept, separators=(",", ":")).encode())
        for old in rdir.glob(".rep-*.tmp"):         # leftovers of a killed writer
            try:
                if time.time() - old.stat().st_mtime > 600:
                    old.unlink()
            except OSError:
                pass
    return [f"{doc['id']}.json", "index.json"]


def generate(kind: str, now: float | None = None, tz: tzinfo | str | None = None, keep: int = KEEP,
             opts: dict | None = None, write: bool = True) -> dict:
    """Build (and by default write) the report for the last complete day / 7 days before `now`."""
    now = time.time() if now is None else float(now)
    zone = tz if isinstance(tz, tzinfo) else resolve_tz(tz)
    per = period_for(kind, now, zone)
    inp = gather(per, zone, opts)
    doc = _scrub(build_report(kind, now, inp, zone), _cleaner())     # the redactor is the LAST step: nothing is shortened or built after it
    if write:
        write_report(doc, keep)
    return doc


# =========================================================================== tasks and CLI
def _run(kind: str, ctx: Ctx) -> Result:
    doc = generate(kind, ctx.now, ctx.opt("timezone") or None, int(ctx.opt("keep", KEEP)),
                   {"ignore_tasks": ctx.opt("ignore_tasks", [])})
    h = doc["health"]
    summary = _asc(f"{kind} report {doc['id']}: {doc['headline']}", 140)           # the headline already carries grade and score
    items = [{"highlight": _asc(t, 140)} for t in doc["highlights"][:4]]
    metrics = {"id": doc["id"], "score": h["score"], "grade": h["grade"], "highlights": len(doc["highlights"]),
               "provisional": h["provisional"], "monitoring_gap": h["monitoring_gap"], "freed_bytes": doc["actions"]["freed_bytes"]}
    if kind == "weekly":
        metrics["digest_text"] = doc["digest_text"]      # for the lead to send via the Notifier once a week
    return Result("ok", summary, metrics, items, alert=False)


@task("report_daily", klass="C0", tier="daily", title="Daily report", timeout=120)
def report_daily(ctx: Ctx) -> Result:
    return _run("daily", ctx)


@task("report_weekly", klass="C0", tier="weekly", title="Weekly report", timeout=180)
def report_weekly(ctx: Ctx) -> Result:
    return _run("weekly", ctx)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python3 -m homelab_maint.reports", description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=("daily", "weekly", "index"))
    ap.add_argument("--now", type=float, help="pretend it is this epoch (the period is the last complete day/week before it)")
    ap.add_argument("--tz", help="IANA zone name (default: host zone)")
    ap.add_argument("--print", action="store_true", help="print the report JSON instead of writing files")
    a = ap.parse_args(argv)
    if a.what == "index":
        rdir = Path(core.STATE_DIR) / "public" / "reports"
        kept, _dropped = trim_index(load_index(rdir))            # rebuild only: files beyond the retention limit are removed by the next report
        if rdir.is_dir():
            _atomic_write(rdir / "index.json", json.dumps(kept, separators=(",", ":")).encode())
        print(f"index: {len(kept)} reports")
        return 0
    doc = generate(a.what, a.now, a.tz, write=not a.print)
    if a.print:
        print(json.dumps(doc, indent=1))
    else:
        print(f"wrote {doc['id']}: {doc['headline']}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
