"""schedule: stdlib cron-expression engine for the homelab-maint scheduler (timezone and DST correct).

Accepted forms (everything is evaluated in ONE zone, the host TZ unless a zone is passed):

    5-field cron      "30 3 * * *"     minute hour day-of-month month day-of-week
    macros            @hourly @daily @midnight @weekly @monthly @yearly @annually
    names             jan..dec, sun..sat (case-insensitive); ranges "mon-fri", lists "1,15", steps "*/5", "10-40/10"
    month-end         day-of-month "L" (last day), e.g. "0 3 L * *"
    n-th weekday      day-of-week "sat#1" (first Saturday), "fri#L" (last Friday), "6#2"
    sugar             "every 5m" / "every 2h" (divisors of 60 / 24 only: the grid stays wall-clock aligned),
                      "daily 03:30", "weekly sat 01:00",
                      "monthly 1st sat 04:30" | "monthly last fri 02:00" | "monthly L 03:00" | "monthly 15 03:00"

Cron day semantics are Vixie's: when BOTH day-of-month and day-of-week are restricted (do not start with "*") a date
matches if EITHER does; otherwise both must.

DST rules (host TZ is America/Toronto, so both transitions happen every year; all of this is tested):
  * A job whose minute AND hour are plain numbers ("fixed", e.g. "30 2 * * *") runs EXACTLY ONCE per local day:
      - spring-forward gap (02:30 does not exist): it runs once at the same elapsed offset, i.e. 03:30 (wall clock
        shifted forward by the gap, never skipped and never twice);
      - fall-back overlap (01:30 happens twice): it runs at the FIRST occurrence only.
  * An "interval" job (minute or hour has "*", a step, a range) follows real time like cronie does:
      - gap: the wall times that do not exist are skipped, the next real match is the next one on the grid;
      - overlap: it runs on both passes ("0 * * * *" fires 01:00 EDT and 01:00 EST).
  * next_after(t) is strictly greater than t; prev_before(t) is strictly less than t. Both return epoch seconds
    (float) or None when nothing matches within 9 years (e.g. "0 0 31 2 *").

jitter(name, instant, window_s) is a deterministic offset in [0, window_s) derived from a hash, the equivalent of
systemd's RandomizedDelaySec: stable across ticks (the due time never moves) and different per job and per run.

Window specs ("07:30-09:30", "Sat 00:30-08:00", "daily 07:30-09:30", "Wed 07:45-10:00", "1st Sat 04:30-07:00",
"Mon-Fri 22:00-02:00") describe allowed or forbidden hours of the day; a window may wrap past midnight.
"""
from __future__ import annotations

import calendar
import hashlib
import os
import re
from datetime import date, datetime, timedelta, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_DAYS = 366 * 9            # search horizon: covers a Feb-29-only schedule across a century leap gap
_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
           "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_ORD = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


class ScheduleError(ValueError):
    """A schedule or window expression that cannot be parsed (callers treat that job as invalid, never as 'always')."""


# --------------------------------------------------------------------------- host zone
_TZ_CACHE: dict[tuple, tzinfo] = {}


def host_tz(name: str | None = None) -> tzinfo:
    """The zone schedules are evaluated in: explicit name, $HOMELAB_MAINT_TZ, $TZ, /etc/timezone, /etc/localtime, UTC."""
    key = (name, os.environ.get("HOMELAB_MAINT_TZ"), os.environ.get("TZ"))
    z = _TZ_CACHE.get(key)
    if z is not None:
        return z
    cands = [name, os.environ.get("HOMELAB_MAINT_TZ"), (os.environ.get("TZ") or "").lstrip(":")]
    try:
        with open("/etc/timezone") as f:
            cands.append(f.read().strip())
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for c in cands:
        if c:
            try:
                z = ZoneInfo(c)
                break
            except (ZoneInfoNotFoundError, ValueError, OSError):
                continue
    z = z or ZoneInfo("UTC")
    _TZ_CACHE[key] = z
    return z


def _as_tz(tz) -> tzinfo:
    if tz is None or isinstance(tz, str):
        return host_tz(tz)
    return tz


# --------------------------------------------------------------------------- parsing
def _hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ScheduleError(f"bad time {s!r} (want HH:MM)")
    return int(m[1]), int(m[2])


def _dow_num(tok: str) -> int:
    t = tok.strip().lower()
    if t in _DOWS:
        return _DOWS[t]
    if t[:3] in _DOWS and t in (t[:3], {"sun": "sunday", "mon": "monday", "tue": "tuesday", "wed": "wednesday",
                                        "thu": "thursday", "fri": "friday", "sat": "saturday"}[t[:3]]):
        return _DOWS[t[:3]]
    raise ScheduleError(f"bad weekday {tok!r}")


def _translate(expr: str) -> str:
    """Sugar and macros -> a plain 5-field cron string."""
    s = " ".join(expr.strip().split())
    low = s.lower()
    if not s:
        raise ScheduleError("empty schedule")
    if low in _MACROS:
        return _MACROS[low]
    m = re.fullmatch(r"every (\d+) ?(m|min|mins|minutes?|h|hr|hrs|hours?)", low)
    if m:
        n, unit = int(m[1]), m[2][0]
        if unit == "m":
            if n < 1 or 60 % n:
                raise ScheduleError(f"{expr!r}: minutes must divide 60 (use a cron expression otherwise)")
            return "* * * * *" if n == 1 else f"*/{n} * * * *"
        if n < 1 or 24 % n:
            raise ScheduleError(f"{expr!r}: hours must divide 24 (use a cron expression otherwise)")
        return "0 * * * *" if n == 1 else ("0 0 * * *" if n == 24 else f"0 */{n} * * *")
    m = re.fullmatch(r"daily (\d{1,2}:\d{2})", low)
    if m:
        h, mi = _hhmm(m[1])
        return f"{mi} {h} * * *"
    m = re.fullmatch(r"weekly (\w+) (\d{1,2}:\d{2})", low)
    if m:
        h, mi = _hhmm(m[2])
        return f"{mi} {h} * * {_dow_num(m[1])}"
    m = re.fullmatch(r"monthly (\S+)(?: (\w+))? (\d{1,2}:\d{2})", low)
    if m:
        h, mi = _hhmm(m[3])
        what, dow = m[1], m[2]
        if dow:                                           # "1st sat", "last fri"
            n = "L" if what in ("last", "l") else str(_ORD.get(what) or _bad(f"{expr!r}: bad ordinal {what!r}"))
            return f"{mi} {h} * * {_dow_num(dow)}#{n}"
        if what in ("l", "last"):
            return f"{mi} {h} L * *"
        if what.isdigit():
            return f"{mi} {h} {int(what)} * *"
        raise ScheduleError(f"{expr!r}: monthly needs 'N', 'L' or '<nth> <weekday>'")
    return s


def _bad(msg: str):
    raise ScheduleError(msg)


def _parse_field(text: str, lo: int, hi: int, names: dict | None, field: str) -> tuple[set[int], list[tuple[int, int]], bool]:
    """-> (values, nth_rules, has_L). nth_rules/has_L only for day-of-week / day-of-month; see callers."""
    vals: set[int] = set()
    nth: list[tuple[int, int]] = []
    has_l = False

    def num(tok: str) -> int:
        t = tok.strip().lower()
        if names and t in names:
            return names[t]
        if not t.isdigit():
            raise ScheduleError(f"{field}: bad value {tok!r}")
        return int(t)

    for part in text.split(","):
        if not part:
            raise ScheduleError(f"{field}: empty list item in {text!r}")
        if field == "day-of-month" and part.lower() == "l":
            has_l = True
            continue
        if field == "day-of-week" and "#" in part:
            d, _, n = part.partition("#")
            dn = num(d) % 7
            if n.lower() == "l":
                nth.append((dn, -1))
            elif n.isdigit() and 1 <= int(n) <= 5:
                nth.append((dn, int(n)))
            else:
                raise ScheduleError(f"{field}: bad n-th weekday {part!r} (want name#1..5 or name#L)")
            continue
        rng, _, step_s = part.partition("/")
        step = 1
        if "/" in part:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ScheduleError(f"{field}: bad step in {part!r}")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, _, y = rng.partition("-")
            a, b = num(x), num(y)
        else:
            a = num(rng)
            b = hi if "/" in part else a             # "5/15" == "5-max/15"
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise ScheduleError(f"{field}: {part!r} out of range {lo}-{hi}")
        vals.update(range(a, b + 1, step))
    return vals, nth, has_l


class Schedule:
    """A parsed schedule bound to a time zone. Build with parse(); methods are pure functions of their arguments."""

    def __init__(self, expr: str, tz=None):
        self.expr = expr
        self.tz = _as_tz(tz)
        self.cron = _translate(expr)
        f = self.cron.split()
        if len(f) != 5:
            raise ScheduleError(f"{expr!r}: want 5 fields (minute hour day month weekday), got {len(f)}")
        self.minutes = tuple(sorted(_parse_field(f[0], 0, 59, None, "minute")[0]))
        self.hours = tuple(sorted(_parse_field(f[1], 0, 23, None, "hour")[0]))
        dom, _, self.dom_last = _parse_field(f[2], 1, 31, None, "day-of-month")
        self.doms = frozenset(dom)
        self.months = frozenset(_parse_field(f[3], 1, 12, _MONTHS, "month")[0])
        dows, self.nth, _ = _parse_field(f[4], 0, 7, _DOWS, "day-of-week")
        self.dows = frozenset(d % 7 for d in dows)
        self.dom_star = f[2].startswith("*")
        self.dow_star = f[4].startswith("*")
        # "fixed" = a plain numeric minute and hour: one run per local day, DST handled as documented above.
        self.fixed = bool(re.fullmatch(r"\d+(,\d+)*", f[0]) and re.fullmatch(r"\d+(,\d+)*", f[1]))
        if not (self.minutes and self.hours):
            raise ScheduleError(f"{expr!r}: empty minute/hour set")

    def __repr__(self) -> str:
        return f"Schedule({self.cron!r}, tz={getattr(self.tz, 'key', self.tz)})"

    # ---- calendar
    def _date_ok(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        last = calendar.monthrange(d.year, d.month)[1]
        dom_ok = d.day in self.doms or (self.dom_last and d.day == last)
        wd = (d.weekday() + 1) % 7                                      # Monday=0 -> sun=0 numbering
        dow_ok = wd in self.dows
        if not dow_ok:
            for dn, n in self.nth:
                if wd == dn and ((n == -1 and d.day + 7 > last) or (n > 0 and (d.day - 1) // 7 + 1 == n)):
                    dow_ok = True
                    break
        # Vixie/cronie: when EITHER field starts with "*" (even "*/9") both must match; only when both are restricted does a
        # date match if EITHER does. A plain "*" matches every day, so "dom_ok and dow_ok" is then just the other field.
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def _instants(self, d: date) -> list[float]:
        """Every run instant (epoch) of the local calendar day `d`, ascending, with the DST rules applied."""
        out: set[float] = set()
        tz = self.tz
        # Fast path (every day except the two DST days): one fixed UTC offset all day, so wall time maps linearly.
        # The offset is sampled at both ends of the day; a transition anywhere inside makes them differ.
        o0 = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz).utcoffset()
        o1 = datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz).utcoffset()
        if o0 == o1:
            base = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
            return [base + h * 3600 + mi * 60 for h in self.hours for mi in self.minutes]
        for h in self.hours:
            for mi in self.minutes:
                naive = datetime(d.year, d.month, d.day, h, mi)
                ta = naive.replace(tzinfo=tz, fold=0).timestamp()
                tb = naive.replace(tzinfo=tz, fold=1).timestamp()
                ra = datetime.fromtimestamp(ta, tz).replace(tzinfo=None) == naive
                rb = datetime.fromtimestamp(tb, tz).replace(tzinfo=None) == naive
                if ra and rb:
                    out.add(ta)
                    if ta != tb and not self.fixed:                    # overlap: interval jobs run on both passes
                        out.add(tb)
                elif self.fixed:                                       # gap: run once, shifted forward by the gap
                    out.add(ta)
        return sorted(out)

    # ---- queries
    def next_after(self, t: float) -> float | None:
        d = datetime.fromtimestamp(t, self.tz).date() - timedelta(days=1)
        best: float | None = None
        stop = None
        for i in range(MAX_DAYS):
            if stop is not None and i > stop:
                break
            if self._date_ok(d):
                for ts in self._instants(d):
                    if ts > t and (best is None or ts < best):
                        best = ts
                if best is not None and stop is None:
                    stop = i + 1                                       # also look at the next day: DST shifts reorder
            d += timedelta(days=1)
        return best

    def prev_before(self, t: float) -> float | None:
        d = datetime.fromtimestamp(t, self.tz).date() + timedelta(days=1)
        best: float | None = None
        stop = None
        for i in range(MAX_DAYS):
            if stop is not None and i > stop:
                break
            if self._date_ok(d):
                for ts in self._instants(d):
                    if ts < t and (best is None or ts > best):
                        best = ts
                if best is not None and stop is None:
                    stop = i + 1
            d -= timedelta(days=1)
        return best

    def occurrences(self, t0: float, t1: float, limit: int = 500) -> list[float]:
        """Run instants in (t0, t1], ascending, at most `limit` (calendars and the web table)."""
        out: list[float] = []
        cur = t0
        while len(out) < limit:
            nxt = self.next_after(cur)
            if nxt is None or nxt > t1:
                break
            out.append(nxt)
            cur = nxt
        return out

    def min_gap_s(self, t: float, n: int = 4) -> float | None:
        """Smallest distance between the next `n` consecutive runs after `t` (how frequent the job is)."""
        gaps, cur = [], t
        for _ in range(n):
            nxt = self.next_after(cur)
            if nxt is None:
                break
            if cur != t:
                gaps.append(nxt - cur)
            cur = nxt
        return min(gaps) if gaps else None


@lru_cache(maxsize=256)
def _cached(expr: str, tzkey) -> Schedule:
    return Schedule(expr, tzkey if isinstance(tzkey, str) else tzkey)


def parse(expr: str, tz=None) -> Schedule:
    """Parse (cached). `tz` may be a ZoneInfo, a zone name or None for the host zone."""
    z = _as_tz(tz)
    key = getattr(z, "key", None)
    return _cached(expr, key) if key else Schedule(expr, z)


def next_after(expr: str, t: float, tz=None) -> float | None:
    return parse(expr, tz).next_after(t)


def prev_before(expr: str, t: float, tz=None) -> float | None:
    return parse(expr, tz).prev_before(t)


def validate(expr: str, tz=None) -> str | None:
    """None when `expr` parses, else the error text."""
    try:
        parse(expr, tz)
        return None
    except ScheduleError as exc:
        return str(exc)


# --------------------------------------------------------------------------- jitter
def jitter(name: str, instant: float, window_s: float) -> int:
    """Deterministic delay in [0, window_s) for this job and this scheduled instant (RandomizedDelaySec equivalent)."""
    w = int(window_s)
    if w <= 0:
        return 0
    h = hashlib.sha256(f"{name}|{int(instant)}".encode()).digest()
    return int.from_bytes(h[:8], "big") % w


# --------------------------------------------------------------------------- windows
class Window:
    """A daily time-of-day window, optionally restricted to weekdays or to the n-th weekday of the month."""

    _RX = re.compile(r"^(?:(?P<days>.+?)\s+)?(?P<a>\d{1,2}:\d{2})\s*-\s*(?P<b>\d{1,2}:\d{2})$")

    def __init__(self, spec: str, tz=None):
        self.spec = spec
        self.tz = _as_tz(tz)
        m = self._RX.match(" ".join(spec.strip().split()))
        if not m:
            raise ScheduleError(f"bad window {spec!r} (want [days] HH:MM-HH:MM)")
        sa, sb = m["a"], m["b"]
        self.a = self._mins(sa, allow24=False)
        self.b = self._mins(sb, allow24=True)
        if self.a == self.b:
            raise ScheduleError(f"bad window {spec!r}: empty or 24 h")
        self.days: set[int] | None = None
        self.nth: tuple[int, int] | None = None
        days = (m["days"] or "").strip().lower()
        if days in ("", "daily", "*", "every day"):
            return
        if days == "weekdays":
            self.days = {1, 2, 3, 4, 5}
            return
        mm = re.fullmatch(r"(\w+) (\w+)", days)
        if mm and (mm[1] in _ORD or mm[1] in ("last", "l")):
            self.nth = (-1 if mm[1] in ("last", "l") else _ORD[mm[1]], _dow_num(mm[2]))
            return
        ds: set[int] = set()
        for part in days.split(","):
            if "-" in part:
                x, _, y = part.partition("-")
                a, b = _dow_num(x), _dow_num(y)
                ds.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
            else:
                ds.add(_dow_num(part))
        self.days = ds

    @staticmethod
    def _mins(s: str, allow24: bool) -> int:
        if allow24 and s.strip() == "24:00":
            return 24 * 60
        h, m = _hhmm(s)
        return h * 60 + m

    def _day_ok(self, d: date) -> bool:
        wd = (d.weekday() + 1) % 7
        if self.nth:
            n, dn = self.nth
            last = calendar.monthrange(d.year, d.month)[1]
            return wd == dn and ((n == -1 and d.day + 7 > last) or (n > 0 and (d.day - 1) // 7 + 1 == n))
        return self.days is None or wd in self.days

    def contains(self, t: float) -> bool:
        dt = datetime.fromtimestamp(t, self.tz)
        mins = dt.hour * 60 + dt.minute
        if self.a < self.b:
            return self._day_ok(dt.date()) and self.a <= mins < self.b
        return ((self._day_ok(dt.date()) and mins >= self.a)
                or (self._day_ok(dt.date() - timedelta(days=1)) and mins < self.b))

    def next_open(self, t: float) -> float | None:
        """First instant >= t inside the window (t itself when already inside); None if it never opens."""
        if self.contains(t):
            return t
        d = datetime.fromtimestamp(t, self.tz).date()
        for i in range(0, 400):
            day = d + timedelta(days=i)
            if self._day_ok(day):
                h, m = divmod(self.a, 60)
                ts = datetime(day.year, day.month, day.day, h, m).replace(tzinfo=self.tz).timestamp()
                if ts >= t:
                    return ts
        return None


@lru_cache(maxsize=128)
def _window(spec: str, tzkey) -> Window:
    return Window(spec, tzkey)


def in_window(spec: str, t: float, tz=None) -> bool:
    z = _as_tz(tz)
    key = getattr(z, "key", None)
    return (_window(spec, key) if key else Window(spec, z)).contains(t)


def parse_window(spec: str, tz=None) -> Window:
    z = _as_tz(tz)
    key = getattr(z, "key", None)
    return _window(spec, key) if key else Window(spec, z)
