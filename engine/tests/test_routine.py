"""Tests for homelab_maint/routine.py: windows and DST, the 1st-Saturday logic, freeze/PAUSE/catch-up, due() idempotence,
the change log, canary caps, post-check, RunGuard, the tick, routine.json + calendar, every built-in step and the CLI.

Everything runs on tmp dirs with a fake task registry and a fake `sh`; any real subprocess call fails the test."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import gzip
import json
import os
import signal
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from zoneinfo import ZoneInfo

import pytest

from homelab_maint import core, routine
from homelab_maint.core import GIB, Ctx, Result

TZ = ZoneInfo("America/Toronto")
ROOT = Path(__file__).resolve().parent.parent
routine._load_tasks()                  # every real task registers once, into the real registry, before fixtures swap it
REAL_REGISTRY = dict(core.REGISTRY)


def at(y, m, d, h=0, mi=0, s=0, tz=TZ):
    return datetime(y, m, d, h, mi, s, tzinfo=tz).timestamp()


FRI = at(2026, 10, 2, 7, 35)          # inside the daily window 07:30-09:30


# =========================================================================== fixtures
class FakeSh:
    """Stands in for core.sh: answers by command prefix, records every call; unknown commands are 'not found' (127)."""

    def __init__(self):
        self.calls, self.rules = [], []

    def when(self, prefix, stdout="", rc=0, stderr=""):
        self.rules.append((list(prefix), stdout, rc, stderr))
        return self

    def __call__(self, cmd, timeout=60, check=False, input_=None, env=None):
        c = list(cmd) if not isinstance(cmd, str) else cmd.split()
        self.calls.append(c)
        for pre, out, rc, err in reversed(self.rules):
            if c[:len(pre)] == pre:
                return CompletedProcess(c, rc, out(c) if callable(out) else out, err)
        return CompletedProcess(c, 127, "", "not found (test)")

    def ran(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


class World:
    def __init__(self, root, mp):
        self.root, self.mp = root, mp
        self.state, self.log, self.conf, self.run = (root / n for n in ("state", "log", "conf", "run"))
        for d in (self.state, self.log, self.conf, self.run):
            d.mkdir(parents=True, exist_ok=True)
        for k, d in (("STATE_DIR", self.state), ("LOG_DIR", self.log), ("CONF_DIR", self.conf), ("RUN_DIR", self.run)):
            mp.setattr(core, k, d)
        self.sh = FakeSh().when(["logger"])
        mp.setattr(core, "sh", self.sh)
        mp.setattr(routine, "sh", self.sh)
        mp.setattr(routine, "_LOADED", True)               # tests use a fake registry: do not import the real tasks
        (self.conf / "protected.toml").write_text('patterns = ["^never-matches$"]\n')
        self.registry({})

    # -- registry / config files ------------------------------------------------------------------
    def registry(self, spec):
        reg = {}
        for name, (klass, tier) in spec.items():
            reg[name] = core.Task(name, klass, tier, lambda ctx: Result("ok", "fine"), title=name.replace("_", " "))
        self.mp.setattr(core, "REGISTRY", reg)
        return reg

    def routine_toml(self, text):
        (self.conf / "routine.toml").write_text(text)

    def maint(self, text):
        (self.conf / "maint.toml").write_text(text)

    def status(self, tasks, now=None):
        core.write_json_atomic(self.state / "status.json", {"generated_at": now or time.time(), "tasks": tasks})

    def audit_done(self, task, when, action="docker-image-rm", target="img", size=0, outcome="done"):
        rec = {"ts": datetime.fromtimestamp(when, TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "task": task, "action": action,
               "target": target, "bytes": size, "outcome": outcome}
        with open(self.log / "audit.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    def no_subprocess(*a, **k):
        raise AssertionError(f"a test ran a real command: {a[:1]}")
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    return World(tmp_path, monkeypatch)


MAIN = """
[settings]
timezone = "America/Toronto"
default_gates = ["backup"]
[windows]
daily = "07:30-09:30"
weekly = "Wed 07:45-10:00"
monthly = "1st Sat 04:30-07:00"
[freeze]
evenings = "18:00-23:30"
[[routine]]
name = "daily"
cadence = "daily"
post_check = ["chk_a"]
steps = ["chk_a", "clean_a", "clean_b", {task = "heavy", disruptive = true}, "verify"]
"""
MAINT = '[tasks.clean_a]\nmode = "apply"\n[tasks.clean_b]\nmode = "report"\n'
SPEC = {"chk_a": ("C0", "daily"), "clean_a": ("C1", "daily"), "clean_b": ("C1", "daily"), "heavy": ("C2", "daily"),
        "routine_verify_daily": ("C0", "daily"), "routine_verify_weekly": ("C0", "weekly"), "report_daily": ("C0", "daily")}


@pytest.fixture
def w(world):
    world.registry(SPEC)
    world.routine_toml(MAIN)
    world.maint(MAINT)
    return world


def states(now, **kw):
    return {s.name: s.state for s in routine.plan(now, **kw)}


def done_state(*keys, occ, now=FRI, status="ok", applied=True):
    st = routine.load_state()
    for k in keys:
        e, s = k.split("/")
        routine.mark_run(st, e, s, occ, now, status, "x", applied=applied, needs_applied=False)
    routine.save_state(st)


# =========================================================================== windows, selectors, DST
@pytest.mark.parametrize("spec,cad,ok", [
    ("07:30-09:30", "daily", True), ("daily 07:30-09:30", "daily", True), ("Wed 07:45-10:00", "weekly", True),
    ("Mon-Fri 07:30-09:30", "weekly", True), ("weekdays 07:00-08:00", "weekly", True), ("Sat-Mon 22:00-02:00", "weekly", True),
    ("1st Sat 04:30-07:00", "monthly", True), ("last Sun 04:00-06:00", "monthly", True), ("day 15 04:00-05:00", "monthly", True),
    ("18:00-24:00", "daily", True), ("22:00-02:00", "daily", True),
    ("07:30", "daily", False), ("07:30-07:30", "daily", False), ("25:00-26:00", "daily", False), ("24:00-02:00", "daily", False),
    ("Wed 07:45-10:00", "daily-nth", False), ("07:30-09:30", "weekly", False), ("Wed 07:45-10:00", "monthly", False),
    ("2nd Wed 07:00-08:00", "daily", False), ("5th Sat 04:00-05:00", "monthly", False), ("1st Sat,Sun 04:00-05:00", "monthly", False),
    ("day 32 04:00-05:00", "monthly", False), ("Funday 07:00-08:00", "weekly", False), ("", "daily", False)])
def test_parse_window(spec, cad, ok):
    if cad == "daily-nth":
        cad = "daily"
        spec = "1st Sat 04:00-05:00"
    if ok:
        assert routine.parse_window(cad, spec).text
    else:
        with pytest.raises(ValueError):
            routine.parse_window(cad, spec)


def test_point_window_and_inferred_cadence():
    assert routine.parse_window(None, "Sat 01:00", point=True).end is None
    assert routine.parse_window(None, "01:00", point=True).cadence == "daily"
    assert routine.parse_window(None, "1st Sat 04:30-07:00").cadence == "monthly"
    with pytest.raises(ValueError):
        routine.parse_window(None, "01:00")                    # a point needs point=True


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_first_saturday_every_month(year):
    """Independent oracle: the first Saturday is the first day of the month whose weekday is 5."""
    win = routine.parse_window("monthly", "1st Sat 04:30-07:00")
    occ = win.occurrences(TZ, date(year, 1, 1), date(year, 12, 31))
    assert len(occ) == 12
    for m, o in enumerate(occ, 1):
        d = date.fromisoformat(o.id)
        first_sat = next(date(year, m, i) for i in range(1, 8) if date(year, m, i).weekday() == 5)
        assert d == first_sat and d.month == m and d.day <= 7


def test_other_selectors_across_months():
    last_sun = routine.parse_window("monthly", "last Sun 04:00-06:00").occurrences(TZ, date(2026, 1, 1), date(2026, 12, 31))
    for o in last_sun:
        d = date.fromisoformat(o.id)
        assert d.weekday() == 6 and (d + timedelta(days=7)).month != d.month
    assert len(last_sun) == 12
    second_wed = routine.parse_window("monthly", "2nd Wed 04:00-06:00").occurrences(TZ, date(2026, 3, 1), date(2026, 3, 31))
    assert [o.id for o in second_wed] == ["2026-03-11"]
    # day 31 clamps to the month's last day (Feb 2028 is a leap February)
    d31 = routine.parse_window("monthly", "day 31 04:00-05:00")
    ids = [o.id for o in d31.occurrences(TZ, date(2028, 1, 1), date(2028, 4, 30))]
    assert ids == ["2028-01-31", "2028-02-29", "2028-03-31", "2028-04-30"]
    assert [o.id for o in d31.occurrences(TZ, date(2027, 2, 1), date(2027, 2, 28))] == ["2027-02-28"]


def test_weekday_ranges_wrap():
    sat_mon = routine._dowset("Sat-Mon")
    assert sat_mon == {5, 6, 0}
    assert routine._dowset("weekdays") == {0, 1, 2, 3, 4} and routine._dowset("Fri,Sat") == {4, 5}


def test_latest_and_next_around_month_boundary():
    win = routine.parse_window("monthly", "1st Sat 04:30-07:00")
    cases = [(at(2026, 10, 31, 12), "2026-10-03", "2026-11-07"),      # Nov 1 is a Sunday: first Saturday = 7th
             (at(2026, 10, 3, 4, 29), "2026-09-05", "2026-10-03"),     # one minute before the window
             (at(2026, 10, 3, 4, 30), "2026-10-03", "2026-11-07"),     # exactly at the start
             (at(2026, 10, 3, 6, 59, 59), "2026-10-03", "2026-11-07"),
             (at(2026, 11, 7, 4, 30), "2026-11-07", "2026-12-05"),
             (at(2027, 1, 1, 0, 0), "2026-12-05", "2027-01-02"),       # year boundary, Jan 1 2027 is a Friday
             (at(2026, 8, 1, 5, 0), "2026-08-01", "2026-09-05")]       # the 1st itself is the 1st Saturday
    for now, latest, nxt in cases:
        assert win.latest(TZ, now).id == latest and win.next(TZ, now).id == nxt


def test_occurrence_at_dst_transitions():
    d = routine.parse_window("daily", "02:30-04:00")
    spring = d.occurrence(date(2026, 3, 8), TZ)                # 02:00 EST jumps to 03:00 EDT: 02:30 does not exist
    assert datetime.fromtimestamp(spring.start, TZ).strftime("%H:%M %Z") == "03:00 EDT"       # the first instant after the gap
    assert spring.end - spring.start == 3600                   # the real window: wall-clock 02:30..04:00 that exists = 03:00..04:00
    ordinary = d.occurrence(date(2026, 3, 7), TZ)
    assert ordinary.end - ordinary.start == 90 * 60
    through = routine.parse_window("daily", "01:30-03:30").occurrence(date(2026, 3, 8), TZ)
    assert through.end - through.start == 3600                 # 01:30 EST .. 03:30 EDT is one real hour
    assert routine.parse_window("daily", "02:15-02:45").occurrence(date(2026, 3, 8), TZ) is None      # wholly inside the gap
    assert routine.parse_window("daily", "02:00-03:00").occurrence(date(2026, 3, 8), TZ) is None
    after_gap = routine.parse_window("daily", "03:00-03:30").occurrence(date(2026, 3, 8), TZ)
    assert datetime.fromtimestamp(after_gap.start, TZ).strftime("%H:%M %Z") == "03:00 EDT" and after_gap.end - after_gap.start == 1800
    fall = routine.parse_window("daily", "00:30-02:30").occurrence(date(2026, 11, 1), TZ)          # 01:00-01:59 happens twice
    assert fall.end - fall.start == 3 * 3600
    amb = routine.parse_window("daily", "01:15-01:45").occurrence(date(2026, 11, 1), TZ)
    assert datetime.fromtimestamp(amb.start, TZ).strftime("%Z") == "EDT"                          # first occurrence
    regular = routine.parse_window("daily", "07:30-09:30").occurrence(date(2026, 3, 8), TZ)
    assert regular.end - regular.start == 2 * 3600
    for tzname in ("Europe/London", "Australia/Lord_Howe", "America/St_Johns"):                    # half-hour and non-1h shifts too
        z = ZoneInfo(tzname)
        for day in (date(2026, 3, 29), date(2026, 4, 5), date(2026, 10, 4), date(2026, 3, 8), date(2026, 11, 1)):
            for m in range(0, 1440, 15):
                t = routine._ts(day, m, z)
                back = datetime.fromtimestamp(t, z)
                assert (back.hour * 60 + back.minute >= m or back.date() > day) and t == t                 # never earlier than asked


def test_window_crossing_midnight_and_dst_day():
    win = routine.parse_window("daily", "22:00-02:00")
    o = win.occurrence(date(2026, 10, 2), TZ)
    assert o.id == "2026-10-02" and o.end - o.start == 4 * 3600
    assert win.latest(TZ, at(2026, 10, 3, 1, 0)).id == "2026-10-02"                  # still yesterday's window at 01:00
    nov = win.occurrence(date(2026, 10, 31), TZ)                                      # ends 02:00 on Nov 1: 5 real hours
    assert nov.end - nov.start == 5 * 3600
    mar = win.occurrence(date(2026, 3, 7), TZ)                                        # 22:00 EST .. 01:59 EST (02:00 is the gap): 4 real hours
    assert mar.end - mar.start == 4 * 3600


def test_freeze_parsing_and_intervals():
    ev = routine.parse_freeze("evenings", "18:00-23:30")
    holiday = routine.parse_freeze("xmas", "2026-12-24..2026-12-26")
    one = routine.parse_freeze("day", "2026-12-25")
    wk = routine.parse_freeze("fri", "Fri 17:00-24:00")
    for bad in ("2026-12-26..2026-12-24", "banana", "25:00-26:00"):
        with pytest.raises(ValueError):
            routine.parse_freeze("x", bad)
    iv = routine.freeze_intervals([ev, holiday, one, wk], TZ, at(2026, 12, 24, 0), at(2026, 12, 26, 12))
    names = [n for _a, _b, n in iv]
    assert names.count("evenings") == 2 and "xmas" in names and "day" in names
    xm = next((a, b) for a, b, n in iv if n == "xmas")
    assert xm == (at(2026, 12, 24), at(2026, 12, 27))                                  # inclusive of the last day
    assert any(n == "fri" and a == at(2026, 12, 25, 17) for a, _b, n in iv)
    assert routine._subtract(10, 20, [(12, 14, "a"), (13, 15, "b"), (19, 30, "c")]) == [(10, 12), (15, 19)]
    assert routine._subtract(10, 20, [(0, 30, "all")]) == []


def test_host_tz_falls_back(monkeypatch):
    assert routine.host_tz("America/Toronto")[1] == "America/Toronto"
    monkeypatch.setenv("TZ", "Nope/Nowhere")
    tz, name = routine.host_tz("Also/Bad")                                             # a typo must not crash anything
    assert tz is not None and isinstance(name, str)


# =========================================================================== configuration
def test_shipped_routine_toml_is_valid_and_real():
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    assert rc.valid, rc.errors
    assert rc.errors == []
    assert [e.name for e in rc.entries] == ["daily", "weekly", "monthly"]
    assert rc.windows["monthly"] == "1st Sat 04:30-07:00" and rc.freeze["evenings"] == "18:00-23:30"
    assert rc.tzname == "America/Toronto" and rc.enforce
    daily = rc.entries[0]
    names = [s.name for s in daily.steps]
    assert names[0] == "spike_review" and names[-2:] == ["verify", "report"]
    assert {"docker_cache", "docker_images", "apt_clean", "snap_revisions", "retention", "trash", "gradle_reaper", "caps"} <= set(names)
    weekly = [s.name for s in rc.entries[1].steps]
    assert {"capacity_review", "smart_selftest", "c2_candidates", "image_updates", "updates_review", "backup_verify", "report"} <= set(weekly)
    monthly = [s.name for s in rc.entries[2].steps]
    assert {"restore_check", "expiry_check", "trend_review", "config_drift", "rotate_logs"} <= set(monthly)
    assert rc.entries[1].depends_on == ["daily"]
    assert all(s.task for e in rc.entries for s in e.steps)
    assert rc.entries[1].steps[2].disruptive is True                                    # c2_candidates: heavy du
    assert any(s["name"] == "backup-system" and s["at"] == "Sat 01:00" for s in rc.system)


def test_shipped_steps_name_registered_tasks(monkeypatch):
    """Against the REAL registry: every step of the shipped routine is a task that exists in this codebase."""
    monkeypatch.setattr(core, "REGISTRY", REAL_REGISTRY)
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    missing = [f"{e.name}/{s.name}->{s.task}" for e in rc.entries for s in e.steps if s.task not in core.REGISTRY]
    assert missing == []
    for e in rc.entries:
        for n in e.post_check:
            assert core.REGISTRY[n].klass == "C0", n                                    # a post-check is always read-only
    managed = {s.task for e in rc.entries for s in e.steps}
    unmanaged = sorted(t.name for t in core.REGISTRY.values() if t.klass in ("C1", "C2") and t.tier != "check" and t.name not in managed)
    assert unmanaged == [], f"add these cleaners/plans to etc/routine.toml or the guard holds them to report-only: {unmanaged}"
    for n in ("routine_spike_review", "routine_capacity", "routine_smart_selftest", "routine_updates", "routine_image_updates",
              "routine_backup_verify", "routine_restore_check", "routine_expiry", "routine_trends", "routine_rotate",
              "routine_verify_daily", "routine_verify_weekly", "routine_verify_monthly"):
        t = core.REGISTRY[n]
        assert t.klass == ("C1" if n == "routine_rotate" else "C0") and t.tier in ("daily", "weekly", "monthly")


def test_config_errors_fail_closed(world):
    assert not routine.load_config().valid and routine.load_config().errors == ["routine.toml not found"]
    assert routine.load_config().tzname != ""                                           # even then times can be shown
    world.routine_toml("this is [not toml")
    assert not routine.load_config().valid
    world.routine_toml(MAIN.replace('evenings = "18:00-23:30"', 'evenings = "banana"'))
    rc = routine.load_config()
    assert not rc.valid and any("freeze evenings" in e for e in rc.errors)              # an unreadable freeze never disappears
    world.registry(SPEC)
    assert routine.plan(FRI) == [] and routine.due(FRI) == []
    world.routine_toml(MAIN.replace('cadence = "daily"', 'cadence = "hourly"'))
    rc = routine.load_config()
    assert not rc.valid and any("cadence" in e for e in rc.errors)
    world.routine_toml("[settings]\ntimezone = 'Nope/Zone'\n")
    assert not routine.load_config().valid


def test_config_drops_bad_pieces_keeps_good(world):
    world.routine_toml(MAIN + """
[[routine]]
name = "daily"
cadence = "daily"
steps = ["chk_a"]
[[routine]]
name = "w2"
cadence = "weekly"
window = "07:00-08:00"
steps = ["chk_a"]
[[routine]]
name = "m1"
cadence = "monthly"
depends_on = ["later"]
steps = ["chk_a"]
[[routine]]
name = "m2"
cadence = "monthly"
steps = ["report"]
[[routine]]
name = "d3"
cadence = "daily"
steps = ["chk_a", {task = "chk_a"}, {task = "x", depends_on = ["y"]}, "good_one"]
""")
    rc = routine.load_config()
    assert rc.valid and [e.name for e in rc.entries] == ["daily", "d3"]
    assert [s.name for s in rc.entries[1].steps] == ["chk_a", "good_one"]
    text = " ".join(rc.errors)
    assert "duplicate" in text and "weekly window" in text and "defined earlier" in text and "no monthly report" in text


def test_settings_and_per_step_options(world):
    world.routine_toml(MAIN.replace('default_gates = ["backup"]', 'default_gates = ["backup", "pressure"]\nmax_attempts = 5\nenforce = false\nmax_pressure_level = 3\n')
                       .replace("[freeze]", '[canary]\nruns = 2\nfraction = 0.25\n[freeze]')
                       .replace('"verify"]', '"verify", {task = "z", gates = [], canary_runs = 0, enabled = false}]'))
    rc = routine.load_config()
    assert (rc.max_attempts, rc.enforce, rc.max_pressure_level, rc.canary_runs, rc.canary_fraction) == (5, False, 3, 2, 0.25)
    assert rc.default_gates == ["backup", "pressure"]
    z = rc.entries[0].steps[-1]
    assert (z.gates, z.canary_runs, z.enabled) == ([], 0, False)


def test_resolve_step():
    assert routine.resolve_step("verify", "weekly") == "routine_verify_weekly"
    assert routine.resolve_step("report", "daily") == "report_daily"
    assert routine.resolve_step("docker_cache", "daily") == "docker_cache"
    with pytest.raises(ValueError):
        routine.resolve_step("report", "monthly")


# =========================================================================== plan / due
def test_in_window_everything_due_in_order(w):
    # verify is a closing step: it waits for every step before it, so it is not due until they are done (see the closing tests)
    assert routine.due(FRI) == ["chk_a", "clean_a", "clean_b", "heavy"]
    s = {x.name: x for x in routine.plan(FRI)}
    assert s["verify"].state == "waiting" and s["verify"].reason == "waiting for chk_a to finish"
    assert s["clean_a"].disruptive and s["clean_a"].mode == "apply" and s["clean_a"].gates == ["backup"]
    assert not s["clean_b"].disruptive and s["clean_b"].mode == "report" and s["clean_b"].gates == []
    assert s["heavy"].disruptive and s["heavy"].klass == "C2" and s["chk_a"].mode == "check"
    assert s["clean_a"].in_window and s["clean_a"].occ == "2026-10-02" and s["clean_a"].reason.startswith("inside the window until 09:30")


def test_due_is_pure_and_idempotent(w):
    before = (w.state / "routine-state.json").exists()
    a, b = routine.plan(FRI), routine.plan(FRI)
    assert a == b and routine.due(FRI) == routine.due(FRI)
    assert (w.state / "routine-state.json").exists() == before == False     # reading never writes
    assert not (w.state / "changes.jsonl").exists()


def test_once_per_occurrence_then_again_next_day(w):
    done_state("daily/chk_a", "daily/clean_a", "daily/clean_b", "daily/heavy", "daily/verify", occ="2026-10-02")
    assert routine.due(FRI) == [] and set(states(FRI).values()) == {"done"}
    assert routine.due(at(2026, 10, 2, 18, 0)) == [] and routine.due(at(2026, 10, 3, 7, 29)) == []     # nothing until tomorrow
    assert routine.due(at(2026, 10, 3, 7, 30)) == ["chk_a", "clean_a", "clean_b", "heavy"]
    nxt = routine.plan(at(2026, 10, 2, 12))[0]
    assert nxt.next_due == at(2026, 10, 3, 7, 30) and nxt.window_start == at(2026, 10, 3, 7, 30)


def test_host_was_off_catch_up_rules(w):
    """Window missed (host off, nothing recorded): disruptive steps are missed, read-only steps run at the next opportunity."""
    late = at(2026, 10, 2, 11, 0)                                       # 90 min after the window closed
    st = states(late)
    assert st == {"chk_a": "due", "clean_a": "missed", "clean_b": "due", "heavy": "missed", "verify": "due"}
    reasons = {s.name: s.reason for s in routine.plan(late)}
    assert "window over" in reasons["clean_a"] and reasons["chk_a"].startswith("catch-up")
    assert routine.due(late) == ["chk_a", "clean_b", "routine_verify_daily"]
    # the next morning's window is a fresh occurrence: everything is due again, nothing from yesterday is replayed
    assert states(at(2026, 10, 3, 7, 31)) == {**dict.fromkeys(st, "due"), "verify": "waiting"}


def test_host_off_for_days_catches_up_only_the_latest(w):
    wed = at(2026, 10, 7, 12, 0)                                         # powered off since Fri: 4 daily windows missed
    steps = routine.plan(wed)
    assert {s.occ for s in steps} == {"2026-10-07"}
    done_state("daily/chk_a", "daily/clean_b", "daily/verify", occ="2026-10-07")
    assert routine.due(wed) == []                                         # one catch-up run, not one per missed day


def test_before_the_window_the_previous_occurrence_governs(w):
    done_state("daily/chk_a", "daily/clean_a", "daily/clean_b", "daily/heavy", "daily/verify", occ="2026-10-01")
    assert routine.due(at(2026, 10, 2, 7, 29)) == []                       # yesterday was done, today has not started
    assert routine.due(at(2026, 10, 2, 7, 30)) != []                       # the instant the window opens


def test_window_boundaries_are_half_open(w):
    assert states(at(2026, 10, 2, 9, 29, 59))["clean_a"] == "due"
    assert states(at(2026, 10, 2, 9, 30, 0))["clean_a"] == "missed"
    assert states(at(2026, 10, 2, 7, 30, 0))["clean_a"] == "due"


def test_evening_freeze_blocks_disruptive_only(w):
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "17:00-20:00"'))
    assert states(at(2026, 10, 2, 17, 30))["clean_a"] == "due"
    s = states(at(2026, 10, 2, 18, 30))                                  # inside the window but inside the freeze, and it never ends
    assert s["clean_a"] == "missed" and s["chk_a"] == "due" and s["clean_b"] == "due" and s["heavy"] == "missed"
    assert routine.due(at(2026, 10, 2, 18, 30)) == ["chk_a", "clean_b"]               # verify waits for them: the window is still open


def test_freeze_in_the_middle_of_a_window_resumes_after_it(w):
    w.routine_toml(MAIN.replace('evenings = "18:00-23:30"', 'midmorning = "08:00-08:30"'))
    mid = at(2026, 10, 2, 8, 10)
    p = {s.name: s for s in routine.plan(mid)}
    assert p["clean_a"].state == "frozen" and p["clean_a"].run_at == at(2026, 10, 2, 8, 30) and "waits until 08:30" in p["clean_a"].reason
    assert p["chk_a"].state == "due" and routine.due(mid) == ["chk_a", "clean_b"]      # verify waits for the frozen clean_a too
    assert p["verify"].state == "waiting" and "waiting for chk_a" in p["verify"].reason
    assert states(at(2026, 10, 2, 8, 30))["clean_a"] == "due"            # the freeze is half open: back at 08:30 sharp


def test_freeze_file_and_pause(w):
    ff = w.conf / "FREEZE"
    ff.write_text("x")
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["clean_a"].state == "frozen" and "FREEZE file" in p["clean_a"].reason and p["clean_a"].run_at is None
    assert p["chk_a"].state == "due"
    assert states(at(2026, 10, 2, 9, 45))["clean_a"] == "missed"         # the window ended while frozen
    ff.unlink()
    assert states(FRI)["clean_a"] == "due"
    (w.conf / "PAUSE").write_text("x")
    s = states(FRI)
    assert s["clean_a"] == "paused" and s["heavy"] == "paused" and s["chk_a"] == "due" and s["clean_b"] == "due"   # PAUSE stops mutation only
    (w.conf / "PAUSE").unlink()
    (w.conf / "PAUSE.clean_a").write_text("x")
    assert states(FRI)["clean_a"] == "paused" and states(FRI)["heavy"] == "due"


def test_dated_freeze_and_holiday(w):
    w.routine_toml(MAIN.replace('evenings = "18:00-23:30"', 'evenings = "18:00-23:30"\nholiday = "2026-10-02"'))
    s = states(FRI)
    assert s["clean_a"] == "missed" and s["chk_a"] == "due"
    assert states(at(2026, 10, 3, 7, 40))["clean_a"] == "due"


def test_error_attempts_and_give_up(w):
    for attempt in (1, 2):
        st = routine.load_state()
        routine.mark_run(st, "daily", "clean_a", "2026-10-02", FRI, "error", "boom", applied=True, needs_applied=True, max_attempts=3)
        routine.save_state(st)
        assert states(FRI)["clean_a"] == "due"
        assert routine.plan(FRI)[1].last_outcome == "error"
    st = routine.load_state()
    routine.mark_run(st, "daily", "clean_a", "2026-10-02", FRI, "error", "boom", applied=True, needs_applied=True, max_attempts=3)
    routine.save_state(st)
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["clean_a"].state == "failed" and "gave up after 3" in p["clean_a"].reason
    assert states(at(2026, 10, 3, 7, 40))["clean_a"] == "due"            # a new occurrence gets a fresh budget


def test_mark_run_semantics():
    st = routine._fresh_state()
    r = routine.mark_run(st, "d", "s", "o1", 1.0, "ok", "x", applied=False, needs_applied=True)
    assert not r["done"] and "report only" in r["note"]                      # a dry run does not satisfy an apply-mode step
    r = routine.mark_run(st, "d", "s", "o1", 2.0, "ok", "x", applied=True, needs_applied=True)
    assert r["done"]
    r = routine.mark_run(st, "d", "t", "o1", 1.0, "skipped", "busy", needs_applied=True)
    assert not r["done"] and r["deferrals"] == 1                              # an apply-mode task that skipped (gate busy) retries
    r = routine.mark_run(st, "d", "u", "o1", 1.0, "skipped", "no smartctl", needs_applied=False)
    assert r["done"]                                                          # a read-only task that skipped has nothing to retry
    r = routine.mark_run(st, "d", "v", "o1", 1.0, "deferred", "gate busy", needs_applied=True)
    assert not r["done"] and r["deferrals"] == 1
    r = routine.mark_run(st, "d", "s", "o2", 3.0, "warn", "x", needs_applied=False)
    assert r["occ"] == "o2" and r["done"] and r["attempts"] == 0              # a new occurrence starts clean
    assert routine._safe("ab " * 100, 50) == ("ab " * 100)[:50]


def test_step_dependencies(w):
    w.routine_toml(MAIN.replace('steps = ["chk_a", "clean_a", "clean_b", {task = "heavy", disruptive = true}, "verify"]',
                                'steps = ["clean_a", {task = "clean_b", depends_on = ["clean_a"]}, {task = "heavy", depends_on = ["clean_b"], disruptive = true}]'))
    assert routine.due(FRI) == ["clean_a", "clean_b", "heavy"]                # a due prerequisite runs first, in order
    st = routine.load_state()
    for _ in range(3):
        routine.mark_run(st, "daily", "clean_a", "2026-10-02", FRI, "error", "x", applied=True, needs_applied=True, max_attempts=3)
    routine.save_state(st)
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["clean_a"].state == "failed" and p["clean_b"].state == "blocked" and "depends on clean_a (failed)" in p["clean_b"].reason
    assert p["heavy"].state == "blocked"
    assert routine.due(FRI) == []


def test_routine_depends_on_routine_same_day(w):
    """weekly depends_on daily, but only its DISRUPTIVE steps wait: the weekly timer (07:45) can fire before a delayed daily one
    (07:30 + up to 20 min), and a one-shot weekly run that finds everything 'waiting' would lose the week's steps."""
    w.registry({**SPEC, "wk": ("C0", "weekly"), "wk_clean": ("C1", "weekly")})
    w.maint(MAINT + '[tasks.wk_clean]\nmode = "apply"\n')
    w.routine_toml(MAIN + """
[[routine]]
name = "weekly"
cadence = "weekly"
depends_on = ["daily"]
steps = ["wk", "wk_clean", "routine_verify_weekly"]
""")
    wed = at(2026, 10, 7, 7, 46)                                          # both windows open, the daily steps have not had their turn
    p = {(s.routine, s.name): s for s in routine.plan(wed)}
    assert p[("weekly", "wk")].state == "due" and "wk" in routine.due(wed)             # read-only: no cross-routine wait
    assert p[("weekly", "wk_clean")].state == "waiting" and "waiting for daily to finish" in p[("weekly", "wk_clean")].reason
    assert "wk_clean" not in routine.due(wed)                                        # a cleaner does wait for the daily steps
    # the daily tier ran once: some steps were done, one apply step was deferred by a busy gate. The weekly cleaner must not
    # wait for it all morning: a step that was attempted no longer holds the weekly routine up
    st = routine.load_state()
    for n in ("chk_a", "clean_b", "heavy", "verify"):
        routine.mark_run(st, "daily", n, "2026-10-07", wed, "ok", "x")
    routine.mark_run(st, "daily", "clean_a", "2026-10-07", wed, "deferred", "gate backup busy", needs_applied=True)
    routine.save_state(st)
    assert states(wed)["clean_a"] == "due" and "wk_clean" in routine.due(wed)
    done_state("daily/chk_a", "daily/clean_a", "daily/clean_b", "daily/heavy", "daily/verify", occ="2026-10-07")
    assert "wk_clean" in routine.due(wed)
    assert {(s.routine, s.name): s for s in routine.plan(at(2026, 10, 7, 9, 45))}[("weekly", "wk")].state == "due"   # daily window over
    before = at(2026, 10, 7, 7, 40)                                       # weekly window not open yet: yesterday's occurrence governs
    assert {(s.routine, s.name): s for s in routine.plan(before)}[("weekly", "wk")].occ == "2026-09-30"


def test_monthly_first_saturday_catch_up_and_missed(w):
    w.registry({**SPEC, "m_read": ("C0", "monthly"), "m_clean": ("C1", "monthly")})
    w.maint(MAINT + '[tasks.m_clean]\nmode = "apply"\n')
    w.routine_toml(MAIN + """
[[routine]]
name = "monthly"
cadence = "monthly"
steps = ["m_read", "m_clean"]
""")
    sat = {s.name: s for s in routine.plan(at(2026, 10, 3, 4, 45)) if s.routine == "monthly"}
    assert sat["m_read"].state == "due" and sat["m_clean"].state == "due" and sat["m_clean"].occ == "2026-10-03"
    for day in (10, 17, 31):                                              # later Saturdays / month end: still October's occurrence
        late = {s.name: s for s in routine.plan(at(2026, 10, day, 12)) if s.routine == "monthly"}
        assert late["m_read"].occ == "2026-10-03" and late["m_read"].state == "due"
        assert late["m_clean"].state == "missed"
    nov = {s.name: s for s in routine.plan(at(2026, 11, 7, 5, 0)) if s.routine == "monthly"}
    assert nov["m_clean"].occ == "2026-11-07" and nov["m_clean"].state == "due"
    done_state("monthly/m_read", occ="2026-10-03")
    assert {s.name: s.state for s in routine.plan(at(2026, 10, 20, 12)) if s.routine == "monthly"}["m_read"] == "done"


def test_dst_days_in_the_plan(w):
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "02:30-04:00"'))
    just_before = at(2026, 3, 8, 3, 0) - 1                                 # 01:59:59 EST: the window has not opened
    assert routine.plan(just_before)[1].occ == "2026-03-07"
    gap = {s.name: s for s in routine.plan(at(2026, 3, 8, 3, 0))}           # spring forward: the window is 03:00-04:00 that day
    assert gap["clean_a"].state == "due" and gap["clean_a"].occ == "2026-03-08" and gap["clean_a"].window_start == at(2026, 3, 8, 3, 0)
    assert states(at(2026, 3, 8, 3, 59, 59))["clean_a"] == "due" and states(at(2026, 3, 8, 4, 0))["clean_a"] == "missed"
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "01:00-03:00"'))
    first = at(2026, 11, 1, 1, 30)                                          # fall back: 01:30 EDT then 01:30 EST
    second = first + 3600
    assert datetime.fromtimestamp(second, TZ).strftime("%H:%M %Z") == "01:30 EST"
    assert states(first)["clean_a"] == "due" and states(second)["clean_a"] == "due"
    assert states(at(2026, 11, 1, 2, 59, 59))["clean_a"] == "due" and states(at(2026, 11, 1, 3, 0))["clean_a"] == "missed"
    p = routine.plan(first)[1]
    assert p.window_end - p.window_start == 3 * 3600                         # 01:00 EDT .. 03:00 EST


def test_unavailable_and_disabled_steps(w):
    w.registry({k: v for k, v in SPEC.items() if k != "heavy"})
    s = {x.name: x for x in routine.plan(FRI)}
    assert s["heavy"].state == "unavailable" and "not registered" in s["heavy"].reason and s["heavy"].disruptive
    w.routine_toml(MAIN.replace('"verify"]', '"verify", {task = "chk_a", enabled = false}]').replace('"chk_a", "clean_a"', '"clean_a"'))
    assert [s.state for s in routine.plan(FRI) if s.name == "chk_a"] == ["disabled"]


def test_check_tier_tasks_are_continuous_never_routine_governed(w):
    w.registry({**SPEC, "disk_forecast": ("C0", "check")})
    w.routine_toml(MAIN.replace('steps = ["chk_a",', 'steps = ["disk_forecast", "chk_a",'))
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["disk_forecast"].state == "continuous" and "check tier" in p["disk_forecast"].reason and p["disk_forecast"].next_due is None
    assert "disk_forecast" not in routine.due(FRI) and "chk_a" in routine.due(FRI)
    g = guard(w)
    for n in range(5):                                                      # the 15-minute tier runs it all day, every day
        d = g.begin(core.REGISTRY["disk_forecast"], False)
        assert d.run and "continuous" in d.reason and d.steps == []
        g.after(core.REGISTRY["disk_forecast"], d, Result("ok", "fine"), 0.1)
    assert "daily/disk_forecast" not in routine.load_state()["steps"]


def test_gates_are_listed_but_not_probed_by_plan(w, monkeypatch):
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (_ for _ in ()).throw(AssertionError("plan() must not probe gates")))
    routine.plan(FRI)
    routine.due(FRI)


# =========================================================================== change log
def test_record_change_fields_and_bytes():
    r = routine.record_change("docker_images", "cleanup", "removed 3 images", 100, 40, now=FRI)
    assert r == {"ts": FRI, "task": "docker_images", "kind": "cleanup", "detail": "removed 3 images", "bytes": 60,
                 "outcome": "done", "verified": False, "before": "100", "after": "40"}
    r = routine.record_change("caps", "config", "ceiling 40g", {"failed_units": "ok"}, {"failed_units": "ok"}, now=FRI + 1)
    assert r["verified"] is True and r["bytes"] == 0
    r = routine.record_change("caps", "config", "x", {"failed_units": "ok"}, {"failed_units": "warn"}, now=FRI + 2)
    assert r["verified"] is False
    assert routine.record_change("t", "bogus", "d", now=FRI)["kind"] == "maintenance"
    assert routine.record_change("t", "restart", "d", bytes=5, verified=True, outcome="done", now=FRI)["verified"] is True
    lines = [json.loads(ln) for ln in (core.STATE_DIR / "changes.jsonl").read_text().splitlines()]
    assert len(lines) == 5 and set(lines[0]) >= {"ts", "task", "kind", "detail", "bytes", "outcome", "verified"}


def test_record_change_redacts_and_never_raises(monkeypatch, tmp_path):
    r = routine.record_change("t", "cleanup", "see https://x.example/a?token=SECRET1234 mail me@x.com " + "A" * 60 + " é\n/home/ohmz/a/b/c/d/e/f", now=FRI)
    d = r["detail"]
    assert "SECRET" not in d and "me@x.com" not in d and "AAAAAAAAAA" not in d and "\n" not in d and d.isascii()
    assert "/home/ohmz/a/b/c" in d and "/d/e/f" not in d
    bad = tmp_path / "file"
    bad.write_text("x")
    monkeypatch.setattr(core, "STATE_DIR", bad / "sub")                      # cannot create the directory
    routine.record_change("t", "cleanup", "d", now=FRI)                      # logs to stderr, raises nothing


def test_read_changes_newest_first_and_limit():
    for i in range(130):
        routine.record_change(f"t{i}", "cleanup", f"d{i}", bytes=i, verified=bool(i % 2), now=FRI + i)
    with open(core.STATE_DIR / "changes.jsonl", "a") as f:
        f.write("not json\n[1,2]\n{\"task\":\"no ts\"}\n")
    ch = routine.read_changes(100)
    assert len(ch) == 100 and ch[0]["task"] == "t129" and ch[-1]["task"] == "t30" and set(ch[0]) == {"ts", "task", "kind", "detail", "bytes", "outcome", "verified"}
    assert [c["ts"] for c in ch] == sorted((c["ts"] for c in ch), reverse=True)
    assert len(routine.read_changes(5, FRI + 126)) == 4 and routine.read_changes(0) == []


def test_concurrent_writers_do_not_interleave():
    def go(n):
        for i in range(40):
            routine.record_change(f"w{n}", "cleanup", "x" * 150 + str(i), now=FRI)
    ts = [threading.Thread(target=go, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = (core.STATE_DIR / "changes.jsonl").read_text().splitlines()
    assert len(lines) == 240 and all(json.loads(ln)["task"].startswith("w") for ln in lines)


# =========================================================================== canary
def test_canary_caps_first_run_only(w):
    w.maint('[caps]\nmax_gib_per_run = 40\nmax_items_per_run = 500\n[tasks.clean_a]\nmode = "apply"\nmax_gib_per_run = 15\nmax_items_per_run = 12\n[tasks.clean_b]\nmode = "report"\n')
    assert routine.canary_caps("clean_a") == {"max_gib_per_run": 1.5, "max_items_per_run": 1}      # 10 % of 15 GiB / 12 items, items >= 1
    assert routine.canary_caps("clean_b") == {"max_gib_per_run": 4.0, "max_items_per_run": 50}       # global caps
    st = routine.load_state()
    st["canary"]["clean_a"] = {"runs": 1}
    assert routine.canary_caps("clean_a", st) is None                                               # graduated
    assert routine.canary_caps("clean_b", st) is not None


def test_canary_per_step_and_per_routine_switches(w):
    w.routine_toml(MAIN.replace('steps = ["chk_a", "clean_a"', 'steps = [{task = "clean_a", canary_runs = 2}').replace(', "clean_b"', ', "clean_b"'))
    st = routine.load_state()
    st["canary"]["clean_a"] = {"runs": 1}
    assert routine.canary_caps("clean_a", st) is not None                       # needs 2 runs
    st["canary"]["clean_a"] = {"runs": 2}
    assert routine.canary_caps("clean_a", st) is None
    w.routine_toml(MAIN.replace('name = "daily"', 'name = "daily"\ncanary = false'))
    assert routine.canary_caps("clean_a") is None
    w.routine_toml(MAIN.replace("[freeze]", "[canary]\nruns = 0\n[freeze]"))
    assert routine.canary_caps("clean_a") is None


# =========================================================================== post-check
def _checks(world, **results):
    """Register C0 checks whose run() returns the given status (str or callable)."""
    reg = dict(core.REGISTRY)
    for name, status in results.items():
        def run(ctx, status=status):
            r = status(ctx) if callable(status) else status
            return r if isinstance(r, Result) else Result(r, f"{r}")
        reg[name] = core.Task(name, "C0", reg[name].tier if name in reg else "check", run, title=name)
    world.mp.setattr(core, "REGISTRY", reg)


def test_post_check_unchanged_or_better_is_ok(w):
    _checks(w, a="ok", b="info", c="warn")
    ok, why = routine.post_check(["a", "b", "c"], {"a": "ok", "b": "ok", "c": "crit"})
    assert ok and "3/3" in why


def test_post_check_regression_and_unknown(w):
    _checks(w, a="warn", b="ok")
    ok, why = routine.post_check(["a", "b", "ghost"], {"a": "ok", "b": "ok"})
    assert not ok and "a: ok->warn" in why and "ghost: unknown check" in why and "b:" not in why


def test_post_check_fail_closed_rules(w):
    _checks(w, boom=lambda ctx: (_ for _ in ()).throw(RuntimeError("x")), fresh="warn", err="error", quiet=Result("warn", "w", alert=False))
    ok, why = routine.post_check(["fresh"], {})
    assert not ok and "fresh: warn, no baseline" in why                          # no baseline + not ok = not verified
    assert not routine.post_check(["boom"], {"boom": "ok"})[0]
    assert "err: error" in routine.post_check(["err"], {"err": "ok"})[1]
    assert routine.post_check(["quiet"], {"quiet": "ok"})[0]                     # alert=False findings never count
    assert routine.post_check([], {})[0]


def test_post_check_refuses_non_c0(w):
    ran = []
    reg = dict(core.REGISTRY)
    reg["cleaner"] = core.Task("cleaner", "C1", "daily", lambda ctx: ran.append(1) or Result("ok", "x"))
    w.mp.setattr(core, "REGISTRY", reg)
    ok, why = routine.post_check(["cleaner"], {"cleaner": "ok"})
    assert not ok and "not a C0 check" in why and ran == []                      # never runs a mutating task as a "check"


def test_post_check_time_budget_and_baseline_from_status(w, monkeypatch):
    _checks(w, a="ok", b="ok")
    ticks = iter([0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
    with monkeypatch.context() as m:
        m.setattr(routine.time, "monotonic", lambda: next(ticks))
        ok, why = routine.post_check(["a", "b"], {"a": "ok", "b": "ok"}, timeout_s=10)
    assert not ok and "b: not run (time budget)" in why
    w.status({"a": {"status": "warn", "alert": True}, "b": {"status": "warn", "alert": False}})
    assert routine.snapshot(["a", "b", "zzz"]) == {"a": "warn", "b": "info"}   # alert=False reads as info; missing is absent


def test_post_check_keeps_the_callers_alarm(w):
    _checks(w, a="ok")
    signal.alarm(0)
    signal.alarm(90)
    try:
        assert routine.post_check(["a"], {"a": "ok"})[0]
        left = signal.alarm(0)
        assert 60 <= left <= 90, left                                            # the task's own timeout is still armed
    finally:
        signal.alarm(0)


# =========================================================================== RunGuard
def task_of(world, name):
    return core.REGISTRY[name]


def guard(world, now=FRI, **kw):
    return routine.RunGuard(now_fn=lambda: now, **kw)


def begin(world, name, apply=True, now=FRI, manual=False):
    g = guard(world, now)
    return g, g.begin(task_of(world, name), apply, manual)


def test_guard_unmanaged_and_continuous_tasks(w):
    w.registry({**SPEC, "other_c1": ("C1", "daily"), "other_c0": ("C0", "daily"), "pressure_response": ("C1", "check")})
    w.maint(MAINT + '[tasks.other_c1]\nmode = "apply"\n[tasks.pressure_response]\nmode = "apply"\n')
    d = begin(w, "other_c1")[1]
    assert d.run and not d.apply and "not in routine.toml" in d.reason
    assert begin(w, "other_c0")[1].apply                                         # C0 has no apply to refuse
    d = begin(w, "pressure_response")[1]
    assert d.run and d.apply and d.applying and "continuous" in d.reason          # check tier: never window-governed


def test_guard_invalid_config_refuses_apply(w):
    w.routine_toml("broken [")
    d = begin(w, "clean_a")[1]
    assert d.run and not d.apply and not d.applying and "invalid" in d.reason
    assert begin(w, "chk_a")[1].run


def test_guard_in_window_apply_with_canary_and_pre_snapshot(w):
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    g, d = begin(w, "clean_a")
    assert d.run and d.apply and d.applying and d.caps == {"max_gib_per_run": 4.0, "max_items_per_run": 50}
    assert d.steps == [("daily", "clean_a", "2026-10-02", True)]
    assert routine.load_state()["pre"]["daily@2026-10-02"]["checks"] == {"chk_a": "ok"}
    cfg = {"tasks": {"clean_a": {"mode": "apply", "max_items_per_run": 12}}, "caps": {}}
    t2 = g.task_cfg(cfg, task_of(w, "clean_a"), d)
    # the canary only ever LOWERS: the task's own 12 stays (50 would have raised it); a limit the task has no key for goes into
    # the copy's [caps], not into [tasks.clean_a], so the task's own "no explicit limit" logic still sees an unset key
    assert t2["tasks"]["clean_a"] == {"mode": "apply", "max_items_per_run": 12} and t2["caps"] == {"max_gib_per_run": 4.0}
    assert cfg["tasks"]["clean_a"]["max_items_per_run"] == 12 and cfg["caps"] == {}  # the original is untouched
    big = {"tasks": {"clean_a": {"mode": "apply", "max_items_per_run": 100, "max_gib_per_run": 9}}, "caps": {"max_items_per_run": 500}}
    t3 = g.task_cfg(big, task_of(w, "clean_a"), d)["tasks"]["clean_a"]
    assert t3 == {"mode": "apply", "max_items_per_run": 50, "max_gib_per_run": 4.0}      # own limits above the canary are lowered
    assert g.task_cfg(cfg, task_of(w, "clean_a"), routine.Decision()) is cfg


def test_guard_after_records_change_verifies_and_graduates_canary(w):
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    g, d = begin(w, "clean_a")
    w.audit_done("clean_a", FRI + 5, "docker-image-rm", "sha256:abc", 2 * GIB)
    rec = g.after(task_of(w, "clean_a"), d, Result("ok", "freed 2.0 GiB (1 images)", reclaimed_bytes=2 * GIB), 3.0)
    assert rec["verified"] is True and rec["bytes"] == 2 * GIB and rec["kind"] == "cleanup" and rec["task"] == "clean_a"
    st = routine.load_state()
    assert st["steps"]["daily/clean_a"]["done"] and st["steps"]["daily/clean_a"]["applied"] and st["canary"]["clean_a"]["runs"] == 1
    assert st["halted"] == {}
    assert routine.read_changes()[0]["verified"] is True
    assert routine.canary_caps("clean_a") is None                                     # graduated after one real change
    assert begin(w, "clean_a", now=FRI + 60)[1].run is False                          # done for this occurrence


def test_guard_after_without_actions_changes_nothing(w):
    g, d = begin(w, "clean_a")
    assert g.after(task_of(w, "clean_a"), d, Result("ok", "nothing to clean"), 1.0) is None
    st = routine.load_state()
    assert st["steps"]["daily/clean_a"]["done"] and "clean_a" not in st["canary"]     # a no-op run does not graduate the canary
    assert not (w.state / "changes.jsonl").exists()


def test_guard_report_only_run_does_not_satisfy_apply_step(w):
    g, d = begin(w, "clean_a", apply=False)                                            # tier started without --apply
    assert d.run and not d.applying and d.caps is None
    g.after(task_of(w, "clean_a"), d, Result("info", "report: would free 1 GiB"), 1.0)
    assert routine.load_state()["steps"]["daily/clean_a"]["done"] is False
    assert states(FRI)["clean_a"] == "due"                                              # still due for a real apply run


def test_guard_regression_halts_the_rest_of_the_occurrence(w):
    state = {"v": "ok"}
    _checks(w, chk_a=lambda ctx: state["v"])
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    w.routine_toml(MAIN.replace('"clean_b", {task = "heavy", disruptive = true}', '{task = "clean_b", disruptive = true}'))
    w.maint('[tasks.clean_a]\nmode = "apply"\n[tasks.clean_b]\nmode = "apply"\n')
    g, d = begin(w, "clean_a")
    state["v"] = "warn"                                                                  # the cleanup broke something
    w.audit_done("clean_a", FRI + 2)
    rec = g.after(task_of(w, "clean_a"), d, Result("ok", "freed 1 GiB", reclaimed_bytes=GIB), 1.0)
    assert rec["verified"] is False
    h = routine.load_state()["halted"]["daily@2026-10-02"]
    assert h["by"] == "clean_a" and "chk_a: ok->warn" in h["why"]
    assert any(r["outcome"].startswith("regressed") for r in map(json.loads, (w.log / "audit.jsonl").read_text().splitlines()))
    d2 = begin(w, "clean_b", now=FRI + 10)[1]
    assert not d2.run and "halted" in d2.reason
    assert begin(w, "chk_a", now=FRI + 10)[1].run                                       # read-only steps are never halted
    assert routine.due(FRI + 10) == ["chk_a"]                                           # verify follows once chk_a has had its turn
    assert states(FRI + 10)["clean_b"] == "halted" and states(FRI + 10)["verify"] == "waiting"
    assert routine.main(["clear-halt", "daily"]) == 0 and routine.load_state()["halted"] == {}
    assert begin(w, "clean_b", now=FRI + 20)[1].run
    assert begin(w, "clean_b", now=at(2026, 10, 3, 7, 40))[1].run                       # the next occurrence is a clean slate anyway


def test_guard_not_in_window_and_manual_override(w):
    g, d = begin(w, "clean_a", now=at(2026, 10, 2, 12, 0))
    assert not d.run and d.reason.startswith("missed") and "window over" in d.reason
    g, m = begin(w, "clean_a", now=at(2026, 10, 2, 12, 0), manual=True)
    assert m.run and m.apply and m.applying and "manual run" in m.reason and m.caps is not None   # owner asked: window/done do not stop it
    (w.conf / "PAUSE").write_text("x")
    assert begin(w, "clean_a", manual=True)[1].apply is False                             # PAUSE always wins


def test_manual_override_is_recorded_as_such(w):
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    late = at(2026, 10, 2, 20, 0)                                                         # evening, inside the freeze, window long over
    g = guard(w, late)
    d = g.begin(task_of(w, "clean_a"), True, manual=True, force=True)                     # a freeze is only overridden by --force
    assert d.overridden and d.applying
    w.audit_done("clean_a", late + 3)
    rec = g.after(task_of(w, "clean_a"), d, Result("ok", "freed", reclaimed_bytes=GIB), 1.0)
    assert rec["outcome"] == "override" and rec["verified"] is True


def test_guard_pause_runs_report_only(w):
    (w.conf / "PAUSE").write_text("x")
    d = begin(w, "clean_a")[1]
    assert d.run and not d.apply and not d.applying and d.reason == "PAUSE: report only" and d.caps is None
    assert begin(w, "chk_a")[1].run and begin(w, "clean_b")[1].run


def test_guard_gates_defer_and_retry(w, monkeypatch):
    seen = []
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (seen.append(n) or (True, "backup-system.service is active")))
    g, d = begin(w, "clean_a")
    assert not d.run and "gate backup busy" in d.reason and seen == ["backup"]
    rec = routine.load_state()["steps"]["daily/clean_a"]
    assert rec["deferrals"] == 1 and not rec["done"] and rec["status"] == "deferred"
    assert routine.plan(FRI)[1].last_outcome == "deferred" and states(FRI)["clean_a"] == "due"
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (False, "idle"))
    assert begin(w, "clean_a", now=FRI + 600)[1].run
    # a report-mode cleaner is not disruptive: no gate is consulted at all
    seen.clear()
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (seen.append(n) or (True, "busy")))
    assert begin(w, "clean_b")[1].run and seen == []
    # a heavy read-only (C2) step is gated too
    assert not begin(w, "heavy")[1].run and seen == ["backup"]


def test_guard_pressure_gate_reads_gate_level_not_the_host_level(w):
    """io-only or gpu-only pressure (level 3, gate_level 0) must not defer maintenance; memory/cpu pressure still does. A record without
    gate_level is read through its dims (max of mem and cpu); one without either falls back to the host level."""
    w.routine_toml(MAIN.replace('default_gates = ["backup"]', 'default_gates = ["pressure"]'))

    def at(metrics):
        w.status({"pressure_state": {"status": "info", "last_run": FRI - 60, "metrics": metrics}}, FRI)
        return begin(w, "clean_a")[1]
    assert at({"level": 3, "gate_level": 0, "level_name": "io stall"}).run               # io stall: nobody waits on it
    d = at({"level": 3, "gate_level": 2})
    assert not d.run and "pressure level 2" in d.reason                                  # the gate level is what is compared and shown
    assert at({"level": 4, "dims": {"io": {"level": 4}, "mem": {"level": 1}, "cpu": {"level": 0}}}).run      # pre-gate_level record
    assert not at({"level": 4, "dims": {"io": {"level": 0}, "mem": {"level": 3}, "cpu": {"level": 0}}}).run
    assert not at({"level": 2}).run                                                      # nothing but the host level: stricter reading
    assert routine.pressure_level(FRI) == 2                                              # the freeze-lift reads the same level
    at({"level": 5, "gate_level": 1})
    assert routine.pressure_level(FRI) == 1


def test_guard_pressure_gate(w):
    w.routine_toml(MAIN.replace('default_gates = ["backup"]', 'default_gates = ["pressure"]'))
    assert begin(w, "clean_a")[1].run                                                   # no status.json yet: no pressure data
    w.status({"pressure_state": {"status": "ok", "last_run": FRI - 60, "metrics": {"level": 1}}}, FRI)
    assert begin(w, "clean_a")[1].run
    w.status({"pressure_state": {"status": "warn", "last_run": FRI - 60, "metrics": {"level": 2}}}, FRI)
    d = begin(w, "clean_a")[1]
    assert not d.run and "pressure level 2" in d.reason
    w.status({"pressure_state": {"status": "ok", "last_run": FRI - 7200, "metrics": {"level": 0}}}, FRI)
    assert "old" in begin(w, "clean_a", now=FRI + 5)[1].reason                              # stale pressure data = busy
    w.status({"pressure_state": {"status": "error", "last_run": FRI, "metrics": {}}}, FRI)
    assert not begin(w, "clean_a", now=FRI + 6)[1].run
    w.status({"other": {}}, FRI)
    assert begin(w, "clean_a", now=FRI + 7)[1].run                                       # pressure_state not installed: idle
    (w.state / "status.json").write_text("{broken")
    assert not begin(w, "clean_a", now=FRI + 8)[1].run                                   # unreadable status: fail closed
    assert routine.gate_busy("pressure", routine.RoutineConfig(max_pressure_level=1), FRI)[0] is True


def test_guard_ext_gate_fails_closed(w):
    assert routine._ext_busy("nonexistent-gate")[0] is True                              # unknown gate name = busy
    assert routine.gate_busy("nonexistent-gate")[0] is True


def test_guard_advisory_mode_never_blocks(w, monkeypatch):
    w.routine_toml(MAIN.replace("[settings]", "[settings]\nenforce = false"))
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (True, "busy"))
    d = begin(w, "clean_a", now=at(2026, 10, 2, 12))[1]
    assert d.run and d.would_block.startswith("missed")
    d = begin(w, "clean_a")[1]
    assert d.run and "gate backup busy" in d.would_block


def test_guard_order(w):
    w.registry({**SPEC, "x_c0": ("C0", "daily"), "x_c1": ("C1", "daily"), "cc": ("C0", "check"), "bb": ("C0", "check")})
    names = ["x_c1", "routine_verify_daily", "x_c0", "heavy", "clean_b", "clean_a", "chk_a", "cc", "bb"]
    g = guard(w)
    out = [t.name for t in g.order([core.REGISTRY[n] for n in names])]
    # managed daily-tier tasks in routine order (verify last); everything else the way cmd_run sorts it: class, then name
    assert out == ["chk_a", "clean_a", "clean_b", "heavy", "routine_verify_daily", "bb", "cc", "x_c0", "x_c1"]


def test_guard_runs_with_a_real_run_task(w):
    """End to end through core.run_task: the canary caps reach the task's Ctx, the audit trail proves the change."""
    seen = {}

    def clean(ctx):
        seen["caps"] = (ctx.cap_bytes, ctx.cap_items, ctx.apply)
        ctx.act("docker-image-rm", "img1", 100, lambda: 100)
        return Result("ok", "freed")
    reg = dict(core.REGISTRY)
    reg["clean_a"] = core.Task("clean_a", "C1", "daily", clean, title="clean a")
    w.mp.setattr(core, "REGISTRY", reg)
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, time.time())
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "00:00-24:00"').replace('evenings = "18:00-23:30"', 'x = "2020-01-01"'))
    g = routine.RunGuard()                                                    # the real clock: the window covers the whole day
    t, cfg = core.REGISTRY["clean_a"], core.load_config()
    d = g.begin(t, True)
    assert d.run and d.caps == {"max_gib_per_run": 4.0, "max_items_per_run": 50}
    res, _dur = core.run_task(t, g.task_cfg(cfg, t, d), True)
    assert seen["caps"] == (int(4.0 * GIB), 50, True) and res.reclaimed_bytes == 100
    rec = g.after(t, d, res, 0.1)
    assert rec["task"] == "clean_a" and rec["bytes"] == 100 and rec["verified"] is True and rec["kind"] == "cleanup"
    assert routine.load_state()["canary"]["clean_a"]["runs"] == 1 and routine.canary_caps("clean_a") is None


# =========================================================================== tick
def test_run_due_runs_in_order_and_marks_from_status(w):
    calls = []

    def runner(task, apply):
        calls.append((task, apply))
        # what cli.cmd_run would leave in status.json (no RunGuard glue in this test)
        tasks = (core.read_json(w.state / "status.json", {}) or {}).get("tasks", {})
        tasks[task] = {"status": "ok", "summary": "fine", "last_run": FRI, "mode": "apply" if apply else "check"}
        w.status(tasks, FRI)
        return 0
    rows = routine.run_due(True, now_fn=lambda: FRI, runner=runner)
    assert [c[0] for c in calls] == ["chk_a", "clean_a", "clean_b", "heavy", "routine_verify_daily"]
    assert [r["state"] for r in rows] == ["done"] * 5
    st = routine.load_state()["steps"]
    assert all(st[f"daily/{n}"]["done"] for n in ("chk_a", "clean_a", "clean_b", "heavy", "verify"))
    assert routine.run_due(True, now_fn=lambda: FRI, runner=runner) == []             # idempotent: nothing left


def test_run_due_dry_run_leaves_apply_steps_due_and_stops_loops(w):
    calls = []

    def runner(task, apply):
        calls.append(task)
        tasks = (core.read_json(w.state / "status.json", {}) or {}).get("tasks", {})
        tasks[task] = {"status": "ok", "summary": "fine", "last_run": FRI, "mode": "dry-run"}
        w.status(tasks, FRI)
        return 0
    rows = routine.run_due(False, now_fn=lambda: FRI, runner=runner)
    assert len(calls) == 4 and len(set(calls)) == 4                                   # each step is attempted once per call
    assert {r["step"]: r["state"] for r in rows}["daily/clean_a"] == "ok"            # ran, but a dry run is not "done"
    assert states(FRI)["clean_a"] == "due" and states(FRI)["chk_a"] == "done"
    # verify waits for the apply step a dry run could not complete (it would otherwise summarise a day it never saw), and
    # is released at the end of the window: it runs once then, with the apply step still open
    assert states(FRI)["verify"] == "waiting" and "routine_verify_daily" not in calls
    assert states(at(2026, 10, 2, 9, 30))["verify"] == "due"


def test_run_due_task_that_did_not_run_is_retried_later_and_errors_are_isolated(w):
    def runner(task, apply):
        if task == "clean_a":
            raise RuntimeError("boom")
        return 0                                                                      # tier lock busy / disabled: status untouched
    rows = routine.run_due(True, now_fn=lambda: FRI, runner=runner)
    assert {r["task"]: r["rc"] for r in rows}["clean_a"] == 255 and all(r["state"] == "not run" for r in rows)
    assert states(FRI)["chk_a"] == "due"


def test_run_due_stale_status_entry_is_not_trusted(w):
    w.status({"chk_a": {"status": "ok", "summary": "old", "last_run": FRI - 3600, "mode": "check"}}, FRI)
    rows = routine.run_due(False, now_fn=lambda: FRI, runner=lambda t, a: 0, cfg=routine.load_config())
    assert all(r["state"] == "not run" for r in rows)


def test_run_due_stops_after_a_halt(w):
    """After a post-check halt the next disruptive step is skipped by the very next evaluation."""
    w.routine_toml(MAIN.replace('"clean_b", {task = "heavy", disruptive = true}', '{task = "clean_b", disruptive = true}'))
    w.maint('[tasks.clean_a]\nmode = "apply"\n[tasks.clean_b]\nmode = "apply"\n')
    calls = []

    def runner(task, apply):
        calls.append(task)
        if task == "clean_a":
            st = routine.load_state()
            st["halted"]["daily@2026-10-02"] = {"ts": FRI, "by": "clean_a", "why": "x"}
            routine.save_state(st)
        tasks = (core.read_json(w.state / "status.json", {}) or {}).get("tasks", {})
        tasks[task] = {"status": "ok", "summary": "x", "last_run": FRI, "mode": "apply"}
        w.status(tasks, FRI)
        return 0
    routine.run_due(True, now_fn=lambda: FRI, runner=runner)
    assert "clean_b" not in calls and "chk_a" in calls and "routine_verify_daily" in calls


# =========================================================================== export + calendar
def test_export_shape(w):
    routine.record_change("docker_images", "cleanup", "removed 2 images", bytes=3 * GIB, verified=True, now=FRI - 100)
    w.status({"clean_b": {"status": "info", "summary": "x", "last_run": FRI - 30}}, FRI)
    d = routine.export(FRI, timers={})
    assert set(d) == {"schema", "generated_at", "timezone", "valid", "enforce", "errors", "windows", "freeze", "state", "routine", "changes", "calendar"}
    assert d["generated_at"] == FRI and d["valid"] and d["timezone"] == "America/Toronto"
    assert d["windows"]["daily"] == "07:30-09:30" and d["freeze"] == {"evenings": "18:00-23:30"}
    r = d["routine"][0]
    assert r["name"] == "daily" and r["cadence"] == "daily" and r["window"] == "07:30-09:30" and r["occurrence"] == "2026-10-02"
    assert [s["task"] for s in r["steps"]] == ["chk_a", "clean_a", "clean_b", "heavy", "routine_verify_daily"]
    for s in r["steps"]:
        assert {"task", "class", "next_due", "last_run", "last_outcome", "mode"} <= set(s)
    cb = r["steps"][2]
    assert cb["last_run"] == FRI - 30 and cb["last_outcome"] == "info" and cb["mode"] == "report"      # status.json is the truth for last run
    assert r["steps"][0]["last_run"] is None and r["steps"][0]["last_outcome"] == "never"
    assert r["next_run"] == FRI                                                                           # steps are due right now
    assert d["changes"] == [{"ts": FRI - 100, "task": "docker_images", "kind": "cleanup", "detail": "removed 2 images",
                             "bytes": 3 * GIB, "outcome": "done", "verified": True}]
    assert len(d["calendar"]) == 14 and d["calendar"][0]["date"] == "2026-10-02" and d["calendar"][-1]["date"] == "2026-10-15"
    json.dumps(d)


def test_export_next_run_when_idle_is_the_next_window(w):
    done_state("daily/chk_a", "daily/clean_a", "daily/clean_b", "daily/heavy", "daily/verify", occ="2026-10-02")
    d = routine.export(at(2026, 10, 2, 12), timers={})
    assert d["routine"][0]["next_run"] == at(2026, 10, 3, 7, 30) and d["routine"][0]["counts"] == {"done": 5}
    assert d["routine"][0]["next_window"] == [at(2026, 10, 3, 7, 30), at(2026, 10, 3, 9, 30)]


def test_export_changes_capped_and_sorted(w):
    for i in range(150):
        routine.record_change("t", "cleanup", f"c{i}", now=FRI + i)
    ch = routine.export(FRI + 200, timers={})["changes"]
    assert len(ch) == 100 and ch[0]["detail"] == "c149" and ch[-1]["detail"] == "c50"


def test_export_invalid_config_still_exports(world):
    world.routine_toml("junk [")
    d = routine.export(FRI, timers={})
    assert d["valid"] is False and d["routine"] == [] and d["errors"] and len(d["calendar"]) == 14


def test_export_size_cap(w):
    for i in range(100):
        routine.record_change("t", "cleanup", "x" * 190, now=FRI + i)
    assert len(json.dumps(routine.export(FRI + 500, timers={}))) < 200_000


def test_write_export_atomic_and_public(w):
    p = routine.write_export(now=FRI, timers={})
    assert p == w.state / "public" / "routine.json" and oct(p.stat().st_mode & 0o777) == "0o644"
    assert json.loads(p.read_text())["generated_at"] == FRI and not list(p.parent.glob("*.tmp"))


def test_system_timers_parsing(w):
    w.sh.when(["systemctl", "list-timers"], json.dumps([
        {"next": 1790920991971339, "left": 1, "last": 0, "passed": 0, "unit": "a.timer", "activates": "a.service"},
        {"next": None, "left": None, "last": 1790279831404059, "passed": 1, "unit": "b.timer", "activates": None}, {"junk": 1}, "x"]))
    t = routine.system_timers()
    assert t["a.timer"] == {"next": 1790920991.971339, "last": None} and t["b.timer"]["next"] is None and len(t) == 2
    w.sh.when(["systemctl", "list-timers"], "not json")
    assert routine.system_timers() == {}
    w.sh.when(["systemctl", "list-timers"], "", rc=1)
    assert routine.system_timers() == {}


def test_calendar_first_saturday_and_system_overrides(w):
    w.routine_toml(MAIN + """
[[routine]]
name = "monthly"
cadence = "monthly"
steps = ["chk_a"]
[[system]]
name = "backup-system"
title = "System backup"
kind = "backup"
at = "Sat 01:00"
unit = "backup-system.timer"
[[system]]
name = "diun"
title = "Diun scan"
at = "09:00"
managed = "app"
[[system]]
name = "twice"
title = "Twice a day"
kind = "bogus"
at = ["06:00", "18:00"]
""")
    now = at(2026, 10, 2, 12)
    real_next = at(2026, 10, 3, 1, 13, 9)
    cal = routine.calendar(routine.load_config(), now, {"backup-system.timer": {"next": real_next, "last": None}})
    by = {d["date"]: d["items"] for d in cal}
    assert len(cal) == 14
    sat = by["2026-10-03"]
    assert [i["time"] for i in sat][:2] == ["01:13", "04:30"] and sat[0]["kind"] == "backup" and sat[0]["title"] == "System backup"   # systemd's real time
    assert any(i["kind"] == "monthly" and i["time"] == "04:30" and "04:30-07:00" in i["title"] for i in sat)                          # the 1st Saturday
    assert not any(i["kind"] == "monthly" for i in by["2026-10-10"])
    assert by["2026-10-10"][0]["time"] == "01:00" and by["2026-10-10"][0]["title"] == "System backup"                                  # later runs: nominal
    assert any(i["kind"] == "daily" for i in by["2026-10-07"]) and not any(i["kind"] == "weekly" for i in by["2026-10-07"])   # no weekly entry here
    assert [i["time"] for i in by["2026-10-04"] if i["title"] == "Twice a day"] == ["06:00", "18:00"]
    assert all(i["kind"] in ("daily", "weekly", "monthly", "backup", "system") for items in by.values() for i in items)
    assert all(i["source"] in ("routine", "os", "app") for items in by.values() for i in items)
    assert sum(1 for i in by["2026-10-02"] if i["kind"] == "daily") == 1


def test_calendar_across_the_fall_back_week_and_dated_freeze(w):
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "01:30-03:00"').replace('evenings = "18:00-23:30"', 'xmas = "2026-11-03..2026-11-04"') + """
[[system]]
name = "n"
title = "Night job"
at = "02:30"
""")
    cal = {d["date"]: d["items"] for d in routine.calendar(routine.load_config(), at(2026, 10, 30, 12), {})}
    assert list(cal)[0] == "2026-10-30" and "2026-11-12" in cal and len(cal) == 14
    for day in ("2026-10-31", "2026-11-01", "2026-11-02"):
        assert any(i["kind"] == "daily" and i["time"] == "01:30" for i in cal[day])
    assert any(i["title"].startswith("Change freeze: xmas") and i["time"] == "00:00" for i in cal["2026-11-03"])
    cal2 = {d["date"]: d["items"] for d in routine.calendar(routine.load_config(), at(2026, 3, 5, 12), {})}   # spring forward on Mar 8
    assert [i["time"] for i in cal2["2026-03-08"] if i["title"] == "Night job"] == ["03:00"]                    # 02:30 does not exist
    assert [i["time"] for i in cal2["2026-03-07"] if i["title"] == "Night job"] == ["02:30"]


# =========================================================================== built-in steps
def ctx_for(world, name, now=FRI):
    return Ctx(core.load_config(), name, False, now)


def check_result(r):
    assert isinstance(r, Result) and r.status in ("ok", "info", "warn", "crit", "skipped", "error")
    assert len(r.summary) <= 140 and r.summary.isascii() and "\n" not in r.summary
    assert len(r.items) <= 12 and all(isinstance(v, (str, int, float, bool, type(None))) for it in r.items for v in it.values())
    assert all(isinstance(v, (str, int, float, bool)) for v in r.metrics.values())
    return r


def jl(path, recs):
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def test_spike_review(w):
    c = ctx_for(w, "routine_spike_review")
    r = check_result(routine.spike_review(c))
    assert r.status == "warn" and "sampler has no samples" in r.summary                    # no data is not "all good"
    jl(w.state / "samples.jsonl", [{"t": FRI - 86400 + i * 900, "kind": "sample", "host": {}, "c": {}} for i in range(96)])
    jl(w.state / "spikes.jsonl", [
        {"t": FRI - 7200, "kind": "spike", "id": "a", "state": "closed", "peak_level": 2, "duration_s": 540, "contributors": [{"name": "tunarr-host-net"}],
         "outcome": "resolved by itself, nothing killed", "restarted": [], "stopped": [], "oom_kills": 0},
        {"t": FRI - 86400 * 3, "kind": "spike", "id": "old", "peak_level": 5, "restarted": ["x"], "oom_kills": 0},
        {"t": FRI - 3600, "kind": "other"}])
    r = check_result(routine.spike_review(c))
    assert r.status == "info" and r.summary.startswith("1 spike(s) in 24 h, worst L2, nothing killed") and r.metrics["samples_24h"] == 96
    assert r.metrics["top"] == "tunarr-host-net" and r.items[0]["minutes"] == 9 and not r.alert
    jl(w.state / "spikes.jsonl", [{"t": FRI - 600, "kind": "spike", "id": "b", "peak_level": 4, "restarted": ["comfyui"], "stopped": [], "oom_kills": 0}])
    r = routine.spike_review(c)
    assert r.status == "warn" and "restart/stop/OOM" in r.summary
    (w.state / "samples.jsonl").write_text("".join(json.dumps({"t": FRI - 86400 + i * 900 * 4}) + "\n" for i in range(24)))
    r = routine.spike_review(c)
    assert r.metrics["sampler_cover_pct"] == 25 and r.status == "warn"                       # the sampler fell behind


def test_verify_reports_steps_postcheck_and_unverified_changes(w):
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    c = ctx_for(w, "routine_verify_daily")
    st = routine.load_state()
    for s in ("chk_a", "clean_a", "clean_b", "heavy"):
        routine.mark_run(st, "daily", s, "2026-10-02", FRI, "ok", "x", applied=True)
    st["pre"]["daily@2026-10-02"] = {"ts": FRI, "checks": {"chk_a": "ok"}}
    routine.save_state(st)
    r = check_result(routine._verify(c, "daily"))
    assert r.status == "ok" and r.summary.startswith("verify daily ok: 4/4 steps done") and not r.alert
    routine.record_change("clean_a", "cleanup", "freed", bytes=5, verified=False, now=FRI + 1)
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 5), "daily")
    assert r.status == "warn" and "1 change(s) not verified" in r.summary and r.alert
    _checks(w, chk_a="crit")
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 6), "daily")
    assert "post-check: chk_a: ok->crit" in r.summary
    w.routine_toml("junk [")
    r = routine._verify(c, "daily")
    assert r.status == "warn" and "cannot verify" in r.summary


def test_verify_missed_is_info_not_a_failure(w):
    _checks(w, chk_a="ok")
    r = routine._verify(ctx_for(w, "routine_verify_daily", at(2026, 10, 2, 11)), "daily")        # host was off during the window
    assert r.status == "info" and "2 missed" in r.summary and not r.alert
    assert {it["state"] for it in r.items} >= {"missed"}


def test_capacity_review(w):
    c = ctx_for(w, "routine_capacity")
    r = check_result(routine.capacity_review(c))
    assert r.status == "info" and "no disk data yet" in r.summary
    mounts = [{"mount": "/", "free_h": "931.8 GiB", "used_pct": 48.6, "days": None, "level": "ok", "info": False},
              {"mount": "/mnt/backup/system", "free_h": "1.1 TiB", "used_pct": 77.6, "days": 40, "level": "ok", "info": False},
              {"mount": "/media/Immich", "free_h": "800 GiB", "used_pct": 90.0, "days": 12.3, "level": "warn", "info": False},
              {"mount": "/media/seagate18tb", "free_h": "5 TiB", "used_pct": 30, "days": None, "level": "ok", "info": True}]
    w.status({"disk_forecast": {"metrics": {"mounts": mounts}}, "growth_watch": {"metrics": {"n_over": 1, "worst_path": "/var/log", "worst_gib_day": 0.9}},
              "docker_df": {"metrics": {"images_reclaim_gib": 30, "images_reclaim_h": "30.0 GiB"}}}, FRI)
    day = 86400
    jl(w.state / "samples.jsonl", [{"t": FRI - 5 * day + i * 3600, "kind": "sample", "c": {"a": {"anon": 10 * GIB}}} for i in range(24)]
       + [{"t": FRI - 3600 * (i + 1), "kind": "sample", "c": {"a": {"anon": 16 * GIB}}} for i in range(24)])
    r = check_result(routine.capacity_review(c))
    assert r.status == "warn" and "tightest /media/Immich" in r.summary and "full in 12 d" in r.summary and "memory baseline +6.0 GiB" in r.summary
    assert r.metrics["recommendations"] == 5 and r.items[0]["mount"] == "/media/Immich" and not any(i["mount"] == "/media/seagate18tb" for i in r.items)
    assert not r.alert                                                                           # the real alert is disk_forecast's


def test_smart_selftest(w):
    scan = "/dev/sda -d scsi # /dev/sda, SCSI device\n/dev/sdb -d scsi # b\n/dev/sdc -d scsi # c\n/dev/sdd -d scsi # d\n/dev/nvme0 -d nvme # n\n"
    w.sh.when(["smartctl", "--scan"], scan)
    ok_test = {"ata_smart_self_test_log": {"standard": {"table": [
        {"type": {"string": "Short offline"}, "status": {"passed": True, "string": "Completed without error"}, "lifetime_hours": 15000}]}}}
    never = {"ata_smart_self_test_log": {"standard": {"count": 0}}}
    failed = {"ata_smart_self_test_log": {"standard": {"table": [
        {"type": {"string": "Extended offline"}, "status": {"passed": False, "string": "Read failure"}, "lifetime_hours": 100}]}}}
    asleep = {"smartctl": {"exit_status": 2, "messages": [{"string": "Device is in STANDBY mode, exit(2)", "severity": "information"}]}}
    nvme = {"nvme_self_test_log": {"current_self_test_operation": {"value": 0}}}
    by = {"/dev/sda": ok_test, "/dev/sdb": never, "/dev/sdc": failed, "/dev/sdd": asleep, "/dev/nvme0": nvme}
    w.sh.when(["smartctl", "-n", "standby", "-l", "selftest", "-j"], lambda c: json.dumps(by[c[-1]]))
    w.sh.when(["smartctl", "-n", "standby", "-A", "-j"], json.dumps({"power_on_time": {"hours": 16703}}))
    w.sh.when(["systemctl", "is-active", "smartd"], "active\n")
    r = check_result(routine.smart_selftest(ctx_for(w, "routine_smart_selftest")))
    assert r.status == "crit" and r.alert and "FAILED on /dev/sdc" in r.summary and r.metrics["failed"] == 1 and r.metrics["asleep"] == 1
    assert r.metrics["never"] == 2 and r.metrics["smartd_schedule"] is False and "no -s schedule" in r.summary
    rows = {i["disk"]: i for i in r.items}
    assert rows["/dev/sda"]["age_d"] == 71 and r.metrics["stale"] == 1 and rows["/dev/sda"]["last_test"] == "Short offline" and rows["/dev/sdc"]["result"] == "FAILED"
    assert rows["/dev/sdd"]["last_test"] == "asleep" and "1 asleep" in r.summary
    by["/dev/sdc"] = never
    r = routine.smart_selftest(ctx_for(w, "routine_smart_selftest"))
    assert r.status == "warn" and not r.alert and "reminder: smartctl -t short DEV" in r.summary
    denied = {"smartctl": {"exit_status": 2, "messages": [{"string": "Smartctl open device: /dev/sda failed: Permission denied"}]}}
    for k in by:
        by[k] = denied
    r = routine.smart_selftest(ctx_for(w, "routine_smart_selftest"))
    assert r.status == "info" and "cannot open any of 5 disks (not root?)" in r.summary and r.metrics["unreadable"] == 5
    w.sh.rules.clear()
    w.sh.when(["logger"])
    r = routine.smart_selftest(ctx_for(w, "routine_smart_selftest"))
    assert r.status == "info" and "no readable disks" in r.summary


def test_updates_review(w, monkeypatch):
    apt = ("Listing...\nlibc6/noble-updates 2.39-0ubuntu8.6 amd64 [upgradable from: 2.39-0ubuntu8.5]\n"
           "openssl/noble-security 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13-0ubuntu3.5]\nvim/noble-updates 9 amd64 [upgradable from: 8]\n")
    w.sh.when(["apt", "list", "--upgradable"], apt)
    w.sh.when(["apt-mark", "showhold"], "linux-image-generic\n")
    w.sh.when(["snap", "refresh", "--list"], "Name  Version  Rev  Size  Publisher  Notes\nfirmware-updater  0+git  258  13MB  canonical**  -\n")
    r = check_result(routine.updates_review(ctx_for(w, "routine_updates")))
    assert r.status == "warn" and r.metrics["apt"] == 3 and r.metrics["security"] == 1 and r.metrics["snap"] == 1 and r.metrics["held"] == 1
    assert r.items[0] == {"kind": "security", "package": "openssl", "detail": "3.0.13-0ubuntu3.6"} and "1 held" in r.summary
    w.sh.when(["apt", "list", "--upgradable"], "Listing...\n")
    w.sh.when(["snap", "refresh", "--list"], "")
    w.sh.when(["apt-mark", "showhold"], "")
    r = routine.updates_review(ctx_for(w, "routine_updates"))
    assert r.status in ("ok", "info", "warn") and r.metrics["apt"] == 0 and "0 apt (0 security), 0 snap" in r.summary
    w.sh.when(["apt", "list", "--upgradable"], "", rc=100)
    assert "apt state unreadable" in routine.updates_review(ctx_for(w, "routine_updates")).summary


def test_image_updates(w):
    esc = "\x1b[90m{ts}\x1b[0m \x1b[32mINF\x1b[0m \x1b[1m{msg}\x1b[0m \x1b[36mimage=\x1b[0m{img} \x1b[36mprovider=\x1b[0mdocker"
    now = at(2026, 10, 2, 12)
    lines = [esc.format(ts="Wed, 30 Sep 2026 09:00:23 EDT", msg="Image update found", img="docker.io/linuxserver/transmission:latest"),
             esc.format(ts="Thu, 01 Oct 2026 09:00:26 EDT", msg="New image found", img="docker.io/library/caddy:2.11.4@sha256:" + "0" * 64),
             "\x1b[90mThu, 01 Oct 2026 09:00:29 EDT\x1b[0m \x1b[32mINF\x1b[0m \x1b[1mJobs completed\x1b[0m \x1b[36madded=\x1b[0m1",
             "garbage line"]
    w.sh.when(["docker", "ps", "-a"], "running\n")
    w.sh.when(["docker", "logs"], "\n".join(lines))
    r = check_result(routine.image_updates(ctx_for(w, "routine_image_updates", now)))
    assert r.status == "info" and r.metrics["updates"] == 2 and "Thu 09:00" in r.summary and "OVERDUE" not in r.summary
    assert {i["image"] for i in r.items} == {"linuxserver/transmission:latest", "caddy:2.11.4"}                  # digests and docker.io/ stripped
    assert "updating is manual" in r.summary
    r = routine.image_updates(ctx_for(w, "routine_image_updates", now + 4 * 86400))
    assert r.status == "warn" and "OVERDUE" in r.summary                                                           # Diun stopped scanning
    w.sh.when(["docker", "ps", "-a"], "")
    assert "no update watcher" in routine.image_updates(ctx_for(w, "routine_image_updates", now)).summary
    w.sh.when(["docker", "ps", "-a"], "", rc=1)
    assert routine.image_updates(ctx_for(w, "routine_image_updates", now)).summary == "image updates: docker unavailable"


NOW_BK = at(2026, 9, 28, 12)


def make_backups(w, root, *, result="ok", fin="2026-09-26 02:39:38", errors=0, dump_age_days=6, zstd_rc=0, tooling=True):
    tgt = root / "bk"
    (tgt / "dbdumps").mkdir(parents=True, exist_ok=True)
    for f in ("RESTORE.md", "restore-system.sh"):
        (tgt / f).unlink(missing_ok=True)
        if tooling:
            (tgt / f).write_text("x")
    for n, size in (("immich", 5000), ("tday", 3000), ("mariadb", 2000)):
        f = tgt / "dbdumps" / f"{n}-2026-09-26.sql.zst"
        f.write_bytes(b"z" * size)
        os.utime(f, (NOW_BK - dump_age_days * 86400, NOW_BK - dump_age_days * 86400))
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "system-status.json").write_text(json.dumps({"job": "system", "result": result, "errors": errors, "finished": fin, "target": str(tgt),
                                                         "warn_free_gb": 1, "snapshots_kept": 4, "retain_snapshots": 4, "files_summary": "599,114 files"}))
    w.sh.when(["zstd", "-tq"], "", rc=zstd_rc)
    ok = root / "LAST_OK"
    ok.write_text("x")
    os.utime(ok, (NOW_BK - 3600, NOW_BK - 3600))
    w.maint(MAINT + f'[tasks.routine_backup_verify]\nstatus_glob = "{logs}/*-status.json"\ndump_dirs = ["{tgt}/dbdumps"]\nstack_last_ok = "{ok}"\n')
    return tgt


def test_backup_verify_all_good_and_each_failure(w, tmp_path):
    now = NOW_BK
    make_backups(w, tmp_path)
    c = lambda: Ctx(core.load_config(), "routine_backup_verify", False, now)      # noqa: E731
    r = check_result(routine.backup_verify(c()))
    assert r.status == "ok" and not r.alert and "all ok" in r.summary and r.metrics["checked"] == 5
    assert w.sh.ran("zstd", "-tq")                                                    # small dumps were integrity-tested
    for kw, needle in (({"result": "fail"}, "result fail"), ({"errors": 3}, "3 errors"), ({"fin": "2026-09-01 01:00:00"}, "finished"),
                       ({"zstd_rc": 1}, "fails zstd -t"), ({"tooling": False}, "RESTORE.md missing"), ({"dump_age_days": 30}, "dump 30 d old")):
        make_backups(w, tmp_path, **kw)
        r = routine.backup_verify(c())
        assert r.status == "warn" and r.alert and needle in r.summary, (kw, r.summary)
    (tmp_path / "bk" / "dbdumps" / "tday-2026-09-26.sql.zst").write_bytes(b"")
    make_backups(w, tmp_path)
    (tmp_path / "bk" / "dbdumps" / "tday-2026-09-26.sql.zst").write_bytes(b"")
    assert "tday dump empty" in routine.backup_verify(c()).summary
    (tmp_path / "logs" / "system-status.json").write_text("{")
    assert "unreadable" in routine.backup_verify(c()).summary
    for f in (tmp_path / "logs").glob("*"):
        f.unlink()
    w.maint(MAINT + '[tasks.routine_backup_verify]\nstatus_glob = "/nonexistent/*.json"\ndump_dirs = []\nstack_last_ok = ""\n')
    assert routine.backup_verify(c()).summary == "backup verification: no backup status files found"


def test_backup_verify_zstd_timeout_is_not_corruption(w, tmp_path):
    """A stalled backup disk (zstd -t killed by the timeout, rc 124) must read "unverified", never "CORRUPT"; one retry may rescue it;
    after a double timeout the remaining dumps are not probed (the task keeps its time budget)."""
    c = lambda: Ctx(core.load_config(), "routine_backup_verify", False, NOW_BK)      # noqa: E731
    make_backups(w, tmp_path, zstd_rc=124)
    r = routine.backup_verify(c())
    assert r.status == "warn" and r.alert and "not corruption" in r.summary and "fails zstd -t" not in r.summary, r.summary
    assert not any(row["check"] == "CORRUPT" for row in r.items)
    assert sum(1 for row in r.items if row["check"].startswith("unverified")) == 3        # immich/mariadb/tday: the first timed out twice, the others are skipped
    assert sum(1 for cl in w.sh.calls if cl[:2] == ["zstd", "-tq"]) == 2                  # exactly one try + one retry, then the stall short-circuits
    seq = iter([124, 0, 0, 0])                                                             # first read stalls, the retry (and the next dump) succeed
    fake = w.sh

    def flaky(cmd, timeout=60, check=False, input_=None, env=None):
        if list(cmd)[:2] == ["zstd", "-tq"]:
            return CompletedProcess(list(cmd), next(seq), "", "")
        return fake(cmd, timeout, check, input_, env)

    w.mp.setattr(routine, "sh", flaky)
    make_backups(w, tmp_path)
    r = routine.backup_verify(c())
    assert r.status == "ok" and not r.alert and "all ok" in r.summary, r.summary
    seq2 = iter([1, 1, 1])                                                                # a real zstd error is still corruption
    w.mp.setattr(routine, "sh", lambda cmd, timeout=60, **k: CompletedProcess(list(cmd), next(seq2), "", "") if list(cmd)[:2] == ["zstd", "-tq"] else fake(cmd, timeout, **k))
    r = routine.backup_verify(c())
    assert r.status == "warn" and "fails zstd -t" in r.summary


def test_restore_check(w, tmp_path):
    base = make_backups(w, tmp_path)
    (base / "snapshots" / "2026-09-26" / "root").mkdir(parents=True)
    (base / "snapshots" / "2026-09-26" / "root" / "f").write_text("x")
    (base / "root" / "etc").mkdir(parents=True)
    (base / "root" / "etc" / "hostname").write_text(Path("/etc/hostname").read_text())
    (base / "root" / "etc" / "fstab").write_text("changed since the backup")
    w.maint(MAINT + f'[tasks.routine_restore_check]\nrestore_dir = "{base}"\nsample_files = ["/etc/fstab", "/etc/hostname"]\n')
    c = Ctx(core.load_config(), "routine_restore_check", False, FRI)
    r = check_result(routine.restore_check(c))
    rows = {i["item"]: i for i in r.items}
    assert rows["restore tooling next to the backup"]["state"] == "ok" and rows["newest snapshot has files"]["detail"] == "2026-09-26"
    assert rows["sample files read back from the mirror"]["detail"].startswith("1 identical, 1 changed")
    assert rows["smallest database dump decompresses"]["state"] == "ok" and rows["test restore by the owner"]["state"] == "DUE"
    assert r.status == "warn" and not r.alert and "test restore never done" in r.summary
    assert routine.main(["ack", "restore_drill", "restored hostname file to /tmp"]) == 0
    r = routine.restore_check(Ctx(core.load_config(), "routine_restore_check", False, time.time()))
    assert r.status == "ok" and "all checks ok" in r.summary
    assert "restored hostname file" in {i["item"]: i for i in r.items}["test restore by the owner"]["detail"]
    journal = [json.loads(ln) for ln in (w.state / "maintenance-journal.jsonl").read_text().splitlines()]
    assert journal[0]["title"] == "restore drill done" and journal[0]["detail"].startswith("restored hostname")
    old = Ctx(core.load_config(), "routine_restore_check", False, time.time() + 200 * 86400)
    assert "overdue" in routine.restore_check(old).summary
    (base / "restore-system.sh").unlink()
    r = routine.restore_check(c)
    assert r.status == "warn" and r.alert and "FAILED" in r.summary


def test_expiry_check(w):
    now = at(2026, 10, 2, 12)
    w.sh.when(["tailscale", "status", "--json"], json.dumps({"Self": {"KeyExpiry": "2026-10-16T08:12:52Z", "Expired": False}}))
    w.sh.when(["openssl", "x509"], lambda c: "notAfter=Jan  1 00:00:00 4096 GMT\n" if "cool" in c[-1] else "notAfter=Oct  5 12:00:00 2026 GMT\n")
    d = w.root / "certs"
    d.mkdir()
    (d / "soon.pem").write_text("x")
    (d / "cool.crt").write_text("x")
    w.maint(MAINT + f'[tasks.routine_expiry]\ncert_globs = ["{d}/*.pem", "{d}/*.crt"]\nitems = [{{ name = "domain renewal", date = "2026-10-20", note = "renew it" }}, {{ name = "junk", date = "x" }}]\n')
    r = check_result(routine.expiry_check(Ctx(core.load_config(), "routine_expiry", False, now)))
    assert r.status == "crit" and r.alert and "cert soon.pem in 3 d" in r.summary and "tailscale node key in 14 d" in r.summary
    rows = {i["item"]: i for i in r.items}
    assert rows["cert cool.crt"]["expires"].startswith("never") and rows["domain renewal"]["days"] == 18 and rows["tailscale node key"]["level"] == "warn"
    assert rows["cert soon.pem"]["hint"] == "renew the certificate" and r.items[0]["item"] == "cert soon.pem"          # soonest first
    r = routine.expiry_check(Ctx(core.load_config(), "routine_expiry", False, now + 5 * 86400))
    assert "EXPIRED" in r.summary and r.status == "crit"
    w.sh.when(["tailscale", "status", "--json"], "", rc=1)
    w.maint(MAINT + '[tasks.routine_expiry]\ncert_globs = []\nitems = []\n')
    r = routine.expiry_check(Ctx(core.load_config(), "routine_expiry", False, now))
    assert r.status == "info" and r.summary == "expiry: nothing to check"
    w.sh.when(["tailscale", "status", "--json"], json.dumps({"Self": {"KeyExpiry": "2027-12-01T00:00:00Z"}}))
    r = routine.expiry_check(Ctx(core.load_config(), "routine_expiry", False, now))
    assert r.status == "ok" and "none due within 30 d" in r.summary


def test_trend_review_and_slope(w):
    pts = [(i * 86400.0, 1000 * GIB - 10 * GIB * i) for i in range(8)]
    assert round(routine._slope_per_day(pts) / GIB, 3) == -10.0
    assert routine._slope_per_day(pts[:3] * 1) is None and routine._slope_per_day([(0, 1)] * 10) is None
    now = FRI
    recs = [{"t": now - (7 - i) * 86400, "kind": "disk", "mount": "/", "free": int((1000 - 100 * i) * GIB)} for i in range(8)]
    recs += [{"t": now - 3600 * i, "kind": "task", "task": "failed_units", "status": "warn"} for i in range(5)]
    jl(w.state / "history.jsonl", recs + [{"t": now - 100, "kind": "disk", "mount": "/media/x", "free": 5 * GIB}])
    w.audit_done("docker_images", now - 86400, size=7 * GIB)
    r = check_result(routine.trend_review(ctx_for(w, "routine_trends")))
    assert r.status == "warn" and "1 concern(s): /: shrinking 100.0 GiB/day, about 3 days left" in r.summary and "only 7 d kept" in r.summary
    young = routine.trend_review(Ctx(core.load_config(), "routine_trends", False, now - 5 * 86400 + 1000))          # almost no history yet
    assert young.summary.startswith("trends: collecting data")
    rows = {i["metric"]: i for i in r.items}
    assert rows["free /media/x"]["per_day"] == "n/a" and rows["noisy check failed_units"]["now"] == "5 warn/crit runs"
    assert rows["freed by maintenance (30 d)"]["now"] == "7.0 GiB"
    (w.state / "history.jsonl").unlink()
    r = routine.trend_review(ctx_for(w, "routine_trends"))
    assert r.status == "info"


def mk_rotate_world(w, now):
    old, new = now - 120 * 86400, now - 5 * 86400
    jl(w.log / "audit.jsonl", [{"ts": datetime.fromtimestamp(old, TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "task": "a", "outcome": "done"},
                               {"ts": datetime.fromtimestamp(new, TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "task": "b", "outcome": "done"}])
    jl(w.state / "changes.jsonl", [{"ts": new, "task": "x"}])
    jl(w.state / "maintenance-journal.jsonl", [{"ts": datetime.fromtimestamp(old, TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "title": "owner"}])
    return old, new


def test_rotate_logs_report_then_apply(w):
    now = FRI
    old, new = mk_rotate_world(w, now)
    before = (w.log / "audit.jsonl").read_text()
    mode_before = (w.log / "audit.jsonl").stat().st_mode & 0o777
    c = Ctx(core.load_config(), "routine_rotate", False, now)
    r = check_result(routine.rotate_logs(c))
    after = (w.log / "audit.jsonl").read_text()
    assert after.startswith(before) and not (w.log / "archive").exists()                         # report mode: nothing moves
    assert json.loads(after[len(before):])["outcome"] == "dry-run"                                # ...but the would-do is audited
    assert r.status == "info" and r.metrics["pending"] == 1 and r.metrics["mode"] == "report" and "report: 1 log(s)" in r.summary
    w.maint(MAINT + '[tasks.routine_rotate]\nmode = "apply"\n')
    c = Ctx(core.load_config(), "routine_rotate", True, now)
    assert c.apply
    r = check_result(routine.rotate_logs(c))
    assert r.metrics["rotated"] == 1 and r.reclaimed_bytes == 0 and c.freed == 0                  # rotating frees nothing
    kept = [json.loads(ln) for ln in (w.log / "audit.jsonl").read_text().splitlines()]
    assert [k["task"] for k in kept[:1]] == ["b"]
    arch = list((w.log / "archive").glob("audit-*.jsonl.gz"))
    assert len(arch) == 1 and arch[0].name == f"audit-{datetime.fromtimestamp(old, TZ):%Y-%m}.jsonl.gz"
    assert json.loads(gzip.open(arch[0]).read().splitlines()[0])["task"] == "a"
    assert (w.state / "maintenance-journal.jsonl").read_text().count("owner") == 1                  # the owner's journal is never rotated
    assert (w.log / "audit.jsonl").stat().st_mode & 0o777 == mode_before                              # permissions survive the rewrite
    r2 = routine.rotate_logs(Ctx(core.load_config(), "routine_rotate", True, now))                   # idempotent
    assert r2.metrics["rotated"] == 0 and len(list((w.log / "archive").glob("*"))) == 1
    done = [json.loads(ln) for ln in (w.log / "audit.jsonl").read_text().splitlines()]
    assert any(d.get("action") == "rotate-log" and d["outcome"] == "done" for d in done)             # the rotation itself is audited


def test_rotate_logs_safety(w, tmp_path):
    now = FRI
    mk_rotate_world(w, now)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(json.dumps({"ts": 1}) + "\n")
    link = w.state / "link.jsonl"
    link.symlink_to(outside)
    w.maint(MAINT + f'[tasks.routine_rotate]\nmode = "apply"\ntargets = [{{ path = "{outside}", keep_days = 30 }}, {{ path = "{link}", keep_days = 30 }}, '
                    f'{{ path = "{w.state}/maintenance-journal.jsonl", keep_days = 30 }}, {{ path = "{w.state}/changes.jsonl", keep_days = 1 }}, {{ path = "{w.state}/status.json", keep_days = 30 }}]\n')
    r = routine.rotate_logs(Ctx(core.load_config(), "routine_rotate", True, now))
    assert outside.read_text() == json.dumps({"ts": 1}) + "\n" and (w.state / "maintenance-journal.jsonl").read_text().count("\n") == 1
    assert "keep_days" not in r.summary and r.metrics["rotated"] == 0                                # keep_days < 7 is refused, nothing outside STATE/LOG
    assert (w.state / "changes.jsonl").read_text().count("\n") == 1


def test_rotate_protected_target_is_refused(w):
    now = FRI
    mk_rotate_world(w, now)
    (w.conf / "protected.toml").write_text('patterns = ["audit"]\n')
    w.maint(MAINT + '[tasks.routine_rotate]\nmode = "apply"\n')
    before = (w.log / "audit.jsonl").read_text()
    r = routine.rotate_logs(Ctx(core.load_config(), "routine_rotate", True, now))
    assert (w.log / "audit.jsonl").read_text().startswith(before.splitlines()[0]) and r.metrics["rotated"] == 0
    assert any(i["state"] == "refused" for i in r.items)


def test_rotate_keeps_lines_appended_meanwhile(w):
    now = FRI
    old, _new = mk_rotate_world(w, now)
    p = w.state / "changes.jsonl"
    jl(p, [{"ts": old, "task": "ancient"}])
    real = Path.read_bytes
    calls = []

    def racy(self):
        data = real(self)
        if self == p and not calls:
            calls.append(1)
            jl(p, [{"ts": now, "task": "appended-during-rotation"}])
        return data
    w.mp.setattr(Path, "read_bytes", racy)
    routine._rotate_file(p, now - 90 * 86400, TZ)
    left = [json.loads(ln)["task"] for ln in p.read_text().splitlines()]
    assert "ancient" not in left and "appended-during-rotation" in left and "x" in left


# =========================================================================== CLI
def run_cli(capsys, *argv):
    rc = routine.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_plan_status_due_explain(w, capsys):
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "plan")
    assert rc == 0 and "daily" in out and "clean_a" in out and "*due" in out and "freeze: none" in out
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 18:30", "plan", "daily")
    assert "freeze: evenings (until 23:30)" in out and "*missed" in out
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "--json", "plan")
    assert json.loads(out)[0]["task"] == "chk_a"
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "due")
    assert out.split() == ["chk_a", "clean_a", "clean_b", "heavy"]                      # verify waits for them
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "status")
    assert rc == 0 and "routine.toml: ok  zone America/Toronto" in out and "4 due" in out and "1 waiting" in out
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "explain", "clean_a")
    assert rc == 0 and "daily/clean_a" in out and "needs quiet window: True" in out and "canary: first apply run capped" in out
    rc, out, _ = run_cli(capsys, "explain", "nothing")
    assert rc == 1 and "not in any routine" in out
    rc, _out, err = run_cli(capsys, "--now", "2026-10-02 07:35", "--config", str(w.root / "none.toml"), "plan")
    assert rc == 1 and "INVALID" in err
    with pytest.raises(SystemExit):
        routine.main(["--now", "yesterday-ish", "plan"])


def test_cli_export_note_halt_canary(w, capsys):
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "export")
    assert json.loads(out)["routine"][0]["name"] == "daily"
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "export", "--write", "--out", str(w.root / "r.json"))
    assert (w.root / "r.json").exists() and out.strip().endswith("r.json")
    rc, out, _ = run_cli(capsys, "note", "replaced the CPU fan", "--kind", "config", "--task", "hardware")
    ch = routine.read_changes()[0]
    assert rc == 0 and ch["task"] == "hardware" and ch["kind"] == "config" and ch["outcome"] == "manual" and ch["verified"]
    assert "replaced the CPU fan" in (w.state / "maintenance-journal.jsonl").read_text()
    st = routine.load_state()
    st["halted"]["daily@2026-10-02"] = {"ts": time.time(), "by": "x", "why": "y"}
    st["canary"]["clean_a"] = {"runs": 1}
    routine.save_state(st)
    rc, out, _ = run_cli(capsys, "status")
    assert "HALTED daily@2026-10-02" in out and "last changes:" in out
    assert run_cli(capsys, "clear-halt", "daily")[1].startswith("cleared 1 halt")
    assert run_cli(capsys, "canary-reset", "clean_a")[0] == 0 and routine.load_state()["canary"] == {}
    rc, out, _ = run_cli(capsys, "check")
    assert rc == 0 and out.strip().endswith("routine.toml ok") and "task report_daily is not registered" not in out
    w.routine_toml("x [")
    assert run_cli(capsys, "check")[0] == 1


def test_cli_run_uses_the_runner(w, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(routine, "_cli_runner", lambda task, apply: calls.append((task, apply)) or 0)
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "00:00-24:00"'))
    # Pin the clock: `routine run` uses the wall clock, and the fixture's evening freeze (18:00-23:30) holds clean_a (apply) in the evening.
    real_run_due, t8 = routine.run_due, datetime(2026, 10, 2, 8, 0, tzinfo=TZ).timestamp()
    monkeypatch.setattr(routine, "run_due", lambda apply, cfg=None: real_run_due(apply, now_fn=lambda: t8, cfg=cfg))
    rc, out, _ = run_cli(capsys, "run", "--apply")
    assert rc == 0 and [c[0] for c in calls][:2] == ["chk_a", "clean_a"] and all(c[1] is True for c in calls)
    assert "STEP" in out and "daily/chk_a" in out
    calls.clear()
    monkeypatch.setattr(routine, "due_steps", lambda *a, **k: [])
    assert run_cli(capsys, "run")[1].strip() == "nothing due" and calls == []
    w.routine_toml("x [")
    rc, _o, err = run_cli(capsys, "run", "--apply")
    assert rc == 1 and "invalid" in err


def test_cli_runner_goes_through_cmd_run(w, monkeypatch):
    from homelab_maint import cli
    seen = {}
    monkeypatch.setattr(cli, "cmd_run", lambda a: seen.setdefault("ns", a) and 0)
    assert routine._cli_runner("docker_cache", True) == 0
    ns = seen["ns"]
    assert (ns.task, ns.apply, ns.dry_run, ns.scheduled) == ("docker_cache", True, False, True)
    routine._cli_runner("docker_cache", False)


# =========================================================================== safety properties
def test_nothing_here_touches_the_live_system(w):
    """The whole module imports and plans without running a command (subprocess is booby-trapped by the fixture) and every
    write lands under the tmp STATE/LOG dirs."""
    routine.plan(FRI)
    routine.export(FRI, timers={})
    routine.record_change("t", "cleanup", "d", now=FRI)
    assert str(core.STATE_DIR).startswith(str(w.root)) and str(core.LOG_DIR).startswith(str(w.root))
    assert not any(c[0] in ("docker", "systemctl", "apt-get", "snap", "rm", "kill") for c in w.sh.calls)


def test_record_approved(w):
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    r = routine.record_approved("c2_candidates", Result("ok", "archived 3 items, freed 12.0 GiB", reclaimed_bytes=12 * GIB), now=FRI)
    assert r["outcome"] == "approved" and r["bytes"] == 12 * GIB and r["verified"] is True and r["kind"] == "cleanup"
    _checks(w, chk_a="crit")
    assert routine.record_approved("c2_candidates", Result("ok", "x"), now=FRI + 1)["verified"] is False
    w.routine_toml("junk [")
    assert routine.record_approved("c2_candidates", Result("ok", "x"), now=FRI + 2)["verified"] is False             # fail closed


# =========================================================================== the cli.py glue (exact edits handed to the lead)
CLI_GLUE = [
    ("from . import core\nfrom .core import (", "from . import core, routine\nfrom .core import ("),
    ('TIERS = ("check", "daily", "weekly")', 'TIERS = ("check", "daily", "weekly", "monthly")'),
    ('        importlib.import_module(f"{pkg.__name__}.{m.name}")\n',
     '        importlib.import_module(f"{pkg.__name__}.{m.name}")\n'
     '    importlib.import_module(f"{__package__}.reports")       # report_daily / report_weekly live outside tasks/\n'),
    ('''        order = {"C0": 0, "C1": 1, "C2": 2}
        done: list[tuple] = []
        # Tasks run WITHOUT the state lock (they can take minutes); only the merge below is locked.
        # C0 tasks sort by name, so spike_sampler runs before stuck_detector (which reads its sample).
        for t in sorted(selected, key=lambda x: (order[x.klass], x.name)):
            res, dur = run_task(t, cfg, apply)
            done.append((t, res, dur, time.time()))
''', '''        done: list[tuple] = []
        guard = routine.RunGuard()      # etc/routine.toml: windows, freeze, gates, canary caps, post-check (fails closed)
        # Tasks run WITHOUT the state lock (they can take minutes); only the merge below is locked.
        # guard.order: routine order for managed daily/weekly/monthly tasks (verify and report last), everything else
        # C0 before C1 before C2 by name (spike_sampler runs before stuck_detector, which reads its sample).
        for t in guard.order(selected):
            # manual = the owner (--override/--force, or a terminal on stdin), never a timer, the scheduler or the tick
            d = guard.begin(t, apply, manual=routine.is_manual(a), force=bool(getattr(a, "force", False)))
            if not d.run:
                continue                # not this task's moment (window, freeze, busy gate, done): keep its last result
            res, dur = run_task(t, guard.task_cfg(cfg, t, d), apply and d.apply)
            guard.after(t, d, res, dur)
            done.append((t, res, dur, time.time(), apply and d.apply))
'''),
    ("            for t, res, dur, ts in done:", "            for t, res, dur, ts, applied in done:"),
    ('"mode": "apply" if (apply and tcfg.get("mode") == "apply" and t.klass != "C0"',
     '"mode": "apply" if (applied and tcfg.get("mode") == "apply" and t.klass != "C0"'),
    ('        kuma_push(cfg, f"tier-{tier}"', '''        try:
            routine.write_export()      # public/routine.json: windows, plan, change log, 14-day calendar
        except Exception as exc:  # noqa: BLE001 - the website is optional, maintenance is not
            print(f"[warn] routine export failed: {exc}", file=sys.stderr)
        kuma_push(cfg, f"tier-{tier}"'''),
    ("for t, r, _d, _ts in done if r.status", "for t, r, _d, _ts, _a in done if r.status"),
    ('    res, _ = run_task(t, cfg, apply=True)\n    print(f"{res.status}: {res.summary} (freed {human(res.reclaimed_bytes)})")\n',
     '    res, _ = run_task(t, cfg, apply=True)\n    print(f"{res.status}: {res.summary} (freed {human(res.reclaimed_bytes)})")\n'
     '    try:\n        routine.record_approved(a.task, res)      # change log + post-check of the applied plan\n'
     '    except Exception as exc:  # noqa: BLE001\n        print(f"[warn] change log: {exc}", file=sys.stderr)\n'),
    ('def main(argv=None) -> int:\n    ap = argparse.ArgumentParser(prog="homelab-maint")',
     'def main(argv=None) -> int:\n    args = sys.argv[1:] if argv is None else list(argv)\n'
     '    if args[:1] == ["routine"]:             # the routine has its own parser (--now, --json, ... come before the verb)\n'
     '        return int(routine.main(args[1:]) or 0)\n    ap = argparse.ArgumentParser(prog="homelab-maint")'),
    ('    r.add_argument("--task")\n',
     '    r.add_argument("--task")\n'
     '    r.add_argument("--override", action="store_true", help="owner override of the routine window and done-state for --task (a freeze or a halt still holds)")\n'
     '    r.add_argument("--force", action="store_true", help="with --task: also override a freeze or a halt (implies --override)")\n'
     '    r.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)\n'),
    ('    sub.add_parser("serve")\n    a = ap.parse_args(argv)',
     '    sub.add_parser("serve")\n    sub.add_parser("routine", help="maintenance routine and change management (see: routine --help)")\n    a = ap.parse_args(argv)'),
]


def glued_cli(tmp_path):
    """homelab_maint.cli with the glue edits applied to a COPY of the source (or cli itself once the lead applied them)."""
    import importlib.util
    src = (ROOT / "homelab_maint" / "cli.py").read_text()
    if "routine.RunGuard" in src:
        from homelab_maint import cli
        return cli
    for old, new in CLI_GLUE:
        assert src.count(old) == 1, f"glue anchor not found exactly once: {old[:60]!r}"
        src = src.replace(old, new)
    p = tmp_path / "cli_glued.py"
    p.write_text(src)
    spec = importlib.util.spec_from_file_location("homelab_maint.cli_glued", p)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "homelab_maint"
    spec.loader.exec_module(mod)
    return mod


def test_cli_glue_end_to_end(w, tmp_path):
    import argparse as ap
    ran = []

    def mk(name, klass, tier, status="ok", act=False):
        def run(ctx):
            ran.append(name)
            if act:
                ctx.act("docker-image-rm", f"img-{name}", 100, lambda: 100)
            return Result(status, f"{name} fine")
        return core.Task(name, klass, tier, run, title=name.replace("_", " "))
    reg = {n: mk(n, k, t, act=(n == "clean_a")) for n, (k, t) in SPEC.items()}
    reg["chk_a"] = mk("chk_a", "C0", "daily")
    reg["disk_forecast"] = mk("disk_forecast", "C0", "check")
    reg["failed_units"] = mk("failed_units", "C0", "check")
    w.mp.setattr(core, "REGISTRY", reg)
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "00:00-24:00"').replace('evenings = "18:00-23:30"', 'x = "2020-01-01"')
                   .replace('post_check = ["chk_a"]', 'post_check = ["failed_units", "disk_forecast"]').replace('"verify"]', '"verify", "report"]'))
    w.status({"failed_units": {"status": "ok", "alert": True}, "disk_forecast": {"status": "ok", "alert": True}})
    cli = glued_cli(tmp_path)
    assert cli.TIERS[-1] == "monthly"
    daily = ap.Namespace(tier="daily", task=None, apply=True, dry_run=False)
    assert cli.cmd_run(daily) == 0
    # the routine's order, verify last; the guard ran each step once; the C2 plan task ran in the tier too
    assert [n for n in ran if n not in ("failed_units", "disk_forecast")] == [
        "chk_a", "clean_a", "clean_b", "heavy", "routine_verify_daily", "report_daily"]       # routine order: verify, then the report
    assert ran.index("failed_units") == 2 and ran.index("disk_forecast") == 3                 # the post-check runs right after clean_a changed something
    status = core.read_json(w.state / "status.json")
    assert status["tasks"]["clean_a"]["mode"] == "apply" and status["tasks"]["clean_b"]["mode"] == "dry-run"
    st = routine.load_state()
    assert all(st["steps"][f"daily/{n}"]["done"] for n in ("chk_a", "clean_a", "clean_b", "heavy", "verify"))
    assert st["canary"]["clean_a"]["runs"] == 1
    ch = routine.read_changes()
    assert len(ch) == 1 and ch[0]["task"] == "clean_a" and ch[0]["verified"] is True and ch[0]["bytes"] == 100
    pub = json.loads((w.state / "public" / "routine.json").read_text())                  # the export is published with the status
    assert pub["routine"][0]["counts"] == {"done": 6} and pub["changes"][0]["task"] == "clean_a"
    ran.clear()
    assert cli.cmd_run(daily) == 0 and ran == []                                          # idempotent: nothing is due twice
    assert cli.cmd_run(ap.Namespace(tier="daily", task="clean_a", apply=True, dry_run=False)) == 0
    assert [n for n in ran if n not in ("failed_units", "disk_forecast")] == []                # no --override, no terminal (a timer's
    ran.clear()                                                                                # `run --task`): held to the done-state
    assert cli.cmd_run(ap.Namespace(tier="daily", task="clean_a", apply=True, dry_run=False, override=True)) == 0
    assert [n for n in ran if n not in ("failed_units", "disk_forecast")] == ["clean_a"]       # the owner's override: only that task
    ran.clear()
    # check tier: continuous, always runs, never recorded as a routine step
    assert cli.cmd_run(ap.Namespace(tier="check", task=None, apply=False, dry_run=False)) == 0
    assert sorted(ran) == ["disk_forecast", "failed_units"]
    # the new subcommand and the dry-run tier: a dry run does not satisfy an apply-mode step
    assert cli.main(["routine", "--now", "2026-10-03 08:00", "due"]) == 0
    st = routine.load_state()
    st["steps"] = {}
    routine.save_state(st)
    ran.clear()
    assert cli.cmd_run(ap.Namespace(tier="daily", task=None, apply=False, dry_run=True)) == 0
    assert routine.load_state()["steps"]["daily/clean_a"]["done"] is False


def test_cli_glue_busy_gate_defers_and_freeze_blocks(w, tmp_path, monkeypatch):
    import argparse as ap
    ran = []
    reg = {n: core.Task(n, k, t, (lambda ctx, n=n: ran.append(n) or Result("ok", "x")), title=n) for n, (k, t) in SPEC.items()}
    w.mp.setattr(core, "REGISTRY", reg)
    w.routine_toml(MAIN.replace('daily = "07:30-09:30"', 'daily = "00:00-24:00"').replace('evenings = "18:00-23:30"', 'x = "2020-01-01"'))
    cli = glued_cli(tmp_path)
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (True, "backup-system.service is active"))
    daily = ap.Namespace(tier="daily", task=None, apply=True, dry_run=False)
    assert cli.cmd_run(daily) == 0
    assert "clean_a" not in ran and "heavy" not in ran and "chk_a" in ran and "clean_b" in ran     # disruptive steps deferred, read-only ran
    assert routine.load_state()["steps"]["daily/clean_a"]["deferrals"] == 1
    (w.conf / "FREEZE").write_text("x")
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (False, "idle"))
    ran.clear()
    assert cli.cmd_run(daily) == 0 and "clean_a" not in ran                                            # FREEZE file: still not
    (w.conf / "FREEZE").unlink()
    assert cli.cmd_run(daily) == 0 and "clean_a" in ran and "heavy" in ran                             # lifted: the retry goes through


# =========================================================================== more properties: the shipped windows across two years, robustness
def test_shipped_windows_sweep_two_years_of_dst():
    """The shipped windows keep their local start time and length every single day of 2026-2027 (both DST changes included),
    and the monthly one is the first Saturday of every month."""
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    daily, weekly, monthly = (e.window for e in rc.entries)
    d0, d1 = date(2026, 1, 1), date(2027, 12, 31)
    occ = daily.occurrences(rc.tz, d0, d1)
    assert len(occ) == (d1 - d0).days + 1
    for o in occ:
        assert o.end - o.start == 2 * 3600
        assert datetime.fromtimestamp(o.start, rc.tz).strftime("%H:%M") == "07:30"
    wk = weekly.occurrences(rc.tz, d0, d1)
    assert {date.fromisoformat(o.id).weekday() for o in wk} == {2} and len(wk) in (104, 105)
    assert all(o.end - o.start == 135 * 60 and datetime.fromtimestamp(o.start, rc.tz).strftime("%H:%M") == "07:45" for o in wk)
    mo = monthly.occurrences(rc.tz, d0, d1)
    assert len(mo) == 24
    assert all(date.fromisoformat(o.id).weekday() == 5 and date.fromisoformat(o.id).day <= 7 and o.end - o.start == 150 * 60 for o in mo)
    assert all(datetime.fromtimestamp(o.start, rc.tz).strftime("%H:%M") == "04:30" for o in mo)
    # consecutive daily windows are 24 h apart except across the two DST days of each year (23 h / 25 h)
    gaps = {round((b.start - a.start) / 3600) for a, b in zip(occ, occ[1:])}
    assert gaps == {23, 24, 25}


def test_guard_never_raises(w, monkeypatch, capsys):
    g = guard(w)
    monkeypatch.setattr(routine, "_evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    d = g.begin(task_of(w, "clean_a"), True)
    assert d.run and not d.apply and "guard error" in d.reason                      # degrade to report-only, never block the tier
    assert g.begin(task_of(w, "chk_a"), True).run
    monkeypatch.setattr(routine, "update_state", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert g.after(task_of(w, "clean_a"), routine.Decision(steps=[("daily", "clean_a", "2026-10-02", True)]), Result("ok", "x")) is None
    assert "bookkeeping error" in capsys.readouterr().err


def test_update_state_is_atomic_under_threads():
    def go(n):
        for i in range(25):
            routine.update_state(lambda st, n=n, i=i: routine.mark_run(st, "daily", f"s{n}_{i}", "o", FRI, "ok", "x"))
    ts = [threading.Thread(target=go, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(routine.load_state()["steps"]) == 150                                # no lost update
    assert not list(core.STATE_DIR.glob("*.tmp"))


def test_state_survives_damage_and_prunes_old_halts():
    (core.STATE_DIR / "routine-state.json").write_text("{not json")
    assert routine.load_state() == routine._fresh_state()
    (core.STATE_DIR / "routine-state.json").write_text(json.dumps({"steps": "junk", "canary": {"t": {"runs": 1}}}))
    st = routine.load_state()
    assert st["steps"] == {} and st["canary"] == {"t": {"runs": 1}}                 # a wrong-typed field is dropped, the rest kept
    routine.update_state(lambda s: s["halted"].update({"daily@old": {"ts": FRI - 40 * 86400}, "daily@new": {"ts": FRI}}), FRI)
    assert list(routine.load_state()["halted"]) == ["daily@new"]


def test_explain_a_step_by_name_and_status_with_changes(w, capsys):
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "explain", "verify")
    assert rc == 0 and "daily/verify -> task routine_verify_daily" in out
    routine.record_change("docker_images", "cleanup", "freed 3 GiB", bytes=3 * GIB, verified=False, now=FRI - 60)
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "status")
    assert "NOT verified" in out and "docker_images" in out


def test_step_for_a_task_in_two_routines_is_satisfied_by_one_run(w):
    w.registry({**SPEC, "cfg_drift": ("C0", "weekly")})
    w.routine_toml(MAIN + """
[[routine]]
name = "wk"
cadence = "weekly"
window = "Fri 07:00-08:00"
steps = ["cfg_drift"]
[[routine]]
name = "mo"
cadence = "monthly"
window = "day 2 07:00-08:00"
steps = ["cfg_drift"]
""")
    g = guard(w)
    d = g.begin(task_of(w, "cfg_drift"), True)
    assert d.run and {s[0] for s in d.steps} == {"wk", "mo"}                            # FRI is inside both windows
    g.after(task_of(w, "cfg_drift"), d, Result("ok", "drift: none"), 1.0)
    st = routine.load_state()["steps"]
    assert all(st[k]["done"] for k in st if k.endswith("/cfg_drift"))
    assert not g.begin(task_of(w, "cfg_drift"), True).run                               # both occurrences are done: skip


def test_tick_rechecks_the_window_when_the_step_really_starts(w, tmp_path):
    """run_due picks clean_a at 09:29:59; by the time cmd_run starts it the window has closed. The tick's own run (scheduled)
    is refused by the guard; a manual `run --task` is the owner's override and goes ahead."""
    import argparse as ap
    clock = [at(2026, 10, 2, 9, 29, 59)]
    w.mp.setattr(time, "time", lambda: clock[0])
    ran = []
    reg = {n: core.Task(n, k, t, (lambda ctx, n=n: ran.append(n) or Result("ok", "x")), title=n) for n, (k, t) in SPEC.items()}
    w.mp.setattr(core, "REGISTRY", reg)
    cli = glued_cli(tmp_path)

    def runner(task, apply):
        if task == "clean_a":
            clock[0] += 10                                                      # the previous step took a while
        return cli.cmd_run(ap.Namespace(tier="check", task=task, apply=apply, dry_run=not apply, scheduled=True))
    routine.run_due(True, now_fn=lambda: clock[0], runner=runner)
    assert "chk_a" in ran and "clean_a" not in ran
    assert routine.load_state()["steps"].get("daily/clean_a", {}).get("done") is not True
    assert cli.cmd_run(ap.Namespace(tier="daily", task="clean_a", apply=True, dry_run=False)) == 0 and "clean_a" not in ran    # a timer's `run --task`
    assert cli.cmd_run(ap.Namespace(tier="daily", task="clean_a", apply=True, dry_run=False, override=True)) == 0 and "clean_a" in ran   # the owner


# =========================================================================== review fixes: regression tests (one block per finding)
def conts(w, **opts):
    """A registry with the shipped continuous restart tasks (check tier, C1, all in apply mode) plus an ordinary continuous C1
    task and the spike ladder; opts = extra [tasks.<name>] lines, e.g. other_check_c1="disruptive = true"."""
    names = ("immich_recycle", "comfyui_idle_reclaim", "other_check_c1", "pressure_response")
    w.registry({**SPEC, **{n: ("C1", "check") for n in names}})
    w.maint(MAINT + "".join(f'[tasks.{n}]\nmode = "apply"\n{opts.get(n, "")}\n' for n in names))


EVENING = at(2026, 10, 2, 20, 24)                    # the owner's movie night: 18:00-23:30 freeze


# ---- 1. the freeze holds check-tier tasks that restart a user-facing service --------------------------------------------
def test_continuous_restart_tasks_wait_out_the_freeze(w):
    conts(w)
    for name in ("immich_recycle", "comfyui_idle_reclaim"):
        d = begin(w, name, now=EVENING)[1]                                   # the evening freeze window
        assert d.run and not d.apply and not d.applying and d.reason == "freeze: report only (freeze window evenings)", name
    (w.conf / "FREEZE").write_text("x")                                      # the owner's FREEZE file, any hour
    d = begin(w, "immich_recycle", now=FRI)[1]
    assert d.run and not d.applying and "FREEZE file present" in d.reason
    (w.conf / "FREEZE").unlink()
    d = begin(w, "immich_recycle", now=FRI)[1]                               # 07:35: not frozen
    assert d.applying and d.apply and d.reason == "continuous task: not window-governed"
    assert begin(w, "immich_recycle", now=EVENING, apply=False)[1].applying is False
    # an ordinary continuous task (checks, reclaim) is never frozen: the ladder must act at any hour
    d = begin(w, "other_check_c1", now=EVENING)[1]
    assert d.applying and d.apply and d.reason == "continuous task: not window-governed"
    assert begin(w, "chk_a", now=EVENING)[1].apply                           # a check: nothing to hold


def test_frozen_continuous_restart_really_runs_report_only(w):
    """Through core.run_task: the Ctx the task sees has apply = False, so `docker restart` is only a 'would' in the audit."""
    seen = []

    def run(ctx):
        seen.append(ctx.apply)
        ctx.act("docker-restart", "immich_server", 0, lambda: 0)
        return Result("ok", "recycle")
    conts(w)
    reg = dict(core.REGISTRY)
    reg["immich_recycle"] = core.Task("immich_recycle", "C1", "check", run, title="immich")
    w.mp.setattr(core, "REGISTRY", reg)
    t, cfg = reg["immich_recycle"], core.load_config()
    g, d = begin(w, "immich_recycle", now=EVENING)
    core.run_task(t, g.task_cfg(cfg, t, d), True and d.apply)
    g2, d2 = begin(w, "immich_recycle", now=FRI)
    core.run_task(t, g2.task_cfg(cfg, t, d2), True and d2.apply)
    assert seen == [False, True]
    outcomes = [json.loads(ln)["outcome"] for ln in (w.log / "audit.jsonl").read_text().splitlines()]
    assert outcomes == ["dry-run", "done"]


def test_continuous_freeze_is_configurable_fail_closed_and_advisory(w):
    conts(w, other_check_c1="disruptive = true")
    d = begin(w, "other_check_c1", now=EVENING)[1]                           # [tasks.X] disruptive = true joins the list
    assert not d.applying and "freeze window evenings" in d.reason
    w.routine_toml(MAIN.replace("[freeze]", '[continuous]\ndisruptive = ["other_check_c1"]\n[freeze]'))
    assert begin(w, "immich_recycle", now=EVENING)[1].applying                  # the list is the owner's: this one is no longer on it
    assert not begin(w, "other_check_c1", now=EVENING)[1].applying
    w.routine_toml("broken [")                                              # freeze windows unknown: hold (default list applies)
    d = begin(w, "immich_recycle", now=FRI)[1]
    assert d.run and not d.applying and "routine.toml invalid" in d.reason
    w.routine_toml(MAIN.replace("[settings]", "[settings]\nenforce = false"))   # advisory: say it, hold nothing
    d = begin(w, "immich_recycle", now=EVENING)[1]
    assert d.applying and d.would_block == "freeze: freeze window evenings"


def test_continuous_freeze_manual_needs_force(w):
    conts(w)
    g = guard(w, EVENING)
    t = task_of(w, "immich_recycle")
    assert not g.begin(t, True, manual=True).applying                         # the owner's override does not get past a freeze
    d = g.begin(t, True, manual=True, force=True)
    assert d.applying and d.apply
    assert not g.begin(t, True, force=True).applying                          # --force without the owner (manual) means nothing


def test_spike_ladder_is_held_rung_by_rung(w):
    """pressure_response keeps reclaiming and throttling in a freeze; its destructive rungs go to report-only unless it is an
    emergency. Checked against the real rung-mode logic of tasks/pressure.py."""
    from homelab_maint.tasks import pressure
    conts(w, pressure_response='restart = "apply"\nemergency = "apply"\nthrottle = "apply"')
    t, cfg = task_of(w, "pressure_response"), core.load_config()

    def modes(d, g):
        return pressure._modes(Ctx(g.task_cfg(cfg, t, d), "pressure_response", True, EVENING))

    g, d = begin(w, "pressure_response", now=EVENING)
    assert d.applying and d.overrides == {"restart": "report", "emergency": "report"} and "held to report-only" in d.reason
    assert modes(d, g) == {"reclaim": "apply", "throttle": "apply", "restart": "report", "emergency": "report"}
    g, d = begin(w, "pressure_response", now=FRI)                             # outside the freeze: all rungs as configured
    assert d.overrides == {} and modes(d, g) == {"reclaim": "apply", "throttle": "apply", "restart": "apply", "emergency": "apply"}
    w.status({"pressure_state": {"status": "crit", "last_run": EVENING - 60, "metrics": {"level": 4}}}, EVENING)
    g, d = begin(w, "pressure_response", now=EVENING)                         # a host-wide stall: the freeze no longer holds it
    assert d.overrides == {} and "freeze lifted: pressure level 4" in d.reason
    w.status({"pressure_state": {"status": "warn", "last_run": EVENING - 60, "metrics": {"level": 3}}}, EVENING)
    assert begin(w, "pressure_response", now=EVENING)[1].overrides                   # level 3 is not an emergency
    w.status({"pressure_state": {"status": "crit", "last_run": EVENING - 7200, "metrics": {"level": 5}}}, EVENING)
    assert begin(w, "pressure_response", now=EVENING)[1].overrides                    # stale data: unknown level = held
    w.status({"pressure_state": {"status": "error", "last_run": EVENING, "metrics": {}}}, EVENING)
    assert begin(w, "pressure_response", now=EVENING)[1].overrides
    # a rung that is report or off is left exactly as it is (the freeze never turns "off" into "report")
    conts(w, pressure_response='restart = "off"\nemergency = "report"')
    g, d = begin(w, "pressure_response", now=EVENING)
    assert d.overrides == {} and pressure._modes(Ctx(g.task_cfg(core.load_config(), t, d), "pressure_response", True))["restart"] == "off"


def test_plan_shows_the_freeze_on_continuous_restart_steps(w):
    conts(w)
    w.routine_toml(MAIN.replace('steps = ["chk_a",', 'steps = ["immich_recycle", "other_check_c1", "chk_a",'))
    p = {s.name: s for s in routine.plan(EVENING)}
    assert p["immich_recycle"].state == "continuous" and p["immich_recycle"].frozen and "report-only while a freeze" in p["immich_recycle"].reason
    assert p["other_check_c1"].state == "continuous" and not p["other_check_c1"].frozen
    assert not {s.name: s for s in routine.plan(FRI)}["immich_recycle"].frozen


# ---- 2. verify: a standing warn is not a regression --------------------------------------------------------------------
def test_verify_with_a_standing_warn_and_no_change_does_not_alert(w):
    """Observed live: failed_units already warn (2 unhealthy containers), a day with no change, so no baseline was recorded and
    the verify step warned (and paged) 'failed_units: warn, no baseline'."""
    _checks(w, chk_a="warn")
    w.status({"chk_a": {"status": "warn", "alert": True}}, FRI)
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 600), "daily")           # nothing ran: no stored baseline at all
    assert r.status != "warn" and not r.alert and r.metrics["post_check_ok"] and "no baseline" not in r.summary
    g, d = begin(w, "chk_a", now=FRI)                                                       # a read-only first step takes the baseline
    assert routine.load_state()["pre"]["daily@2026-10-02"]["checks"] == {"chk_a": "warn"}
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 600), "daily")
    assert not r.alert and r.metrics["post_check_ok"]
    # ... but a real regression (ok when the occurrence began, warn now) is still flagged
    st = routine.load_state()
    st["pre"]["daily@2026-10-02"]["checks"] = {"chk_a": "ok"}
    routine.save_state(st)
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 700), "daily")
    assert r.status == "warn" and r.alert and "chk_a: ok->warn" in r.summary


def test_baseline_is_taken_at_the_first_managed_step_whatever_it_is(w):
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    assert "pre" in routine.load_state() and routine.load_state()["pre"] == {}
    g, d = begin(w, "chk_a", apply=False)                                                   # C0, report only, not disruptive
    assert d.run and not d.applying
    assert routine.load_state()["pre"]["daily@2026-10-02"] == {"ts": FRI, "checks": {"chk_a": "ok"}}
    w.status({"chk_a": {"status": "crit", "alert": True}}, FRI + 60)                        # later: the baseline is not rewritten
    begin(w, "clean_b", now=FRI + 60)
    begin(w, "clean_a", now=FRI + 61)
    assert routine.load_state()["pre"]["daily@2026-10-02"]["checks"] == {"chk_a": "ok"}
    d = begin(w, "clean_a", now=at(2026, 10, 3, 7, 40))[1]                                  # a new occurrence takes its own
    assert routine.load_state()["pre"]["daily@2026-10-03"]["checks"] == {"chk_a": "crit"}
    # an unmanaged task or a task not due writes nothing
    before = json.dumps(routine.load_state()["pre"], sort_keys=True)
    begin(w, "clean_a", now=at(2026, 10, 3, 12, 0))
    assert json.dumps(routine.load_state()["pre"], sort_keys=True) == before


def test_after_change_still_fails_closed_without_a_baseline(w):
    """Only the post-CHANGE check keeps 'no baseline = not ok'."""
    _checks(w, chk_a="warn")
    g, d = begin(w, "clean_a")
    st = routine.load_state()
    st["pre"].clear()                                                                       # damaged / pruned state
    routine.save_state(st)
    w.audit_done("clean_a", FRI + 2)
    rec = g.after(task_of(w, "clean_a"), d, Result("ok", "freed", reclaimed_bytes=GIB), 1.0)
    assert rec["verified"] is False and "daily@2026-10-02" in routine.load_state()["halted"]


# ---- 3 + 6. the canary cannot starve a cleaner and never raises a cap ----------------------------------------------------
def big_cleaner(w, size_gib=12):
    """A C1 task whose single action is bigger than a canary's byte cap, written like the real cleaners (oversize is reported
    in the metrics, the task still says ok)."""
    def run(ctx):
        size = size_gib * GIB
        if size > ctx.cap_bytes:
            return Result("ok", f"freed 0 B (0 builders), 1 over cap", {"oversize": 1, "deferred": 0, "capped": False})
        ctx.act("buildx-prune", "builder:default", size, lambda: size)
        return Result("ok", "freed 12.0 GiB (1 builders)", {"oversize": 0, "deferred": 0, "capped": False})
    reg = dict(core.REGISTRY)
    reg["clean_a"] = core.Task("clean_a", "C1", "daily", run, title="clean a")
    w.mp.setattr(core, "REGISTRY", reg)
    w.maint('[tasks.clean_a]\nmode = "apply"\nmax_gib_per_run = 30\n')
    return reg["clean_a"]


def one_day(w, t, day, hour=7, minute=40):
    now = at(2026, 10, day, hour, minute)
    g = guard(w, now)
    cfg = core.load_config()
    d = g.begin(t, True)
    res, dur = core.run_task(t, g.task_cfg(cfg, t, d), True and d.apply) if d.run else (None, 0)
    rec = g.after(t, d, res, dur) if d.run else None
    return d, res, rec


def test_canary_that_applied_nothing_is_surfaced_then_relaxed(w):
    """Real finding: docker_cache 20 GB, cap 30 GiB, canary 3 GiB: the 12 GiB prune is always over the canary cap, nothing
    changed, the canary never graduated and the cleaner never ran. Now: day 1 is surfaced, day 2 runs with the normal byte cap."""
    t = big_cleaner(w)
    d1, r1, rec1 = one_day(w, t, 2)
    assert d1.caps == {"max_gib_per_run": 3.0, "max_items_per_run": 50} and rec1 is None
    assert r1.summary.endswith("; canary-throttled") and r1.metrics["canary"] == "throttled" and len(r1.summary) <= 140
    c = routine.load_state()["canary"]["clean_a"]
    assert c["runs"] == 0 and c["throttled"] == 1
    assert routine.export(at(2026, 10, 2, 8), timers={})["state"]["canary_throttled"] == {"clean_a": 1}
    d2, r2, rec2 = one_day(w, t, 3)
    assert d2.caps == {"max_gib_per_run": 30.0, "max_items_per_run": 50}                    # normal byte cap, small item count
    assert rec2 and rec2["task"] == "clean_a" and rec2["bytes"] == 12 * GIB and "canary-throttled" not in r2.summary
    assert routine.load_state()["canary"]["clean_a"]["runs"] == 1
    d3, r3, rec3 = one_day(w, t, 4)
    assert d3.caps is None and rec3                                                          # graduated: the day-1 slowdown was the cost
    assert "daily/clean_a" in routine.load_state()["steps"]


def test_canary_relax_is_configurable_and_a_no_op_run_is_not_throttling(w):
    w.routine_toml(MAIN.replace("[freeze]", "[canary]\nrelax_after = 2\n[freeze]"))
    t = big_cleaner(w)
    assert one_day(w, t, 2)[0].caps["max_gib_per_run"] == 3.0
    assert one_day(w, t, 3)[0].caps["max_gib_per_run"] == 3.0                                # still strict after one throttled run
    assert one_day(w, t, 4)[0].caps["max_gib_per_run"] == 30.0                               # relaxed after two
    w.routine_toml(MAIN.replace("[freeze]", "[canary]\nrelax_after = 0\n[freeze]"))
    routine.update_state(lambda st: st["canary"].update({"clean_a": {"runs": 0, "throttled": 9}}))
    assert routine.canary_caps("clean_a")["max_gib_per_run"] == 3.0                           # 0 = never relax (the old behaviour)
    # a run that simply found nothing to clean is not "throttled": the canary stays strict and quiet
    w.routine_toml(MAIN)
    routine.update_state(lambda st: st["canary"].clear())
    g, d = begin(w, "clean_a", now=at(2026, 10, 5, 7, 40))
    res = Result("ok", "nothing to clean", {"oversize": 0, "deferred": 0, "capped": False})
    g.after(task_of(w, "clean_a"), d, res, 1.0)
    assert "clean_a" not in routine.load_state()["canary"] and res.summary == "nothing to clean"
    for day, s in enumerate(({"oversize": 2}, {"deferred": 1}, {"capped": True}), 6):
        routine.update_state(lambda st: st["canary"].clear())
        g, d = begin(w, "clean_a", now=at(2026, 10, day, 7, 40))
        res = Result("info", "x", s)
        g.after(task_of(w, "clean_a"), d, res, 1.0)
        assert res.metrics["canary"] == "throttled" and not d.canary_relaxed
    assert routine._throttled(Result("warn", "cap reached: x"))
    # once relaxed the byte cap is the normal one: an oversize result is the task's normal cap, not the canary, so it is not
    # blamed on it (and not counted again)
    g, d = begin(w, "clean_a", now=at(2026, 10, 9, 7, 40))
    assert d.canary_relaxed and d.caps["max_gib_per_run"] == 30.0
    res = Result("ok", "freed 0 B, 1 over cap", {"oversize": 1})
    g.after(task_of(w, "clean_a"), d, res, 1.0)
    assert "canary" not in res.metrics and "canary-throttled" not in res.summary
    assert routine.load_state()["canary"]["clean_a"]["throttled"] == 1


def test_real_docker_cache_is_not_starved_by_the_canary(w, monkeypatch):
    """The reviewer's reproduction with the real cleaner: 20 GB build cache, high 15 / low 8 GiB, max 30 GiB per run."""
    from homelab_maint.tasks import cleaners
    reg = {n: REAL_REGISTRY[n] for n in ("docker_cache", "routine_verify_daily")}
    w.mp.setattr(core, "REGISTRY", reg)
    monkeypatch.setattr(cleaners, "sh", w.sh)
    monkeypatch.setattr(cleaners, "_busy", lambda n: (False, "idle"))
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (False, "idle"))
    w.sh.when(["docker", "buildx", "ls"], json.dumps({"Name": "default", "Nodes": [{"Status": "running"}]}))
    w.sh.when(["docker", "buildx", "du"], "ID  RECLAIMABLE SIZE\nTotal:\t\t20GB\n")
    w.sh.when(["docker", "buildx", "prune"])
    w.maint('[tasks.docker_cache]\nmode = "apply"\nhigh_gib = 15\nlow_gib = 8\nmax_gib_per_run = 30\n')
    w.routine_toml(MAIN.replace('"clean_a", "clean_b", {task = "heavy", disruptive = true}, "verify"', '"docker_cache", "verify"')
                   .replace('steps = ["chk_a", ', 'steps = [').replace('post_check = ["chk_a"]', 'post_check = []'))
    t = reg["docker_cache"]
    pruned = []
    for day in (2, 3, 4):
        now = at(2026, 10, day, 7, 40)
        g = guard(w, now)
        d = g.begin(t, True)
        res, dur = core.run_task(t, g.task_cfg(core.load_config(), t, d), True and d.apply)
        g.after(t, d, res, dur)
        pruned.append(w.sh.ran("docker", "buildx", "prune"))
        w.sh.calls.clear()
    assert pruned == [False, True, True], pruned                  # strict canary, then relaxed (prunes), then graduated
    assert routine.load_state()["canary"]["docker_cache"]["runs"] == 2 and routine.read_changes()[0]["task"] == "docker_cache"


def test_effective_canary_cap_never_exceeds_the_normal_cap_for_any_c1_task(w):
    """The canary may only LOWER what a task would have used. Over every real C1 task and four configurations (the task's own
    explicit limits, only the global default, big/small own limits)."""
    w.mp.setattr(core, "REGISTRY", REAL_REGISTRY)
    c1 = sorted(n for n, t in REAL_REGISTRY.items() if t.klass == "C1")
    assert {"docker_cache", "docker_containers_prune", "docker_images", "retention", "immich_recycle", "pressure_response"} <= set(c1)
    variants = ['[caps]\nmax_gib_per_run = 40\nmax_items_per_run = 500\n',
                '[caps]\nmax_gib_per_run = 2\nmax_items_per_run = 7\n',
                '[caps]\nmax_gib_per_run = 40\nmax_items_per_run = 500\n' + "".join(f'[tasks.{n}]\nmax_gib_per_run = 15\nmax_items_per_run = 12\n' for n in c1),
                '[caps]\nmax_gib_per_run = 1\nmax_items_per_run = 3\n' + "".join(f'[tasks.{n}]\nmax_gib_per_run = 90\nmax_items_per_run = 900\n' for n in c1)]
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    for text in variants:
        w.maint(text)
        cfg = core.load_config()
        g = routine.RunGuard(rc=rc, now_fn=lambda: FRI, mcfg=cfg)
        for n in c1:
            caps = routine.canary_caps(n, {}, cfg, rc)
            assert caps is not None, n
            normal = Ctx(cfg, n, True)
            canary = Ctx(g.task_cfg(cfg, REAL_REGISTRY[n], routine.Decision(caps=caps)), n, True)
            assert canary.cap_bytes <= normal.cap_bytes and canary.cap_items <= normal.cap_items, (n, text[:40])
            assert cfg == core.load_config()                                           # task_cfg works on a copy


def test_canary_keeps_docker_containers_prune_tighter_default(w):
    """Real finding: the task applies min(cap, 25) only while it has NO explicit max_items_per_run; the old canary wrote 50
    into [tasks.docker_containers_prune] and so made the first run (50) looser than every later one (25)."""
    from homelab_maint.tasks import native
    w.mp.setattr(core, "REGISTRY", REAL_REGISTRY)
    w.maint('[caps]\nmax_gib_per_run = 40\nmax_items_per_run = 500\n[tasks.docker_containers_prune]\nmode = "apply"\n')
    seen = []

    class Rec(native._Acts):
        def __init__(self, ctx):
            super().__init__(ctx)
            seen.append(ctx.cap_items)
    w.mp.setattr(native, "_Acts", Rec)
    w.mp.setattr(native, "_containers_inventory", lambda: [])
    t = REAL_REGISTRY["docker_containers_prune"]
    cfg = core.load_config()
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    g = routine.RunGuard(rc=rc, now_fn=lambda: FRI, mcfg=cfg)
    caps = routine.canary_caps("docker_containers_prune", {}, cfg, rc)
    assert caps["max_items_per_run"] == 50
    core.run_task(t, cfg, True)                                                    # a normal (graduated) run
    core.run_task(t, g.task_cfg(cfg, t, routine.Decision(caps=caps)), True)        # the canary run
    assert seen == [25, 25]
    # a task with its own explicit limit: the canary lowers it, never raises it
    w.maint('[tasks.docker_containers_prune]\nmode = "apply"\nmax_items_per_run = 20\n')
    cfg = core.load_config()
    seen.clear()
    core.run_task(t, g.task_cfg(cfg, t, routine.Decision(caps=routine.canary_caps("docker_containers_prune", {}, cfg, rc))), True)
    assert seen == [2]


# ---- 4. only the owner is a "manual" run; a freeze and a halt need --force ------------------------------------------------
def test_is_manual_needs_the_owner_not_just_a_task(w, monkeypatch):
    import argparse as ap
    N = lambda **k: ap.Namespace(**{"task": "clean_a", **k})            # noqa: E731
    monkeypatch.delenv("HOMELAB_MAINT_SCHEDULED", raising=False)
    assert routine.is_manual(N(override=True)) and routine.is_manual(N(force=True))
    assert not routine.is_manual(N())                                      # pytest has no terminal: a timer's `run --task X`
    assert not routine.is_manual(ap.Namespace(task=None, override=True))      # a tier run is never manual
    assert not routine.is_manual(N(override=True, scheduled=True))            # the tick says so itself
    monkeypatch.setenv("HOMELAB_MAINT_SCHEDULED", "1")
    assert not routine.is_manual(N(override=True))
    monkeypatch.delenv("HOMELAB_MAINT_SCHEDULED")

    class Tty:
        def fileno(self):
            return 0
    monkeypatch.setattr(routine.sys, "stdin", Tty())
    monkeypatch.setattr(routine.os, "isatty", lambda fd: True)
    assert routine.is_manual(N())                                              # the owner at a terminal
    assert not routine.is_manual(N(scheduled=True))
    monkeypatch.setattr(routine.os, "isatty", lambda fd: False)
    assert not routine.is_manual(N())                                          # the scheduler starts jobs with stdin = /dev/null
    monkeypatch.setattr(routine.os, "isatty", lambda fd: (_ for _ in ()).throw(OSError("closed")))
    assert not routine.is_manual(N())


def test_manual_run_does_not_get_past_the_freeze_file_or_a_halt_without_force(w):
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    (w.conf / "FREEZE").write_text("x")
    g = guard(w, FRI)
    t = task_of(w, "clean_a")
    d = g.begin(t, True, manual=True)
    assert d.run and not d.apply and not d.applying and d.caps is None
    assert d.reason == "manual run: FREEZE file present; report only (--force overrides)"
    d = g.begin(t, True, manual=True, force=True)
    assert d.applying and d.overridden and d.caps is not None
    w.audit_done("clean_a", FRI + 3)
    assert g.after(t, d, Result("ok", "freed", reclaimed_bytes=GIB), 1.0)["outcome"] == "override"
    (w.conf / "FREEZE").unlink()
    # the evening freeze window and a window long over: the manual override skips the window, not the freeze
    late = at(2026, 10, 2, 20, 0)
    assert not guard(w, late).begin(t, True, manual=True).applying
    assert guard(w, late).begin(t, True, manual=True, force=True).applying
    noon = at(2026, 10, 2, 12, 0)                                              # outside the window, outside the freeze: owner's call
    assert guard(w, noon).begin(t, True, manual=True).applying
    # a halted routine (post-check regression): scheduled run blocked, manual one report-only, forced one goes ahead
    routine.update_state(lambda st: st["steps"].clear())
    routine.update_state(lambda st: st["halted"].update({"daily@2026-10-02": {"ts": FRI, "by": "clean_a", "why": "chk_a: ok->warn"}}), FRI)
    d = guard(w, FRI + 5).begin(t, True)
    assert not d.run and "halted" in d.reason
    d = guard(w, FRI + 5).begin(t, True, manual=True)
    assert d.run and not d.applying and d.reason == "manual run: halted by clean_a; report only (--force overrides)"
    assert guard(w, FRI + 5).begin(t, True, manual=True, force=True).applying
    # a read-only task is not held: nothing for a freeze or a halt to protect
    assert guard(w, FRI + 5).begin(task_of(w, "chk_a"), True, manual=True).run


def test_scheduler_started_run_task_is_held_to_the_rules(w, tmp_path):
    """jobs.task_entries starts `homelab-maint run --task X --apply` with stdin = /dev/null and no flags. The old glue called
    that a manual override: it ran in the freeze and past a halt. Now it is a normal, enforced run."""
    import argparse as ap
    ran = []
    reg = {n: core.Task(n, k, t, (lambda ctx, n=n: (ran.append((n, ctx.apply)), ctx.act("docker-image-rm", "img", 100, lambda: 100), Result("ok", "x"))[2]), title=n)
           for n, (k, t) in SPEC.items()}
    w.mp.setattr(core, "REGISTRY", reg)
    cli = glued_cli(tmp_path)
    w.mp.setattr(time, "time", lambda: EVENING)
    (w.conf / "FREEZE").write_text("x")
    sched = ap.Namespace(tier="check", task="clean_a", apply=True, dry_run=False)           # exactly what the scheduler runs
    assert cli.cmd_run(sched) == 0 and ran == [] and not (w.log / "audit.jsonl").exists()   # window over + freeze: not run at all
    # the owner: --override gets past the window but not the freeze (report-only), --force gets past both
    assert cli.cmd_run(ap.Namespace(tier="check", task="clean_a", apply=True, dry_run=False, override=True)) == 0
    assert ran == [("clean_a", False)] and '"done"' not in (w.log / "audit.jsonl").read_text()
    ran.clear()
    assert cli.cmd_run(ap.Namespace(tier="check", task="clean_a", apply=True, dry_run=False, force=True)) == 0
    assert [r for r in ran if r[0] == "clean_a"] == [("clean_a", True)] and '"done"' in (w.log / "audit.jsonl").read_text()
    # and a halted routine blocks the scheduled run too
    (w.conf / "FREEZE").unlink()
    w.mp.setattr(time, "time", lambda: FRI)
    routine.update_state(lambda st: st["halted"].update({"daily@2026-10-02": {"ts": FRI, "by": "clean_a", "why": "x"}}), FRI)
    ran.clear()
    assert cli.cmd_run(sched) == 0 and ran == []


# ---- 5. a continuous task's change is not an "unverified restart" ---------------------------------------------------------
def test_continuous_changes_are_not_applicable_never_unverified(w):
    conts(w)
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    g, d = begin(w, "pressure_response", now=FRI)
    assert d.applying and d.steps == []
    w.audit_done("pressure_response", FRI + 1, "ollama-unload", "ollama:qwen", 0)
    rec = g.after(task_of(w, "pressure_response"), d, Result("ok", "L2 unloaded 1 idle model"), 1.0)
    # an idle-model unload is a cleanup, not a restart, and there is no post-change window to verify in
    assert rec["kind"] == "cleanup" and rec["verified"] is None and rec["outcome"] == "done"
    assert routine.read_changes()[0]["verified"] is None
    assert json.loads((w.state / "changes.jsonl").read_text().splitlines()[0])["verified"] is None
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 600), "daily")
    assert "not verified" not in r.summary and r.metrics["unverified"] == 0 and not r.alert
    # real restarts and throttles say what they are, derived from what the audit trail shows
    for i, (action, kind) in enumerate((("docker-restart", "restart"), ("docker restart", "restart"), ("docker stop", "restart"),
                                        ("docker-update-throttle", "config"), ("comfyui-free", "cleanup")), 1):
        g, d = begin(w, "pressure_response", now=FRI + 1000 * i)
        w.audit_done("pressure_response", FRI + 1000 * i + 1, action, "x", 0)
        rec = g.after(task_of(w, "pressure_response"), d, Result("ok", "x"), 1.0)
        assert rec["kind"] == kind, action
        assert rec["verified"] is None
    g, d = begin(w, "immich_recycle", now=FRI + 200)
    w.audit_done("immich_recycle", FRI + 201, "docker-restart", "immich_server", 0)
    assert g.after(task_of(w, "immich_recycle"), d, Result("ok", "restarted"), 1.0)["kind"] == "restart"
    assert "canary" not in routine.load_state() or "immich_recycle" not in routine.load_state()["canary"]     # no canary for a continuous task
    # a managed change that was NOT verified still counts, and the website/CLI render null as n/a
    routine.record_change("clean_a", "cleanup", "freed", bytes=5, verified=False, now=FRI + 500)
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 600), "daily")
    assert r.metrics["unverified"] == 1 and r.alert and "1 change(s) not verified" in r.summary


def test_record_change_not_applicable_and_status_rendering(w, capsys):
    r = routine.record_change("pressure_response", "cleanup", "unloaded", verified=routine.NA, now=FRI)
    assert r["verified"] is None and json.loads((core.STATE_DIR / "changes.jsonl").read_text())["verified"] is None
    assert routine.record_change("t", "cleanup", "x", verified=True, now=FRI)["verified"] is True
    assert routine.record_change("t", "cleanup", "x", verified=False, now=FRI)["verified"] is False
    assert routine.record_change("t", "cleanup", "x", {"a": "ok"}, {"a": "ok"}, now=FRI)["verified"] is True      # computed as before
    (core.STATE_DIR / "changes.jsonl").write_text('{"ts": %s, "task": "old", "kind": "cleanup", "detail": "no key", "bytes": 0, "outcome": "done"}\n' % FRI)
    assert routine.read_changes()[0]["verified"] is False                              # a record without the key: fail closed
    routine.record_change("pressure_response", "cleanup", "unloaded an idle model", verified=routine.NA, now=FRI - 30)
    rc, out, _ = run_cli(capsys, "--now", "2026-10-02 07:35", "status")
    assert "n/a" in out and "pressure_response" in out and "NOT verified" in out
    d = routine.export(FRI, timers={})
    assert [c["verified"] for c in d["changes"]] == [False, None]
    json.dumps(d)


# ---- 7. the routine tick is a hard dependency, and weekly does not need a punctual daily ---------------------------------
def test_routine_check_fails_when_the_tick_is_not_enabled(w, capsys):
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "disabled\n", rc=1)
    rc, out, _ = run_cli(capsys, "check")
    assert rc == 1 and "homelab-maint-tick.timer is not enabled" in out and out.strip().endswith("routine.toml ok")
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "", rc=1, stderr="Failed to get unit file state for homelab-maint-tick.timer: No such file or directory")
    assert run_cli(capsys, "check")[0] == 1                                       # not installed at all
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "enabled\n", rc=0)
    rc, out, _ = run_cli(capsys, "check")
    assert rc == 0 and "not enabled" not in out and "cannot tell" not in out
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "", rc=1, stderr="System has not been booted with systemd as init system")
    rc, out, _ = run_cli(capsys, "check")
    assert rc == 0 and "warning: cannot tell whether homelab-maint-tick.timer is enabled" in out      # a container: only a warning
    w.sh.rules.clear()
    assert routine.tick_state() == "unknown"                                        # systemctl missing (127)
    w.sh.when(["systemctl", "is-enabled", "custom.timer"], "enabled-runtime\n", rc=0)
    assert routine.tick_state(routine.RoutineConfig(tick_unit="custom.timer")) == "enabled"
    w.routine_toml("x [")
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "enabled\n", rc=0)
    assert run_cli(capsys, "check")[0] == 1                                          # an invalid routine.toml still fails it


def test_status_says_when_the_tick_is_missing(w, capsys):
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "disabled\n", rc=1)
    out = run_cli(capsys, "--now", "2026-10-02 07:35", "status")[1]
    assert "tick: homelab-maint-tick.timer NOT ENABLED" in out
    w.sh.when(["systemctl", "is-enabled", "homelab-maint-tick.timer"], "enabled\n", rc=0)
    assert "tick: homelab-maint-tick.timer enabled" in run_cli(capsys, "--now", "2026-10-02 07:35", "status")[1]


def test_shipped_weekly_does_not_lose_its_steps_when_the_daily_run_is_late(w):
    """Reviewer's scenario: the weekly timer fires Wed 07:45, the daily one 07:30 + up to 20 min of random delay, so the daily
    steps are all untried at 07:46. Real shipped routine.toml and real task registry; nothing is configured to apply."""
    w.mp.setattr(core, "REGISTRY", REAL_REGISTRY)
    w.routine_toml((ROOT / "etc" / "routine.toml").read_text())
    w.maint("")
    wed = at(2026, 10, 7, 7, 46)
    weekly = {s.name: s for s in routine.plan(wed) if s.routine == "weekly"}
    readonly = [n for n, s in weekly.items() if not s.disruptive and s.name not in ("verify", "report")]
    assert readonly and all(weekly[n].state == "due" for n in readonly), {n: weekly[n].state for n in readonly}
    assert weekly["c2_candidates"].state == "waiting" and "waiting for daily" in weekly["c2_candidates"].reason     # the heavy one waits
    assert weekly["verify"].state == "waiting" and weekly["report"].state == "waiting"          # and the summary waits for it
    assert {"capacity_review", "smart_selftest", "backup_verify", "updates_review"} <= set(readonly)
    # once the daily steps have run (the tick, at :52), the heavy step and the summary follow in the same tick
    done = [f"daily/{s.name}" for s in routine.plan(wed) if s.routine == "daily"]
    done_state(*done, occ="2026-10-07")
    nxt = {s.name: s for s in routine.plan(at(2026, 10, 7, 7, 52)) if s.routine == "weekly"}
    assert nxt["c2_candidates"].state == "due"


# ---- 8. verify and report close a routine: they wait for every earlier step -----------------------------------------------
def test_verify_and_report_wait_for_a_deferred_step(w, tmp_path, monkeypatch):
    """Reviewer's reproduction: backup gate busy at 07:35. The old tick ran verify (and the report) with clean_a and heavy
    deferred, marked them done and never ran them again once clean_a finally ran."""
    import argparse as ap
    ran = []
    reg = {n: core.Task(n, k, t, (lambda ctx, n=n: ran.append(n) or Result("ok", "x")), title=n)
           for n, (k, t) in {**SPEC, "report_daily": ("C0", "daily")}.items()}
    w.mp.setattr(core, "REGISTRY", reg)
    w.routine_toml(MAIN.replace('"verify"]', '"verify", "report"]'))
    cli = glued_cli(tmp_path)
    clock = [at(2026, 10, 2, 7, 35)]
    w.mp.setattr(time, "time", lambda: clock[0])
    busy = [True]
    monkeypatch.setattr(routine, "_ext_busy", lambda n: (busy[0], "backup-system.service is active"))

    def runner(task, apply):
        return cli.cmd_run(ap.Namespace(tier="check", task=task, apply=apply, dry_run=not apply, scheduled=True))
    routine.run_due(True, now_fn=lambda: clock[0], runner=runner)
    assert ran == ["chk_a", "clean_b"]                                                         # clean_a and heavy deferred ...
    assert "routine_verify_daily" not in ran and "report_daily" not in ran                     # ... so the summaries wait
    p = {s.name: s for s in routine.plan(clock[0])}
    assert p["verify"].state == "waiting" and "waiting for clean_a" in p["verify"].reason and p["report"].state == "waiting"
    busy[0], ran[:] = False, []
    clock[0] += 900
    routine.run_due(True, now_fn=lambda: clock[0], runner=runner)
    assert ran == ["clean_a", "heavy", "routine_verify_daily", "report_daily"]                 # the day's work, then verify, then the report
    assert states(clock[0])["verify"] == "done" and states(clock[0])["report"] == "done"
    ran.clear()
    assert routine.run_due(True, now_fn=lambda: clock[0], runner=runner) == [] and ran == []


def test_closing_steps_wait_only_while_the_window_is_open_and_only_for_steps_that_can_run(w):
    w.routine_toml(MAIN.replace('"verify"]', '"verify", "report"]'))
    w.registry({**SPEC, "report_daily": ("C0", "daily")})
    st = routine.load_state()
    for n in ("chk_a", "clean_b", "heavy"):
        routine.mark_run(st, "daily", n, "2026-10-02", FRI, "ok", "x")
    for i in range(2):                                                                         # clean_a errors twice: a retry is still coming
        routine.mark_run(st, "daily", "clean_a", "2026-10-02", FRI, "error", "boom", applied=True, needs_applied=True, max_attempts=3)
    routine.save_state(st)
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["clean_a"].state == "due" and p["verify"].state == "waiting" and p["report"].state == "waiting"
    assert p["report"].reason == "waiting for clean_a to finish"                               # (verify is not "done" yet, but clean_a is first)
    # the third failure: it gave up, which is terminal: verify runs (and reports the failure), then the report
    routine.mark_run(st, "daily", "clean_a", "2026-10-02", FRI, "error", "boom", applied=True, needs_applied=True, max_attempts=3)
    routine.save_state(st)
    assert states(FRI)["clean_a"] == "failed" and states(FRI)["verify"] == "due" and states(FRI)["report"] == "waiting"
    assert routine.due(FRI) == ["routine_verify_daily"]
    done_state("daily/verify", occ="2026-10-02")
    assert states(FRI)["report"] == "due"
    # the window ended with clean_a still retrying: nothing waits any more, each closing step runs once
    st = routine.load_state()
    st["steps"].pop("daily/verify")
    st["steps"]["daily/clean_a"].update(final=False, attempts=1)
    routine.save_state(st)
    late = at(2026, 10, 2, 9, 30)
    assert states(late)["clean_a"] == "missed"                                                  # a cleaner never runs late
    st = routine.load_state()
    st["steps"]["daily/chk_a"].update(done=False, final=False, attempts=1, status="error")      # a read-only step still retrying
    routine.save_state(st)
    s2 = states(late)
    assert s2["chk_a"] == "due" and s2["verify"] == "due" and s2["report"] == "due"           # window over: they close it with what is left
    assert states(at(2026, 10, 2, 9, 29, 59))["verify"] == "waiting"


def test_verify_counts_pending_steps_and_ignores_the_closing_steps(w):
    w.routine_toml(MAIN.replace('"verify"]', '"verify", "report"]'))
    w.registry({**SPEC, "report_daily": ("C0", "daily")})
    _checks(w, chk_a="ok")
    w.status({"chk_a": {"status": "ok", "alert": True}}, FRI)
    st = routine.load_state()
    for n in ("chk_a", "clean_b", "heavy", "clean_a"):
        routine.mark_run(st, "daily", n, "2026-10-02", FRI, "ok", "x", applied=True)
    st["pre"]["daily@2026-10-02"] = {"ts": FRI, "checks": {"chk_a": "ok"}}
    routine.save_state(st)
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 60), "daily")
    assert r.summary.startswith("verify daily ok: 4/4 steps done;") and r.status == "ok"          # `report` is not part of the work
    st = routine.load_state()
    st["steps"]["daily/clean_a"].update(done=False)
    routine.save_state(st)
    r = routine._verify(ctx_for(w, "routine_verify_daily", at(2026, 10, 2, 9, 40)), "daily")      # after the window: manual/late verify
    assert r.status == "info" and "1 missed" in r.summary and "pending" not in r.summary          # a missed cleaner is info, not pending
    r = routine._verify(ctx_for(w, "routine_verify_daily", FRI + 60), "daily")
    assert r.status == "info" and "1 still pending" in r.summary and not r.alert


def test_a_task_disabled_in_maint_toml_does_not_hold_the_closing_steps_up(w):
    w.maint('[tasks.clean_a]\nmode = "apply"\n[tasks.clean_b]\nmode = "report"\nenabled = false\n')
    p = {s.name: s for s in routine.plan(FRI)}
    assert p["clean_b"].state == "disabled" and p["clean_b"].reason == "disabled in maint.toml"      # cmd_run never selects it
    done_state("daily/chk_a", "daily/clean_a", "daily/heavy", occ="2026-10-02")
    assert states(FRI)["verify"] == "due"


def test_shipped_continuous_and_canary_settings_and_their_parsing(world):
    rc = routine.load_config(ROOT / "etc" / "routine.toml")
    assert rc.valid and rc.errors == []
    assert rc.restart_tasks == ["immich_recycle", "comfyui_idle_reclaim"] == list(routine.RESTART_TASKS)
    assert rc.frozen_rungs == ["restart", "emergency"] and rc.emergency_level == 4 and rc.canary_relax_after == 1
    assert rc.tick_unit == "homelab-maint-tick.timer"
    assert "freezes everything now" not in (ROOT / "etc" / "routine.toml").read_text()       # the comment no longer overpromises
    world.routine_toml(MAIN.replace("[freeze]", '[continuous]\ndisruptive = ["a", 5, " b "]\nfrozen_rungs = []\nemergency_level = 9\n'
                                    '[canary]\nrelax_after = 0\n[freeze]').replace("[settings]", '[settings]\ntick_unit = "x.timer"'))
    rc = routine.load_config()
    assert rc.restart_tasks == ["a", "b"] and rc.frozen_rungs == [] and rc.emergency_level == 5 and rc.canary_relax_after == 0
    assert rc.tick_unit == "x.timer"
    world.routine_toml(MAIN)
    rc = routine.load_config()                                                                 # absent: the safe defaults
    assert rc.restart_tasks == ["immich_recycle", "comfyui_idle_reclaim"] and rc.canary_relax_after == 1 and rc.emergency_level == 4
    assert routine.RoutineConfig(valid=False).restart_tasks == list(routine.RESTART_TASKS)    # a broken file still holds restarts
