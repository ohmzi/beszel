"""Tests for payloads_metrics.py (the /thermal and /load payloads the ops-thermals / ops-load v2 widgets read). The widget definitions
and their real-runtime render checks are tested in test_widgets_v2.py."""
import conftest  # noqa: F401  (points homelab_maint at throw-away dirs before it is imported)

import importlib.util
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = ROOT / "widgets"
FIX = WIDGETS / "fixtures-metrics"
sys.path.insert(0, str(WIDGETS))

import build_v2 as bv  # noqa: E402
from homelab_maint import payloads_metrics as pm  # noqa: E402

NOW = bv.RING_NOW                  # Thu 2026-10-01 21:55:00 America/Toronto, five minutes before the hour turns
CASES = ["full_ring", "partial_ring", "stale", "all_none", "no_gpu", "hot"]
ROUTES = ["thermal", "load"]
COLORS = {"teal", "yellow", "orange", "red", "gray"}


@pytest.fixture(autouse=True)
def toronto():
    """x labels are rendered in the host time zone: pin it, restore it afterwards."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def size(p) -> int:
    return len(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def tiles(p) -> dict:
    return {t["l"]: t for g in p["groups"] for t in g["tiles"]}


@pytest.fixture(params=CASES)
def case(request):
    return request.param


# =========================================================================== shape, size, JSON hygiene
def test_every_fixture_and_route_is_complete_small_and_json_clean(case):
    for route in ROUTES:
        p = pm.build(route, NOW, fx(case))
        assert size(p) < 14_000, (case, route, size(p))
        assert {"stale", "age_min", "ago", "c", "head", "rows", "xt", "groups", "lx", "lt", "loop", "ring", "rp", "full",
                "extra"} <= set(p)
        assert p["c"] in COLORS
        json.loads(json.dumps(p, allow_nan=False))                  # strict JSON: no NaN / Infinity anywhere
        for g in p["groups"]:
            assert len(g["tiles"]) == 3
            for t in g["tiles"]:
                assert t["c"] in COLORS and t["k"].startswith("light-dark(")
                assert re.fullmatch(r"(-?[\d.]+|--) / (-?[\d.]+|--) / (-?[\d.]+|--)", t["a"]), t["a"]


def test_server_encoding_stays_under_budget_in_utf8(case):
    """server.py sends json.dumps(..., ensure_ascii=False).encode(): the degree signs are 2 bytes each."""
    for route in ROUTES:
        assert size(pm.build(route, NOW, fx(case))) < 14_000


def test_full_ring_sizes_leave_headroom():
    sizes = {r: size(pm.build(r, NOW, fx("full_ring"))) for r in ROUTES}
    assert sizes["thermal"] < 12_000 and sizes["load"] < 12_000, sizes


def test_worst_case_values_still_fit_and_fit_keeps_the_newest_hour():
    e = fx("full_ring")
    for m in pm_metrics(e):
        e["series"][m] = [-123.456 - i * 0.37 for i in range(168)]       # every point 3 digits + decimals, no gaps
    for route in ROUTES:
        p = pm.build(route, NOW, e)
        assert size(p) < 14_000
        assert p["rows"][-1]["x"] == "Thu 21h"


def pm_metrics(e):
    return [m for m in e["series"] if m != "t"]


# =========================================================================== chart rows
def test_rows_are_168_hours_oldest_first_ending_at_the_hour_in_progress():
    p = pm.thermal(fx("full_ring"), NOW)
    xs = [r["x"] for r in p["rows"]]
    assert len(xs) == 168 and len(set(xs)) == 168
    assert xs[-1] == "Thu 21h"                      # 21:55 EDT: the hour in progress is the right edge
    assert xs[0] == "Thu 22h"                       # 167 h earlier, i.e. the same weekday a week ago
    assert xs[-2] == "Thu 20h" and xs[-24] == "Wed 22h"
    assert p["xt"] == ["Fri 0h", "Sat 0h", "Sun 0h", "Mon 0h", "Tue 0h", "Wed 0h", "Thu 0h"]
    assert set(p["xt"]) <= set(xs)                  # ticks must be real categories or recharts drops them


def test_rows_values_match_the_export_and_use_short_keys():
    e = fx("full_ring")
    p = pm.thermal(e, NOW)
    for i in (0, 57, 167):
        r = p["rows"][i]
        assert set(r) <= {"x", "c", "g", "r"}
        assert r["c"] == pytest.approx(e["series"]["cpu_temp"][i], abs=0.05)
        assert r["g"] == pytest.approx(e["series"]["gpu_temp"][i], abs=0.05)
    q = pm.load(e, NOW)
    assert q["rows"][10]["c"] == pytest.approx(e["series"]["cpu_pct"][10], abs=0.05)
    assert q["rows"][10]["r"] == pytest.approx(e["series"]["ram_pct"][10], abs=0.05)


def test_missing_values_are_gaps_never_zeros():
    e = fx("full_ring")
    e["series"]["gpu_temp"][100] = None
    e["series"]["cpu_temp"][101] = None
    p = pm.thermal(e, NOW)
    assert "g" not in p["rows"][100] and "c" in p["rows"][100]
    assert "c" not in p["rows"][101]
    # a real 0 (idle GPU load) stays a 0
    e["series"]["gpu_pct"][5] = 0.0
    assert pm.load(e, NOW)["rows"][5]["g"] == 0


def test_integral_values_are_emitted_as_ints_to_save_bytes():
    e = fx("full_ring")
    e["series"]["cpu_temp"][3] = 62.0
    assert pm.thermal(e, NOW)["rows"][3]["c"] == 62 and isinstance(pm.thermal(e, NOW)["rows"][3]["c"], int)


def test_labels_follow_the_host_time_zone():
    e = fx("full_ring")
    toronto = pm.thermal(e, NOW)["rows"][-1]["x"]
    os.environ["TZ"] = "UTC"
    time.tzset()
    assert toronto == "Thu 21h" and pm.thermal(e, NOW)["rows"][-1]["x"] == "Fri 1h"
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    assert pm.thermal(e, NOW)["rows"][-1]["x"] == "Fri 10h"


def test_labels_do_not_depend_on_the_locale_or_strftime():
    ts = [1790906100.0 - i * 3600 for i in range(5)]
    assert pm.labels(ts[::-1])[-1] == "Thu 21h"


def test_dst_fall_back_keeps_168_unique_labels():
    """2026-11-01 01:00-02:00 happens twice in Toronto; a categorical axis would merge the two points."""
    end = time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))
    ts = [float(int(end // 3600) * 3600 - (167 - i) * 3600) for i in range(168)]
    labs = pm.labels(ts)
    assert len(set(labs)) == 168
    assert sum(1 for x in labs if x.startswith("Sun 1h")) == 2 and "Sun 1hb" in labs
    e = fx("full_ring")
    e["series"]["t"] = ts
    p = pm.thermal(e, ts[-1] + 600)
    assert len({r["x"] for r in p["rows"]}) == 168 and set(p["xt"]) <= {r["x"] for r in p["rows"]}


def test_dst_spring_forward_is_unique_too():
    end = time.mktime((2027, 3, 14, 12, 0, 0, 0, 0, -1))
    ts = [float(int(end // 3600) * 3600 - (167 - i) * 3600) for i in range(168)]
    assert len(set(pm.labels(ts))) == 168


# =========================================================================== tiles
def test_thermal_tiles_current_averages_and_max():
    e = fx("full_ring")
    t = tiles(pm.thermal(e, NOW))
    assert list(t) == ["CPU", "GPU", "RAM", "CPU fan", "Case fan", "GPU fan"]
    cpu = t["CPU"]
    assert cpu["v"] == f"{e['current']['cpu_temp']:.0f}" and cpu["u"] == "°C"
    assert cpu["a"] == " / ".join(f"{e[b]['cpu_temp'] if b != 'hour_avg' else e['hour_avg']['cpu_temp']:.0f}"
                                   for b in ("hour_avg", "avg_24h", "avg_7d"))
    assert cpu["x"] == f"max {e['max_7d']['cpu_temp']:.0f}"
    assert t["CPU fan"]["u"] == "" and t["CPU fan"]["x"] == ""          # rpm is in the group title, the sparkline needs the room


def test_load_tiles_percent_formatting():
    t = tiles(pm.load(fx("full_ring"), NOW))
    assert list(t) == ["CPU", "GPU", "RAM"] and all(x["u"] == "%" for x in t.values())
    e = fx("full_ring")
    e["current"]["gpu_pct"], e["current"]["cpu_pct"] = 0.0, 3.54
    t = tiles(pm.load(e, NOW))
    assert t["GPU"]["v"] == "0" and t["CPU"]["v"] == "3.5"


def test_current_falls_back_to_the_hour_average_when_the_sensor_blips():
    e = fx("full_ring")
    e["current"]["cpu_temp"] = None
    assert tiles(pm.thermal(e, NOW))["CPU"]["v"] == f"{e['hour_avg']['cpu_temp']:.0f}"


def test_status_ladder_hot_fixture():
    p = pm.thermal(fx("hot"), NOW)
    t = tiles(p)
    assert (t["CPU"]["c"], t["CPU"]["b"]) == ("orange", "hot")           # 94 C: >= 92
    assert (t["GPU"]["c"], t["GPU"]["b"]) == ("orange", "hot")           # 80 C: >= 78
    assert (t["RAM"]["c"], t["RAM"]["b"]) == ("yellow", "warm")          # 58 C: >= 55
    assert (t["CPU fan"]["c"], t["CPU fan"]["b"]) == ("orange", "stop")  # a stopped CPU fan is flagged
    assert p["c"] == "orange" and p["head"].endswith("hot") and not p["stale"]


@pytest.mark.parametrize("kind,value,expect", [
    ("cpu_temp", 81.9, "teal"), ("cpu_temp", 82, "yellow"), ("cpu_temp", 92, "orange"), ("cpu_temp", 99, "red"),
    ("gpu_temp", 69.9, "teal"), ("gpu_temp", 70, "yellow"), ("gpu_temp", 78, "orange"), ("gpu_temp", 83, "red"),
    ("ram_temp", 54.9, "teal"), ("ram_temp", 55, "yellow"), ("ram_temp", 70, "orange"), ("ram_temp", 85, "red")])
def test_ladder_boundaries(kind, value, expect):
    e = fx("full_ring")
    e["current"][kind] = value
    label = {"cpu_temp": "CPU", "gpu_temp": "GPU", "ram_temp": "RAM"}[kind]
    assert tiles(pm.thermal(e, NOW))[label]["c"] == expect


def test_gpu_fan_falls_back_to_percent_when_the_card_reports_no_rpm():
    e = fx("full_ring")
    for blk in ("current", "hour_avg", "avg_24h", "avg_7d", "max_7d"):
        e[blk]["gpu_fan_rpm"] = None
    e["series"]["gpu_fan_rpm"] = [None] * 168
    e["current"]["gpu_fan_pct"] = 34.0
    t = tiles(pm.thermal(e, NOW))["GPU fan"]
    assert t["v"] == "34" and t["u"] == "%"


def test_fan_sparklines_are_scaled_per_fan_and_keep_gaps():
    e = fx("full_ring")
    e["series"]["cpu_fan_rpm"][20] = None
    s = tiles(pm.thermal(e, NOW))["CPU fan"]["s"]
    assert len(s) == 168 and s[20] is None
    vals = [v for v in s if v is not None]
    assert min(vals) == 0 and 0 <= max(vals) <= 100 and max(vals) >= 90
    # a perfectly steady fan is a flat line, not noise blown up to full height
    e["series"]["case_fan_rpm"] = [600.0] * 168
    assert set(tiles(pm.thermal(e, NOW))["Case fan"]["s"]) == {0}
    # a fan that never ran is a flat line at 0, not a crash
    e["series"]["gpu_fan_rpm"] = [0.0] * 168
    assert set(tiles(pm.thermal(e, NOW))["GPU fan"]["s"]) == {0}


def test_spark_helper():
    assert pm._spark([None, None]) == []
    assert pm._spark([10, 20, None, 30]) == [0, 50, None, 100]            # span 20 > 25 % of 30
    assert pm._spark([100, 101]) == [0, 4]                                # span floor: 25 % of the peak


# =========================================================================== axes
@pytest.mark.parametrize("vals", [[31.7, 79.0], [40, 41], [55, 60], [20, 99.9], [0.5, 1.0], [95, 101]])
def test_temp_axis_domain_ends_on_ticks_and_has_few_ticks(vals):
    dom, ticks = pm._temp_axis(vals)
    assert dom[0] == ticks[0] and dom[1] == ticks[-1]
    assert dom[0] <= min(vals) - 0 and dom[1] >= max(vals)
    assert 3 <= len(ticks) <= 6 and dom[1] - dom[0] >= 30


def test_fan_axis_starts_at_zero_and_covers_the_peak():
    for top in (0, 300, 900, 1188, 2290, 3037, 9000):
        dom, ticks = pm._fan_axis([top] if top else [])
        assert dom[0] == 0 and dom[1] >= top and ticks[0] == 0 and ticks[-1] == dom[1] and len(ticks) <= 6


def test_load_axis_is_fixed_0_100():
    g = pm.load(fx("full_ring"), NOW)["groups"][0]
    assert g["dom"] == [0, 100] and g["yt"] == [0, 50, 100]


def test_series_specs_match_row_keys_and_use_stable_colours():
    p = pm.thermal(fx("full_ring"), NOW)
    g = p["groups"][0]
    assert [s["name"] for s in g["series"]] == ["c", "g", "r"]
    q = pm.load(fx("full_ring"), NOW)["groups"][0]
    # the same entity keeps the same hue in both widgets
    assert [s["color"] for s in g["series"]] == [s["color"] for s in q["series"]]
    assert p["groups"][1]["series"] == [] and not {"h", "yw"} & set(p["groups"][1])       # fans: sparkline tiles, no chart


# =========================================================================== ring marker
def test_full_ring_marker_names_the_hour_that_is_overwritten_next():
    p = pm.thermal(fx("full_ring"), NOW)
    assert p["lx"] == p["rows"][0]["x"] == "Thu 22h" and p["lt"] == "overwritten next"
    assert p["loop"] == "overwrites next at 22:00 (in 5m): drops Thu 22h"
    assert p["ring"] == "168/168 h" and p["rp"] == 100 and p["full"] is True


def test_marker_countdown_and_due():
    e = fx("full_ring")
    assert "(in 1m)" in pm.thermal(e, NOW + 240)["loop"]
    assert "(due)" in pm.thermal(e, NOW + 700)["loop"]                       # sampler lagging past the hour


def test_partial_ring_marker_sits_on_the_first_hour_with_data():
    p = pm.thermal(fx("partial_ring"), NOW)
    assert p["ring"] == "20/168 h" and p["rp"] == 12 and p["full"] is False
    assert p["lt"] == "ring start" and p["lx"] == p["rows"][-20]["x"] == "Thu 2h"
    assert "x" in p["rows"][0] and set(p["rows"][0]) == {"x"}                 # empty hours stay as empty categories
    assert p["loop"] == "filling: first overwrite in 6d 4h"                   # Thu 2h + 168 h - now (Thu 21:55)


def test_ring_with_a_hole_but_a_full_oldest_hour_still_says_overwrite():
    e = fx("full_ring")
    e["loop"]["complete"] = False
    for m in pm_metrics(e):
        e["series"][m][80] = None
    p = pm.thermal(e, NOW)
    assert p["ring"] == "167/168 h" and p["loop"].startswith("overwrites next at 22:00") and p["full"] is False


# =========================================================================== stale / no data
def test_stale_fixture():
    for route in ROUTES:
        p = pm.build(route, NOW, fx("stale"))
        assert p["stale"] is True and p["c"] == "gray" and p["ago"] == "47m" and p["age_min"] == 47


def test_staleness_is_recomputed_from_now_so_a_published_file_goes_stale_by_itself():
    e = fx("full_ring")
    assert e["stale"] is False
    sampled = e["current"]["sampled_at"]
    assert pm.thermal(e, sampled + 299)["stale"] is False
    p = pm.thermal(e, sampled + 301)
    assert p["stale"] is True and p["c"] == "gray"
    assert pm.thermal(e, sampled - 30)["age_min"] == 0                        # clock a bit behind the sampler: not negative


def test_export_stale_flag_wins_even_with_a_fresh_looking_timestamp():
    e = fx("full_ring")
    e["stale"] = True
    assert pm.load(e, NOW)["stale"] is True


def test_all_none_has_every_key_and_renders_dashes():
    for route in ROUTES:
        p = pm.build(route, NOW, fx("all_none"))
        assert p["head"] == "no data" and p["c"] == "gray" and not p["stale"]
        assert p["ring"] == "0/168 h" and p["loop"] == "" and p["lx"] == ""
        assert len(p["rows"]) == 168 and all(set(r) == {"x"} for r in p["rows"])
        for t in tiles(p).values():
            assert t["v"] == "--" and t["a"] == "-- / -- / --" and t["c"] == "gray" and t["u"] == ""
            assert not t.get("s")
        assert size(p) < 5_000


def test_no_gpu_fixture_keeps_everything_else():
    p = pm.thermal(fx("no_gpu"), NOW)
    t = tiles(p)
    assert t["GPU"]["v"] == "--" and t["GPU fan"]["v"] == "--" and t["CPU"]["v"] != "--"
    assert all("g" not in r for r in p["rows"]) and all("c" in r for r in p["rows"])
    assert p["head"] == "all cool"
    q = pm.load(fx("no_gpu"), NOW)
    assert tiles(q)["GPU"]["v"] == "--" and "GPU" not in q["extra"] and "VRAM" not in q["extra"]


def test_load_head_and_ram_pressure_colour():
    e = fx("full_ring")
    assert pm.load(e, NOW)["head"] == "idle"
    e["current"].update(cpu_pct=40.0, gpu_pct=2.0)
    assert pm.load(e, NOW)["head"] == "active"
    e["current"].update(cpu_pct=75.0)
    assert pm.load(e, NOW)["head"] == "busy" and pm.load(e, NOW)["c"] == "teal"       # busy is work, not a problem
    e["current"].update(ram_pct=93.0)
    assert pm.load(e, NOW)["c"] == "yellow"
    e["current"].update(ram_pct=98.0)
    assert pm.load(e, NOW)["c"] == "orange"


def test_load_extra_line():
    e = fx("full_ring")
    e["current"].update(gpu_mem_pct=38.4, gpu_power_w=127.85, load1=32.16)
    assert pm.load(e, NOW)["extra"] == "VRAM 38% / GPU 128 W / load 32.2"


# =========================================================================== never raises
GARBAGE = [None, [], "x", 42, {}, {"series": "x"}, {"series": {"t": "x"}}, {"series": {"t": [None] * 168}},
           {"series": {"t": [1, 2, 3], "cpu_temp": "abc"}, "current": []}, {"current": {"sampled_at": "soon"}},
           {"loop": [], "series": []}, {"series": {"t": [float("nan")] * 3}}, {"avg_7d": {"cpu_temp": float("inf")}}]


@pytest.mark.parametrize("junk", GARBAGE)
def test_garbage_exports_never_raise_and_stay_strict_json(junk):
    for route in ROUTES:
        p = pm.build(route, NOW, junk) if junk is not None else pm.ROUTES[route](None, NOW)
        json.dumps(p, allow_nan=False)
        assert p["stale"] is True and {"groups", "rows", "head"} <= set(p)


def test_bool_and_string_values_are_not_numbers():
    e = fx("full_ring")
    e["current"]["cpu_temp"] = True
    e["avg_7d"]["cpu_temp"] = "hot"
    t = tiles(pm.thermal(e, NOW))["CPU"]
    assert t["v"] == f"{e['hour_avg']['cpu_temp']:.0f}"                      # bool ignored -> falls back to the hour avg
    assert t["a"].endswith("-- ")  or t["a"].split(" / ")[2] == "--"


def test_build_turns_an_internal_failure_into_an_in_band_error(monkeypatch):
    monkeypatch.setitem(pm.ROUTES, "thermal", lambda e, n: 1 / 0)
    p = pm.build("thermal", NOW, fx("full_ring"))
    assert p["error"] == "payload failure: ZeroDivisionError" and p["stale"] is True and p["groups"] == []


def block_ring_import(monkeypatch):
    """Make `from . import metrics_ring` fail whatever ran before. Once any test (or the sampler tests) has imported the submodule
    the package carries it as an attribute, and `from . import x` returns that attribute without ever looking at sys.modules, so
    blocking sys.modules alone only works when this file happens to run first."""
    import homelab_maint
    monkeypatch.setitem(sys.modules, "homelab_maint.metrics_ring", None)
    monkeypatch.delattr(homelab_maint, "metrics_ring", raising=False)


def test_build_without_any_ring_reports_no_metrics_yet(tmp_path, monkeypatch):
    importlib.import_module("homelab_maint.metrics_ring")       # regression: the state a full-suite run is in (module already imported)
    monkeypatch.setattr(pm, "STATE_DIR", tmp_path)
    block_ring_import(monkeypatch)                              # import fails -> file fallback -> no file
    for route in ROUTES:
        p = pm.build(route, NOW)
        assert p["error"] == "no metrics yet" and p["stale"] is True and p["head"] == "no data"


def test_block_ring_import_really_blocks_after_the_module_was_imported(monkeypatch):
    """The old, sys.modules-only block silently did nothing once the submodule had been imported by an earlier test."""
    import homelab_maint
    importlib.import_module("homelab_maint.metrics_ring")
    assert hasattr(homelab_maint, "metrics_ring")
    monkeypatch.setitem(sys.modules, "homelab_maint.metrics_ring", None)
    from homelab_maint import metrics_ring as leaked                # what the old test left in place: still importable
    assert leaked is not None
    block_ring_import(monkeypatch)
    with pytest.raises(ImportError):
        from homelab_maint import metrics_ring as blocked  # noqa: F401


def test_empty_real_ring_says_no_metrics_yet_but_a_running_sampler_without_sensors_does_not(tmp_path, monkeypatch):
    """Production path: metrics_ring.export() of a missing ring is a fresh all-None dict, not an exception, so build() used to
    return head "no data" with no explanation. The widget shows `error`; no sample time and no hour with data means "nothing yet"."""
    mr = pytest.importorskip("homelab_maint.metrics_ring")
    monkeypatch.setattr(mr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(pm, "STATE_DIR", tmp_path)
    for route in ROUTES:
        p = pm.build(route, NOW)                                    # live export of an empty state dir
        assert p["error"] == "no metrics yet" and p["stale"] is True and p["head"] == "no data" and p["ago"] == "never"
        assert p["ring"] == "0/168 h" and p["groups"] and len(p["rows"]) == 168          # still a complete, renderable shape
    for route in ROUTES:                                            # sampler alive, no sensors at all: dashes, no error
        assert "error" not in pm.build(route, NOW, fx("all_none"))


def test_corrupt_real_ring_says_no_metrics_yet_and_is_left_alone(tmp_path, monkeypatch):
    mr = pytest.importorskip("homelab_maint.metrics_ring")
    monkeypatch.setattr(mr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(pm, "STATE_DIR", tmp_path)
    ring = tmp_path / "metrics-ring.json"
    ring.write_text("{ this is not json")
    for route in ROUTES:
        p = pm.build(route, NOW)
        assert p["error"] == "no metrics yet" and p["stale"] is True
    assert ring.read_text() == "{ this is not json" and not (tmp_path / "metrics-ring.json.bad").exists()   # read-only path


def test_pure_payload_functions_flag_an_empty_export_themselves():
    """Glue may call thermal()/load() directly instead of build(): the explanation must not depend on the wrapper."""
    for fn in (pm.thermal, pm.load):
        for empty in (None, {}, {"current": {"sampled_at": None}, "series": {}}):
            assert fn(empty, NOW)["error"] == "no metrics yet"
        assert "error" not in fn(fx("all_none"), NOW) and "error" not in fn(fx("full_ring"), NOW)
    # history without a live sample (exporter lost its `current`) is stale data, not "no metrics yet"
    e = fx("full_ring")
    e["current"]["sampled_at"] = None
    assert "error" not in pm.thermal(e, NOW) and pm.thermal(e, NOW)["stale"] is True


def test_load_export_prefers_the_ring_and_falls_back_to_the_published_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE_DIR", tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "metrics.json").write_text(json.dumps(fx("full_ring")))

    class Ring:
        @staticmethod
        def export(now=None):
            return {"marker": "live"}

    monkeypatch.setitem(sys.modules, "homelab_maint.metrics_ring", Ring)
    import homelab_maint
    monkeypatch.setattr(homelab_maint, "metrics_ring", Ring, raising=False)
    assert pm.load_export(NOW) == {"marker": "live"}

    class Broken:
        @staticmethod
        def export(now=None):
            raise OSError("ring unreadable")

    monkeypatch.setitem(sys.modules, "homelab_maint.metrics_ring", Broken)
    monkeypatch.setattr(homelab_maint, "metrics_ring", Broken, raising=False)
    assert pm.load_export(NOW)["current"]["sampled_at"] == fx("full_ring")["current"]["sampled_at"]
    p = pm.build("thermal", NOW)
    assert p["head"] == "all cool" and "error" not in p


def test_works_against_the_real_metrics_ring(tmp_path, monkeypatch):
    """The real sampler's own export (not a fixture) goes through both payloads; skipped until metrics_ring exists."""
    mr = pytest.importorskip("homelab_maint.metrics_ring")
    monkeypatch.setattr(mr, "STATE_DIR", tmp_path)
    t = float(int(NOW // 3600) * 3600 + 120)
    for i in range(30):                                                       # 30 one-minute-spaced samples over 30 hours
        mr.record(t - (29 - i) * 3600 + 5, {"cpu_temp": 40.0 + i, "gpu_temp": 50.0, "ram_temp": 36.0, "cpu_fan_rpm": 900.0,
                                            "case_fan_rpm": 600.0, "gpu_fan_rpm": 0.0, "gpu_fan_pct": 0.0, "cpu_pct": 12.0,
                                            "gpu_pct": 0.0, "ram_pct": 55.0, "gpu_mem_pct": 6.0, "gpu_power_w": 30.0,
                                            "load1": 3.0})
    exp = mr.export(t)
    for route in ROUTES:
        p = pm.build(route, t, exp)
        assert size(p) < 14_000 and p["stale"] is False and p["head"] != "no data"
        assert p["ring"] == "30/168 h" and p["lt"] == "ring start"
    assert tiles(pm.thermal(exp, t))["CPU"]["v"] == "69"


# =========================================================================== chart geometry (the Homarr v2 board is a fixed-pixel grid)
@pytest.mark.parametrize("route", ROUTES)
def test_chart_height_and_axis_width_are_plain_pixel_numbers(route):
    """`h` was the CSS string clamp(..vw..): vw follows the browser window, not the tile, and is zoomed with the canvas, so the chart height
    varied per device while the tile does not. It is a number of logical px now, and `yw` widens the y axis so "100%" is not clipped."""
    for case_name in CASES:
        g = pm.build(route, NOW, fx(case_name))["groups"][0]
        assert type(g["h"]) is int and 150 <= g["h"] <= 320, g["h"]
        assert type(g["yw"]) is int and 36 <= g["yw"] <= 64, g["yw"]
    assert pm.CHART_H[route] == g["h"] and pm.Y_AXIS_W[route] == g["yw"]


def test_percent_axis_is_wider_than_the_degree_axis():
    """"100%" is wider than "100 deg" at the same font size: that is what clipped the top tick of the load chart at 32 px."""
    assert pm.Y_AXIS_W["load"] > pm.Y_AXIS_W["thermal"] >= 36


def test_every_group_that_draws_a_chart_carries_geometry_and_the_rest_do_not():
    for route in ROUTES:
        for g in pm.build(route, NOW, fx("full_ring"))["groups"]:
            assert bool(g["series"]) == ("h" in g) == ("yw" in g)


def test_geometry_survives_the_empty_and_error_paths():
    for e in ({}, None):
        for route in ROUTES:
            p = pm.build(route, NOW, e)
            assert all(isinstance(g.get("h", 0), int) and isinstance(g.get("yw", 0), int) for g in p["groups"])
    assert not any("vw" in json.dumps(pm.build(r, NOW, fx(c))) for r in ROUTES for c in CASES)         # no viewport-relative CSS leaks back in


def test_fixtures_are_what_the_generator_produces():
    spec = importlib.util.spec_from_file_location("make_fixtures_metrics", FIX / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in CASES:
        assert json.loads(json.dumps(mod.build(name))) == fx(name), f"{name}.json is stale: run make_fixtures.py"
    # the four fixtures SPEC2 asks for exist
    assert {"full_ring", "partial_ring", "stale", "all_none"} <= {p.stem for p in FIX.glob("*.json")}


def test_fixture_shapes_follow_the_ring_export_contract():
    for name in CASES:
        e = fx(name)
        assert e["slots"] == 168 and e["interval_s"] == 3600 and len(e["series"]["t"]) == 168
        assert e["series"]["t"][-1] % 3600 == 0 and e["series"]["t"][-1] <= e["generated_at"] < e["series"]["t"][-1] + 3600
        assert all(len(v) == 168 for v in e["series"].values())
        assert {"current", "hour_avg", "prev_hour_avg", "avg_24h", "avg_7d", "max_7d", "loop"} <= set(e)
        assert math.isclose(e["loop"]["overwrites_next_at"], e["series"]["t"][-1] + 3600)
