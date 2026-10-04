"""JSON payloads behind the Homarr v2 custom widgets ops-thermals and ops-load (7-day hourly history).

Input is the metrics-ring export (`homelab_maint.metrics_ring.export()`, SPEC2 section 3); output is a flat, precomputed dict
for the Homarr v2 widget runtime, which can neither format dates or numbers nor call methods on missing data safely:

  * strings and colour words are built here ("42", "teal", "Thu 21h", "41 / 40 / 38");
  * `rows` is the chart data, one row per hour, oldest first, newest (the hour in progress) last: `{"x":"Thu 21h","c":47.1,...}`
    with short keys (c/g/r) to stay < 14 KB; a missing key is a gap in the line, never a zero;
  * `groups` holds everything per chart: tiles (current value, 1 h / 24 h / 7 d averages, 7 d max), series colour specs,
    y domain and ticks, axis unit, layout hint. Thermals has two groups (temperature, fans) because two scales never share an
    axis: temperature is a line chart over `rows`, the fan tiles each carry their own 168-point sparkline (`s`), one scale per
    fan (small multiples); load has one group. A chart group also carries `h` (chart height in logical px: the board canvas is
    a fixed-pixel grid, so a plain number, never a viewport-relative CSS length) and `yw` (y axis label width in px, wide
    enough for the widest tick label incl. its unit, so "100%" is not clipped);
  * the ring marker: `lx`/`lt` (the x label of the hour that the next hour overwrites) and `loop` (text), plus `ring` ("143/168 h").

Every key is present even with no data (empty lists, "" strings); trouble adds `error` and `stale: true`.
`build(route)` never raises, which is what server.py needs (Homarr shows a red triangle on non-200).
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from .core import STATE_DIR, read_json

SLOTS = 168
HOUR = 3600
STALE_S = 5 * 60                 # newest sample older than this => "sampler stopped" (same rule as metrics_ring.export)
MAX_BYTES = 14_000               # spec: each payload < 14 KB
NO_METRICS = "no metrics yet"    # in-band `error` while the ring holds nothing at all
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # fixed table: strftime("%a") follows the locale

# Series colours, categorical slots 1-3 of the validated dataviz palette (light, dark): the SAME entity keeps the SAME hue in
# both widgets (CPU blue, GPU orange, RAM / chassis aqua). Homarr is dark-first, `light-dark()` follows the colour scheme.
BLUE, ORANGE, AQUA = ("light-dark(#2a78d6,#3987e5)", "light-dark(#eb6834,#d95926)", "light-dark(#1baf7a,#199e70)")

# Chart geometry in logical px (Homarr v2 boards are a fixed 212 px track grid, CSS-zoomed as a whole: widgets/CONVENTIONS.md).
# Heights keep the tallest content (hot fixture: tiles + chart + footer) inside a 3x3 footprint (616 x 616 usable, measured 463 / 437 px). Axis widths fit the widest tick label
# at the template's 10 px font: "100%" needs more than "100 deg" because "%" is the wider glyph (the old 32 px clipped both).
CHART_H = {"thermal": 260, "load": 300}
Y_AXIS_W = {"thermal": 40, "load": 44}     # measured minimum without clipping: 34 / 38 px; +6 px for a wider host font

# Warm / hot / critical thresholds in degrees C. CPU and GPU follow the existing Thermals widget ladders; RAM (DDR5 SPD hub,
# its own alarm is 55 C high / 85 C crit) gets one of its own.
LADDER = {"cpu": (82, 92, 99), "gpu": (70, 78, 83), "ram": (55, 70, 85)}
LEVEL_WORDS = ("", "warm", "hot", "critical")
COLORS = ("teal", "yellow", "orange", "red")


# --------------------------------------------------------------------------- small helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _age_txt(sec: float | None) -> str:
    if sec is None:
        return "never"
    m = int(max(sec, 0) // 60)
    if m < 90:
        return f"{m}m"
    h = m // 60
    return f"{h}h" if h < 48 else f"{h // 24}d"


def _span_txt(sec: float) -> str:
    """"45m", "31h", "6d 23h": how long until something happens."""
    m = int(max(sec, 0) // 60)
    if m < 90:
        return f"{m}m"
    h = m // 60
    return f"{h}h" if h < 48 else f"{h // 24}d {h % 24}h"


def _fmt(v: float | None, kind: str) -> str:
    """Display text for one value: "--" when missing. kind: t (temp, C), r (rpm), p (percent), w (watt), l (load)."""
    if v is None:
        return "--"
    if kind == "p" and 0.05 <= abs(v) < 10:            # one decimal where it matters (3.5 %), a plain 0 for idle
        return f"{v:.1f}"
    if kind == "l":
        return f"{v:.1f}"
    return f"{v:.0f}"


def _tidy(v: float | None, digits: int) -> int | float | None:
    """Chart value: rounded, integral floats become ints (62.0 -> 62) because every byte counts x168 rows."""
    if v is None:
        return None
    r = round(v, digits)
    return int(r) if r == int(r) else r


def _clock(t: float) -> str:
    lt = time.localtime(t)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}"


def labels(ts: list[float]) -> list[str]:
    """Short x labels ("Thu 21h") from hour epochs in the HOST time zone, made unique: 168 consecutive hours have unique
    weekday+hour pairs except across a DST fall-back, where the repeated hour gets a "b" suffix (a categorical chart axis
    would otherwise merge the two points)."""
    out, seen = [], set()
    for t in ts:
        lt = time.localtime(t)
        lab = f"{DAYS[lt.tm_wday]} {lt.tm_hour}h"
        while lab in seen:
            lab += "b"
        seen.add(lab)
        out.append(lab)
    return out


def _level(kind: str, v: float | None) -> int:
    if v is None:
        return 0
    return sum(1 for lim in LADDER[kind] if v >= lim)


def _temp_axis(vals: list[float]) -> tuple[list[int], list[int]]:
    """y domain + ticks for a temperature chart: the data range padded by 2 C, a tick every 10/20/40 C (never more than 5), the
    domain ends ON ticks so the top grid line is a labelled one."""
    if not vals:
        return [20, 80], [20, 40, 60, 80]
    span = max(vals) - min(vals) + 4
    step = 10 if span <= 40 else 20 if span <= 80 else 40
    lo = max(0, math.floor((min(vals) - 2) / step) * step)
    hi = lo + max(math.ceil((max(vals) + 2 - lo) / step), 3) * step
    return [lo, hi], list(range(lo, hi + 1, step))


def _fan_axis(vals: list[float]) -> tuple[list[int], list[int]]:
    """y domain + ticks for rpm: 0 .. just above the peak, a tick every 250/500/1000/2000 rpm (<= ~4 ticks)."""
    if not vals:
        return [0, 2000], [0, 1000, 2000]
    top = max(vals)
    step = 250 if top <= 800 else 500 if top <= 1500 else 1000 if top <= 3000 else 2000
    hi = max(math.ceil(top * 1.05 / step) * step, step * 2)
    return [0, hi], list(range(0, hi + 1, step))


# --------------------------------------------------------------------------- export access
def _hist(exp: dict, metric: str, n: int) -> list[float | None]:
    """The last `n` hourly means of a metric, padded with None on the left when the export is short."""
    col = (exp.get("series") or {}).get(metric)
    col = [_num(v) for v in col] if isinstance(col, list) else []
    col = col[-n:]
    return [None] * (n - len(col)) + col


def _scalar(exp: dict, block: str, metric: str) -> float | None:
    b = exp.get(block)
    return _num(b.get(metric)) if isinstance(b, dict) else None


def _hours(exp: dict) -> list[float]:
    t = (exp.get("series") or {}).get("t")
    ts = [_num(x) for x in t] if isinstance(t, list) else []
    # a hole in the time axis would misalign every column against its labels: treat that ring as unreadable
    return [x for x in ts if x is not None][-SLOTS:] if None not in ts else []


def _spark(col: list[float | None]) -> list[int | None]:
    """Sparkline points 0..100, scaled to the series' own 7-day min..max (the tile text carries the absolute numbers). Mantine's
    Sparkline always starts its axis at 0, which would flatten a fan that idles at 900 rpm into a hairline at the top. The span is
    never less than 25 % of the peak, so a steady fan's sensor noise is not blown up into a mountain range."""
    vals = [v for v in col if v is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 0.25 * hi, 1.0)
    return [None if v is None else int(round((v - lo) / span * 100)) for v in col]


def _tile(exp: dict, metric: str, label: str, kind: str, color: str, *, lvl_kind: str | None = None, unit: str = "",
          spark: bool = False) -> tuple[dict, float | None]:
    cur = _scalar(exp, "current", metric)
    if cur is None:                                   # sensor momentarily missing: show the hour so far instead of "--"
        cur = _scalar(exp, "hour_avg", metric)
    avgs = [_scalar(exp, "hour_avg", metric), _scalar(exp, "avg_24h", metric), _scalar(exp, "avg_7d", metric)]
    mx = _scalar(exp, "max_7d", metric)
    lvl = _level(lvl_kind, cur) if lvl_kind else 0
    tile = {"l": label, "v": _fmt(cur, kind), "u": unit if cur is not None else "", "c": COLORS[lvl] if cur is not None else "gray",
            "b": LEVEL_WORDS[lvl],
            "k": color, "a": " / ".join(_fmt(a, kind) for a in avgs),
            "x": ("max " + _fmt(mx, kind)) if mx is not None and not spark else ""}
    if spark:                                          # per-tile history for metrics that do not share a chart axis
        tile["s"] = _spark(_hist(exp, metric, len(_hours(exp))))
    return tile, cur


def _marker(exp: dict, labs: list[str], rows_with_data: list[int], now: float) -> dict:
    """Where the ring loops: the oldest hour (left edge) is the slot the next hour overwrites. While the oldest hour is still
    empty (first week, or the sampler was down) nothing is overwritten yet, so the marker sits on the first hour with data and
    the text says when overwriting starts. `full` is the export's own verdict that every slot holds an hour."""
    loop = exp.get("loop") if isinstance(exp.get("loop"), dict) else {}
    ts = _hours(exp)
    n = len(rows_with_data)
    nxt = _num(loop.get("overwrites_next_at")) or (ts[-1] + HOUR if ts else None)
    complete = loop.get("complete") if isinstance(loop.get("complete"), bool) else n >= SLOTS
    out = {"lx": "", "lt": "", "loop": "", "ring": f"{n}/{SLOTS} h", "rp": int(round(100 * min(n, SLOTS) / SLOTS)), "full": complete}
    if not labs or not n:
        return out
    first = rows_with_data[0]
    if first == 0 and nxt is not None:
        out["lx"], out["lt"] = labs[0], "overwritten next"
        mins = max(0, math.ceil((nxt - now) / 60))
        when = f"in {mins}m" if now < nxt else "due"
        out["loop"] = f"overwrites next at {_clock(nxt)} ({when}): drops {labs[0]}"
    else:
        out["lx"], out["lt"] = labs[first], "ring start"
        # the first overwrite happens when the hour in progress reaches (first hour with data) + 168 h
        left = ts[first] + SLOTS * HOUR - now
        out["loop"] = f"filling: first overwrite in {_span_txt(left)}"
    return out


# --------------------------------------------------------------------------- envelope
def _base(exp: Any, now: float | None) -> tuple[dict, dict, float]:
    now = time.time() if now is None else float(now)
    exp = exp if isinstance(exp, dict) else {}
    cur = exp.get("current") if isinstance(exp.get("current"), dict) else {}
    sampled = _num(cur.get("sampled_at"))
    age = None if sampled is None else max(0.0, now - sampled)
    stale = bool(exp.get("stale")) or age is None or age > STALE_S
    p = {"stale": stale, "age_min": None if age is None else int(age // 60), "ago": _age_txt(age), "c": "gray",
         "head": "no data", "rows": [], "xt": [], "groups": [], "lx": "", "lt": "", "loop": "", "ring": "0/168 h", "rp": 0,
         "full": False, "extra": ""}
    return p, exp, now


def error_payload(msg: str) -> dict:
    p, _, _ = _base(None, 0)
    p["error"] = str(msg)[:80]
    return p


def _rows_and_ticks(exp: dict, keys: dict[str, str]) -> tuple[list[dict], list[str], list[str], list[int]]:
    """Chart rows ({x, <key>: value}), midnight tick labels, all labels, and indexes of rows that carry any value.
    keys: row key -> "metric:digits"."""
    ts = _hours(exp)
    labs = labels(ts)
    cols = {k: _hist(exp, spec.split(":")[0], len(ts)) for k, spec in keys.items()}
    digits = {k: int(spec.split(":")[1]) for k, spec in keys.items()}
    rows, ticks, have = [], [], []
    for i, lab in enumerate(labs):
        row: dict[str, Any] = {"x": lab}
        for k, col in cols.items():
            v = _tidy(col[i], digits[k])
            if v is not None:
                row[k] = v
        if len(row) > 1:
            have.append(i)
        rows.append(row)
        lt = time.localtime(ts[i])
        if lt.tm_hour == 0 and lt.tm_min == 0 and i > 0:
            ticks.append(lab)
    return rows, ticks, labs, have


def _finish(p: dict) -> dict:
    """Common tail of thermal() / load(): explain an empty ring, then enforce the size budget.

    "Nothing was ever sampled" (no sample time and no hour with data) is what a real ring export looks like on a first install
    or after the ring file went missing / corrupt (metrics_ring.export() then returns a fresh, all-None ring, not an error), so
    the in-band `error` has to come from the payload, not only from build()'s "no export at all". A running sampler on a host
    without sensors is different (it has a sample time): that one shows plain dashes and no error."""
    if p["age_min"] is None and not p["rp"]:
        p["error"] = NO_METRICS
    return _fit(p)


def _fit(p: dict) -> dict:
    """Last line of defence for the < 14 KB budget (fixtures come in around 12 KB): thin the rows to every 2nd hour."""
    if len(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace")) > MAX_BYTES:
        thin = lambda xs: xs[::-1][::2][::-1]  # noqa: E731 - keeps the newest point
        p["rows"] = thin(p["rows"])
        for g in p["groups"]:
            for t in g["tiles"]:
                if t.get("s"):
                    t["s"] = thin(t["s"])
        p["cut"] = True
    return p


# --------------------------------------------------------------------------- thermals
def thermal(export: dict | None, now: float | None = None) -> dict:
    p, e, now = _base(export, now)
    rows, xt, labs, have = _rows_and_ticks(e, {"c": "cpu_temp:1", "g": "gpu_temp:1", "r": "ram_temp:1"})

    t_tiles, t_cur = [], []
    for metric, lab, color, lk in (("cpu_temp", "CPU", BLUE, "cpu"), ("gpu_temp", "GPU", ORANGE, "gpu"),
                                   ("ram_temp", "RAM", AQUA, "ram")):
        tile, cur = _tile(e, metric, lab, "t", color, lvl_kind=lk, unit="\u00b0C")
        t_tiles.append(tile)
        t_cur.append((lk, cur, tile))
    f_tiles = []
    for metric, lab, color in (("cpu_fan_rpm", "CPU fan", BLUE), ("case_fan_rpm", "Case fan", AQUA), ("gpu_fan_rpm", "GPU fan", ORANGE)):
        tile, cur = _tile(e, metric, lab, "r", color, spark=True)   # unit rpm is in the group title: more room for the sparkline
        if metric == "gpu_fan_rpm" and cur is None and _scalar(e, "current", "gpu_fan_pct") is not None:
            # no rpm sensor on this card/driver: the percent is the only fan reading, show that on the tile
            tile, cur = _tile(e, "gpu_fan_pct", lab, "r", color, unit="%", spark=True)
        if metric == "cpu_fan_rpm" and cur == 0:       # a stopped CPU fan is the one fan fault worth a colour
            tile.update(c="orange", b="stop")
        f_tiles.append(tile)

    tdom, tticks = _temp_axis([v for k in ("c", "g", "r") for v in (r.get(k) for r in rows) if v is not None])
    p["rows"], p["xt"] = rows, xt
    p["groups"] = [
        {"n": "Temperature, \u00b0C", "u": "\u00b0C", "uy": "\u00b0", "tiles": t_tiles, "dom": tdom, "yt": tticks, "xa": True,
         "h": CHART_H["thermal"], "yw": Y_AXIS_W["thermal"],
         "series": [{"name": "c", "label": "CPU", "color": BLUE}, {"name": "g", "label": "GPU", "color": ORANGE},
                    {"name": "r", "label": "RAM", "color": AQUA}]},
        {"n": "Fans, rpm", "u": "rpm", "uy": "", "tiles": f_tiles, "series": []},
    ]
    p.update(_marker(e, labs, have, now))
    cur_any = any(c is not None for _lk, c, _t in t_cur) or any(t["v"] != "--" for t in f_tiles)
    worst = max(((_level(lk, c), lk, t["l"]) for lk, c, t in t_cur if c is not None), default=(0, "", ""))
    if cur_any or have:
        p["head"] = "all cool" if worst[0] == 0 else f"{worst[2]} {LEVEL_WORDS[worst[0]]}"
        p["c"] = "gray" if p["stale"] else COLORS[worst[0]]
    return _finish(p)


# --------------------------------------------------------------------------- load
def load(export: dict | None, now: float | None = None) -> dict:
    p, e, now = _base(export, now)
    keys = {"c": "cpu_pct:1", "g": "gpu_pct:1", "r": "ram_pct:1"}
    rows, xt, labs, have = _rows_and_ticks(e, keys)
    tiles, cur_vals = [], {}
    for metric, lab, color in (("cpu_pct", "CPU", BLUE), ("gpu_pct", "GPU", ORANGE), ("ram_pct", "RAM", AQUA)):
        tile, cur = _tile(e, metric, lab, "p", color, unit="%")
        tiles.append(tile)
        cur_vals[metric] = cur
    p["rows"], p["xt"] = rows, xt
    p["groups"] = [{"n": "Utilisation, %", "u": "%", "uy": "%", "tiles": tiles, "dom": [0, 100], "yt": [0, 50, 100], "xa": True,
                    "h": CHART_H["load"], "yw": Y_AXIS_W["load"],
                    "series": [{"name": "c", "label": "CPU", "color": BLUE}, {"name": "g", "label": "GPU", "color": ORANGE},
                               {"name": "r", "label": "RAM", "color": AQUA}]}]
    p.update(_marker(e, labs, have, now))
    if any(v is not None for v in cur_vals.values()) or have:
        top = max((v for v in (cur_vals["cpu_pct"], cur_vals["gpu_pct"]) if v is not None), default=0)
        ram = cur_vals["ram_pct"] or 0
        p["head"] = "idle" if top < 15 else "active" if top < 60 else "busy"
        # RAM pressure is the only thing here that is ever a problem; busy CPU/GPU is just work being done
        p["c"] = "gray" if p["stale"] else "orange" if ram >= 97 else "yellow" if ram >= 92 else "teal"
    gm, pw, l1 = (_scalar(e, "current", "gpu_mem_pct"), _scalar(e, "current", "gpu_power_w"), _scalar(e, "current", "load1"))
    p["extra"] = " / ".join(s for s in (f"VRAM {gm:.0f}%" if gm is not None else "", f"GPU {pw:.0f} W" if pw is not None else "",
                                         f"load {l1:.1f}" if l1 is not None else "") if s)
    return _finish(p)


# --------------------------------------------------------------------------- server handlers
ROUTES = {"thermal": thermal, "load": load}


def load_export(now: float | None = None) -> dict | None:
    """The ring export: computed live by metrics_ring when available, else the published STATE_DIR/public/metrics.json."""
    try:
        from . import metrics_ring
        e = metrics_ring.export(now)
        if isinstance(e, dict):
            return e
    except Exception:  # noqa: BLE001 - missing module, unreadable ring: fall through to the published file
        pass
    e = read_json(STATE_DIR / "public" / "metrics.json")
    return e if isinstance(e, dict) else None


def build(route: str, now: float | None = None, export: dict | None = None) -> dict:
    """Payload for /thermal or /load. Never raises: trouble becomes an in-band `error` + `stale: true`."""
    try:
        exp = export if export is not None else load_export(now)
        p = ROUTES[route](exp, now)
        if exp is None:
            p["error"] = NO_METRICS
        return p
    except Exception as exc:  # noqa: BLE001 - a widget must always get a 200
        return error_payload(f"payload failure: {type(exc).__name__}")


def main(argv: list[str] | None = None) -> int:
    """Debug helper: python3 -m homelab_maint.payloads_metrics thermal|load [EXPORT_JSON] [NOW]"""
    import sys
    a = argv if argv is not None else sys.argv[1:]
    if not a or a[0] not in ROUTES:
        print("usage: payloads_metrics ROUTE [EXPORT_JSON] [NOW]  ROUTE in " + "|".join(ROUTES), file=sys.stderr)
        return 2
    from pathlib import Path
    exp = read_json(Path(a[1])) if len(a) > 1 else None
    print(json.dumps(build(a[0], float(a[2]) if len(a) > 2 else None, exp), separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
