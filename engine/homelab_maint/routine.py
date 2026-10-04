"""routine: declarative maintenance routine + change management for homelab-maint (SPEC3 S1).

WHAT IT DECIDES (this module never touches the host itself: the runner executes, this module authorises, orders, records)
  * etc/routine.toml declares per cadence (daily|weekly|monthly) a maintenance WINDOW in HOST-LOCAL time, FREEZE windows
    (evenings, dated holidays, a CONF_DIR/FREEZE file) and ordered STEPS (task names or the built-in review steps below).
  * Time is wall-clock in the host zone (America/Toronto, DST aware). Every window is converted to epoch seconds through
    zoneinfo: a time inside the spring-forward gap becomes the first instant after the gap (02:30 -> 03:00 EDT), an
    ambiguous fall-back time takes the FIRST occurrence, and a window's length is the real elapsed time between its
    converted ends (a window that lies wholly inside the gap does not exist that day).
  * An OCCURRENCE is one instance of a window, id = its local start date ("2026-10-03"). "1st Sat", "last Sun", "day 15"
    and "Mon-Fri" selectors are computed from the calendar, so month boundaries and 5-Saturday months are exact.
  * due()/plan() are pure functions of (now, state, config): calling them twice gives the same answer, and a step is only
    ever due ONCE per occurrence (the state records occurrence + outcome). Catch-up when the host was off:
        - steps that need a quiet window (apply-mode cleaners, heavy jobs, anything marked disruptive) run only INSIDE the
          occurrence window and never inside a freeze; a missed window is reported as "missed", never replayed;
        - read-only steps are due at the next opportunity, at most once per occurrence (a freeze does not stop them);
        - only the LATEST occurrence is caught up: three days off does not queue three daily digests.
  * RunGuard is the 4-call adapter for cli.cmd_run (order / begin / task_cfg / after). begin() answers "may this task run,
    may it apply, with which caps" (window, freeze, PAUSE, halt-after-regression, busy gates incl. spike pressure, canary
    caps, dependencies); after() marks the state, writes the change log, runs the post-check and halts further changes of the
    occurrence if it regressed. Tasks of the check tier are CONTINUOUS: never window-governed and never throttled by the
    routine (the spike ladder must be able to act at any hour) with ONE exception, the freeze: a check-tier task that
    RESTARTS a user-facing service (immich_recycle, comfyui_idle_reclaim, [continuous] disruptive) is held to report-only while
    a freeze is active, and the spike ladder's destructive rungs (restart, emergency) are too unless the pressure level is an
    emergency (>= [continuous] emergency_level); its reclaim and throttle rungs are protective and keep running. A task that
    no routine names is UNMANAGED: it runs, but a C1/C2 one is held to report-only.
  * MANUAL runs: `run --task X` is the owner's override of the window and the done-state ONLY when it really is the owner
    (is_manual: --override/--force, or a terminal on stdin; timers, the scheduler and the tick have none, so a task they
    start is held to every rule). Even then a FREEZE (file, evening window, dated) and a halt after a post-check regression
    stay in force (the run is report-only) unless --force is given. PAUSE and busy gates always apply. Recorded as "override".
  * `run_due()` is the same thing as a tick (`routine run [--apply]`, every 15 min from the scheduler job `routine-run`): it executes whatever due()
    lists, one step at a time through cli.cmd_run, re-evaluating after each, so retries (a busy gate), the monthly window
    and catch-up after downtime need no timer of their own. Whoever runs a step first marks it; a second run is skipped.
  * Canary: the first N (default 1) apply runs of a task that really changed something run with 10 % caps (items and bytes).
    The canary only ever LOWERS what the task would have used (it never overwrites a limit the task applies itself) and it
    cannot starve a cleaner whose single action is bigger than 10 % of the byte cap: a canary run that applied nothing
    because of the caps is surfaced ("canary-throttled" in the summary and metrics) and the next canary run gets the normal
    byte cap, still with the small item count ([canary] relax_after).
  * Baseline + post-check: the first managed step of every occurrence (any step, applying or not) records the statuses of the
    routine's post_check checks. A post-check re-runs the named C0 checks through the registry and compares with that
    baseline. After a change a regression, an error, a timeout or a missing baseline = NOT verified and further disruptive
    steps of that occurrence are halted (fail closed). The verify steps flag only REGRESSIONS: a check that was already warn
    when the occurrence began is compared with itself. A change by a continuous (check-tier) task has no post-change window:
    it is logged with verified = null (not applicable) and never counts as unverified.
  * Closing steps (verify, report) wait for every earlier step of their routine to reach a terminal state while the window
    is open; after the window ends they run once with whatever is left. Read-only steps never wait for ANOTHER routine
    (weekly does not wait for daily); disruptive ones do. The routine tick (scheduler job `routine-run`) is a hard
    dependency for retries, the monthly window and deferred steps: `routine check` fails when its timer is not enabled.
  * Everything the website needs is in export() (routine.json): windows, freeze, routine, steps, 100 changes, 14-day calendar.

BUILT-IN STEPS (registered tasks, C0 unless noted, all fail-closed: an unreadable source is "unknown", never "fine")
  routine_spike_review, routine_verify_{daily,weekly,monthly}, routine_capacity, routine_smart_selftest, routine_updates,
  routine_image_updates, routine_backup_verify, routine_restore_check, routine_expiry, routine_trends, and routine_rotate
  (the only C1: rotates the tool's own logs, report-only unless [tasks.routine_rotate] mode = "apply").
  They are named in routine.toml by short words (spike_review, verify, ...); see BUILTIN_STEPS.

State (the tool's own): STATE_DIR/routine-state.json, STATE_DIR/changes.jsonl, STATE_DIR/public/routine.json,
STATE_DIR/maintenance-journal.jsonl (manual notes/acks only; the owner's journal is never rotated).
API: plan / due / due_steps, record_change / read_changes, post_check / snapshot, canary_caps, RunGuard, is_manual, run_due, export /
write_export, record_approved (glue for `approve`), tick_state, load_config / parse_window.
CLI: python3 -m homelab_maint.routine [--now T] [--config F] plan|status|due|export [--write]|run [--apply]|explain|check|
note|ack|clear-halt|canary-reset   (homelab-maint routine ... with the cli glue)
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import re
import signal
import sys
import time
import tomllib
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple
from zoneinfo import ZoneInfo

from . import core
from .core import GIB, LEVELS, Ctx, Result, Task, human
from .core import task as register_task

sh = core.sh                                 # tests patch routine.sh (the built-in steps call it)
CADENCES = ("daily", "weekly", "monthly")
KINDS = ("maintenance", "config", "restart", "cleanup")
OK_STATUS = ("ok", "info", "warn", "crit")   # the task RAN and said something; error/skipped did not complete
ACTIVE = ("due", "waiting", "frozen")        # a step in one of these states can still run in this occurrence
DEFAULT_WINDOWS = {"daily": "07:30-09:30", "weekly": "Wed 07:45-10:00", "monthly": "1st Sat 04:30-07:00"}
MAX_CHANGES = 100                            # newest changes exported in routine.json
CAL_DAYS = 14

# step name in routine.toml -> registered task. {c} = the entry's cadence, so one `verify` word works in every routine.
BUILTIN_STEPS = {
    "spike_review": "routine_spike_review", "capacity_review": "routine_capacity",
    "smart_selftest": "routine_smart_selftest", "updates_review": "routine_updates",
    "image_updates": "routine_image_updates", "backup_verify": "routine_backup_verify",
    "restore_check": "routine_restore_check", "expiry_check": "routine_expiry",
    "trend_review": "routine_trends", "rotate_logs": "routine_rotate",
    "verify": "routine_verify_{c}", "report": "report_{c}",       # report_daily / report_weekly come from reports.py
}
# change-log kind per task when the task's whole business is one kind (default: derived from the audit actions, _kind_from, so
# the spike ladder's reclaim/unload is "cleanup", a throttle "config" and only a real `docker restart` says "restart")
CHANGE_KIND = {"caps": "config", "qos_classes": "config", "gradle_reaper": "maintenance", "routine_rotate": "maintenance",
               "snap_revisions": "maintenance", "stale_driver_packages": "maintenance", "apt_autoremove_unused": "maintenance",
               "flatpak_unused": "maintenance", "swap_auto_relief": "maintenance"}      # cleaners v2 (pkgs): package upkeep, not a cleanup of data; a swap relief moves pages, deletes nothing
NA = "n/a"                                   # record_change(verified=NA): no post-change window exists (written as null)
# check-tier tasks (continuous, never window-governed) whose apply RESTARTS a user-facing service: they wait out a freeze
RESTART_TASKS = ("immich_recycle", "comfyui_idle_reclaim")
PRESSURE_TASK = "pressure_response"          # the spike ladder: only its destructive rungs wait out a freeze (see RunGuard)
TICK_UNIT = "homelab-maint-tick.timer"       # the scheduler timer that runs the `routine-run` job (retries, monthly window)


# =========================================================================== small helpers
_ASCII = re.compile(r"[^\x20-\x7e]")
_URLQ = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG = re.compile(r"[A-Za-z0-9_+=]{40,}")
_HOME = re.compile(r"(/home/[^/\s]+(?:/[^/\s]+){0,3})(/[^\s]*)?")


def _safe(s: Any, n: int = 140) -> str:
    """ASCII, single line, no URL query strings, e-mail addresses, long token-like runs or deep home paths."""
    t = _HOME.sub(lambda m: m.group(1) + ("/..." if m.group(2) else ""), str(s))
    t = _LONG.sub("[redacted]", _MAIL.sub("[email]", _URLQ.sub(r"\1", t)))
    return _ASCII.sub("?", t.replace("\n", " ").replace("\r", " "))[:n]


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def _rec_time(r: dict, keys=("ts", "t")) -> float | None:
    for k in keys:
        v = r.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(v, fmt).timestamp()
                except ValueError:
                    pass
    return None


def _jsonl(path: Path, since: float = 0.0, tail_bytes: int = 4 * 1024 * 1024, keys=("ts", "t")) -> list[dict]:
    """Records of a JSON-lines file newer than `since` (epoch), reading at most the last `tail_bytes`. Never raises.
    Each record gets `_t` (its time, or None when it has none: such records are kept)."""
    out: list[dict] = []
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - tail_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return out
    lines = data.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]                          # first line is cut in the middle
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        t = _rec_time(r, keys)
        if t is None or t >= since:
            r["_t"] = t
            out.append(r)
    return out


def _fmt(ts: float | None, tz, fmt: str = "%a %H:%M") -> str:
    return "" if ts is None else datetime.fromtimestamp(ts, tz).strftime(fmt)


# =========================================================================== time: windows, freezes, DST
_DOWS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DOWNAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6, **_DOWS}
_ORD = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "last": -1}      # no 5th: not every month has one


def _clock(s: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m or int(m[2]) > 59 or int(m[1]) > 24 or (int(m[1]) == 24 and int(m[2])):
        raise ValueError(f"bad clock time {s!r}")
    return int(m[1]) * 60 + int(m[2])


def _dowset(text: str) -> frozenset[int]:
    """'Wed' | 'Mon-Fri' | 'Fri,Sat' | 'Sat-Mon' (wraps) | 'weekdays' | 'weekends' -> weekday numbers (Mon=0)."""
    out: set[int] = set()
    for part in text.lower().split(","):
        part = part.strip()
        if part == "weekdays":
            out.update(range(5))
        elif part == "weekends":
            out.update((5, 6))
        elif "-" in part:
            a, b = (p.strip() for p in part.split("-", 1))
            if a not in _DOWNAMES or b not in _DOWNAMES:
                raise ValueError(f"bad weekday range {part!r}")
            i = _DOWNAMES[a]
            while True:
                out.add(i)
                if i == _DOWNAMES[b]:
                    break
                i = (i + 1) % 7
        elif part in _DOWNAMES:
            out.add(_DOWNAMES[part])
        else:
            raise ValueError(f"bad weekday {part!r}")
    return frozenset(out)


def _ts(d: date, minute: int, tz) -> float:
    """Epoch of the first instant whose local wall-clock is at or after `minute` (minutes after midnight, 1440 = next
    midnight) on date `d`. That one definition serves window starts and (exclusive) ends alike:
      * an ordinary time is itself; an ambiguous fall-back time (01:30 happens twice) is its FIRST occurrence;
      * a time inside the spring-forward gap (02:30 does not exist) is the first instant after the gap (03:00)."""
    if minute >= 1440:
        d, minute = d + timedelta(days=1), minute - 1440
    naive = datetime.combine(d, dtime(minute // 60, minute % 60))
    hi = naive.replace(tzinfo=tz).timestamp()                              # fold=0
    if datetime.fromtimestamp(hi, tz).replace(tzinfo=None) == naive:
        return hi
    lo = naive.replace(tzinfo=tz, fold=1).timestamp()                      # gap: lo is before the transition, hi after it
    off = datetime.fromtimestamp(hi, tz).utcoffset()
    a, b = int(lo), int(hi)
    while a < b:                                                           # smallest second already on the new offset
        mid = (a + b) // 2
        if datetime.fromtimestamp(mid, tz).utcoffset() == off:
            b = mid
        else:
            a = mid + 1
    return float(a)


class Occ(NamedTuple):
    id: str          # local start date, ISO
    start: float     # epoch
    end: float


@dataclass(frozen=True)
class Window:
    """A recurring wall-clock window: selector (weekdays / Nth weekday / day of month) + HH:MM[-HH:MM]."""
    cadence: str
    dows: frozenset = frozenset()
    nth: int = 0                 # monthly: 1..4 or -1 (last); with exactly one weekday in `dows`
    dom: int = 0                 # monthly: day of month (clamped to the month's last day)
    start: int = 0               # minutes after local midnight
    end: int | None = None       # None = a point in time (system events); end <= start means "next day"
    text: str = ""

    def matches(self, d: date) -> bool:
        last = monthrange(d.year, d.month)[1]
        if self.dows and d.weekday() not in self.dows:
            return False
        if self.nth > 0 and (d.day - 1) // 7 + 1 != self.nth:
            return False
        if self.nth < 0 and d.day + 7 <= last:
            return False
        return not (self.dom and d.day != min(self.dom, last))

    def occurrence(self, d: date, tz) -> Occ | None:
        if not self.matches(d):
            return None
        s = _ts(d, self.start, tz)
        if self.end is None:
            return Occ(d.isoformat(), s, s)
        e = _ts(d, self.end, tz) if self.end > self.start else _ts(d + timedelta(days=1), self.end, tz)
        return Occ(d.isoformat(), s, e) if e > s else None       # a window swallowed by a DST gap does not exist

    def occurrences(self, tz, d0: date, d1: date) -> list[Occ]:
        out, d = [], d0
        while d <= d1:
            o = self.occurrence(d, tz)
            if o:
                out.append(o)
            d += timedelta(days=1)
        return out

    def latest(self, tz, now: float) -> Occ | None:
        """Most recent occurrence that has started (start <= now): the one a catch-up decision is about."""
        d = datetime.fromtimestamp(now, tz).date()
        for i in range(0, 45):
            o = self.occurrence(d - timedelta(days=i), tz)
            if o and o.start <= now:
                return o
        return None

    def next(self, tz, now: float) -> Occ | None:
        d = datetime.fromtimestamp(now, tz).date() - timedelta(days=1)   # a window that wrapped midnight starts yesterday
        for i in range(0, 75):
            o = self.occurrence(d + timedelta(days=i), tz)
            if o and o.start > now:
                return o
        return None

    def clock(self) -> str:
        e = "" if self.end is None else f"-{self.end // 60:02d}:{self.end % 60:02d}"
        return f"{self.start // 60:02d}:{self.start % 60:02d}{e}"


def parse_window(cadence: str | None, spec: str, point: bool = False) -> Window:
    """'07:30-09:30' | 'daily 07:30-09:30' | 'Wed 07:45-10:00' | 'Mon-Fri 07:30-09:30' | '1st Sat 04:30-07:00' |
    'last Sun 04:00-06:00' | 'day 15 04:00-05:00'. cadence=None infers it (system events); point=True allows a bare
    'HH:MM' (a point in time). Raises ValueError with the reason."""
    text = " ".join(str(spec).split())
    m = re.fullmatch(r"(?:(?P<sel>.+?)\s+)?(?P<a>\d{1,2}:\d{2})(?:\s*-\s*(?P<b>\d{1,2}:\d{2}))?", text)
    if not m:
        raise ValueError(f"cannot parse window {spec!r}")
    a = _clock(m["a"])
    if a >= 1440:
        raise ValueError("start must be before 24:00")
    b = _clock(m["b"]) if m["b"] else None
    if b is None and not point:
        raise ValueError(f"window {spec!r} needs HH:MM-HH:MM")
    if b is not None and b == a:
        raise ValueError(f"window {spec!r} is empty")
    dows, nth, dom, sel = frozenset(), 0, 0, (m["sel"] or "").strip()
    if sel.lower() in ("daily", "every day", "*"):
        sel = ""
    if sel:
        mo, md = re.fullmatch(r"(1st|2nd|3rd|4th|last)\s+(\w+)", sel, re.I), re.fullmatch(r"day\s+(\d{1,2})", sel, re.I)
        if mo:
            nth, dows = _ORD[mo[1].lower()], _dowset(mo[2])
            if len(dows) != 1:
                raise ValueError(f"{sel!r}: one weekday expected")
        elif md:
            dom = int(md[1])
            if not 1 <= dom <= 31:
                raise ValueError(f"bad day of month {dom}")
        else:
            dows = _dowset(sel)
    kind = "monthly" if (nth or dom) else ("weekly" if dows else "daily")
    if cadence is None:
        cadence = kind
    elif cadence not in CADENCES:
        raise ValueError(f"unknown cadence {cadence!r}")
    elif cadence == "weekly" and kind != "weekly":
        raise ValueError(f"weekly window {spec!r} needs weekday(s), e.g. 'Wed 07:45-10:00'")
    elif cadence == "monthly" and kind != "monthly":
        raise ValueError(f"monthly window {spec!r} needs '1st Sat ...' or 'day 15 ...'")
    elif cadence == "daily" and (nth or dom):
        raise ValueError(f"daily window {spec!r} cannot name a day of the month")
    return Window(cadence, dows, nth, dom, a, b, text)


@dataclass(frozen=True)
class Freeze:
    """A freeze: a recurring clock window ('18:00-23:30', 'Fri,Sat 18:00-24:00') or inclusive local dates."""
    name: str
    text: str
    win: Window | None = None
    first: date | None = None
    last: date | None = None


def parse_freeze(name: str, spec: str) -> Freeze:
    """'18:00-23:30' | 'Fri 17:00-24:00' | '2026-12-24' | '2026-12-24..2027-01-02' (inclusive local dates)."""
    m = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2})\s*(?:(?:\.\.|/|to)\s*(\d{4}-\d{2}-\d{2}))?\s*", str(spec))
    if m:
        a = date.fromisoformat(m[1])
        b = date.fromisoformat(m[2]) if m[2] else a
        if b < a:
            raise ValueError(f"freeze {spec!r} ends before it starts")
        return Freeze(name, str(spec).strip(), None, a, b)
    return Freeze(name, " ".join(str(spec).split()), parse_window(None, spec))


def freeze_intervals(freezes: Iterable[Freeze], tz, t0: float, t1: float) -> list[tuple[float, float, str]]:
    """Freeze intervals [a, b) (epoch) overlapping [t0, t1], each with the freeze's name."""
    out: list[tuple[float, float, str]] = []
    d0 = datetime.fromtimestamp(t0, tz).date() - timedelta(days=2)
    d1 = datetime.fromtimestamp(t1, tz).date() + timedelta(days=1)
    for f in freezes:
        if f.win is not None:
            out += [(o.start, o.end, f.name) for o in f.win.occurrences(tz, d0, d1) if o.end > t0 and o.start < t1]
        elif f.first and f.last:
            a, b = _ts(f.first, 0, tz), _ts(f.last + timedelta(days=1), 0, tz)
            if b > t0 and a < t1:
                out.append((a, b, f.name))
    return sorted(out)


def freeze_active(rc: "RoutineConfig", now: float) -> str:
    """'' when no freeze is active at `now`, else why: the CONF_DIR/FREEZE file, or the name of the freeze window / dated freeze.
    The one definition RunGuard uses for check-tier restarts and for manual runs."""
    if (core.CONF_DIR / "FREEZE").exists():
        return "FREEZE file present"
    for a, b, name in freeze_intervals(rc.freezes, rc.tz, now - 1, now + 1):
        if a <= now < b:
            return f"freeze window {name}"
    return ""


def _subtract(a: float, b: float, cuts: Iterable[tuple]) -> list[tuple[float, float]]:
    """[a, b) minus every [c0, c1): the part of a window that is not frozen."""
    segs = [(a, b)]
    for c0, c1, *_ in sorted(cuts):
        nxt = []
        for s0, s1 in segs:
            if c1 <= s0 or c0 >= s1:
                nxt.append((s0, s1))
                continue
            if c0 > s0:
                nxt.append((s0, c0))
            if c1 < s1:
                nxt.append((c1, s1))
        segs = nxt
    return [s for s in segs if s[1] > s[0]]


def host_tz(name: str | None = None):
    """(tzinfo, name): the configured IANA zone, else $TZ, else /etc/localtime's zone, else UTC (last resort)."""
    cands = [name, os.environ.get("TZ", "").lstrip(":")]
    try:
        m = re.search(r"zoneinfo/(.+)$", os.path.realpath("/etc/localtime"))
        cands.append(m.group(1) if m else None)
    except OSError:
        pass
    for c in cands:
        if c:
            try:
                return ZoneInfo(c), c
            except Exception:  # noqa: BLE001 - a typo or missing tzdata must not crash the runner
                continue
    return timezone.utc, "UTC"


# =========================================================================== configuration (etc/routine.toml)
@dataclass
class StepDef:
    name: str                          # as written in routine.toml
    task: str                          # registered task it resolves to
    depends_on: list[str] = field(default_factory=list)    # earlier step names of the same routine
    gates: list[str] | None = None     # busy gates consulted before a disruptive run (None = default_gates)
    disruptive: bool | None = None     # None = derive: C1 in apply mode needs a quiet window
    canary_runs: int | None = None
    enabled: bool = True


@dataclass
class Entry:
    name: str
    cadence: str
    window: Window
    steps: list[StepDef]
    depends_on: list[str] = field(default_factory=list)    # routines that must be settled first (same date)
    canary: bool = True
    post_check: list[str] = field(default_factory=list)


@dataclass
class RoutineConfig:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    tz: Any = timezone.utc
    tzname: str = "UTC"
    enforce: bool = True
    max_attempts: int = 3
    default_gates: list[str] = field(default_factory=lambda: ["backup"])
    max_pressure_level: int = 2         # the "pressure" gate is busy at this spike level or above
    post_check_timeout_s: float = 120.0
    canary_runs: int = 1
    canary_fraction: float = 0.10
    canary_relax_after: int = 1         # canary runs throttled by the caps (nothing applied) before the byte cap is normal again
    restart_tasks: list = field(default_factory=lambda: list(RESTART_TASKS))   # [continuous] disruptive
    frozen_rungs: list = field(default_factory=lambda: ["restart", "emergency"])   # spike-ladder rungs held by a freeze
    emergency_level: int = 4            # at this pressure level a freeze no longer holds the ladder's destructive rungs
    tick_unit: str = TICK_UNIT
    windows: dict = field(default_factory=dict)            # cadence -> spec text, as exported
    freeze: dict = field(default_factory=dict)             # name -> spec text(s), as exported
    freezes: list[Freeze] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    system: list[dict] = field(default_factory=list)       # known system jobs for the calendar
    steps_opts: dict = field(default_factory=dict)         # [steps.<name>] options of the built-in steps

    def steps_for(self, task: str) -> list[tuple[Entry, StepDef]]:
        return [(e, s) for e in self.entries for s in e.steps if s.task == task]


def resolve_step(name: str, cadence: str) -> str:
    t = BUILTIN_STEPS.get(name, name)
    if name == "report" and cadence not in ("daily", "weekly"):
        raise ValueError("there is no monthly report task")
    return t.format(c=cadence) if "{c}" in t else t


def _strs(v: Any) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def load_config(path: Path | None = None) -> RoutineConfig:
    """Parse routine.toml. Invalid pieces are dropped and listed in .errors; a missing/unparsable file, an unreadable
    freeze or no usable routine is valid=False, which makes RunGuard refuse every apply (fail closed) and due() empty."""
    p = Path(path) if path else core.CONF_DIR / "routine.toml"

    def invalid(msg: str) -> RoutineConfig:
        tz, name = host_tz()                      # even a broken config must show times in the host zone
        return RoutineConfig(valid=False, errors=[msg], tz=tz, tzname=name)

    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError:
        return invalid(f"{p.name} not found")
    except (OSError, ValueError) as exc:
        return invalid(_safe(f"{p.name}: {exc}", 200))
    rc = RoutineConfig()
    # [[routine]] is an array of tables, so TOML forbids a [routine] table: the globals live under [settings]
    g = raw["settings"] if isinstance(raw.get("settings"), dict) else {}
    rc.tz, rc.tzname = host_tz(g.get("timezone"))
    if g.get("timezone") and rc.tzname != g.get("timezone"):
        rc.errors.append(f"timezone {g.get('timezone')!r} unknown, using {rc.tzname}")
    rc.enforce = bool(g.get("enforce", True))
    rc.max_attempts = max(1, int(_num(g.get("max_attempts"), 3) or 3))
    rc.default_gates = _strs(g.get("default_gates")) if "default_gates" in g else ["backup"]
    rc.max_pressure_level = max(1, int(_num(g.get("max_pressure_level"), 2) or 2))
    rc.post_check_timeout_s = max(5.0, _num(g.get("post_check_timeout_s"), 120.0) or 120.0)
    cn = raw["canary"] if isinstance(raw.get("canary"), dict) else {}
    rc.canary_runs = max(0, int(_num(cn.get("runs"), 1) or 0))
    rc.canary_fraction = min(1.0, max(0.01, _num(cn.get("fraction"), 0.10) or 0.10))
    rc.canary_relax_after = max(0, int(_num(cn.get("relax_after"), 1) or 0))
    ct = raw["continuous"] if isinstance(raw.get("continuous"), dict) else {}
    if "disruptive" in ct:
        rc.restart_tasks = _strs(ct["disruptive"])
    if "frozen_rungs" in ct:
        rc.frozen_rungs = _strs(ct["frozen_rungs"])
    rc.emergency_level = min(5, max(1, int(_num(ct.get("emergency_level"), 4) or 4)))
    rc.tick_unit = str(g.get("tick_unit") or TICK_UNIT)
    wins = {k: str(v) for k, v in (raw["windows"] if isinstance(raw.get("windows"), dict) else {}).items()
            if isinstance(v, str)}
    rc.windows = {**DEFAULT_WINDOWS, **wins}
    fr = raw["freeze"] if isinstance(raw.get("freeze"), dict) else {}
    for name, spec in fr.items():
        specs = [spec] if isinstance(spec, str) else _strs(spec)
        rc.freeze[name] = spec if isinstance(spec, str) else specs
        for s in specs:
            try:
                rc.freezes.append(parse_freeze(name, s))
            except ValueError as exc:
                rc.valid = False                     # a freeze we cannot read must not silently disappear
                rc.errors.append(_safe(f"freeze {name}: {exc}", 160))
    seen: list[str] = []
    for i, e in enumerate(raw["routine"] if isinstance(raw.get("routine"), list) else []):
        ent = _parse_entry(e, i, rc, rc.windows, seen)
        if ent:
            rc.entries.append(ent)
            seen.append(ent.name)
    if not rc.entries:
        rc.valid = False
        rc.errors.append("no usable [[routine]] entries")
    rc.system = [s for s in raw.get("system", []) if isinstance(s, dict)]
    rc.steps_opts = raw["steps"] if isinstance(raw.get("steps"), dict) else {}
    return rc


def _parse_entry(e: Any, i: int, rc: RoutineConfig, wins: dict, earlier: list[str]) -> Entry | None:
    if not isinstance(e, dict):
        rc.errors.append(f"routine #{i + 1}: not a table")
        return None
    name, cad = str(e.get("name", "")).strip(), str(e.get("cadence", "")).strip()

    def bad(msg: str) -> None:
        rc.errors.append(_safe(f"routine {name or '#' + str(i + 1)}: {msg}", 200))

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or name in earlier:
        bad("missing, invalid or duplicate name")
        return None
    if cad not in CADENCES:
        bad(f"cadence must be one of {CADENCES}")
        return None
    wkey = str(e.get("window", cad))
    try:
        win = parse_window(cad, wins.get(wkey, wkey))
    except ValueError as exc:
        bad(str(exc))
        return None
    deps = _strs(e.get("depends_on"))
    if any(d not in earlier for d in deps):
        bad("depends_on must name routines defined earlier in the file")
        return None
    steps: list[StepDef] = []
    for s in e.get("steps", []) if isinstance(e.get("steps"), list) else []:
        d = {"task": s} if isinstance(s, str) else (s if isinstance(s, dict) else {})
        sname = str(d.get("task", "")).strip()
        try:
            task_name = resolve_step(sname, cad)
        except ValueError as exc:
            bad(f"step {sname}: {exc}")
            continue
        sdeps = _strs(d.get("depends_on"))
        if not re.fullmatch(r"[A-Za-z0-9_]{1,48}", sname) or sname in [x.name for x in steps] \
                or any(x not in [y.name for y in steps] for x in sdeps):
            bad(f"step {sname!r}: invalid, duplicated or depends on a later step")
            continue
        steps.append(StepDef(sname, task_name, sdeps, _strs(d["gates"]) if "gates" in d else None,
                             d["disruptive"] if isinstance(d.get("disruptive"), bool) else None,
                             int(d["canary_runs"]) if isinstance(d.get("canary_runs"), int)
                             and not isinstance(d.get("canary_runs"), bool) else None,
                             bool(d.get("enabled", True))))
    if not steps:
        bad("no valid steps")
        return None
    return Entry(name, cad, win, steps, deps, bool(e.get("canary", True)), _strs(e.get("post_check")))


# =========================================================================== state (the routine's own bookkeeping)
def _state_path() -> Path:
    return core.STATE_DIR / "routine-state.json"


def _fresh_state() -> dict:
    return {"v": 1, "steps": {}, "canary": {}, "pre": {}, "halted": {}, "acks": {}}


def load_state() -> dict:
    st = core.read_json(_state_path(), None)
    base = _fresh_state()
    if isinstance(st, dict):
        for k in base:
            if isinstance(st.get(k), type(base[k])):
                base[k] = st[k]
    return base


@contextlib.contextmanager
def _locked(name: str):
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.STATE_DIR / name, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def update_state(fn: Callable[[dict], None], now: float | None = None) -> dict:
    """Atomic read-modify-write of routine-state.json (tier runs and the routine tick may overlap)."""
    with _locked("routine.lock"):
        st = load_state()
        fn(st)
        cutoff = _now(now) - 30 * 86400
        for k in ("pre", "halted"):
            st[k] = {a: b for a, b in st[k].items() if not isinstance(b, dict) or (_num(b.get("ts"), 0) or 0) >= cutoff}
        core.write_json_atomic(_state_path(), st, 0o644)
    return st


def save_state(state: dict) -> None:
    """Replace the whole state (tests and the CLI; normal code uses update_state)."""
    update_state(lambda st: (st.clear(), st.update(state)))


def mark_run(state: dict, entry: str, step: str, occ: str, now: float, status: str, summary: str = "",
             applied: bool = False, needs_applied: bool = False, max_attempts: int = 3, note: str = "") -> dict:
    """Record one run of a step in `state` (in place). done = it ran and said ok/info/warn/crit and, for a step that only
    counts when it really changed something (an apply-mode cleaner), it was applied. `skipped` from a read-only step is
    done too (nothing to retry: the data is not there). error counts attempts and gives up after `max_attempts`;
    skipped from an apply-mode step (a gate said busy) and a `deferred` decision only count deferrals."""
    key = f"{entry}/{step}"
    rec = state["steps"].get(key)
    if not isinstance(rec, dict) or rec.get("occ") != occ:
        rec = {"occ": occ, "attempts": 0, "deferrals": 0, "done": False, "final": False}
    rec.update(last_run=now, status=status, summary=_safe(summary), applied=bool(applied), note=_safe(note, 120))
    if status in OK_STATUS and (applied or not needs_applied):
        rec["done"] = True
    elif status == "skipped" and not needs_applied:
        rec["done"], rec["note"] = True, rec["note"] or "skipped by the task"
    elif status == "error":
        rec["attempts"] += 1
        rec["final"] = rec["attempts"] >= max_attempts
    elif status in OK_STATUS:
        rec["note"] = rec["note"] or "report only: apply not permitted"
    else:
        rec["deferrals"] += 1
    state["steps"][key] = rec
    return rec


# =========================================================================== plan / due
@dataclass
class Step:
    """One step as the engine sees it at `now` (what plan() returns, what export() serialises)."""
    routine: str
    cadence: str
    name: str
    task: str
    title: str = ""
    klass: str = "?"
    mode: str = "unavailable"
    disruptive: bool = False       # needs a quiet window (inside it, not frozen)
    state: str = "scheduled"       # done due waiting blocked halted frozen missed failed scheduled paused disabled unavailable continuous
    reason: str = ""
    occ: str = ""
    window_start: float | None = None
    window_end: float | None = None
    run_at: float | None = None
    next_due: float | None = None
    last_run: float | None = None
    last_outcome: str = "never"
    last_summary: str = ""
    in_window: bool = False
    frozen: bool = False
    gates: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


CLOSING_TASKS = ("routine_verify_", "report_")           # the steps that summarise a routine: they run last (verify, report)
_LOADED = False


def _load_tasks() -> None:
    """Import homelab_maint.tasks.* (registers the tasks); idempotent. cli.load_tasks() does the same."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        import importlib
        import pkgutil
        from . import tasks as pkg
        for m in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg.__name__}.{m.name}")
        importlib.import_module("homelab_maint.reports")        # report_daily/report_weekly live outside tasks/
    except Exception as exc:  # noqa: BLE001
        print(f"routine: task import failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _task_info(name: str, mcfg: dict) -> tuple[str, str, str, bool]:
    """(class, mode, title, registered)."""
    t = core.REGISTRY.get(name)
    if t is None:
        return "?", "unavailable", name, False
    if t.klass == "C0":
        return "C0", "check", t.title, True
    if t.klass == "C2":
        return "C2", "plan", t.title, True
    mode = ((mcfg.get("tasks", {}) or {}).get(name, {}) or {}).get("mode", "report")
    return "C1", "apply" if mode == "apply" else "report", t.title, True


def _restart_kind(rc: "RoutineConfig", mcfg: dict, task: str) -> bool:
    """A check-tier task whose apply restarts a user-facing service: [continuous] disruptive in routine.toml (default
    RESTART_TASKS) or `disruptive = true` in its [tasks.<name>] of maint.toml. A freeze holds it to report-only."""
    return task in rc.restart_tasks or ((mcfg.get("tasks", {}) or {}).get(task) or {}).get("disruptive") is True


def _needs_window(sd: StepDef, klass: str, mode: str, exists: bool) -> bool:
    if sd.disruptive is not None:
        return sd.disruptive
    return True if not exists else (klass == "C1" and mode == "apply")     # unknown task: fail closed


def _evaluate(rc: RoutineConfig, now: float, state: dict, mcfg: dict | None = None) -> list[Step]:
    """Every step of every routine at `now`. Pure: no state is written, nothing runs."""
    if not rc.valid:
        return []
    _load_tasks()
    mcfg = mcfg if mcfg is not None else core.load_config()
    tz, freeze_file = rc.tz, (core.CONF_DIR / "FREEZE").exists()
    out: list[Step] = []
    by_entry: dict[str, list[Step]] = {}
    for e in rc.entries:
        occ, nxt = e.window.latest(tz, now), e.window.next(tz, now)
        ivs: list[tuple[float, float]] = []
        if occ:
            ivs = _subtract(occ.start, occ.end, freeze_intervals(rc.freezes, tz, occ.start, occ.end))
        in_win = bool(occ and occ.start <= now < occ.end)
        in_eff = in_win and not freeze_file and any(a <= now < b for a, b in ivs)
        frozen = in_win and not in_eff
        later = [a for a, _b in ivs if a > now] if not freeze_file else []
        halted = state["halted"].get(f"{e.name}@{occ.id}") if occ else None
        wait_for = _routine_wait(rc, e, occ, by_entry, now, state)
        mine: dict[str, Step] = {}
        for sd in e.steps:
            klass, mode, title, exists = _task_info(sd.task, mcfg)
            nw = _needs_window(sd, klass, mode, exists)
            s = Step(e.name, e.cadence, sd.name, sd.task, title, klass, mode, nw, occ=occ.id if occ else "",
                     gates=list(sd.gates if sd.gates is not None else (rc.default_gates if nw else [])),
                     after=list(sd.depends_on), in_window=in_eff, frozen=frozen)
            rec = state["steps"].get(f"{e.name}/{sd.name}")
            same = bool(isinstance(rec, dict) and occ and rec.get("occ") == occ.id)
            if isinstance(rec, dict):
                s.last_run, s.last_outcome = _num(rec.get("last_run")), str(rec.get("status", "never"))
                s.last_summary = str(rec.get("summary", ""))
                if s.last_outcome == "deferred" or (s.last_outcome == "skipped" and rec.get("deferrals") and not rec.get("done")):
                    s.last_outcome = "deferred"
            mine[sd.name] = s
            out.append(s)
            nxt_start = nxt.start if nxt else None
            s.next_due = nxt_start
            if not sd.enabled:
                s.state, s.reason = "disabled", "disabled in routine.toml"
            elif not exists:
                s.state, s.reason = "unavailable", "task not registered (module not installed yet?)"
            elif (mcfg.get("tasks", {}).get(sd.task) or {}).get("enabled", True) is False:
                s.state, s.reason = "disabled", "disabled in maint.toml"      # cmd_run never selects it: it must not hold verify/report up
            elif core.REGISTRY[sd.task].tier == "check":      # a continuous check: the check tier runs it, never the routine
                s.state, s.reason, s.next_due = "continuous", "runs with the check tier, not window-governed", None
                if _restart_kind(rc, mcfg, sd.task):                  # ... but a restart waits out a freeze
                    s.reason, s.frozen = "runs with the check tier; report-only while a freeze is active", bool(freeze_active(rc, now))
            elif occ is None:
                s.state, s.reason = "scheduled", "no window in the look-back range"
            elif same and rec.get("done"):
                s.state, s.reason = "done", f"done for the {occ.id} window"
            elif same and rec.get("final"):
                s.state, s.reason = "failed", f"gave up after {rec.get('attempts')} failed attempts"
            elif nw and core.paused(sd.task):
                s.state, s.reason = "paused", "kill switch (PAUSE) present: apply refused"
            else:
                ok = True
                if nw:
                    if in_eff:
                        s.reason = f"inside the window until {_fmt(occ.end, tz, '%H:%M')}"
                    elif frozen:
                        why = "FREEZE file present" if freeze_file else "freeze window"
                        ok = False
                        if later:
                            s.state, s.run_at, s.next_due = "frozen", later[0], later[0]
                            s.reason = f"{why}: waits until {_fmt(later[0], tz, '%H:%M')}"
                        elif freeze_file and now < occ.end:
                            s.state, s.next_due = "frozen", None
                            s.reason = f"{why}: remove it to proceed (window ends {_fmt(occ.end, tz, '%H:%M')})"
                        else:
                            s.state, s.reason = "missed", f"{why} covers the rest of the window"
                    else:
                        ok = False
                        s.state, s.reason = "missed", "window over; disruptive steps never run late"
                else:
                    s.reason = "in window" if in_win else f"catch-up: the {occ.id} window is over"
                if ok:
                    s.state = "due"
                    why = _block_reason(sd, mine, wait_for, halted, nw, closing=sd.task.startswith(CLOSING_TASKS), window_open=in_win)
                    if why:
                        s.state, s.reason = why
                    else:
                        s.run_at = max(now, occ.start)
            if s.state in ACTIVE + ("blocked", "halted", "paused") and occ:
                s.window_start, s.window_end = occ.start, occ.end
                if s.state in ("due", "waiting"):
                    s.next_due = now
                elif s.state in ("blocked", "halted", "paused"):
                    s.next_due = nxt_start
            elif nxt:
                s.window_start, s.window_end = nxt.start, nxt.end
        by_entry[e.name] = list(mine.values())
    return out


def _block_reason(sd: StepDef, mine: dict[str, Step], wait_for: str, halted: dict | None, nw: bool,
                  closing: bool = False, window_open: bool = False) -> tuple[str, str] | None:
    """(state, reason) when a step that is otherwise due must wait or is blocked, else None. A CLOSING step (verify, report)
    also waits for every earlier step of its routine that can still run, while the window is open: a step that was deferred
    (busy gate), is being retried after an error or has not had its turn yet would otherwise be missing from the summary."""
    if halted and nw:
        return "halted", f"halted: post-check failed after {halted.get('by')}"
    for d in sd.depends_on:
        dep = mine.get(d)
        if dep is None or dep.state in ("done", "due"):
            continue                                 # a due dependency runs first (steps are ordered)
        if dep.state in ("failed", "halted", "blocked", "paused"):
            return "blocked", f"depends on {d} ({dep.state})"
        if dep.state in ("waiting", "frozen"):
            return "waiting", f"waiting for {d}"
        if dep.state == "unavailable" and nw:
            return "blocked", f"depends on {d} (task not registered)"
        # missed / scheduled / disabled / unavailable (read-only dependent): the dependency can no longer run, go on
    if closing and window_open:
        for n, dep in mine.items():
            if n == sd.name:
                break                                    # only the steps before it
            if dep.state in ACTIVE:
                return "waiting", f"waiting for {n} to finish"
    if wait_for and nw:                                  # a read-only step never waits for ANOTHER routine
        return "waiting", wait_for
    return None


def _routine_wait(rc: RoutineConfig, e: Entry, occ: Occ | None, by_entry: dict, now: float, state: dict) -> str:
    """'' or why this routine's DISRUPTIVE steps wait for the routines it depends_on (same local date: weekly after daily;
    _block_reason lets read-only steps ignore it, so a weekly timer that fires before a delayed daily one loses nothing).
    It waits only for steps of the other routine that have not had their turn yet: a step that was attempted and deferred
    (a busy gate) does not hold up a one-shot weekly run for the rest of the window."""
    if not occ:
        return ""
    for dn in e.depends_on:
        dep = next((x for x in rc.entries if x.name == dn), None)
        if dep is None:
            continue
        do = dep.window.occurrence(date.fromisoformat(occ.id), rc.tz)
        if do is None or now >= do.end:
            continue                                  # no such window that day, or it is over
        if now < do.start:
            return f"waiting for the {dn} window ({_fmt(do.start, rc.tz, '%H:%M')})"
        for s in by_entry.get(dn, []):
            tried = (state["steps"].get(f"{dn}/{s.name}") or {}).get("occ") == do.id
            if s.state in ACTIVE and not tried:
                return f"waiting for {dn} to finish"
    return ""


def plan(now: float | None = None, state: dict | None = None, cfg: RoutineConfig | None = None,
         mcfg: dict | None = None) -> list[Step]:
    """What will run when: every routine step with its window/freeze/catch-up decision (see Step.state)."""
    rc = cfg or load_config()
    return _evaluate(rc, _now(now), state if state is not None else load_state(), mcfg)


def due_steps(now: float | None = None, state: dict | None = None, cfg: RoutineConfig | None = None,
              mcfg: dict | None = None) -> list[Step]:
    return [s for s in plan(now, state, cfg, mcfg) if s.state == "due"]


def due(now: float | None = None, state: dict | None = None, cfg: RoutineConfig | None = None,
        mcfg: dict | None = None) -> list[str]:
    """Task names due right now, in routine order, each once. Idempotent: it reads, it never records. A step whose
    prerequisite is also in this list is included (steps are ordered); steps that must wait or are blocked are not."""
    seen: list[str] = []
    for s in due_steps(now, state, cfg, mcfg):
        if s.task not in seen:
            seen.append(s.task)
    return seen


# =========================================================================== change log
def changes_path() -> Path:
    return core.STATE_DIR / "changes.jsonl"


def _level(v: Any) -> int | None:
    """Level of a status word ('ok'..'crit'); None for anything else."""
    return LEVELS.get(v) if isinstance(v, str) else None


def _not_worse(before: Any, after: Any) -> bool | None:
    """True/False when `before`/`after` are comparable snapshots (status words, or {check: status} dicts), else None."""
    if isinstance(before, dict) and isinstance(after, dict):
        pairs = [(_level(before.get(k)), _level(v)) for k, v in after.items()]
        pairs = [(a, b) for a, b in pairs if b is not None]
        return None if not pairs else all(a is not None and b <= a for a, b in pairs)
    a, b = _level(before), _level(after)
    return None if a is None or b is None else b <= a


def record_change(task: str, kind: str, detail: str, before: Any = None, after: Any = None, *, bytes: int | None = None,
                  outcome: str = "done", verified: bool | str | None = None, now: float | None = None) -> dict:
    """Append one line to STATE_DIR/changes.jsonl: {"ts","task","kind","detail","bytes","outcome","verified"} (+ short
    "before"/"after" snapshots when given). `before`/`after` are state snapshots: a status word, a {check: status} dict
    (then `verified` defaults to "nothing got worse") or a number (then `bytes` defaults to |after - before|).
    verified=NA ("n/a") writes null: there was no post-change window to verify in (a task that runs all day).
    Single O_APPEND write, so concurrent writers never interleave. Returns the record. Raises nothing."""
    t = _now(now)
    if bytes is None and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (before, after)) \
            and before is not None and after is not None:
        bytes = int(abs(after - before))
    if verified is None:
        verified = bool(_not_worse(before, after))
    elif verified == NA:
        verified = None
    rec: dict[str, Any] = {"ts": round(t, 3), "task": _safe(task, 48), "kind": kind if kind in KINDS else "maintenance",
                           "detail": _safe(detail, 200), "bytes": max(int(bytes or 0), 0), "outcome": _safe(outcome, 40),
                           "verified": None if verified is None else bool(verified)}
    for k, v in (("before", before), ("after", after)):
        if v is not None:
            rec[k] = _safe(json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v, 80)
    line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(changes_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"routine: cannot write the change log: {exc}", file=sys.stderr)
    return rec


def read_changes(limit: int = MAX_CHANGES, since: float = 0.0) -> list[dict]:
    """Newest first, at most `limit`, as exported (the internal `_t` key removed). `verified` is true, false, or null (not
    applicable: a continuous task has no post-change window); a record without the key reads as false (fail closed)."""
    rows = [r for r in _jsonl(changes_path(), since, keys=("ts",)) if r.get("_t") is not None]
    rows.sort(key=lambda r: r["_t"], reverse=True)
    out = []
    for r in rows[:limit]:
        out.append({"ts": r["_t"], "task": _safe(r.get("task", "?"), 48),
                    "kind": r.get("kind") if r.get("kind") in KINDS else "maintenance",
                    "detail": _safe(r.get("detail", ""), 200), "bytes": int(_num(r.get("bytes"), 0) or 0),
                    "outcome": _safe(r.get("outcome", ""), 40),
                    "verified": None if ("verified" in r and r["verified"] is None) else bool(r.get("verified"))})
    return out


def _audit_done(task: str, since: float) -> list[dict]:
    """Audit records of real mutations ("done") by `task` since `since` (the proof a run actually changed something)."""
    return [r for r in _jsonl(core.LOG_DIR / "audit.jsonl", since - 1, tail_bytes=1024 * 1024)
            if r.get("task") == task and r.get("outcome") == "done"]


# =========================================================================== gates, post-check, canary
def _ext_busy(name: str) -> tuple[bool, str]:
    """tasks.gates.busy with a fail-closed wrapper (any import/probe error means busy). Tests patch this."""
    try:
        from .tasks import gates
        b, why = gates.busy(name)
        return bool(b), _safe(why, 120)
    except Exception as exc:  # noqa: BLE001
        return True, f"gate error: {type(exc).__name__}"


def _gate_level(m: Any) -> float | None:
    """The level maintenance gates on, from a pressure_state `metrics` dict: its `gate_level` (memory and cpu only), else max(mem, cpu)
    of its dims, else `level` (scheduler.gate_level_of, the one reader). io-only or gpu-only pressure ("nobody waits on io") must
    not defer the routine: this host holds io PSI high for hours every night. None = unusable."""
    m = m if isinstance(m, dict) else {}
    try:
        from .scheduler import gate_level_of
        got = gate_level_of(m)
        return None if got is None else float(got[0])
    except Exception:  # noqa: BLE001 - the scheduler module is optional here: fall back to the host level (the stricter reading)
        return _num(m.get("level"))


def _pressure_gate(limit: int, now: float) -> tuple[bool, str]:
    """Busy when the spike manager's current level is >= limit (admission control: no maintenance during a spike).
    No status / no pressure_state task = module not installed = idle; present but stale, errored or unreadable = busy."""
    p = core.STATE_DIR / "status.json"
    if not p.exists():
        return False, "no pressure data yet"
    st = core.read_json(p, None)
    if not isinstance(st, dict):
        return True, "status.json unreadable"
    e = (st.get("tasks") or {}).get("pressure_state")
    if not isinstance(e, dict):
        return False, "no pressure data (pressure_state not installed)"
    age = now - (_num(e.get("last_run"), 0) or 0)
    lvl = _gate_level(e.get("metrics"))
    if e.get("status") == "error" or lvl is None:
        return True, "pressure_state errored"
    if age > 3600:
        return True, f"pressure data {age / 60:.0f} min old"
    return lvl >= limit, f"pressure level {int(lvl)}"


def pressure_level(now: float) -> int | None:
    """The spike manager's current level from status.json, or None when it is unknown (no data, errored, older than an hour)."""
    st = core.read_json(core.STATE_DIR / "status.json", None)
    e = ((st.get("tasks") or {}) if isinstance(st, dict) else {}).get("pressure_state")
    if not isinstance(e, dict) or e.get("status") == "error":
        return None
    lvl = _gate_level(e.get("metrics"))
    return None if lvl is None or now - (_num(e.get("last_run"), 0) or 0) > 3600 else int(lvl)


def gate_busy(name: str, rc: RoutineConfig | None = None, now: float | None = None) -> tuple[bool, str]:
    """(busy, why). 'pressure' is answered here (status.json); every other name goes to tasks.gates (fail closed)."""
    if name == "pressure":
        return _pressure_gate((rc or RoutineConfig()).max_pressure_level, _now(now))
    return _ext_busy(name)


def _lvl(status: Any, alert: bool = True) -> int:
    """Severity of a result for comparisons. alert=False findings never count; an unknown word is crit (fail closed)."""
    return 0 if not alert else LEVELS.get(status, 2) if isinstance(status, str) else 2


def snapshot(names: Iterable[str]) -> dict[str, str]:
    """{check: status word} from status.json (alert=False findings read as 'info'); checks without an entry are absent."""
    st = core.read_json(core.STATE_DIR / "status.json", {}) or {}
    tasks = st.get("tasks") if isinstance(st, dict) and isinstance(st.get("tasks"), dict) else {}
    out = {}
    for n in names:
        e = tasks.get(n)
        if isinstance(e, dict) and isinstance(e.get("status"), str):
            out[n] = e["status"] if e.get("alert", True) else "info"
    return out


def post_check(names: Iterable[str], pre: dict | None = None, cfg: dict | None = None, timeout_s: float = 120.0
               ) -> tuple[bool, str]:
    """Re-run the named C0 checks through the registry and compare with the pre-state (`pre` = {check: status}, default:
    the statuses status.json holds now). (True, detail) only if every check ran and none got worse. Fail closed: an
    unknown name, a non-C0 task (never run here), an error, a timeout or a warn/crit with no baseline is NOT ok."""
    names = list(dict.fromkeys(n for n in names if isinstance(n, str)))
    if not names:
        return True, "no checks named"
    _load_tasks()
    mcfg = cfg if cfg is not None else core.load_config()
    pre = pre if isinstance(pre, dict) else snapshot(names)
    t0, bad, good = time.monotonic(), [], 0
    # run_task() arms and then cancels SIGALRM for itself: park the CALLER's alarm (a task's own timeout) and re-arm it after
    parked = signal.alarm(0)
    try:
        good = _post_check_loop(names, pre, mcfg, timeout_s, t0, bad)
    finally:
        if parked:
            signal.alarm(max(1, parked - int(time.monotonic() - t0)))
    return (not bad), _safe("; ".join(bad) if bad else f"{good}/{len(names)} checks unchanged or better", 200)


def _post_check_loop(names: list[str], pre: dict, mcfg: dict, timeout_s: float, t0: float, bad: list[str]) -> int:
    good = 0
    for n in names:
        t = core.REGISTRY.get(n)
        if t is None:
            bad.append(f"{n}: unknown check")
        elif t.klass != "C0":
            bad.append(f"{n}: not a C0 check, refused")
        elif time.monotonic() - t0 > timeout_s:
            bad.append(f"{n}: not run (time budget)")
        else:
            try:
                res, _dur = core.run_task(t, mcfg, False)
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{n}: {type(exc).__name__}")
                continue
            new, old = _lvl(res.status, res.alert), pre.get(n)
            if res.status == "error":
                bad.append(f"{n}: error")
            elif old is None and new > 0:
                bad.append(f"{n}: {res.status}, no baseline")
            elif old is not None and new > _lvl(old):
                bad.append(f"{n}: {old}->{res.status}")
            else:
                good += 1
    return good


def canary_caps(task: str, state: dict | None = None, cfg: dict | None = None, rc: RoutineConfig | None = None
                ) -> dict | None:
    """Reduced caps for a task's first N apply runs that really changed something (default 1 run at 10 % caps), or None
    once it has graduated (or canary is off for its routine). {"max_gib_per_run": float, "max_items_per_run": int}; the
    base is the task's own cap, else the global [caps], so the numbers can only be LOWER than a normal run's (RunGuard.task_cfg
    lowers, never overwrites, what the task uses). A canary run whose caps let nothing through (a single action bigger than
    the byte cap, like one 12 GiB build-cache prune under a 3 GiB canary) is counted as throttled by after(); after
    [canary] relax_after such runs (default 1) the byte cap is the normal one again and only the item count stays small,
    so a cleaner with big single actions is slowed for one run, never starved for good."""
    return _canary(task, state, cfg, rc)[0]


def _canary(task: str, state: dict | None, cfg: dict | None, rc: RoutineConfig | None) -> tuple[dict | None, bool]:
    """(caps, relaxed): canary_caps() plus whether the byte cap is already back to normal (relax_after reached)."""
    rc = rc or load_config()
    runs, steps = rc.canary_runs, rc.steps_for(task)
    if steps:
        e, s = steps[0]
        runs = 0 if not e.canary else (s.canary_runs if s.canary_runs is not None else rc.canary_runs)
    st = state if state is not None else load_state()
    c = st.get("canary", {}).get(task) or {}
    if runs <= 0 or int(_num(c.get("runs"), 0) or 0) >= runs:
        return None, False
    mcfg = cfg if cfg is not None else core.load_config()
    tcfg, caps = (mcfg.get("tasks", {}) or {}).get(task, {}) or {}, mcfg.get("caps", {}) or {}
    gib = _num(tcfg.get("max_gib_per_run", caps.get("max_gib_per_run", 40)), 40.0) or 40.0
    items = _num(tcfg.get("max_items_per_run", caps.get("max_items_per_run", 500)), 500.0) or 500.0
    relaxed = rc.canary_relax_after > 0 and int(_num(c.get("throttled"), 0) or 0) >= rc.canary_relax_after
    return {"max_gib_per_run": round(gib if relaxed else gib * rc.canary_fraction, 4),
            "max_items_per_run": max(1, int(items * rc.canary_fraction))}, relaxed


def _throttled(res: Result) -> bool:
    """True when a result says the per-run caps held something back: the cleaners report `oversize` / `deferred` / `capped`
    metrics, core.run_task turns a CapExceeded into 'cap reached: ...'."""
    m = res.metrics or {}
    return any((_num(m.get(k), 0) or 0) > 0 for k in ("oversize", "deferred")) or m.get("capped") is True \
        or bool(re.search(r"over cap|deferred by cap|cap reached", res.summary or ""))


# =========================================================================== RunGuard: the cli.cmd_run adapter
@dataclass
class Decision:
    """begin()'s answer for one task run."""
    run: bool = True                 # False: do not run the task now (leave its last result alone)
    apply: bool = True               # False: run it, but it may not mutate (the runner passes apply=False)
    caps: dict | None = None         # canary caps to lower [tasks.<task>] / [caps] with (RunGuard.task_cfg)
    canary_relaxed: bool = False     # the canary's byte cap is already the normal one (relax_after reached): nothing left to throttle
    overrides: dict = field(default_factory=dict)  # [tasks.<task>] keys forced for this run (a frozen ladder's rungs -> "report")
    reason: str = ""
    steps: list = field(default_factory=list)      # [(routine, step, occurrence, disruptive)] this run satisfies
    started: float = 0.0
    manual: bool = False             # the owner's `run --task` (is_manual): the window and the done-state do not stop it
    overridden: bool = False         # a manual run that went ahead although the step was not due: logged as "override"
    applying: bool = False           # the run may really mutate (guard allows it AND the task is in apply mode)
    would_block: str = ""            # advisory mode (enforce=false): what would have been blocked


def is_manual(a: Any) -> bool:
    """Is this `run --task X` the OWNER's override (window and done-state skipped), as opposed to a timer, the scheduler or the
    routine tick starting the same command? Yes when --override or --force was given, or stdin is a terminal; never without
    --task, never when the caller says it is scheduled (`scheduled` attribute or HOMELAB_MAINT_SCHEDULED in the environment).
    The scheduler and systemd start jobs with stdin = /dev/null, so they are held to every window/freeze rule without any
    change to how they build the command line. cli glue: `guard.begin(t, apply, manual=routine.is_manual(a), force=...)`."""
    if not getattr(a, "task", None) or getattr(a, "scheduled", False) or os.environ.get("HOMELAB_MAINT_SCHEDULED"):
        return False
    if getattr(a, "override", False) or getattr(a, "force", False):
        return True
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


class RunGuard:
    """Window / freeze / gate / canary / post-check gatekeeper for cli.cmd_run (see the module docstring).

        guard = routine.RunGuard()
        for t in guard.order(selected):
            d = guard.begin(t, apply, manual=routine.is_manual(a), force=bool(getattr(a, "force", False)))
            if not d.run:
                continue                                     # not this task's moment: keep its last result
            res, dur = run_task(t, guard.task_cfg(cfg, t, d), apply and d.apply)
            guard.after(t, d, res, dur)
    """

    def __init__(self, rc: RoutineConfig | None = None, now_fn: Callable[[], float] | None = None,
                 mcfg: dict | None = None):
        self.rc = rc or load_config()
        self.now = now_fn or time.time
        self._mcfg = mcfg

    def mcfg(self) -> dict:
        if self._mcfg is None:
            self._mcfg = core.load_config()
        return self._mcfg

    def order(self, tasks: Iterable[Task]) -> list[Task]:
        """Routine order for managed daily/weekly/monthly tasks (so `verify` and `report` run last), then everything else
        the way cmd_run sorts it (class, name: spike_sampler before stuck_detector)."""
        pos: dict[str, tuple[int, int]] = {}
        for ei, e in enumerate(self.rc.entries):
            for si, s in enumerate(e.steps):
                pos.setdefault(s.task, (ei, si))
        rank = {"C0": 0, "C1": 1, "C2": 2}

        def key(t: Task):
            if t.tier != "check" and t.name in pos:
                return (0, *pos[t.name], t.name)
            return (1, rank.get(t.klass, 3), 0, t.name)
        return sorted(tasks, key=key)

    def _mode_apply(self, t: Task) -> bool:
        return t.klass == "C1" and (self.mcfg().get("tasks", {}).get(t.name, {}) or {}).get("mode", "report") == "apply"

    def begin(self, t: Task, apply: bool, manual: bool = False, force: bool = False) -> Decision:
        """Decide whether/how `t` runs now. `manual` = the owner's override (is_manual); `force` (only meaningful with manual)
        also overrides a freeze and a halt. Never raises: an unforeseen error means "run, but apply nothing" (C0 tasks are
        read-only anyway), so a bug here can degrade maintenance but never block the checks or mutate unchecked."""
        try:
            return self._begin(t, apply, bool(manual), bool(manual and force))
        except Exception as exc:  # noqa: BLE001
            print(f"routine: guard error for {t.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return Decision(run=True, apply=False, reason=f"guard error ({type(exc).__name__}): apply refused", started=self.now())

    def _begin(self, t: Task, apply: bool, manual: bool, force: bool) -> Decision:
        now, rc = self.now(), self.rc
        d = Decision(started=now, manual=manual, apply=not core.paused(t.name))          # PAUSE always wins, even over a manual run
        want = bool(apply) and self._mode_apply(t)                # the task is asked to mutate (PAUSE is d.apply)
        if t.tier == "check":                       # continuous tasks (checks, the spike ladder) are never window-governed
            d.applying, d.reason = want and d.apply, "continuous task: not window-governed"
            if d.applying:
                self._freeze_continuous(t, d, now, force)
            return d
        if not rc.valid:
            d.apply = t.klass == "C0"
            d.reason = "" if t.klass == "C0" else "routine.toml missing or invalid: apply refused"
            return d
        st = load_state()
        mine = [s for s in _evaluate(rc, now, st, self.mcfg()) if s.task == t.name]
        if not mine:                                              # not part of any routine
            d.apply = t.klass == "C0"
            d.reason = "" if t.klass == "C0" else "not in routine.toml: report only"
            return d
        chosen = [s for s in mine if s.state == "due"]
        if not chosen:
            paused = [s for s in mine if s.state == "paused"]
            why = f"{mine[0].state}: {mine[0].reason}"
            if paused and not manual:
                chosen, d.apply, d.reason = paused, False, "PAUSE: report only"
            elif manual or not rc.enforce:
                chosen, d.would_block = mine, ("" if manual else why)
                if manual:
                    d.overridden, d.reason = True, f"manual run ({why})"
            else:
                d.run, d.reason = False, why
                return d
        d.steps = [(s.routine, s.name, s.occ, s.disruptive) for s in chosen if s.occ]
        self._baseline(d, now, st)
        heavy = any(s.disruptive for s in chosen)
        if manual and d.apply and want and heavy:
            # the owner's override skips the window and the done-state, never a freeze or a halt: those take --force
            hold = freeze_active(rc, now) or next((f"halted by {h.get('by')}" for en, _s, occ, _nw in d.steps
                                                   if (h := st["halted"].get(f"{en}@{occ}"))), "")
            if hold and force:
                d.overridden, d.reason = True, d.reason or f"forced past: {hold}"
            elif hold:
                d.apply = False
                d.reason = f"manual run: {hold}; report only (--force overrides)"
        wants_apply = want and d.apply
        if d.apply and heavy and (wants_apply or t.klass != "C1"):
            for g in dict.fromkeys(g for s in chosen if s.disruptive for g in s.gates):
                busy, gwhy = gate_busy(g, rc, now)
                if busy:
                    msg = f"gate {g} busy: {gwhy}"
                    if rc.enforce:
                        self._defer(d, msg, now)
                        d.run, d.reason = False, msg
                        return d
                    d.would_block = d.would_block or msg
        d.applying = wants_apply
        if d.applying:
            d.caps, d.canary_relaxed = _canary(t.name, None, self.mcfg(), rc)
            if d.caps:
                d.reason = d.reason or f"canary caps: {d.caps['max_items_per_run']} item(s), {d.caps['max_gib_per_run']:g} GiB"
        return d

    def _freeze_continuous(self, t: Task, d: Decision, now: float, force: bool) -> None:
        """A check-tier task is never window-governed, but one that RESTARTS a user-facing service waits out a freeze like any
        disruptive step: report-only while the FREEZE file or a freeze window is active. The spike ladder is held rung by
        rung: its destructive rungs (restart, emergency) go to report-only, its reclaim and throttle rungs keep protecting
        people; an emergency (pressure level >= [continuous] emergency_level) lifts the hold. An invalid routine.toml means
        the freeze windows are unknown: hold (fail closed). --force (manual only) overrides."""
        rc = self.rc
        ladder = t.name == PRESSURE_TASK
        if not (ladder or _restart_kind(rc, self.mcfg(), t.name)):
            return
        why = freeze_active(rc, now) or ("" if rc.valid else "routine.toml invalid: freeze unknown")
        if not why:
            return
        if force:                                   # the owner's explicit --force: the run is logged as an override
            d.overridden, d.reason = True, f"{d.reason}; forced past: {why}"
            return
        if not rc.enforce:                          # advisory mode: say what would have been held, hold nothing
            d.would_block = f"freeze: {why}"
            return
        if not ladder:
            d.apply = d.applying = False
            d.reason = f"freeze: report only ({why})"
            return
        lvl = pressure_level(now)
        if lvl is not None and lvl >= rc.emergency_level:
            d.reason += f"; freeze lifted: pressure level {lvl}"
            return
        tcfg = self.mcfg().get("tasks", {}).get(t.name) or {}
        held = {r: "report" for r in rc.frozen_rungs if tcfg.get(r) == "apply"}     # a rung that is off/report stays as it is
        if held:
            d.overrides = held
            d.reason += f"; freeze ({why}): {', '.join(sorted(held))} held to report-only"

    def _defer(self, d: Decision, msg: str, now: float) -> None:
        def fn(st: dict) -> None:
            for e, s, occ, _nw in d.steps:
                mark_run(st, e, s, occ, now, "deferred", msg, needs_applied=True, note=msg)
        update_state(fn, now)

    def _baseline(self, d: Decision, now: float, st: dict) -> None:
        """Statuses of the routine's post-check checks at the first managed step of the occurrence, whatever that step is and
        whether or not it applies: the baseline every later post-check and the verify step compare with, so a check that was
        already warn when the occurrence began is compared with itself, not reported as a regression."""
        ents = {e.name: e for e in self.rc.entries}
        need = [(en, occ) for en, _s, occ, _nw in d.steps if en in ents and f"{en}@{occ}" not in st["pre"]]
        if not need:
            return

        def fn(s: dict) -> None:
            for en, occ in need:
                s["pre"].setdefault(f"{en}@{occ}", {"ts": now, "checks": snapshot(ents[en].post_check)})
        update_state(fn, now)

    def task_cfg(self, cfg: dict, t: Task, d: Decision) -> dict:
        """cfg with the canary caps and the forced keys applied for this one run (a copy; cfg itself is untouched). A canary
        cap only ever LOWERS: where the task has its own explicit limit in [tasks.<task>] it becomes min(own, canary); where
        it uses the global default the canary goes into the copy's [caps], NOT into [tasks.<task>], so a task that applies
        a tighter default of its own when it has no explicit limit (docker_containers_prune: 25 items) still does."""
        if not d.caps and not d.overrides:
            return cfg
        tasks, caps = dict(cfg.get("tasks", {})), dict(cfg.get("caps", {}))
        mine = dict(tasks.get(t.name) or {})
        for key, val in (d.caps or {}).items():
            own = _num(mine.get(key)) if key in mine else None
            if key in mine and own is not None:
                mine[key] = val if val < own else mine[key]               # never raise the task's own limit
            elif key in mine:
                mine[key] = val                                           # an unreadable own limit: the canary replaces it
            else:
                base = _num(caps.get(key))
                caps[key] = val if base is None or val < base else caps[key]
        mine.update(d.overrides)
        tasks[t.name] = mine
        return {**cfg, "tasks": tasks, "caps": caps}

    def after(self, t: Task, d: Decision, res: Result, dur: float = 0.0) -> dict | None:
        """Book-keeping after the run: step state, canary count, and for a run that really changed something the change
        log record, the post-check and (on regression) the halt. Returns the change record or None. Never raises."""
        try:
            return self._after(t, d, res)
        except Exception as exc:  # noqa: BLE001 - a bookkeeping failure must not fail the tier run
            print(f"routine: bookkeeping error for {t.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None

    def _after(self, t: Task, d: Decision, res: Result) -> dict | None:
        now, rc = self.now(), self.rc
        acts = _audit_done(t.name, d.started) if d.applying else []
        changed = bool(d.applying and (acts or res.reclaimed_bytes))
        throttled = bool(d.caps and d.applying and not changed and not d.canary_relaxed and _throttled(res))   # the canary let nothing through
        rec, halt_why = None, ""
        if changed:
            if t.tier == "check":
                verified = NA                       # runs all day: no post-change window, so "not applicable", never "unverified"
            else:
                ents = {e.name: e for e in rc.entries}
                names = list(dict.fromkeys(n for en, *_ in d.steps for n in (ents[en].post_check if en in ents else [])))
                pre: dict = {}
                for en, _s, occ, _nw in d.steps:
                    pre.update((load_state()["pre"].get(f"{en}@{occ}") or {}).get("checks") or {})
                ok, why = post_check(names, pre, self.mcfg(), rc.post_check_timeout_s) if names \
                    else (False, "no post-check configured")
                verified = bool(names) and ok
                if names and not ok:
                    core.audit("routine", "post-check", t.name, 0, f"regressed: {why}"[:160])
                    halt_why = why
            kind = CHANGE_KIND.get(t.name) or _kind_from(acts)
            nbytes = res.reclaimed_bytes or int(sum(_num(a.get("bytes"), 0) or 0 for a in acts))
            outcome = "partial" if res.status == "error" else "override" if d.overridden else "done"
            rec = record_change(t.name, kind, res.summary, bytes=nbytes, outcome=outcome, verified=verified, now=now)
        if throttled:                                # say so where the owner looks: the summary and the metrics of the status
            res.metrics["canary"] = "throttled"
            res.summary = _safe(res.summary[:117].rstrip() + "; canary-throttled", 140)
        mode_apply = self._mode_apply(t)

        def fn(st: dict) -> None:
            for en, sn, occ, _nw in d.steps:
                mark_run(st, en, sn, occ, now, res.status, res.summary, applied=d.applying,
                         needs_applied=mode_apply, max_attempts=rc.max_attempts, note=d.reason if d.reason else "")
            if (changed or throttled) and t.tier != "check":          # a continuous task has no canary
                c = st["canary"].setdefault(t.name, {"runs": 0, "first": now})
                if changed:
                    c["runs"], c["last"] = int(c.get("runs", 0)) + 1, now
                else:
                    c["throttled"], c["last_throttle"] = int(c.get("throttled", 0)) + 1, now
            if halt_why:
                for en, _s, occ, _nw in d.steps:
                    st["halted"].setdefault(f"{en}@{occ}", {"ts": now, "by": t.name, "why": _safe(halt_why, 160)})
        update_state(fn, now)
        return rec


def record_approved(task: str, res: Result, now: float | None = None) -> dict:
    """Glue for `homelab-maint approve`: log the applied C2 plan in the change log and verify it with the first routine's
    post-check checks (compared with status.json as it was before the change). Returns the change record."""
    rc = load_config()
    names = next((e.post_check for e in rc.entries if e.post_check), []) if rc.valid else []
    ok, _why = post_check(names, None, None, rc.post_check_timeout_s) if names else (False, "no post-check configured")
    return record_change(task, CHANGE_KIND.get(task, "cleanup"), res.summary, bytes=res.reclaimed_bytes, outcome="approved",
                         verified=bool(names) and ok, now=now)


def _kind_from(acts: list[dict]) -> str:
    words = " ".join(str(a.get("action", "")) for a in acts).lower()
    if "restart" in words or "stop" in words:
        return "restart"
    if "update" in words or "-set" in words or "set " in words:
        return "config"
    return "cleanup"


# =========================================================================== tick: execute whatever is due
def _cli_runner(task: str, apply: bool) -> int:
    """Run ONE task through cli.cmd_run (tier lock, status.json merge, alert state: all reused)."""
    from . import cli
    # scheduled=True: this is the routine's own tick, not the owner typing `run --task`: the guard must still check the window
    return int(cli.cmd_run(argparse.Namespace(tier="check", task=task, apply=apply, dry_run=not apply, scheduled=True)))


def run_due(apply: bool = False, now_fn: Callable[[], float] = time.time, runner: Callable[[str, bool], int] | None = None,
            cfg: RoutineConfig | None = None) -> list[dict]:
    """Execute the due steps one by one, re-evaluating after each (a post-check halt or a busy gate takes effect for the
    next step). Each step runs through `runner` (default: cli.cmd_run for that task). With the RunGuard glue in cmd_run
    the guard has already marked the step; without it the step is marked here from status.json. Returns one row per
    attempt: {"step","task","rc","state"}. A step is attempted at most once per call (the next tick retries)."""
    rc = cfg or load_config()
    tried: set[tuple[str, str]] = set()
    rows: list[dict] = []
    run = runner or _cli_runner
    for _ in range(len(rc.entries) * 64 + 8):
        todo = [s for s in due_steps(now_fn(), None, rc) if (s.routine, s.name) not in tried]
        if not todo:
            break
        s = todo[0]
        tried.add((s.routine, s.name))
        t0 = now_fn()
        try:
            code = run(s.task, apply)
        except Exception as exc:  # noqa: BLE001 - one broken task must not stop the tick
            code = 255
            print(f"routine: {s.task}: {type(exc).__name__}: {exc}", file=sys.stderr)
        _mark_from_status(s, t0, now_fn(), rc.max_attempts)
        rec = load_state()["steps"].get(f"{s.routine}/{s.name}") or {}
        same = rec.get("occ") == s.occ
        rows.append({"step": f"{s.routine}/{s.name}", "task": s.task, "rc": code,
                     "state": "done" if same and rec.get("done") else (rec.get("status") if same else None) or "not run"})
    return rows


def _mark_from_status(s: Step, t0: float, now: float, max_attempts: int = 3) -> None:
    """If nothing marked the step during the run (cmd_run has no RunGuard glue), take the outcome from status.json."""
    rec = load_state()["steps"].get(f"{s.routine}/{s.name}")
    if isinstance(rec, dict) and rec.get("occ") == s.occ and (_num(rec.get("last_run"), 0) or 0) >= t0 - 1:
        return
    e = ((core.read_json(core.STATE_DIR / "status.json", {}) or {}).get("tasks") or {}).get(s.task)
    if not isinstance(e, dict) or (_num(e.get("last_run"), 0) or 0) < t0 - 1:
        return                                                    # the task did not run (tier lock, disabled): retry later
    update_state(lambda st: mark_run(st, s.routine, s.name, s.occ, now, str(e.get("status", "error")), str(e.get("summary", "")),
                                     applied=e.get("mode") == "apply", needs_applied=(s.klass == "C1" and s.mode == "apply"),
                                     max_attempts=max_attempts), now)


# =========================================================================== export: routine.json + 14-day calendar
def system_timers() -> dict[str, dict]:
    """{unit: {"next": epoch|None, "last": epoch|None}} from `systemctl list-timers --all --output=json` (epoch
    microseconds there). {} when systemctl is unavailable: the calendar then shows the nominal times."""
    r = sh(["systemctl", "list-timers", "--all", "--output=json", "--no-pager"], timeout=20)
    if r.returncode != 0:
        return {}
    try:
        rows = json.loads(r.stdout)
    except ValueError:
        return {}
    out: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("unit"), str):
            nx, ls = _num(row.get("next")), _num(row.get("last"))
            out[row["unit"]] = {"next": nx / 1e6 if nx else None, "last": ls / 1e6 if ls else None}
    return out


def _hhmm(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def calendar(rc: RoutineConfig, now: float, timers: dict | None = None, days: int = CAL_DAYS) -> list[dict]:
    """[{"date","items":[{"time","title","kind","source"}]}] for `days` local days from today: the routine windows plus the
    known system jobs ([[system]] in routine.toml). The first upcoming run of a system job whose `unit` is a live systemd
    timer takes systemd's real time (random delay included); later runs show the nominal time."""
    tz = rc.tz
    d0 = datetime.fromtimestamp(now, tz).date()
    dN = d0 + timedelta(days=days - 1)
    cal: dict[date, list[dict]] = {d0 + timedelta(days=i): [] for i in range(days)}

    def add(d: date, time_s: str, title: str, kind: str, source: str, **extra: Any) -> None:
        if d in cal:
            cal[d].append({"time": time_s, "title": _safe(title, 90), "kind": kind, "source": source, **extra})

    for e in rc.entries:
        for o in e.window.occurrences(tz, d0, dN):
            add(date.fromisoformat(o.id), _hhmm(o.start, tz),
                f"{e.cadence.capitalize()} routine {_hhmm(o.start, tz)}-{_hhmm(o.end, tz)} ({len(e.steps)} steps)",
                e.cadence, "routine", steps=len(e.steps))
    for s in rc.system:
        title, kind = str(s.get("title") or s.get("name") or "?"), s.get("kind") if s.get("kind") in ("backup", "system") else "system"
        source = str(s.get("managed", "os"))
        specs = [s["at"]] if isinstance(s.get("at"), str) else _strs(s.get("at"))
        mine: dict[date, list[dict]] = {}
        for spec in specs:
            try:
                w = parse_window(None, spec, point=True)
            except ValueError:
                continue
            for o in w.occurrences(tz, d0, dN):
                item = {"time": _hhmm(o.start, tz), "title": _safe(title, 90), "kind": kind, "source": source, "_ts": o.start}
                mine.setdefault(date.fromisoformat(o.id), []).append(item)
        actual = _num(((timers or {}).get(str(s.get("unit"))) or {}).get("next"))
        if actual and actual > now and d0 <= datetime.fromtimestamp(actual, tz).date() <= dN:
            ad = datetime.fromtimestamp(actual, tz).date()
            near = min(mine.get(ad, []), key=lambda i: abs(i["_ts"] - actual), default=None)
            if near is None:
                near = {"title": _safe(title, 90), "kind": kind, "source": source}
                mine.setdefault(ad, []).append(near)
            near.update(time=_hhmm(actual, tz), _ts=actual)
        for d, items in mine.items():
            for it in items:
                it.pop("_ts", None)
                if d in cal:
                    cal[d].append(it)
    for f in rc.freezes:
        if f.first and f.last and f.last >= d0 and f.first <= dN:
            add(max(f.first, d0), "00:00", f"Change freeze: {f.name} (until {f.last.isoformat()})", "system", "routine")
    return [{"date": d.isoformat(), "items": sorted(items, key=lambda i: (i["time"], i["title"]))}
            for d, items in sorted(cal.items())]


def _status_tasks() -> dict:
    st = core.read_json(core.STATE_DIR / "status.json", {}) or {}
    return st.get("tasks") if isinstance(st, dict) and isinstance(st.get("tasks"), dict) else {}


def export(now: float | None = None, cfg: RoutineConfig | None = None, state: dict | None = None, mcfg: dict | None = None,
           timers: dict | None = None) -> dict:
    """The public routine.json (see SPEC3 S1). Never raises on missing inputs: an invalid routine.toml gives
    valid=false with the errors and empty routines. Strings are redacted (ASCII, no URLs with queries, no e-mails)."""
    t = _now(now)
    rc = cfg or load_config()
    st = state if state is not None else load_state()
    steps = _evaluate(rc, t, st, mcfg)
    tasks = _status_tasks()
    routines = []
    for e in rc.entries:
        ss = [s for s in steps if s.routine == e.name]
        rows, counts = [], {}
        for s in ss:
            last, outcome = s.last_run, s.last_outcome
            ent = tasks.get(s.task)
            if isinstance(ent, dict) and (_num(ent.get("last_run"), 0) or 0) > (last or 0):
                last, outcome = _num(ent.get("last_run")), str(ent.get("status", outcome))
            counts[s.state] = counts.get(s.state, 0) + 1
            rows.append({"task": s.task, "name": s.name, "title": _safe(s.title, 40), "class": s.klass, "mode": s.mode,
                         "state": s.state, "reason": _safe(s.reason, 120), "disruptive": s.disruptive,
                         "next_due": s.next_due, "last_run": last, "last_outcome": _safe(outcome, 20)})
        occ, nxt = e.window.latest(rc.tz, t), e.window.next(rc.tz, t)
        active = [s.next_due for s in ss if s.state in ("due", "waiting", "frozen") and s.next_due is not None]
        routines.append({"name": e.name, "cadence": e.cadence, "window": e.window.text, "occurrence": occ.id if occ else None,
                         "window_start": occ.start if occ else None, "window_end": occ.end if occ else None,
                         "counts": counts, "steps": rows, "next_run": min(active) if active else (nxt.start if nxt else None),
                         "next_window": [nxt.start, nxt.end] if nxt else None})
    doc: dict[str, Any] = {
        "schema": 1, "generated_at": t, "timezone": rc.tzname, "valid": rc.valid, "enforce": rc.enforce,
        "errors": [_safe(x, 160) for x in rc.errors[:5]], "windows": dict(rc.windows),
        "freeze": {k: v for k, v in rc.freeze.items()},
        "state": {"paused": core.paused(), "freeze_file": (core.CONF_DIR / "FREEZE").exists(),
                  "halted": sorted(k for k in st.get("halted", {}))[:10],
                  "canary": {k: int((v or {}).get("runs", 0)) for k, v in sorted(st.get("canary", {}).items())},
                  "canary_throttled": {k: int((v or {}).get("throttled", 0)) for k, v in sorted(st.get("canary", {}).items())
                                       if (v or {}).get("throttled")}},
        "routine": routines, "changes": read_changes(MAX_CHANGES), "calendar": calendar(rc, t, timers),
    }
    while len(json.dumps(doc)) > 190_000 and doc["changes"]:       # SPEC2: every public file < 200 KB; drop the oldest changes
        doc["changes"] = doc["changes"][: len(doc["changes"]) // 2]
    return doc


def write_export(path: Path | None = None, **kw: Any) -> Path:
    """export() -> STATE_DIR/public/routine.json (atomic, 0644). Returns the path."""
    p = Path(path) if path else core.STATE_DIR / "public" / "routine.json"
    if kw.get("timers") is None:
        kw["timers"] = system_timers()
    core.write_json_atomic(p, export(**kw), 0o644)
    return p


# =========================================================================== built-in steps (registered tasks)
# All read-only (C0) except routine_rotate. They summarise and VERIFY; none of them installs, restarts or deletes anything.
# Options come from [tasks.<name>] in maint.toml, else [steps.<short name>] in routine.toml (see etc/routine.toml).
class _O:
    """option lookup: maint.toml [tasks.<name>] wins over routine.toml [steps.<name>]"""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        short, so = next((k for k, v in BUILTIN_STEPS.items() if v == ctx.name), ctx.name), load_config().steps_opts
        self.base = so.get(short) or so.get(ctx.name) or {}

    def __call__(self, key: str, default: Any = None) -> Any:
        v = self.ctx.opt(key, None)
        return v if v is not None else self.base.get(key, default)


def _tz():
    """The routine's zone (routine.toml [settings] timezone, else the host zone)."""
    return load_config().tz


def _res(status: str, summary: str, metrics: dict | None = None, items: list | None = None, alert: bool = False) -> Result:
    return Result(status, _safe(summary, 140), metrics or {}, (items or [])[:12], alert=alert)


def _text(p: Path | str, limit: int = 1_000_000) -> str | None:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


_SAMPLE_T = re.compile(rb'^\{"t":\s*([0-9.]+)')


def _sample_times(since: float, tail_bytes: int = 16 * 1024 * 1024) -> list[float]:
    """Timestamps of spike_sampler's samples (STATE_DIR/samples.jsonl) newer than `since`; no JSON parsing needed."""
    out: list[float] = []
    try:
        with open(core.STATE_DIR / "samples.jsonl", "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - tail_bytes))
            lines = f.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        m = _SAMPLE_T.match(ln)
        if m and float(m[1]) >= since:
            out.append(float(m[1]))
    return out


def _anon_by_day(now: float, tz, days: int = 14) -> dict[str, float]:
    """{local date: mean total container anon memory in GiB} from the guard samples (the memory baseline)."""
    sums: dict[str, list[float]] = {}
    for r in _jsonl(core.STATE_DIR / "samples.jsonl", now - days * 86400, tail_bytes=24 * 1024 * 1024, keys=("t",)):
        c = r.get("c")
        if r.get("_t") is None or not isinstance(c, dict):
            continue
        tot = sum(_num(v.get("anon"), 0) or 0 for v in c.values() if isinstance(v, dict))
        sums.setdefault(datetime.fromtimestamp(r["_t"], tz).date().isoformat(), []).append(tot / GIB)
    return {d: sum(v) / len(v) for d, v in sorted(sums.items()) if v}


# --------------------------------------------------------------------------- daily: spike review
@register_task("routine_spike_review", klass="C0", tier="daily", title="Spike review", timeout=60)
def spike_review(ctx: Ctx) -> Result:
    """Last 24 h of spikes (pressure_state's ledger) and whether the sampler kept up. Reviews, never pages."""
    since = ctx.now - 86400
    spikes = {}
    for r in _jsonl(core.STATE_DIR / "spikes.jsonl", since, keys=("t",)):
        if r.get("kind") == "spike" and r.get("_t") is not None:
            spikes[r.get("id", r["_t"])] = r                    # the latest line per spike id wins
    sp = sorted(spikes.values(), key=lambda r: r["_t"])
    harmed = [r for r in sp if r.get("restarted") or r.get("stopped") or (_num(r.get("oom_kills"), 0) or 0) > 0]
    worst = max([int(_num(r.get("peak_level"), 0) or 0) for r in sp], default=0)
    times = _sample_times(since)
    expect = min(96.0, (ctx.now - min(times)) / 900) if times else 0.0
    cover = round(100 * len(times) / expect) if expect >= 8 else None
    who: dict[str, int] = {}
    for r in sp:
        for c in (r.get("contributors") or [])[:3]:
            n = c.get("name") if isinstance(c, dict) else None
            if isinstance(n, str):
                who[n] = who.get(n, 0) + 1
    items = [{"time": _fmt(r["_t"], _tz(), "%a %H:%M"), "level": int(_num(r.get("peak_level"), 0) or 0),
              "minutes": round((_num(r.get("duration_s"), 0) or 0) / 60), "outcome": _safe(r.get("outcome", "?"), 80)} for r in sp[-12:]]
    gap = cover is not None and cover < 60
    if not sp:
        s = "no spikes in 24 h"
    else:
        s = f"{len(sp)} spike(s) in 24 h, worst L{worst}, " + ("a restart/stop/OOM happened" if harmed else "nothing killed")
    s += "; sampler " + ("has no samples" if not times else f"{len(times)} samples" + (f" ({cover}% of expected)" if cover is not None else ""))
    status = "warn" if (harmed or gap or not times) else "info" if sp else "ok"
    return _res(status, s, {"spikes_24h": len(sp), "worst_level": worst, "harmed": len(harmed), "samples_24h": len(times),
                            "sampler_cover_pct": cover if cover is not None else -1, "top": ",".join(sorted(who, key=who.get, reverse=True)[:3])}, items)


# --------------------------------------------------------------------------- verify (per cadence)
def _verify(ctx: Ctx, cadence: str) -> Result:
    """After the cleanups: did every step of this cadence's routine finish, did the post-check checks stay as good as they were
    when the occurrence began (REGRESSIONS only: a check that was already warn is compared with itself), and is every change
    logged in this occurrence verified (a change with verified = null, by a continuous task, is not applicable)?"""
    rc = load_config()
    if not rc.valid:
        return _res("warn", f"cannot verify: routine.toml {rc.errors[0] if rc.errors else 'invalid'}", alert=True)
    ents = [e for e in rc.entries if e.cadence == cadence]
    st = load_state()
    steps = [s for s in _evaluate(rc, ctx.now, st, ctx.cfg)           # the work: not the closing steps (verify, report) themselves
             if s.cadence == cadence and not s.task.startswith(CLOSING_TASKS)]
    c = {k: sum(1 for s in steps if s.state == k) for k in ("done", "due", "waiting", "failed", "halted", "blocked", "missed", "unavailable")}
    names = list(dict.fromkeys(n for e in ents for n in e.post_check))
    pre: dict = {}
    since = ctx.now
    for e in ents:
        occ = e.window.latest(rc.tz, ctx.now)
        if occ:
            since = min(since, occ.start)
            pre.update((st["pre"].get(f"{e.name}@{occ.id}") or {}).get("checks") or {})
    base = snapshot(names)                       # no stored baseline (the host was off, a fresh install): the standing state
    base.update(pre)                             # ... else the statuses taken when the occurrence began
    ok, why = post_check(names, base, ctx.cfg, rc.post_check_timeout_s) if names else (True, "no post-check configured")
    unverified = [x for x in read_changes(50, since) if x["verified"] is False]
    pending = sum(1 for s in steps if s.state in ACTIVE)
    problems = []
    if not ok:
        problems.append(f"post-check: {why}")
    if c["failed"] or c["halted"] or c["blocked"]:
        problems.append(f"{c['failed']} failed, {c['halted']} halted, {c['blocked']} blocked step(s)")
    if unverified:
        problems.append(f"{len(unverified)} change(s) not verified")
    note = f"{c['done']}/{len(steps)} steps done" + (f", {c['missed']} missed (window passed)" if c["missed"] else "") \
        + (f", {pending} still pending" if pending else "") + (f", {c['unavailable']} not installed" if c["unavailable"] else "")
    items = [{"step": f"{s.routine}/{s.name}", "state": s.state, "why": _safe(s.reason, 80)} for s in steps if s.state not in ("done",)][:12]
    status = "warn" if problems else ("info" if c["missed"] or c["unavailable"] or pending else "ok")
    return _res(status, ("verify " + cadence + ": " + "; ".join(problems) + f" ({note})") if problems else f"verify {cadence} ok: {note}; {why}",
                {"done": c["done"], "steps": len(steps), "missed": c["missed"], "failed": c["failed"] + c["halted"],
                 "unverified": len(unverified), "post_check_ok": bool(ok)}, items, alert=bool(problems))


def _make_verify(cadence: str) -> None:
    register_task(f"routine_verify_{cadence}", klass="C0", tier=cadence, title=f"Verify {cadence} routine", timeout=240)(
        lambda ctx: _verify(ctx, cadence))


for _c in CADENCES:
    _make_verify(_c)


# --------------------------------------------------------------------------- weekly: capacity review
@register_task("routine_capacity", klass="C0", tier="weekly", title="Capacity review", timeout=90)
def capacity_review(ctx: Ctx) -> Result:
    """Weekly capacity checkpoint from what the checks already measured: days to full, biggest growers, memory baseline."""
    tasks = _status_tasks()
    mounts = [m for m in ((tasks.get("disk_forecast") or {}).get("metrics") or {}).get("mounts") or [] if isinstance(m, dict) and not m.get("info")]
    mounts.sort(key=lambda m: (_num(m.get("days")) if _num(m.get("days")) is not None else 1e9, -(_num(m.get("used_pct"), 0) or 0)))
    recs: list[str] = []
    items = []
    for m in mounts[:12]:
        d = _num(m.get("days"))
        items.append({"mount": _safe(m.get("mount"), 40), "free": _safe(m.get("free_h"), 12), "used_pct": _num(m.get("used_pct"), -1),
                      "days_to_full": d if d is not None else -1, "level": _safe(m.get("level"), 8)})
        if (d is not None and d <= 60) or (_num(m.get("used_pct"), 0) or 0) >= 85 or m.get("level") in ("warn", "crit"):
            recs.append(f"{m.get('mount')}: {m.get('free_h')} free" + (f", about {d:.0f} days to full" if d is not None else ""))
    gw = (tasks.get("growth_watch") or {}).get("metrics") or {}
    if (_num(gw.get("n_over"), 0) or 0) > 0:
        recs.append(f"runaway growth at {gw.get('worst_path')}: {gw.get('worst_gib_day')} GiB/day")
    dm = (tasks.get("docker_df") or {}).get("metrics") or {}
    if (_num(dm.get("images_reclaim_gib"), 0) or 0) >= 20:
        recs.append(f"Docker has {dm.get('images_reclaim_h')} of unused images (docker_images is the cleaner)")
    base = _anon_by_day(ctx.now, _tz(), 14)
    drift = None
    if len(base) >= 2:
        ks = sorted(base)
        drift = base[ks[-1]] - base[ks[0]]
        if drift >= 2.0 and drift >= 0.2 * base[ks[0]]:
            recs.append(f"container memory baseline rose {drift:.1f} GiB over {len(ks)} days: check for a leak")
    tight = mounts[0] if mounts else None
    s = "capacity: " + (f"{len(mounts)} mounts, tightest {tight.get('mount')} {tight.get('free_h')} free" if tight else "no disk data yet")
    if tight and _num(tight.get("days")) is not None:
        s += f" (full in {_num(tight.get('days')):.0f} d)"
    s += f"; memory baseline {drift:+.1f} GiB" if drift is not None else "; memory baseline collecting"
    s += f"; {len(recs)} recommendation(s)" if recs else ""
    for r in recs[:3]:
        items.append({"mount": "recommend", "free": _safe(r, 120), "used_pct": -1, "days_to_full": -1, "level": "info"})
    return _res("warn" if recs else "ok" if mounts else "info", s,
                {"mounts": len(mounts), "recommendations": len(recs), "baseline_drift_gib": round(drift, 2) if drift is not None else -1}, items)


# --------------------------------------------------------------------------- weekly: SMART self-test reminder
def _smart_json(args: list[str]) -> dict | None:
    r = sh(["smartctl", "-n", "standby", *args], timeout=30)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


@register_task("routine_smart_selftest", klass="C0", tier="weekly", title="SMART self-tests", timeout=240, needs_root=True)
def smart_selftest(ctx: Ctx) -> Result:
    """Reminder, never an action: when did each disk last complete a SMART self-test, and does smartd schedule any?
    Sleeping disks are not woken (smartctl -n standby). A FAILED self-test is a real finding and alerts."""
    o = _O(ctx)
    r = sh(["smartctl", "--scan"], timeout=20)
    devs = [ln.split()[0] for ln in r.stdout.splitlines() if ln.startswith("/dev/")] if r.returncode == 0 else []
    if not devs:
        return _res("info", "SMART self-tests: no readable disks (smartctl missing or not root)", {"disks": 0})
    max_age_d = _num(o("max_age_days", 35), 35) or 35
    rows, never, stale, failed, asleep, denied = [], 0, 0, [], 0, 0
    for dev in devs[:24]:
        lg = _smart_json(["-l", "selftest", "-j", dev])
        if lg is None:
            rows.append({"disk": dev, "last_test": "unreadable", "age_d": -1, "result": "?"})
            continue
        sc = lg.get("smartctl") or {}
        if int(_num(sc.get("exit_status"), 0) or 0) & 2 and not (lg.get("ata_smart_self_test_log") or lg.get("nvme_self_test_log")):
            msgs = " ".join(str(m.get("string", "")) for m in sc.get("messages") or [] if isinstance(m, dict)).lower()
            if "standby" in msgs:
                asleep += 1                                         # a sleeping disk is left asleep, not woken for a log
            else:
                denied += 1                                         # not root, or the device cannot be opened
            rows.append({"disk": dev, "last_test": "asleep" if "standby" in msgs else "unreadable", "age_d": -1, "result": "-"})
            continue
        tab = ((lg.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or \
              (lg.get("nvme_self_test_log") or {}).get("table") or []
        tab = [t for t in tab if isinstance(t, dict)]
        bad = [t for t in tab if isinstance(t.get("status"), dict) and t["status"].get("passed") is False]
        if bad:
            failed.append(dev)
        last = next((t for t in tab if isinstance(t.get("status"), dict) and t["status"].get("passed") is True), None)
        if last is None:
            never += 0 if tab else 1
            rows.append({"disk": dev, "last_test": "never" if not tab else "none passed", "age_d": -1,
                         "result": "FAILED" if bad else "no tests"})
            continue
        poh = ((_smart_json(["-A", "-j", dev]) or {}).get("power_on_time") or {}).get("hours")
        age = (poh - last["lifetime_hours"]) / 24 if isinstance(poh, (int, float)) and isinstance(last.get("lifetime_hours"), (int, float)) else None
        stale += bool(age is not None and age > max_age_d)
        rows.append({"disk": dev, "last_test": _safe((last.get("type") or {}).get("string", "?"), 20),
                     "age_d": round(age) if age is not None else -1, "result": "FAILED" if bad else "ok"})
    if denied == len(devs):
        return _res("info", f"SMART self-tests: cannot open any of {len(devs)} disks (not root?)", {"disks": len(devs), "unreadable": denied})
    conf = _text("/etc/smartd.conf") or ""
    sched = any(re.search(r"(^|\s)-s\s", ln) for ln in conf.splitlines() if not ln.lstrip().startswith("#"))
    active = sh(["systemctl", "is-active", "smartd"], timeout=10).stdout.strip() == "active" or \
        sh(["systemctl", "is-active", "smartmontools"], timeout=10).stdout.strip() == "active"
    s = f"SMART self-tests: {len(devs)} disks, {never} never tested" + (f", {stale} older than {max_age_d:.0f} d" if stale else "")
    s += f", {asleep} asleep" if asleep else ""
    s += "; smartd " + ("is not running" if not active else "schedules tests" if sched else "has no -s schedule")
    if never or stale or not sched:
        s += " (reminder: smartctl -t short DEV)"
    if failed:
        s = f"SMART self-test FAILED on {', '.join(failed)}; " + s
    rows.sort(key=lambda x: (x["result"] != "FAILED", x["last_test"] != "never", x["age_d"] if x["age_d"] >= 0 else 1e9))
    return _res("crit" if failed else ("warn" if (never or stale) else "ok"), s,
                {"disks": len(devs), "never": never, "stale": stale, "failed": len(failed), "asleep": asleep,
                 "unreadable": denied, "smartd_active": active, "smartd_schedule": sched}, rows, alert=bool(failed))


# --------------------------------------------------------------------------- weekly: pending updates
@register_task("routine_updates", klass="C0", tier="weekly", title="Pending updates", timeout=120)
def updates_review(ctx: Ctx) -> Result:
    """apt and snap updates waiting, held packages, reboot-required age, newer kernel installed than running. Read-only:
    `apt list --upgradable` and `snap refresh --list` change nothing."""
    ap = sh(["apt", "list", "--upgradable"], timeout=60)
    rows = [ln for ln in ap.stdout.splitlines() if "[upgradable" in ln]
    sec = [ln for ln in rows if "-security" in ln.split(" ", 1)[0]]
    hold = sh(["apt-mark", "showhold"], timeout=20).stdout.split()
    sn = sh(["snap", "refresh", "--list"], timeout=60)
    snaps = [ln.split()[0] for ln in sn.stdout.splitlines()[1:] if ln.strip()] if sn.returncode == 0 else []
    rb = Path("/var/run/reboot-required")
    rb_age = (ctx.now - rb.stat().st_mtime) / 86400 if rb.exists() else None
    run = os.uname().release
    vkey = lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v)]      # noqa: E731
    inst = sorted((p.name[len("vmlinuz-"):] for p in Path("/boot").glob("vmlinuz-*")), key=vkey)
    newer = inst[-1] if inst and vkey(inst[-1]) > vkey(run) else None
    unreadable = ap.returncode != 0 and not rows
    s = "updates: " + ("apt state unreadable" if unreadable else f"{len(rows)} apt ({len(sec)} security)") + f", {len(snaps)} snap"
    s += f", {len(hold)} held" if hold else ""
    s += f"; reboot pending {rb_age:.0f} d" if rb_age is not None else "; no reboot pending"
    s += f" (kernel {newer} installed, running {run})" if newer else ""
    items = [{"kind": "security", "package": _safe(ln.split("/", 1)[0], 40), "detail": _safe(ln.split(" ", 2)[1] if " " in ln else "", 40)} for ln in sec[:8]]
    items += [{"kind": "snap", "package": _safe(n, 40), "detail": ""} for n in snaps[: 12 - len(items)]]
    warn = bool(sec) or (rb_age is not None and rb_age > 14)
    return _res("warn" if warn else "info" if (rows or snaps) else "ok", s,
                {"apt": len(rows), "security": len(sec), "snap": len(snaps), "held": len(hold),
                 "reboot_days": round(rb_age, 1) if rb_age is not None else -1, "kernel_newer": bool(newer)}, items)


# --------------------------------------------------------------------------- weekly: image update review (Diun)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DIUN = re.compile(r"(?P<ts>\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2}) \w+\s+\w+\s+(?P<msg>.*)")


@register_task("routine_image_updates", klass="C0", tier="weekly", title="Image update review", timeout=90)
def image_updates(ctx: Ctx) -> Result:
    """Which container images have newer versions upstream, as Diun (the update watcher already on this host) announced in
    the last 7 days. Never pulls, restarts or updates anything: updating stays the owner's decision."""
    o = _O(ctx)
    name = str(o("diun_container", "diun"))
    st = sh(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"], timeout=20)
    if st.returncode != 0:
        return _res("info", "image updates: docker unavailable", {"watcher": "unknown"})
    if st.stdout.strip() != "running":
        return _res("info", f"image updates: no update watcher ({name}) running; nothing to review",
                    {"watcher": "none" if not st.stdout.strip() else st.stdout.strip()[:12]})
    lg = sh(["docker", "logs", "--since", "168h", name], timeout=40)
    if lg.returncode != 0:
        return _res("info", "image updates: cannot read the Diun log", {"watcher": "unreadable"})
    tz, found, last_scan = _tz(), {}, None
    for raw in (lg.stdout + "\n" + lg.stderr).splitlines():
        m = _DIUN.search(_ANSI.sub("", raw))
        if not m:
            continue
        try:
            when = datetime.strptime(m["ts"], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=tz).timestamp()
        except ValueError:
            continue
        msg = m["msg"]
        if msg.startswith("Jobs completed"):
            last_scan = max(last_scan or 0, when)
        im = re.search(r"image=(\S+)", msg)
        if im and msg.startswith(("Image update found", "New image found")):
            found[re.sub(r"@sha256:\w+", "", im[1]).replace("docker.io/", "").replace("library/", "")] = when
    stale = last_scan is None or ctx.now - last_scan > 36 * 3600
    items = [{"image": _safe(k, 60), "announced": _fmt(v, tz, "%a %d %b %H:%M")} for k, v in sorted(found.items(), key=lambda kv: -kv[1])[:12]]
    s = f"image updates: {len(found)} image(s) with updates announced in 7 d" if found else "image updates: none announced in 7 d"
    s += "; Diun last scan " + (_fmt(last_scan, tz, "%a %H:%M") if last_scan else "not seen") + (" (OVERDUE)" if stale else "")
    s += "; updating is manual" if found else ""
    return _res("warn" if stale else "info" if found else "ok", s, {"updates": len(found), "scan_overdue": stale,
                "last_scan_age_h": round((ctx.now - last_scan) / 3600, 1) if last_scan else -1}, items)


# --------------------------------------------------------------------------- weekly: backup verification
def _zstd_probe(path: Path) -> str:
    """`zstd -t` verdict: ok | corrupt | timeout | missing. A read that stalls (a sleeping or busy backup disk) is NOT corruption:
    rc 124 is retried once with a longer limit and, if it stalls again, reported as "timeout" (unverified), never as "corrupt"."""
    r = sh(["zstd", "-tq", str(path)], timeout=90)
    if r.returncode == 124:
        r = sh(["zstd", "-tq", str(path)], timeout=240)
    return "missing" if r.returncode == 127 else "timeout" if r.returncode == 124 else "ok" if r.returncode == 0 else "corrupt"


def _zstd_ok(path: Path) -> bool | None:
    """True/False from `zstd -t`; None when it cannot say (zstd missing, or the read timed out)."""
    v = _zstd_probe(path)
    return None if v in ("missing", "timeout") else v == "ok"


def _dump_rows(dump_dir: Path, now: float, max_age_d: float, max_mib: float) -> tuple[list[dict], list[str]]:
    """Newest dump per prefix (immich-2026-09-26.sql.zst -> immich): present, recent, non-empty, and (small ones) zstd -t."""
    newest: dict[str, tuple[float, Path, int]] = {}
    try:
        for p in dump_dir.iterdir():
            m = re.match(r"(.+?)-\d{4}-\d{2}-\d{2}", p.name)
            if m and p.is_file():
                s = p.stat()
                if m[1] not in newest or s.st_mtime > newest[m[1]][0]:
                    newest[m[1]] = (s.st_mtime, p, s.st_size)
    except OSError:
        return [], [f"{dump_dir}: unreadable"]
    rows, bad = [], []
    stalled = False                      # one dump timed out twice: the disk is not answering, so do not burn the task budget on the rest
    for k, (mt, p, size) in sorted(newest.items()):
        age = (now - mt) / 86400
        row = {"item": f"dump {k}", "age_d": round(age, 1), "size": human(size), "check": "size"}
        if size < 1024:
            bad.append(f"{k} dump empty")
        elif age > max_age_d:
            bad.append(f"{k} dump {age:.0f} d old")
        elif size <= max_mib * 1024 * 1024:
            v = "missing" if not p.name.endswith(".zst") else "timeout" if stalled else _zstd_probe(p)
            if v == "timeout" and not stalled:
                stalled = True
                bad.append(f"{k} dump unverified: zstd -t timed out (disk stalled?), not corruption")
            row["check"] = {"ok": "zstd -t ok", "corrupt": "CORRUPT", "timeout": "unverified (read timed out)"}.get(v, "not tested")
            if v == "corrupt":
                bad.append(f"{k} dump fails zstd -t")
        rows.append(row)
    return rows, bad


@register_task("routine_backup_verify", klass="C0", tier="weekly", title="Backup verification", timeout=600)
def backup_verify(ctx: Ctx) -> Result:
    """Beyond 'did it run': each backup status says ok with no errors, is recent, its target is mounted with free space, the
    newest database dumps exist and (small ones) pass zstd -t, the restore tooling sits next to the backup."""
    import glob as _glob
    import shutil
    o = _O(ctx)
    tz = _tz()
    bad: list[str] = []
    rows: list[dict] = []
    limits = o("max_age_hours", {"backup-system": 200, "backup-immich": 200}) or {}
    files = sorted(_glob.glob(str(o("status_glob", "/var/log/backup/*-status.json"))))
    for f in files:
        d = core.read_json(Path(f), None)
        if not isinstance(d, dict):
            bad.append(f"{Path(f).name} unreadable")
            continue
        job = str(d.get("job") or Path(f).name.split("-status")[0])
        problems = []
        if d.get("result") != "ok" or (_num(d.get("errors"), 0) or 0) > 0:
            problems.append(f"result {d.get('result')}, {int(_num(d.get('errors'), 0) or 0)} errors")
        try:
            fin = datetime.strptime(str(d.get("finished")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
            age_h = (ctx.now - fin) / 3600
            if age_h > (_num(limits.get(f"backup-{job}"), 200) or 200):
                problems.append(f"finished {age_h / 24:.1f} d ago")
        except ValueError:
            age_h = None
            problems.append("no finish time")
        tgt = str(d.get("target") or "")
        if tgt:
            try:
                free = shutil.disk_usage(tgt).free / 1e9
                if free < (_num(d.get("warn_free_gb"), 0) or 0):
                    problems.append(f"target free {free:.0f} GB below {d.get('warn_free_gb')}")
            except OSError:
                problems.append("target not mounted")
            if d.get("snapshots_kept") is not None and d.get("retain_snapshots") is not None \
                    and (_num(d["snapshots_kept"], 0) or 0) < (_num(d["retain_snapshots"], 0) or 0):
                problems.append(f"{d['snapshots_kept']}/{d['retain_snapshots']} snapshots kept")
            for need in o("restore_files", ["RESTORE.md", "restore-system.sh"]) if job == "system" else []:
                if not (Path(tgt) / need).exists():
                    problems.append(f"{need} missing next to the backup")
        rows.append({"item": f"backup {job}", "age_d": round(age_h / 24, 1) if age_h is not None else -1,
                     "size": _safe(d.get("files_summary", ""), 40), "check": "ok" if not problems else _safe("; ".join(problems), 80)})
        bad += [f"{job}: {p}" for p in problems]
    for dd in _strs(o("dump_dirs", ["/mnt/backup/system/dbdumps"])):
        r2, b2 = _dump_rows(Path(dd), ctx.now, _num(o("dump_max_age_days", 10), 10) or 10, _num(o("integrity_max_mib", 64), 64) or 64)
        rows += r2
        bad += b2
    ok_file = o("stack_last_ok", "/media/SandiskSSD/ai-stack-backups/LAST_OK")
    if ok_file:
        try:
            age_h = (ctx.now - Path(ok_file).stat().st_mtime) / 3600
            rows.append({"item": "stack-backup", "age_d": round(age_h / 24, 1), "size": "", "check": "ok"})
            if age_h > (_num(o("stack_backup_max_hours", 26), 26) or 26):
                bad.append(f"stack-backup last OK {age_h:.0f} h ago")
        except OSError:
            bad.append("stack-backup LAST_OK missing")
    if not files and not rows:
        return _res("info", "backup verification: no backup status files found", {"backups": 0})
    n_ok = sum(1 for r in rows if r["check"] in ("ok", "size", "zstd -t ok"))
    return _res("warn" if bad else "ok", ("backup verification FAILED: " + "; ".join(bad)) if bad else f"backups verified: {len(rows)} item(s) checked, all ok",
                {"checked": len(rows), "ok": n_ok, "problems": len(bad)}, sorted(rows, key=lambda r: r["check"] in ("ok", "size", "zstd -t ok")), alert=bool(bad))


# --------------------------------------------------------------------------- monthly: restore drill checklist
def journal_note(title: str, detail: str = "", now: float | None = None) -> None:
    """Append to STATE_DIR/maintenance-journal.jsonl (manual maintenance the runner cannot see; publish.py exports it)."""
    rec = {"ts": datetime.fromtimestamp(_now(now), timezone.utc).astimezone(_tz()).strftime("%Y-%m-%dT%H:%M:%S%z"),
           "title": _safe(title, 120), "detail": _safe(detail, 300)}
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(core.STATE_DIR / "maintenance-journal.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


@register_task("routine_restore_check", klass="C0", tier="monthly", title="Restore drill checklist", timeout=300)
def restore_check(ctx: Ctx) -> Result:
    """Monthly restore-drill checklist. The automatic items prove the backup is READABLE (tooling next to it, a snapshot
    with files, sample files match the live ones, the smallest database dump passes zstd -t); the manual item is a real
    test restore by the owner, recorded with `homelab-maint routine ack restore_drill "what I restored"`."""
    o = _O(ctx)
    base = Path(str(o("restore_dir", "/mnt/backup/system")))
    rows: list[dict] = []

    def add(item: str, state: str, detail: str) -> None:
        rows.append({"item": item, "state": state, "detail": _safe(detail, 90)})

    miss = [f for f in _strs(o("restore_files", ["RESTORE.md", "restore-system.sh"])) if not (base / f).exists()]
    have = not miss and base.exists()
    add("restore tooling next to the backup", "ok" if have else "FAIL", "present" if have else f"missing: {', '.join(miss) or str(base)}")
    snaps = sorted([p for p in (base / "snapshots").glob("*") if p.is_dir()], key=lambda p: p.stat().st_mtime) if (base / "snapshots").is_dir() else []
    try:
        ok_snap = bool(snaps) and any(snaps[-1].iterdir())
    except OSError:
        ok_snap = False
    add("newest snapshot has files", "ok" if ok_snap else "FAIL", snaps[-1].name if snaps else "no snapshots found")
    same = diff = 0
    for f in _strs(o("sample_files", ["/etc/fstab", "/etc/hostname"])):
        a, b = Path(f), base / "root" / f.lstrip("/")
        try:
            ha, hb = hashlib.sha256(a.read_bytes()[:1 << 20]).hexdigest(), hashlib.sha256(b.read_bytes()[:1 << 20]).hexdigest()
            same, diff = same + (ha == hb), diff + (ha != hb)
        except OSError:
            add(f"sample file {f}", "FAIL", "unreadable in the backup mirror")
    if same or diff:
        add("sample files read back from the mirror", "ok", f"{same} identical, {diff} changed since the backup (normal)")
    dumps = [p for p in (base / "dbdumps").glob("*.zst") if p.is_file()] if (base / "dbdumps").is_dir() else []
    if dumps:
        small = min(dumps, key=lambda p: p.stat().st_size)
        v = _zstd_probe(small)
        add("smallest database dump decompresses", "ok" if v == "ok" else "FAIL" if v == "corrupt" else "manual",
            f"{small.name}: " + {"ok": "zstd -t ok", "corrupt": "zstd -t failed", "timeout": "zstd -t timed out (backup disk stalled?), not a corruption verdict"}.get(v, "zstd not installed"))
    ack = (load_state().get("acks") or {}).get("restore_drill")
    age = (ctx.now - float(ack["ts"])) / 86400 if isinstance(ack, dict) and _num(ack.get("ts")) else None
    limit = _num(o("max_drill_age_days", 180), 180) or 180
    add("test restore by the owner", "ok" if age is not None and age <= limit else "DUE",
        f"last {age:.0f} d ago: {ack.get('note', '')}" if age is not None else "never recorded: restore one file/DB to /tmp and run `routine ack restore_drill`")
    fails = [r for r in rows if r["state"] == "FAIL"]
    due = [r for r in rows if r["state"] == "DUE"]
    s = ("restore drill: " + "; ".join(r["item"] for r in fails) + " FAILED") if fails else \
        ("restore drill: automatic checks ok; test restore " + ("overdue" if age is not None else "never done")) if due else "restore drill: all checks ok, test restore recent"
    return _res("warn" if fails or due else "ok", s, {"auto_failed": len(fails), "drill_age_days": round(age, 1) if age is not None else -1}, rows, alert=bool(fails))


# --------------------------------------------------------------------------- monthly: certificate / key expiry
def _openssl_enddate(path: str) -> float | None:
    r = sh(["openssl", "x509", "-noout", "-enddate", "-in", path], timeout=15)
    m = re.search(r"notAfter=(.+)", r.stdout)
    if r.returncode != 0 or not m:
        return None
    try:
        return datetime.strptime(" ".join(m[1].split()), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


@register_task("routine_expiry", klass="C0", tier="monthly", title="Expiry review", timeout=120)
def expiry_check(ctx: Ctx) -> Result:
    """Things that stop working on a date: TLS certificate files, the Tailscale node key, and dated reminders from
    routine.toml ([[steps.expiry_check.items]]). Warns at `warn_days` (30), critical at `crit_days` (7) or expired."""
    import glob as _glob
    o = _O(ctx)
    warn_d, crit_d = _num(o("warn_days", 30), 30) or 30, _num(o("crit_days", 7), 7) or 7
    found: list[tuple[str, float | None, str]] = []
    r = sh(["tailscale", "status", "--json"], timeout=15)
    try:
        me = (json.loads(r.stdout).get("Self") or {}) if r.returncode == 0 else {}
        ke = me.get("KeyExpiry")
        if ke:
            found.append(("tailscale node key", datetime.fromisoformat(str(ke).replace("Z", "+00:00")).timestamp(), "disable key expiry in the admin console or re-auth"))
        elif me:
            found.append(("tailscale node key", None, "key expiry disabled"))
    except (ValueError, AttributeError):
        pass
    for pat in _strs(o("cert_globs", ["/etc/letsencrypt/live/*/cert.pem", "/etc/coolercontrol/coolercontrol.crt"])):
        for f in sorted(_glob.glob(pat))[:20]:
            found.append((f"cert {Path(f).parent.name if Path(f).name == 'cert.pem' else Path(f).name}", _openssl_enddate(f), "renew the certificate"))
    for it in o("items", []) if isinstance(o("items", []), list) else []:
        d = it.get("date") if isinstance(it, dict) else None
        try:
            ts = datetime.combine(d if isinstance(d, date) else date.fromisoformat(str(d)), dtime(12, 0), tzinfo=_tz()).timestamp()
        except (TypeError, ValueError):
            continue
        found.append((_safe(it.get("name", "item"), 40), ts, _safe(it.get("note", ""), 60)))
    rows, worst, soon = [], 0, []
    for name, ts, hint in found:
        if ts is None:
            rows.append({"item": _safe(name, 40), "expires": "no expiry / unreadable", "days": -1, "level": "info", "hint": ""})
            continue
        days = (ts - ctx.now) / 86400
        if days > 365 * 20:
            rows.append({"item": _safe(name, 40), "expires": "never (placeholder)", "days": -1, "level": "ok", "hint": ""})
            continue
        lvl = 2 if days <= crit_d else 1 if days <= warn_d else 0
        worst = max(worst, lvl)
        if lvl:
            soon.append(f"{name} {'EXPIRED' if days < 0 else f'in {days:.0f} d'}")
        rows.append({"item": _safe(name, 40), "expires": _fmt(ts, _tz(), "%Y-%m-%d"), "days": round(days),
                     "level": ("ok", "warn", "crit")[lvl], "hint": _safe(hint, 60) if lvl else ""})
    rows.sort(key=lambda r: (r["days"] == -1, r["days"]))
    s = ("expiry: " + "; ".join(soon)) if soon else (f"expiry: {len(rows)} item(s) checked, none due within {warn_d:.0f} d" if rows else "expiry: nothing to check")
    return _res(("ok", "warn", "crit")[worst] if rows else "info", s, {"checked": len(rows), "soon": len(soon)}, rows, alert=worst == 2)


# --------------------------------------------------------------------------- monthly: long-term trend review
def _slope_per_day(pts: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (epoch, value) in value units per day; None with fewer than 6 points or under 2 days."""
    if len(pts) < 6 or pts[-1][0] - pts[0][0] < 2 * 86400:
        return None
    n, mx, my = len(pts), sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    return None if den == 0 else sum((p[0] - mx) * (p[1] - my) for p in pts) / den * 86400


@register_task("routine_trends", klass="C0", tier="monthly", title="Long-term trends", timeout=180)
def trend_review(ctx: Ctx) -> Result:
    """Monthly look at the slow movers over the history the runner kept: disk free per mount (GiB/day), the container
    memory baseline, the noisiest checks and what maintenance freed. Says how many days it could actually see."""
    tz = _tz()
    hist = core.read_history(90 * 86400, None)
    span = (ctx.now - min((r.get("t", ctx.now) for r in hist), default=ctx.now)) / 86400
    per: dict[str, list[tuple[float, float]]] = {}
    noisy: dict[str, int] = {}
    for r in hist:
        if r.get("kind") == "disk" and isinstance(r.get("mount"), str) and _num(r.get("free")) is not None and _num(r.get("t")) is not None:
            per.setdefault(r["mount"], []).append((float(r["t"]), float(r["free"])))
        elif r.get("kind") == "task" and r.get("status") in ("warn", "crit", "error") and isinstance(r.get("task"), str):
            noisy[r["task"]] = noisy.get(r["task"], 0) + 1
    rows, recs = [], []
    for mnt, pts in sorted(per.items()):
        sl = _slope_per_day(sorted(pts))
        free = pts[-1][1]
        if sl is None:
            rows.append({"metric": f"free {mnt}", "now": human(free), "per_day": "n/a", "note": "needs 2+ days of data"})
            continue
        rows.append({"metric": f"free {mnt}", "now": human(free), "per_day": f"{sl / GIB:+.2f} GiB", "note": ""})
        if sl < 0 and free / -sl < 90:                                  # sl is bytes per day, so free / -sl is days
            recs.append(f"{mnt}: shrinking {-sl / GIB:.1f} GiB/day, about {free / -sl:.0f} days left")
    base = _anon_by_day(ctx.now, tz, 30)
    if len(base) >= 2:
        ks = sorted(base)
        step = (base[ks[-1]] - base[ks[0]]) / max(1, len(ks) - 1)
        rows.append({"metric": "container memory baseline", "now": f"{base[ks[-1]]:.1f} GiB", "per_day": f"{step:+.2f} GiB",
                     "note": f"{len(ks)} days"})
    for t, n in sorted(noisy.items(), key=lambda kv: -kv[1])[:3]:
        rows.append({"metric": f"noisy check {t}", "now": f"{n} warn/crit runs", "per_day": "", "note": f"in {span:.0f} d"})
    freed = sum(_num(r.get("bytes"), 0) or 0 for r in _jsonl(core.LOG_DIR / "audit.jsonl", ctx.now - 30 * 86400) if r.get("outcome") == "done")
    rows.append({"metric": "freed by maintenance (30 d)", "now": human(freed), "per_day": "", "note": ""})
    s = f"trends over {span:.0f} d of history: " + (f"{len(recs)} concern(s): {recs[0]}" if recs else "no slow-moving problems")
    if span < 3:
        s = f"trends: collecting data ({span:.1f} d of history so far)" + (f"; {recs[0]}" if recs else "")
    elif span < 30:
        s += f" (only {span:.0f} d kept; a 30 d view needs longer retention)"
    return _res("warn" if recs else "ok" if hist else "info", s, {"history_days": round(span, 1), "concerns": len(recs), "freed_30d_h": human(freed)}, rows)


# --------------------------------------------------------------------------- monthly: rotate the tool's own logs (C1)
def _rotate_file(path: Path, cutoff: float, tz) -> int:
    """Move records older than `cutoff` into <dir>/archive/<name>-YYYY-MM.jsonl.gz (gzip members appended) and rewrite the
    file with the rest, atomically. Lines appended while we work are carried over (a window of microseconds remains
    between the final read and os.replace: it is the tool's own log, written by the runner only). Returns bytes moved."""
    data = path.read_bytes()
    keep, old = [], {}
    for ln in data.splitlines(keepends=True):
        try:
            t = _rec_time(json.loads(ln))
        except ValueError:
            t = None
        if t is not None and t < cutoff:
            old.setdefault(datetime.fromtimestamp(t, tz).strftime("%Y-%m"), []).append(ln)
        else:
            keep.append(ln)
    if not old:
        return 0
    arch = path.parent / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    moved = 0
    for month, lines in sorted(old.items()):
        blob = b"".join(l if l.endswith(b"\n") else l + b"\n" for l in lines)
        with gzip.open(arch / f"{path.stem}-{month}.jsonl.gz", "ab") as g:
            g.write(blob)
        moved += len(blob)
    mode = path.stat().st_mode & 0o777
    extra = path.read_bytes()[len(data):]
    tmp = path.with_name(path.name + ".rot")
    tmp.write_bytes(b"".join(keep) + extra)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return moved


@register_task("routine_rotate", klass="C1", tier="monthly", title="Rotate tool logs", timeout=300)
def rotate_logs(ctx: Ctx) -> Result:
    """Monthly: archive old records of the tool's own journals (audit, changes, spikes, pressure log) into gzip files next to
    them and keep the recent ones in place, so reports keep working. Report-only unless [tasks.routine_rotate] mode =
    "apply". Only .jsonl files directly inside STATE_DIR / LOG_DIR are eligible; the owner's maintenance journal never is."""
    o = _O(ctx)
    tz = _tz()
    default = [{"path": str(core.LOG_DIR / "audit.jsonl"), "keep_days": 90}, {"path": str(core.STATE_DIR / "changes.jsonl"), "keep_days": 365},
               {"path": str(core.STATE_DIR / "spikes.jsonl"), "keep_days": 365}, {"path": str(core.STATE_DIR / "pressure-log.jsonl"), "keep_days": 180}]
    targets = o("targets", default)
    roots = [os.path.realpath(core.LOG_DIR), os.path.realpath(core.STATE_DIR)]
    rows, would, done, errors = [], 0, 0, []
    for t in targets if isinstance(targets, list) else default:
        p = Path(str(t.get("path", ""))) if isinstance(t, dict) else Path("")
        keep = _num(t.get("keep_days"), 90) if isinstance(t, dict) else 90
        if not p.name.endswith(".jsonl") or p.name == "maintenance-journal.jsonl" or p.is_symlink() or not p.is_file() \
                or os.path.dirname(os.path.realpath(p)) not in roots or not keep or keep < 7:
            if p.name:
                rows.append({"file": _safe(p.name, 40), "old_lines": 0, "state": "skipped (not eligible or missing)"})
            continue
        cutoff = ctx.now - keep * 86400
        n_old = sum(1 for r in _jsonl(p, 0.0, tail_bytes=1 << 30, keys=("ts", "t")) if r["_t"] is not None and r["_t"] < cutoff)
        if not n_old:
            rows.append({"file": p.name, "old_lines": 0, "state": "nothing older than %d d" % keep})
            continue
        try:
            def rotate(p=p, cutoff=cutoff) -> int:
                _rotate_file(p, cutoff, tz)
                return 0                                 # rotating frees nothing: do not count it as reclaimed bytes
            did = ctx.act("rotate-log", str(p), 0, rotate)
        except Exception as exc:  # noqa: BLE001 - a failed rotation is reported, never fatal
            if isinstance(exc, getattr(core, "_Timeout", ())):
                raise
            errors.append(f"{p.name}: {type(exc).__name__}")
            did = False
        done, would = done + bool(did), would + (0 if did else 1)
        rows.append({"file": p.name, "old_lines": n_old, "state": "rotated" if did else "refused" if ctx.apply else "would rotate (report mode)"})
    du = sh(["journalctl", "--disk-usage"], timeout=15).stdout
    m = re.search(r"take up ([\d.]+\w) ", du)
    s = f"rotated {done} log(s)" if ctx.apply else f"report: {would} log(s) have records to archive"
    s += (f"; journald {m[1]}" if m else "") + (f"; {errors[0]}" if errors else "")
    return _res("warn" if errors else "info" if (would or done) else "ok", s, {"mode": "apply" if ctx.apply else "report", "rotated": done, "pending": would}, rows)


# =========================================================================== CLI:  python3 -m homelab_maint.routine ...
def _parse_now(s: str | None, tz) -> float:
    """--now: epoch seconds, or local 'YYYY-MM-DD[ T]HH:MM[:SS]' in the routine's zone (for what-if planning)."""
    if not s:
        return time.time()
    v = _num(s)
    if v is not None:
        return v
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=tz).timestamp()
    except ValueError:
        raise SystemExit(f"routine: cannot parse --now {s!r} (epoch or 'YYYY-MM-DD HH:MM')")


def _when(ts: float | None, tz, now: float) -> str:
    if ts is None:
        return "-"
    if abs(ts - now) < 60:
        return "now"
    return _fmt(ts, tz, "%a %H:%M" if abs(ts - now) < 6 * 86400 else "%Y-%m-%d %H:%M")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip()   # noqa: E731
    return "\n".join([line(headers), *map(line, rows)])


def _freeze_now(rc: RoutineConfig, now: float) -> str:
    iv = [(a, b, n) for a, b, n in freeze_intervals(rc.freezes, rc.tz, now - 1, now + 1) if a <= now < b]
    ff = (core.CONF_DIR / "FREEZE").exists()
    return ", ".join(([f"FREEZE file"] if ff else []) + [f"{n} (until {_fmt(b, rc.tz, '%H:%M')})" for _a, b, n in iv]) or "none"


def cmd_plan(rc: RoutineConfig, now: float, a: argparse.Namespace) -> int:
    steps = plan(now, None, rc)
    if a.json:
        print(json.dumps([s.__dict__ for s in steps], indent=1, default=str))
        return 0
    if not rc.valid:
        print("routine.toml is INVALID: " + "; ".join(rc.errors), file=sys.stderr)
        return 1
    rows = [[s.routine, s.name, s.klass, s.mode, ("*" if s.disruptive else "") + s.state, _when(s.next_due, rc.tz, now), s.reason[:70]] for s in steps
            if not a.routine or s.routine == a.routine]
    print(f"now {_fmt(now, rc.tz, '%a %Y-%m-%d %H:%M %Z')}   freeze: {_freeze_now(rc, now)}   (* = needs the quiet window)")
    print(_table(["ROUTINE", "STEP", "CLASS", "MODE", "STATE", "WHEN", "WHY"], rows))
    return 0


def cmd_status(rc: RoutineConfig, now: float, a: argparse.Namespace) -> int:
    st = load_state()
    print(f"routine.toml: {'ok' if rc.valid else 'INVALID'}  zone {rc.tzname}  enforce={rc.enforce}"
          + ("  errors: " + "; ".join(rc.errors) if rc.errors else ""))
    print(f"now {_fmt(now, rc.tz, '%a %Y-%m-%d %H:%M %Z')}   PAUSE: {'YES' if core.paused() else 'no'}   freeze: {_freeze_now(rc, now)}")
    ts = tick_state(rc)
    print(f"tick: {rc.tick_unit} {'enabled' if ts == 'enabled' else 'NOT ENABLED: retries, the monthly window and deferred steps will not run' if ts == 'missing' else 'unknown (systemctl unavailable)'}")
    steps = plan(now, st, rc)
    for e in rc.entries:
        ss = [s for s in steps if s.routine == e.name]
        cnt: dict[str, int] = {}
        for s in ss:
            cnt[s.state] = cnt.get(s.state, 0) + 1
        nxt = e.window.next(rc.tz, now)
        print(f"  {e.name:<8} {e.window.text:<22} next {_when(nxt.start if nxt else None, rc.tz, now):<18} "
              + " ".join(f"{v} {k}" for k, v in sorted(cnt.items())))
    for k, h in sorted(st.get("halted", {}).items()):
        print(f"  HALTED {k}: after {h.get('by')}: {h.get('why')}   (clear: routine clear-halt {k.split('@')[0]})")
    ch = read_changes(5)
    if ch:
        print("last changes:")
        for c in ch:
            ver = "n/a" if c["verified"] is None else "verified" if c["verified"] else "NOT verified"
            print(f"  {_fmt(c['ts'], rc.tz, '%m-%d %H:%M')}  {c['kind']:<11} {c['task']:<16} {ver:<12} {c['detail'][:60]}")
    return 0 if rc.valid else 1


def cmd_explain(rc: RoutineConfig, now: float, a: argparse.Namespace) -> int:
    rows = [s for s in plan(now, None, rc) if s.task == a.task or s.name == a.task]
    if not rows:
        print(f"{a.task}: not in any routine ({'valid config' if rc.valid else 'config invalid'}): report-only when run in a daily/weekly/monthly tier")
        return 1
    for s in rows:
        print(f"{s.routine}/{s.name} -> task {s.task} [{s.klass} {s.mode}] state={s.state}")
        print(f"   why: {s.reason}")
        print(f"   needs quiet window: {s.disruptive}; gates: {', '.join(s.gates) or 'none'}; window "
              f"{_when(s.window_start, rc.tz, now)}..{_when(s.window_end, rc.tz, now)}; last {s.last_outcome} {_when(s.last_run, rc.tz, now)}")
        cp = canary_caps(s.task, None, None, rc) if s.mode == "apply" else None
        print(f"   canary: {'first apply run capped: ' + json.dumps(cp) if cp else 'graduated or not applicable'}")
    return 0


def tick_state(rc: RoutineConfig | None = None) -> str:
    """'enabled' | 'missing' | 'unknown': is the scheduler timer that runs the `routine-run` job (the routine tick) enabled?
    Read-only `systemctl is-enabled`. The tick is a HARD dependency: it retries a step a busy gate deferred, runs the monthly
    window (which has no timer of its own), catches read-only steps up after downtime and lets verify/report close a routine
    that had to wait. `routine check` fails and `doctor` should fail when this says 'missing'; 'unknown' (no systemd, no
    systemctl) is only a warning because it cannot be told."""
    unit = (rc.tick_unit if rc else TICK_UNIT)
    r = sh(["systemctl", "is-enabled", unit], timeout=10)
    out = (r.stdout or "").strip().split("\n")[0].strip()
    if r.returncode == 0 and out.startswith("enabled"):
        return "enabled"
    err = (r.stderr or "").lower()
    if r.returncode == 127 or "not been booted" in err or "failed to connect" in err:
        return "unknown"
    return "missing"


def _drop_halt(st: dict, name: str) -> int:
    gone = [k for k in st["halted"] if k.split("@")[0] == name]
    for k in gone:
        del st["halted"][k]
    return len(gone)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="homelab-maint routine", description="maintenance routine and change management")
    ap.add_argument("--config", help="routine.toml to use instead of CONF_DIR/routine.toml")
    ap.add_argument("--now", help="pretend it is this time (epoch or 'YYYY-MM-DD HH:MM' local): what-if planning")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="every step with its window/freeze/catch-up decision")
    p.add_argument("routine", nargs="?")
    sub.add_parser("status", help="config, freeze/PAUSE state, per-routine counts, halts, last changes")
    sub.add_parser("due", help="task names due right now, one per line")
    p = sub.add_parser("export", help="print routine.json (or --write it to STATE_DIR/public)")
    p.add_argument("--write", action="store_true")
    p.add_argument("--out")
    p = sub.add_parser("run", help="execute the due steps now (a tick): dry-run unless --apply")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("explain", help="why is this task/step (not) running")
    p.add_argument("task")
    sub.add_parser("check", help="validate routine.toml")
    p = sub.add_parser("note", help="log a manual change (change log + maintenance journal)")
    p.add_argument("text")
    p.add_argument("--kind", choices=KINDS, default="maintenance")
    p.add_argument("--task", default="manual")
    p = sub.add_parser("ack", help="record that a manual routine item was done, e.g. restore_drill")
    p.add_argument("name")
    p.add_argument("note", nargs="?", default="")
    p = sub.add_parser("clear-halt", help="let a halted routine apply again after you reviewed the post-check failure")
    p.add_argument("routine")
    p = sub.add_parser("canary-reset", help="cap the next apply run of TASK again")
    p.add_argument("task")
    a = ap.parse_args(argv)
    rc = load_config(Path(a.config) if a.config else None)
    now = _parse_now(a.now, rc.tz)
    if a.cmd == "plan":
        return cmd_plan(rc, now, a)
    if a.cmd == "status":
        return cmd_status(rc, now, a)
    if a.cmd == "explain":
        return cmd_explain(rc, now, a)
    if a.cmd == "due":
        print("\n".join(due(now, None, rc)))
        return 0
    if a.cmd == "export":
        if a.write:
            print(write_export(Path(a.out) if a.out else None, now=now, cfg=rc))
        else:
            print(json.dumps(export(now, rc, timers=system_timers()), indent=None if not a.json else 1))
        return 0
    if a.cmd == "run":
        if not rc.valid:
            print("routine.toml invalid: " + "; ".join(rc.errors), file=sys.stderr)
            return 1
        rows = run_due(a.apply, cfg=rc)
        print(_table(["STEP", "TASK", "RC", "RESULT"], [[r["step"], r["task"], str(r["rc"]), str(r["state"])] for r in rows]) if rows else "nothing due")
        return 1 if any(r["rc"] not in (0, None) for r in rows) else 0
    if a.cmd == "check":
        for e in rc.errors:
            print("error:", e)
        _load_tasks()
        for e in rc.entries:
            for s in e.steps:
                if s.task not in core.REGISTRY:
                    print(f"warning: {e.name}/{s.name}: task {s.task} is not registered")
        ts = tick_state(rc)
        if ts == "missing":
            print(f"error: {rc.tick_unit} is not enabled: without the routine tick nothing retries a deferred step, the monthly "
                  "window never opens and a weekly/daily run that had to wait is not finished (install.sh must enable it)")
        elif ts == "unknown":
            print(f"warning: cannot tell whether {rc.tick_unit} is enabled (systemctl unavailable)")
        print("routine.toml " + ("ok" if rc.valid else "INVALID"))
        return 0 if rc.valid and ts != "missing" else 1
    if a.cmd == "note":
        rec = record_change(a.task, a.kind, a.text, outcome="manual", verified=True, now=now)
        journal_note(a.text, f"{a.kind} change by the owner", now)
        print(f"logged: {rec['detail']}")
        return 0
    if a.cmd == "ack":
        update_state(lambda st: st["acks"].__setitem__(a.name, {"ts": now, "note": _safe(a.note, 120)}), now)
        journal_note(f"{a.name.replace('_', ' ')} done", a.note, now)
        print(f"recorded {a.name}")
        return 0
    if a.cmd == "clear-halt":
        n = [0]
        update_state(lambda st: n.__setitem__(0, _drop_halt(st, a.routine)), now)
        print(f"cleared {n[0]} halt(s) for {a.routine}")
        return 0
    if a.cmd == "canary-reset":
        update_state(lambda st: st["canary"].pop(a.task, None), now)
        print(f"{a.task}: the next apply run is capped again")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
