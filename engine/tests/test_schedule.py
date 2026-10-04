"""schedule.py: the cron engine (forms, semantics, DST, windows, jitter). No host access, no clock: every instant is explicit."""
import datetime as dt
import functools
import random
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint imports)

from homelab_maint import schedule as S  # noqa: E402

TO = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")


def at(y, mo, d, h=0, mi=0, tz=TO, fold=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz, fold=fold).timestamp()


def local(t, tz=TO):
    return dt.datetime.fromtimestamp(t, tz)


def runs(expr, t0, t1, tz=TO):
    """Every run instant in (t0, t1] as local datetimes."""
    return [local(x, tz) for x in S.parse(expr, tz).occurrences(t0, t1, limit=5000)]


# --------------------------------------------------------------------------- parsing
def test_five_fields_basic():
    s = S.parse("30 3 * * *", UTC)
    assert s.minutes == (30,) and s.hours == (3,) and s.fixed
    assert local(s.next_after(at(2026, 10, 2, 1, tz=UTC), ), UTC) == dt.datetime(2026, 10, 2, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize("expr,minutes,hours", [
    ("*/15 * * * *", (0, 15, 30, 45), tuple(range(24))),
    ("5,10 1-3 * * *", (5, 10), (1, 2, 3)),
    ("10-40/10 */12 * * *", (10, 20, 30, 40), (0, 12)),
    ("3-58/5 * * * *", tuple(range(3, 59, 5)), tuple(range(24))),
    ("5/20 0 * * *", (5, 25, 45), (0,)),
])
def test_ranges_steps_lists(expr, minutes, hours):
    s = S.parse(expr, UTC)
    assert s.minutes == minutes and s.hours == hours


def test_names_and_case():
    s = S.parse("0 1 * JAN-MAR sat", UTC)
    assert s.months == frozenset({1, 2, 3}) and s.dows == frozenset({6})
    assert S.parse("0 1 * * Mon-Fri", UTC).dows == frozenset({1, 2, 3, 4, 5})
    assert S.parse("0 1 * * 7", UTC).dows == frozenset({0})            # 7 is Sunday like 0


@pytest.mark.parametrize("macro,cron", [("@hourly", "0 * * * *"), ("@daily", "0 0 * * *"), ("@midnight", "0 0 * * *"),
                                         ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *"), ("@yearly", "0 0 1 1 *")])
def test_macros(macro, cron):
    assert S.parse(macro, UTC).cron == cron


@pytest.mark.parametrize("sugar,cron", [
    ("every 5m", "*/5 * * * *"), ("every 1m", "* * * * *"), ("every 2h", "0 */2 * * *"), ("every 1h", "0 * * * *"),
    ("daily 03:30", "30 3 * * *"), ("weekly sat 01:00", "0 1 * * 6"), ("weekly Saturday 01:00", "0 1 * * 6"),
    ("monthly 1st sat 04:30", "30 4 * * 6#1"), ("monthly last fri 02:00", "0 2 * * 5#L"), ("monthly L 03:00", "0 3 L * *"),
    ("monthly 15 03:00", "0 3 15 * *"), ("monthly 2nd mon 06:15", "15 6 * * 1#2"),
])
def test_sugar(sugar, cron):
    assert S.parse(sugar, UTC).cron == cron


@pytest.mark.parametrize("bad", [
    "", "   ", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *", "* * * 13 *", "* * * * 8",
    "*/0 * * * *", "5-1 * * * *", "a * * * *", "1,,2 * * * *", "every 7m", "every 25h", "every 0m", "daily 25:00",
    "daily 3:5", "weekly xyz 01:00", "monthly 3rd 01:00", "* * * * sat#9", "* * * * sat#0", "* * * L *", "@reboot",
])
def test_bad_expressions_raise(bad):
    with pytest.raises(S.ScheduleError):
        S.parse(bad, UTC)
    assert S.validate(bad, UTC)                          # validate() returns the error text, never raises


def test_validate_ok_is_none():
    assert S.validate("0 1 * * sat", UTC) is None


# --------------------------------------------------------------------------- next_after / prev_before
def test_strictly_after_and_before():
    t = at(2026, 10, 2, 3, 30)
    s = S.parse("30 3 * * *", TO)
    assert s.next_after(t) == at(2026, 10, 3, 3, 30)            # an instant ON the schedule is not "after" itself
    assert s.next_after(t - 1) == t
    assert s.prev_before(t) == at(2026, 10, 1, 3, 30)
    assert s.prev_before(t + 1) == t


def test_weekday_and_day_of_month_semantics():
    # Vixie: both restricted -> EITHER matches; one star -> only the other matters.
    both = S.parse("0 4 13 * fri", UTC)
    days = {r.date() for r in runs("0 4 13 * fri", at(2026, 2, 28, tz=UTC), at(2026, 4, 1, tz=UTC), UTC)}
    assert dt.date(2026, 3, 13) in days and dt.date(2026, 3, 6) in days and dt.date(2026, 3, 20) in days
    only_dom = {r.date() for r in runs("0 4 13 * *", at(2026, 2, 28, tz=UTC), at(2026, 4, 1, tz=UTC), UTC)}
    assert only_dom == {dt.date(2026, 3, 13)}
    assert both.dom_star is False and both.dow_star is False


def test_month_end_and_nth_weekday():
    got = runs("0 3 L * *", at(2026, 1, 1), at(2026, 6, 1))
    assert [(r.month, r.day) for r in got] == [(1, 31), (2, 28), (3, 31), (4, 30), (5, 31)]
    first_sat = runs("monthly 1st sat 04:30", at(2026, 9, 1), at(2026, 12, 31))
    assert [(r.month, r.day) for r in first_sat] == [(9, 5), (10, 3), (11, 7), (12, 5)]
    last_fri = runs("monthly last fri 02:00", at(2026, 9, 1), at(2026, 12, 31))
    assert [(r.month, r.day) for r in last_fri] == [(9, 25), (10, 30), (11, 27), (12, 25)]
    fifth = runs("0 1 * * sat#5", at(2026, 1, 1), at(2026, 12, 31))          # 5th Saturdays exist only in some months
    assert all(r.weekday() == 5 and r.day >= 29 for r in fifth) and len(fifth) == 4


def test_leap_day_and_impossible_dates():
    s = S.parse("0 12 29 2 *", UTC)
    assert local(s.next_after(at(2026, 3, 1, tz=UTC)), UTC).date() == dt.date(2028, 2, 29)
    assert S.parse("0 0 31 2 *", UTC).next_after(at(2026, 1, 1, tz=UTC)) is None      # never: None, not a hang
    assert S.parse("0 0 31 2 *", UTC).prev_before(at(2026, 1, 1, tz=UTC)) is None


def test_year_boundary():
    r = runs("30 23 31 12 *", at(2026, 12, 30), at(2028, 1, 2))
    assert [(x.year, x.month, x.day, x.hour, x.minute) for x in r] == [(2026, 12, 31, 23, 30), (2027, 12, 31, 23, 30)]


@functools.lru_cache(maxsize=None)
def _expand(f, lo, hi):
    out = set()
    for part in f.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else ((int(rng.split("-")[0]), int(rng.split("-")[1])) if "-" in rng
                                            else (int(rng), hi if step else int(rng)))
        out |= set(range(a, b + 1, int(step) if step else 1))
    return frozenset(out)


def _oracle(expr, d):
    """Independent, deliberately dumb matcher for numeric/star/step/list expressions (UTC, no DST)."""
    m, h, dom, mon, dow = expr.split()
    wd = (d.weekday() + 1) % 7
    dm = d.day in _expand(dom, 1, 31)
    dw = wd in {x % 7 for x in _expand(dow, 0, 7)}
    day_ok = (dm or dw) if (not dom.startswith("*") and not dow.startswith("*")) else (dm and dw)
    return d.minute in _expand(m, 0, 59) and d.hour in _expand(h, 0, 23) and d.month in _expand(mon, 1, 12) and day_ok


def test_differential_against_brute_force():
    rnd = random.Random(20261002)
    for _ in range(40):
        m = rnd.choice(["*", "*/7", "0", "5,35", "10-50/20", "59"])
        h = rnd.choice(["*", "*/5", "3", "0,12", "1-4", "23"])
        dom = rnd.choice(["*", "1", "15", "28-31", "*/9", "10,20"])
        mon = rnd.choice(["*", "2", "1-3", "*/4", "12"])
        dow = rnd.choice(["*", "0", "1-5", "6", "*/2", "2,4"])
        expr = f"{m} {h} {dom} {mon} {dow}"
        s = S.parse(expr, UTC)
        t = at(2026, rnd.randint(1, 12), rnd.randint(1, 28), rnd.randint(0, 23), rnd.randint(0, 59), UTC)
        got = s.next_after(t)
        cur = dt.datetime.fromtimestamp(t, UTC).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        want = None
        for _i in range(60 * 24 * 400):
            if _oracle(expr, cur):
                want = cur.timestamp()
                break
            cur += dt.timedelta(minutes=1)
        if want is None:                                  # rare combination: the engine may look further than the 400-day oracle
            assert got is None or (got > t + 400 * 86400 and _oracle(expr, dt.datetime.fromtimestamp(got, UTC))), expr
            continue
        assert got == want, expr
        if got is not None:
            assert s.prev_before(got + 1) == got and (s.prev_before(got) or 0) < got


def test_next_prev_are_inverse_walks():
    s = S.parse("*/20 6-9 * * mon-fri", TO)
    t = at(2026, 10, 2, 12)
    fwd = []
    cur = t
    for _ in range(30):
        cur = s.next_after(cur)
        fwd.append(cur)
    back = []
    cur = fwd[-1] + 1
    for _ in range(30):
        cur = s.prev_before(cur)
        back.append(cur)
    assert list(reversed(back)) == fwd
    assert all(local(x).weekday() < 5 and 6 <= local(x).hour <= 9 for x in fwd)


# --------------------------------------------------------------------------- DST (America/Toronto: 2026-03-08 and 2026-11-01)
def test_dst_spring_forward_fixed_job_runs_once_shifted():
    # 02:30 does not exist on 2026-03-08. A fixed-time job runs ONCE, shifted forward by the gap to 03:30 EDT.
    r = runs("30 2 * * *", at(2026, 3, 7, 12), at(2026, 3, 9, 12))
    assert [(x.day, x.hour, x.minute) for x in r] == [(8, 3, 30), (9, 2, 30)]
    assert r[0].utcoffset() == dt.timedelta(hours=-4) and r[1].utcoffset() == dt.timedelta(hours=-4)
    # exactly one run per local day around the change, never zero, never two
    days = [x.date() for x in runs("30 2 * * *", at(2026, 3, 1), at(2026, 3, 20))]
    assert len(days) == len(set(days)) == 19


def test_dst_spring_forward_gap_hour_job_at_0200():
    r = runs("0 2 * * *", at(2026, 3, 7, 12), at(2026, 3, 8, 12))
    assert [(x.hour, x.minute) for x in r] == [(3, 0)]


def test_dst_fall_back_fixed_job_runs_once_first_pass():
    r = runs("30 1 * * *", at(2026, 10, 31, 12), at(2026, 11, 2, 12))
    assert [(x.day, x.hour, x.minute, x.fold) for x in r] == [(1, 1, 30, 0), (2, 1, 30, 0)]
    assert r[0].utcoffset() == dt.timedelta(hours=-4)                 # the FIRST 01:30 (EDT), not the second (EST)
    # sunday 01:00 backup-immich on the overlap day: once
    imm = runs("0 1 * * sun", at(2026, 10, 25, 12), at(2026, 11, 2, 12))
    assert [(x.day, x.hour) for x in imm] == [(1, 1)] and imm[0].utcoffset() == dt.timedelta(hours=-4)


def test_dst_interval_jobs_follow_real_time():
    day0, day1 = at(2026, 11, 1), at(2026, 11, 2)
    assert len(runs("0 * * * *", day0 - 1, day1 - 1)) == 25           # 25-hour day: 01:00 happens on both passes
    assert len(runs("*/30 * * * *", day0 - 1, day1 - 1)) == 50
    g0, g1 = at(2026, 3, 8), at(2026, 3, 9)
    assert len(runs("0 * * * *", g0 - 1, g1 - 1)) == 23              # 23-hour day: 02:00 does not exist
    assert len(runs("*/15 * * * *", g0 - 1, g1 - 1)) == 92
    # the gap hour is simply absent from an interval job
    assert all(x.hour != 2 for x in runs("*/30 * * * *", g0 - 1, g1 - 1))
    # both passes of 01:xx in the overlap are present for an interval job
    ones = [x for x in runs("0 * * * *", day0 - 1, day1 - 1) if x.hour == 1]
    assert len(ones) == 2 and ones[0].utcoffset() != ones[1].utcoffset()


def test_dst_two_hourly_grid_and_next_after_across_gap():
    # "24 */2 * * *" (immich recycle): the 02:24 run does not exist on the gap day and is skipped (interval semantics)
    r = runs("24 */2 * * *", at(2026, 3, 8) - 1, at(2026, 3, 9) - 1)
    assert [x.hour for x in r] == [0, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]
    s = S.parse("24 */2 * * *", TO)
    assert local(s.next_after(at(2026, 3, 8, 1, 30)), TO).hour == 4          # skips straight over the missing 02:24
    assert local(s.prev_before(at(2026, 3, 8, 4, 30)), TO).hour == 4
    assert local(s.prev_before(at(2026, 3, 8, 4, 0)), TO).hour == 0


def test_dst_weekly_and_monthly_forms_unaffected():
    assert [(x.month, x.day, x.hour) for x in runs("0 1 * * sat", at(2026, 3, 1), at(2026, 3, 31))] == \
        [(3, 7, 1), (3, 14, 1), (3, 21, 1), (3, 28, 1)]
    r = runs("monthly 1st sat 04:30", at(2026, 10, 1), at(2026, 11, 30))
    assert [(x.month, x.day, x.hour, x.minute) for x in r] == [(10, 3, 4, 30), (11, 7, 4, 30)]
    assert r[1].utcoffset() == dt.timedelta(hours=-5)                 # after fall-back: EST


def test_dst_next_after_is_monotonic_across_both_changes():
    for expr in ("30 2 * * *", "30 1 * * *", "*/30 * * * *", "0 * * * *", "15 3 * * sun"):
        s = S.parse(expr, TO)
        cur = at(2026, 3, 1)
        prev = cur
        n = 0
        while cur < at(2026, 11, 20) and n < 20000:
            cur = s.next_after(cur)
            assert cur > prev
            prev = cur
            n += 1


def test_gap_instant_maps_to_real_time_not_naive_utc():
    t = S.parse("30 2 * * *", TO).next_after(at(2026, 3, 8, 0, 0))
    assert t == dt.datetime(2026, 3, 8, 7, 30, tzinfo=UTC).timestamp()      # 02:30 EST == 07:30 UTC == 03:30 EDT


# --------------------------------------------------------------------------- zones and helpers
def test_host_tz_resolution(monkeypatch):
    monkeypatch.setenv("HOMELAB_MAINT_TZ", "America/Toronto")
    assert getattr(S.host_tz(), "key", "") == "America/Toronto"
    monkeypatch.setenv("HOMELAB_MAINT_TZ", "Not/AZone")
    monkeypatch.setenv("TZ", "UTC")
    assert S.host_tz() is not None                                    # falls back, never raises
    assert getattr(S.host_tz("Europe/Paris"), "key", "") == "Europe/Paris"


def test_parse_is_cached_per_zone():
    assert S.parse("0 1 * * *", TO) is S.parse("0 1 * * *", TO)
    assert S.parse("0 1 * * *", TO) is not S.parse("0 1 * * *", UTC)


def test_same_expression_two_zones_differ():
    t = at(2026, 6, 1, 0, tz=UTC)
    a = S.next_after("0 1 * * *", t, UTC)
    b = S.next_after("0 1 * * *", t, TO)
    assert b - a == 4 * 3600                                         # 01:00 Toronto (EDT, UTC-4) is 05:00 UTC


def test_min_gap_and_occurrences():
    s = S.parse("*/5 * * * *", UTC)
    assert s.min_gap_s(at(2026, 1, 1, tz=UTC)) == 300
    assert S.parse("0 1 * * sat", UTC).min_gap_s(at(2026, 1, 1, tz=UTC)) == 7 * 86400
    occ = s.occurrences(at(2026, 1, 1, tz=UTC), at(2026, 1, 1, 1, tz=UTC))
    assert len(occ) == 12 and occ[0] == at(2026, 1, 1, 0, 5, UTC)
    assert len(s.occurrences(0, 10 ** 9, limit=7)) == 7


def test_engine_is_fast():
    t0 = time.perf_counter()
    for _ in range(50):
        S.parse("* * * * *", TO).next_after(at(2026, 6, 1, 12))
        S.parse("0 0 31 2 *", TO).next_after(at(2026, 6, 1, 12))
    assert (time.perf_counter() - t0) < 1.5


# --------------------------------------------------------------------------- jitter
def test_jitter_bounds_and_determinism():
    assert S.jitter("a", 1000, 0) == 0 and S.jitter("a", 1000, -5) == 0
    vals = {S.jitter("backup-system", 1_790_000_000 + i * 604800, 900) for i in range(60)}
    assert all(0 <= v < 900 for v in vals) and len(vals) > 40                      # spread, not constant
    assert S.jitter("a", 1000, 900) == S.jitter("a", 1000, 900)                    # stable: the due time never moves between ticks
    assert S.jitter("a", 1000, 900) != S.jitter("b", 1000, 900) or S.jitter("a", 2000, 900) != S.jitter("b", 2000, 900)
    assert S.jitter("a", 1000.9, 60) == S.jitter("a", 1000.1, 60)                  # keyed on the whole second


# --------------------------------------------------------------------------- windows
@pytest.mark.parametrize("spec,t,inside", [
    ("07:30-09:30", at(2026, 10, 2, 7, 30), True), ("07:30-09:30", at(2026, 10, 2, 9, 29), True),
    ("07:30-09:30", at(2026, 10, 2, 9, 30), False), ("07:30-09:30", at(2026, 10, 2, 7, 29), False),
    ("daily 18:00-23:30", at(2026, 10, 2, 20), True),
    ("22:00-02:00", at(2026, 10, 2, 23), True), ("22:00-02:00", at(2026, 10, 3, 1, 59), True),
    ("22:00-02:00", at(2026, 10, 3, 2, 0), False), ("22:00-02:00", at(2026, 10, 2, 12), False),
    ("Sat 00:30-08:00", at(2026, 10, 3, 3), True), ("Sat 00:30-08:00", at(2026, 10, 2, 3), False),
    ("Mon-Fri 22:00-02:00", at(2026, 10, 3, 1), True),            # Fri night spills into Saturday 01:00: still "Fri"
    ("Mon-Fri 22:00-02:00", at(2026, 10, 4, 1), False),           # Sat night spills into Sunday: not a Mon-Fri start
    ("1st Sat 04:30-07:00", at(2026, 10, 3, 5), True), ("1st Sat 04:30-07:00", at(2026, 10, 10, 5), False),
    ("last fri 01:00-03:00", at(2026, 10, 30, 2), True), ("last fri 01:00-03:00", at(2026, 10, 23, 2), False),
    ("weekdays 09:00-17:00", at(2026, 10, 2, 10), True), ("weekdays 09:00-17:00", at(2026, 10, 3, 10), False),
    ("23:00-24:00", at(2026, 10, 2, 23, 59), True),
])
def test_window_contains(spec, t, inside):
    assert S.in_window(spec, t, TO) is inside


@pytest.mark.parametrize("bad", ["", "07:30", "07:30-07:30", "25:00-26:00", "07:30-09:61", "xyz 07:30-09:30", "07:30 - "])
def test_bad_windows(bad):
    with pytest.raises(S.ScheduleError):
        S.parse_window(bad, TO)


def test_window_next_open():
    w = S.parse_window("Sat 00:30-08:00", TO)
    t = at(2026, 10, 2, 12)
    assert w.next_open(t) == at(2026, 10, 3, 0, 30)
    assert w.next_open(at(2026, 10, 3, 3)) == at(2026, 10, 3, 3)               # already inside: itself
    never = S.parse_window("1st Sat 04:30-07:00", TO)
    assert local(never.next_open(at(2026, 10, 4)), TO).date() == dt.date(2026, 11, 7)


def test_window_across_dst_uses_wall_clock():
    # the 02:00-04:00 window on the gap day: 03:00 EDT is inside, and there is no 02:30
    assert S.in_window("02:00-04:00", at(2026, 3, 8, 3, 15), TO)
    assert not S.in_window("02:00-04:00", at(2026, 3, 8, 4, 15), TO)
    assert S.in_window("01:00-02:00", at(2026, 11, 1, 1, 30, fold=1), TO)      # both passes of 01:30 are inside
    assert S.in_window("01:00-02:00", at(2026, 11, 1, 1, 30, fold=0), TO)
