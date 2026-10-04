"""Tests for payloads.py and server.py (the JSON the Homarr v2 widgets read). The widget definitions and the render harness are tested in
test_widgets_v2.py; the Homarr installer has its own tests."""
import conftest  # noqa: F401  (points homelab_maint at throw-away dirs before it is imported)

import copy
import http.client
import json
import re
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = ROOT / "widgets"
sys.path.insert(0, str(WIDGETS))

import make_sample_status as ms  # noqa: E402
from homelab_maint import payloads, server  # noqa: E402

NOW = ms.NOW
ROUTES = list(payloads.ROUTES)


def size(p) -> int:
    return len(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode())


@pytest.fixture(params=ms.STATES)
def state(request):
    return request.param


# =========================================================================== payloads
def test_every_route_every_state_is_small_and_complete(state):
    st = ms.build(state)
    for route in ROUTES:
        p = payloads.build(route, st, NOW)
        assert size(p) < 4096, (route, size(p))
        assert {"stale", "age_min", "ago", "level", "c", "paused", "head"} <= set(p)
        assert p["c"] in {"teal", "yellow", "red", "orange", "gray"}
        assert p["stale"] is (state == "stale")
        json.dumps(p)                                  # JSON-clean


def test_stress_fixture_stays_under_budget():
    """Every list full and every string at maximum length, in ASCII and in 3-byte UTF-8, still fits in 4 KB."""
    ascii_st = ms.stress()
    cjk = json.loads(json.dumps(ascii_st).replace("xxxx", "\u6f22" * 4).replace("TTTT", "\u6f22" * 4).replace("EEEE", "\u6f22" * 4)
                     .replace("mmmm", "\u6f22" * 4).replace("cccc", "\u6f22" * 4).replace("rrrr", "\u6f22" * 4).replace("bbbb", "\u6f22" * 4))
    for st in (ascii_st, cjk):
        for route in ROUTES:
            p = payloads.build(route, st, NOW)
            assert size(p) < 4096, (route, size(p))


def test_fit_trims_lists_then_strings():
    p = {"rows": [{"x": "y" * 100}] * 100, "note": "z" * 5000}
    out = payloads.fit(p, 1000)
    assert size(out) <= 1000 and out["cut"] and out["rows"]


def test_staleness_boundary_is_45_minutes():
    st = ms.build("ok")
    gen = st["generated_at"]
    assert payloads.overview(st, gen + 44 * 60 + 59)["stale"] is False
    p = payloads.overview(st, gen + 45 * 60 + 1)
    assert p["stale"] is True and p["c"] == "gray" and p["ago"] == "45m"


def test_no_status_yields_error_payload_with_every_list_present():
    for route in ROUTES:
        p = payloads.build(route, {}, NOW)
        assert p["error"] and p["stale"] is True and p["level"] == "none" and p["c"] == "gray"
        assert all(isinstance(v, list) for k, v in p.items() if k in ("issues", "tiles", "mounts", "rows", "mem", "ev", "plans"))
    assert payloads.error_payload("x" * 500)["stale"] is True and len(payloads.error_payload("x" * 500)["error"]) <= 80


def test_overview_warn_and_crit_headlines():
    w = payloads.overview(ms.build("warn"), NOW)
    assert w["level"] == "warn" and w["c"] == "yellow" and w["head"] == "5 warn"
    assert {i["n"] for i in w["issues"]} == {"Disk space", "Backups", "Alert path", "Services & containers", "Stuck containers"}
    assert all(i["c"] == "yellow" for i in w["issues"])
    assert all(not re.match(r"(ok|warn|crit|info): ", i["s"]) for i in w["issues"])           # level prefix dropped
    assert "Docker disk" not in {i["n"] for i in w["issues"]}                                  # alert=False never lists as a problem
    root = w["tiles"][0]
    assert (root["l"], root["v"], root["s"], root["c"]) == ("Root free", "183.2 GiB", "full in 12d", "yellow")
    assert w["tiles"][2]["v"] == "7.6 GiB"                    # docker_cache 6.2 + snap 1.4 inside 24 h
    assert (w["ap_on"], w["ap_all"]) == (2, 9)
    c = payloads.overview(ms.build("crit"), NOW)
    assert c["level"] == "crit" and c["c"] == "red" and c["head"] == "3 crit, 1 err"
    # crit first, then the failed check (orange), never a healthy task
    assert [i["c"] for i in c["issues"]] == ["red", "red", "red", "orange"]


def test_overview_ok_is_all_clear_and_alert_false_never_looks_like_a_page():
    ok = payloads.overview(ms.build("ok"), NOW)
    assert ok["head"] == "all clear" and ok["issues"] == [] and ok["c"] == "teal"
    st = ms.build("ok")
    st["tasks"]["orphan_report"].update(status="crit", alert=False)
    p = payloads.overview(st, NOW)
    assert p["head"] == "all clear" and p["issues"] == []


def test_an_acknowledged_problem_is_muted_for_exactly_as_long_as_the_acknowledgement_lasts():
    """SPEC5: status.json marks `acked`; the widgets show it like an alert=False finding (info), the host colour already excludes it, and a
    flag that ended (or is malformed) shows the true colour again. acks.flag_live is the one reader."""
    import time
    st = ms.build("ok")
    st["tasks"]["orphan_report"].update(status="crit", alert=True, acked={"until": time.time() + 3600, "fp": "0" * 16})
    p = payloads.overview(st, NOW)
    assert p["head"] == "all clear" and p["issues"] == [] and payloads._lvl(st["tasks"]["orphan_report"]) == "info"
    for flag in ({"until": time.time() - 5}, {"until": "tomorrow"}, {}, "yes", None):          # ended or junk: fail closed, the true colour
        st["tasks"]["orphan_report"]["acked"] = flag
        assert payloads._lvl(st["tasks"]["orphan_report"]) == "crit", flag
    st["tasks"]["orphan_report"]["acked"] = {"until": time.time() + 3600}
    st["tasks"]["failed_units"].update(status="warn", alert=True)                                  # another, unacknowledged problem still shows
    assert payloads._lvl(st["tasks"]["failed_units"]) == "warn"
    st.pop("overall", None)                                                                         # no precomputed overall: derived from the muted levels
    assert payloads._overall(st) == "warn"


def test_overview_tiers_flag_a_timer_that_stopped_firing():
    st = ms.build("ok")
    st["tier_runs"]["daily"]["last_run"] = NOW - 40 * 3600
    tiers = {t["n"]: t for t in payloads.overview(st, NOW)["tiers"]}
    assert tiers["daily"]["c"] == "orange" and tiers["daily"]["a"] == "40h" and tiers["check"]["c"] == "teal"
    assert tiers["check"]["d"] is True                         # the check tier runs in dry-run


def test_disk_sorting_info_only_mounts_and_days():
    p = payloads.disk(ms.build("warn"), NOW)
    m = p["mounts"]
    assert len(m) == 7 and p["more"] == 5
    assert m[0]["m"] == "/" and m[0]["c"] == "yellow" and m[0]["d"] == "12d" and m[0]["p"] == 90
    assert [r["i"] for r in m[:6]] == [False] * 6              # the six alerting mounts first...
    assert m[6]["i"] is True and m[6]["c"] == "gray"           # ...info-only mounts after, always neutral (7 shown, 5 more)
    assert p["head"] == "/ 183.2 GiB free, full 12d"
    assert [r["l"] for r in p["dk"]] == ["images", "cache", "volumes", "reclaimable"]
    assert p["dk"][1]["v"] == "31.2 GiB" and p["dk"][3]["v"] == "60.9 GiB" and p["dkc"] == "gray"     # docker_df is alert=False


def test_disk_blocks_backups_smart_growth_plex():
    p = payloads.disk(ms.build("warn"), NOW)
    assert [(b["n"], b["a"], b["c"]) for b in p["bk"]] == [("immich", "8d", "yellow"), ("system", "2d", "teal"), ("stack-backup", "9h", "teal")]   # worst first
    assert p["sm"][0]["n"] == "nvme1n1" and p["sm"][0]["t"] == "52C"          # hottest first
    assert [s["x"] for s in p["sm"] if s["n"] == "sdb"] == ["realloc stable"]
    assert [g["r"] for g in p["gr"]][0] == "+0.08 GiB/d" and len(p["gr"]) == 3      # "n/a" (unmeasured) rows are skipped
    assert p["plexc"] == "gray" and "not migrated" in p["plex"]
    crit = payloads.disk(ms.build("crit"), NOW)
    assert crit["plexc"] == "red" and crit["mounts"][0]["c"] == "red" and crit["mounts"][0]["d"] == "2d"


def _dev(name, temp, level="ok", pending=0, note=""):
    return {"dev": name, "model": "m", "temp_c": temp, "realloc": 0, "pending": pending, "crc": 0, "power_on_h": 1,
            "level": level, "note": note}


def _bk(name, age_h, result="ok", level="ok"):
    return {"name": name, "age_h": age_h, "age": f"{age_h:.0f}h", "result": result, "limit_h": 200, "level": level}


def _gp(path, rate, level="ok"):
    return {"path": path, "size": "1.0 GiB", "rate": rate, "intake": "n/a", "limit": "0.5 GiB/d", "level": level}


def _disk_with(task, metric_key, rows):
    st = ms.build("ok")
    st["tasks"][task]["metrics"][metric_key] = rows
    return payloads.disk(st, NOW)


def test_disk_smart_ranks_by_severity_before_cutting_to_five():
    healthy = [_dev(f"sd{c}", 40 + i) for i, c in enumerate("abcdefghi")]                  # nine healthy disks, 40..48 C
    sm = _disk_with("smart_trend", "devices", healthy + [_dev("sdz", 19, "warn", pending=8, note="pending 8")])["sm"]
    assert len(sm) == 5 and (sm[0]["n"], sm[0]["c"], sm[0]["x"], sm[0]["t"]) == ("sdz", "yellow", "pending 8", "19C")
    assert [r["n"] for r in sm[1:]] == ["sdi", "sdh", "sdg", "sdf"]                         # then the hottest healthy ones
    # a task that sends no level at all still has pending sectors ranked as a problem
    bare = {"dev": "sdq", "temp_c": 20, "pending": 3}
    assert _disk_with("smart_trend", "devices", healthy + [bare])["sm"][0]["n"] == "sdq"
    # every problem row survives, even when there are more than five of them; no private sort keys leak out
    many = [_dev(f"bad{i}", 30 + i, "warn", pending=1) for i in range(7)] + [_dev("hot", 70)]
    sm = _disk_with("smart_trend", "devices", many)["sm"]
    assert [r["n"] for r in sm] == [f"bad{i}" for i in range(6, -1, -1)] and all(set(r) == {"n", "t", "x", "c"} for r in sm)


def test_disk_backups_show_a_failed_or_late_fourth_backup():
    base = [_bk("backup-system", 54.2), _bk("backup-immich", 71.5), _bk("backup-stack", 9.5)]
    bk = _disk_with("backup_freshness", "backups", base + [_bk("backup-photos", 30.0, "failed", "crit")])["bk"]
    assert bk == [{"n": "photos", "a": "30h", "c": "red"}, {"n": "immich", "a": "2d", "c": "teal"},
                  {"n": "system", "a": "2d", "c": "teal"}]                                  # failed first, then oldest healthy
    # a failed result is a problem even if the task forgot to set level, and a late (warn) one outranks healthy ones
    late = _bk("backup-late", 300.0, level="warn")
    bk = _disk_with("backup_freshness", "backups", base + [late, {"name": "backup-x", "age_h": 1.0, "result": "FAILED"}])["bk"]
    assert [(r["n"], r["c"]) for r in bk] == [("x", "red"), ("late", "yellow"), ("immich", "teal")]
    bk = _disk_with("backup_freshness", "backups", [_bk(f"backup-b{i}", 10 + i, "failed", "crit") for i in range(5)])["bk"]
    assert len(bk) == 5 and all(r["c"] == "red" for r in bk)                                # all failures kept


def test_disk_growth_shows_a_warning_even_as_the_fourth_configured_path():
    paths = [_gp("/volume1/docker/kavita/config/logs", "+0.01 GiB/d"), _gp("/var/log", "+0.03 GiB/d"),
             _gp("/var/lib/docker/containers", "+0.02 GiB/d"), _gp("/var/log/journal", "+1.20 GiB/d", "warn")]
    gr = _disk_with("growth_watch", "paths", paths)["gr"]
    assert gr[0] == {"p": "/var/log/journal", "r": "+1.20 GiB/d", "c": "yellow"} and len(gr) == 3
    assert [g["p"] for g in gr[1:]] == ["/var/log", "/var/lib/docker/containers"]            # fastest of the rest, not config order
    # nothing wrong: the three fastest growers, not the first three configured
    quiet = [_gp("/a", "+0.01 GiB/d"), _gp("/b", "-0.40 GiB/d"), _gp("/c", "+0.02 GiB/d"), _gp("/d", "+0.08 GiB/d")]
    assert [g["p"] for g in _disk_with("growth_watch", "paths", quiet)["gr"]] == ["/d", "/c", "/a"]
    # a blind (never measured, but flagged warn) path is shown; an unmeasured path that is merely info is not
    blind = [_gp("/a", "+0.01 GiB/d"), _gp("/b", "n/a", "info"), _gp("/blind", "n/a", "warn")]
    assert _disk_with("growth_watch", "paths", blind)["gr"][0] == {"p": "/blind", "r": "unmeasured", "c": "yellow"}
    assert "/b" not in [g["p"] for g in _disk_with("growth_watch", "paths", blind)["gr"]]
    four = [_gp(f"/w{i}", f"+{i + 1}.00 GiB/d", "warn") for i in range(4)]
    assert [g["p"] for g in _disk_with("growth_watch", "paths", four)["gr"]] == ["/w3", "/w2", "/w1", "/w0"]


def test_disk_payload_with_every_list_full_of_problems_still_fits_4kb():
    st = ms.stress()
    st["tasks"]["smart_trend"]["metrics"]["devices"] = [_dev("d" * 12 + str(i), 30 + i, "warn", pending=1, note="n" * 30) for i in range(12)]
    st["tasks"]["growth_watch"]["metrics"]["paths"] = [_gp("/" + "g" * 40 + str(i), "+9.99 GiB/d", "warn") for i in range(8)]
    p = payloads.disk(st, NOW)
    assert size(p) < 4096 and p["sm"] and p["gr"] and p["bk"] and p["mounts"]


def test_disk_rows_fall_back_to_bytes_and_items():
    st = ms.build("ok")
    d = st["tasks"]["disk_forecast"]
    rows = d["metrics"].pop("mounts")
    for r in rows:
        r.pop("free_h")
    d["items"] = rows                                           # no metrics.mounts, no free_h
    p = payloads.disk(st, NOW)
    assert p["mounts"] and p["mounts"][0]["f"].endswith("GiB") or p["mounts"][0]["f"].endswith("TiB")


def test_jobs_rows_prefer_apply_and_surface_broken_checks():
    p = payloads.jobs(ms.build("warn"), NOW)
    assert [r["n"] for r in p["rows"][:2]] == ["Docker build cache", "Old snap revisions"]          # apply-mode first
    assert p["rows"][0]["m"] == "apply" and p["rows"][0]["f"] == "6.2 GiB" and p["rows"][1]["m"] == "apply"
    assert p["rows"][0]["c"] == "teal" and p["rows"][2]["c"] == "gray" and p["rows"][2]["m"] == "report"      # report-mode findings are info
    assert all(r["k"] in ("C1", "C2") for r in p["rows"]) and p["fail"] == 0 and p["head"] == "apply 2/9"
    c = payloads.jobs(ms.build("crit"), NOW)
    assert c["rows"][0]["n"] == "Growth watch" and c["rows"][0]["c"] == "orange" and c["fail"] == 1   # failed C0 check surfaces first


def test_jobs_marks_late_tasks_stale_and_gray():
    st = ms.build("ok")
    st["tasks"]["trash"]["last_run"] = NOW - 3 * 86400          # daily task not run for 3 days
    p = payloads.jobs(st, NOW)
    row = next(r for r in p["rows"] if r["n"] == "Trash")
    assert row["x"] is True and row["c"] == "gray" and row["a"] == "3d" and p["fail"] == 1 and p["rows"][0]["n"] == "Trash"


def test_guard_candidates_memory_and_lines():
    p = payloads.guard(ms.build("warn"), NOW)
    assert p["head"] == "1 stuck" and p["skc"] == "yellow" and p["skn"] == 1
    prot, live = p["sk"]
    assert (prot["k"], prot["c"]) == ("protected", "gray") and (live["k"], live["c"], live["h"]) == ("", "yellow", "5.1 GiB")
    assert prot["r"] == "leak/runaway" and live["r"] == "idle but holding"                 # long reason cut at the colon
    assert {m["l"]: m["v"] for m in p["mem"]}["available"] == "70.1 GiB"
    assert [t["n"] for t in p["top"]] == ["ollama", "immich_machine_learning", "comfyui"]
    assert (p["cn"], p["ta"]) == ("71", "23.1 GiB")
    assert [(r["l"], r["c"]) for r in p["ln"]] == [("Orphans", "gray"), ("Alert path", "yellow"), ("Images", "teal")]
    assert p["ln"][1]["s"].startswith("alert path: smart hook")
    crit = payloads.guard(ms.build("crit"), NOW)
    assert crit["mc"] == "red" and {m["l"]: m["v"] for m in crit["mem"]}["mem psi"] == "22.0%"


def test_guard_accepts_list_style_largest_rows_and_busy_candidates():
    st = ms.build("warn")
    st["tasks"]["spike_sampler"]["metrics"]["largest"] = [["ollama", 9.8], ["x", 1.0]]
    st["tasks"]["stuck_detector"]["items"][1].update(busy="comfyui queue active")
    st["tasks"]["stuck_detector"]["metrics"]["actionable"] = 0
    p = payloads.guard(st, NOW)
    assert p["top"] == [{"n": "ollama", "h": "9.8 GiB"}, {"n": "x", "h": "1.0 GiB"}]
    assert (p["sk"][1]["k"], p["sk"][1]["c"]) == ("busy", "gray") and p["head"] == "2 watched"


def test_reclaim_windows_pending_and_plans():
    p = payloads.reclaim(ms.build("warn"), NOW)
    assert (p["d1"], p["d7"], p["d30"], p["d90"]) == ("7.6 GiB", "24.9 GiB", "27.2 GiB", "27.2 GiB")
    assert p["head"] == "7.6 GiB / 24h"
    assert [e["n"] for e in p["ev"]][:2] == ["Old snap revisions", "Docker build cache"] and p["ev"][0]["w"] == "11h ago"
    assert p["by"][0]["n"] == "Unused Docker images" and p["by"][0]["p"] == 100       # biggest 30-day contributor = 100 %
    assert p["pend"] == "22.3 GiB" and p["pr"][0]["n"] == "Unused Docker images" and p["pr"][0]["m"] == "report"
    assert [r["n"] for r in p["pr"]][:2] == ["Unused Docker images", "Retention rules"]       # apply-mode cleaners never count as pending
    plan = p["plans"][0]
    assert plan["k"] == 6 and plan["cmd"] == f"homelab-maint approve c2_candidates {plan['x']}" and len(plan["x"]) == 12
    assert p["cand"][0]["n"] == "hermes-genesis-gguf" and p["cand"][0]["m"] is True and p["cand"][0]["c"] == "yellow"
    assert payloads.reclaim(ms.build("ok"), NOW)["ev"] == []


def test_payloads_survive_garbage_status():
    st = {"generated_at": NOW, "overall": "weird", "reclaimed_log": ["x", {"t": "a"}, {"t": 1, "bytes": "q"}], "tier_runs": "bad",
          "tasks": {"a": "str", "b": {"status": "???", "metrics": "m", "items": None, "tier": 5, "last_run": "x"},
                    "c": {"status": "ok", "klass": "C2", "plan": "notadict", "metrics": {"mounts": [1, 2]}, "items": [3]},
                    "disk_forecast": {"metrics": {"mounts": [{"mount": None, "used_pct": "x", "days": "y"}], "root_free_h": 5}},
                    "stuck_detector": {"items": [{"name": None}, "x"]}}}
    for route in ROUTES:
        assert size(payloads.build(route, st, NOW)) < 4096
    assert payloads.build("overview", "not a dict", NOW)["error"]
    junk = {"generated_at": NOW, "tasks": {"disk_forecast": {"metrics": {"mounts": [{"level": ["x"], "mount": "/"}]}},
                                         "backup_freshness": {"metrics": {"backups": [{"level": {}, "name": "a", "result": ["r"]}]}},
                                         "smart_trend": {"metrics": {"devices": [{"level": [1], "dev": "d", "temp_c": "hot"}]}}}}
    assert payloads.disk(junk, NOW)["mounts"][0]["c"] == "teal"


def surrogate_status() -> dict:
    """A lone surrogate (valid JSON as \\udcff; what os.scandir gives for a non-UTF-8 file name) in every kind of text field."""
    bad = "bad\udcffname"
    st = ms.build("warn")
    t = st["tasks"]
    t["disk_forecast"]["summary"] = "disk " + bad
    t["disk_forecast"]["metrics"]["mounts"][0]["mount"] = "/mnt/" + bad
    t["stuck_detector"]["items"][0]["name"] = bad
    t["stuck_detector"]["items"][0]["reason"] = bad
    t["spike_sampler"]["metrics"]["largest"][0]["name"] = bad
    t["backup_freshness"]["metrics"]["backups"][0]["name"] = bad
    t["smart_trend"]["metrics"]["devices"][0]["dev"] = bad
    t["growth_watch"]["metrics"]["paths"][0]["path"] = "/var/" + bad
    t["docker_cache"]["title"] = bad
    t["c2_candidates"]["plan"]["items"][0]["name"] = bad
    t["c2_candidates"]["plan_hash"] = "ab" + bad
    st["host"] = bad
    st["reclaimed_log"][0]["task"] = bad
    return st


def test_t_and_short_path_scrub_lone_surrogates():
    assert payloads._t("a\udcffb", 20) == "a?b" and payloads._short_path("/x/\udcff", 22) == "/x/?"
    assert payloads._t("caf\u00e9 \u6f22", 20) == "caf\u00e9 \u6f22"                  # real non-ASCII text is untouched


def test_payloads_survive_lone_surrogates_in_any_text_field():
    st = surrogate_status()
    for route in ROUTES:
        p = payloads.build(route, st, NOW)
        json.dumps(p, ensure_ascii=False).encode("utf-8")      # used to raise UnicodeEncodeError (also inside fit())
        assert "error" not in p and size(p) < 4096, route
    assert payloads.overview(st, NOW)["head"] == "5 warn" and "disk ?" not in payloads.overview(st, NOW)["issues"][0]["s"] + "x"


def test_public_status_strips_plans_and_tracebacks():
    st = ms.build("warn")
    st["tasks"]["disk_forecast"]["metrics"]["traceback"] = "secret path"
    out = payloads.public_status(st)
    assert "plan" not in out["tasks"]["c2_candidates"] and out["tasks"]["c2_candidates"]["plan_hash"]
    assert "traceback" not in out["tasks"]["disk_forecast"]["metrics"] and out["overall"] == "warn"
    assert "plan" in st["tasks"]["c2_candidates"]                 # input untouched


def test_payload_cli_prints_json(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(ms.build("ok")))
    assert payloads.main(["jobs", str(f), str(NOW)]) == 0
    assert json.loads(capsys.readouterr().out)["head"] == "apply 2/9"
    assert payloads.main(["nope"]) == 2


# =========================================================================== server
@pytest.fixture
def srv(tmp_path):
    f = tmp_path / "status.json"
    f.write_text(json.dumps(ms.build("warn")))
    source = server.StatusSource(f, now=NOW)
    httpd = server.make_server("127.0.0.1", 0, source)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], f, source
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_server_routes_return_200_json(srv):
    port, _f, _s = srv
    for route in ROUTES + ["status"]:
        code, hdr, body = req(port, "GET", f"/{route}")
        assert code == 200 and hdr["Content-Type"].startswith("application/json") and hdr["Cache-Control"] == "no-store"
        data = json.loads(body)
        assert "error" not in data
        if route != "status":
            assert len(body) < 4096
    assert json.loads(req(port, "GET", "/overview?x=1")[2])["head"] == "5 warn"          # query ignored
    assert json.loads(req(port, "GET", "/overview/")[2])["head"] == "5 warn"             # trailing slash tolerated
    status = json.loads(req(port, "GET", "/status")[2])
    assert "plan" not in status["tasks"]["c2_candidates"] and status["generated_at"] == ms.build("warn")["generated_at"]


def test_server_unknown_paths_404_and_nothing_is_served_from_disk(srv):
    port = srv[0]
    for path in ("/", "/nope", "/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/status.json", "/widgets/ops-disk.json", "//etc/passwd"):
        code, _h, body = req(port, "GET", path)
        assert code == 404 and b"root:" not in body, path


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_server_other_verbs_405(srv, method):
    code, hdr, _b = req(srv[0], method, "/overview")
    assert code == 405 and hdr["Allow"] == "GET"


def test_server_trouble_is_always_200_with_in_band_error(srv, tmp_path):
    port, f, source = srv
    f.write_text("{not json")
    for route in ROUTES:
        code, _h, body = req(port, "GET", f"/{route}")
        d = json.loads(body)
        assert code == 200 and d["error"] == "no status yet" and d["stale"] is True, route
    d = json.loads(req(port, "GET", "/status")[2])
    assert d == {"error": "no status yet", "stale": True}
    f.unlink()
    assert json.loads(req(port, "GET", "/disk")[2])["error"]
    f.write_text(json.dumps(ms.build("ok")))                                      # recovers without a restart
    assert "error" not in json.loads(req(port, "GET", "/disk")[2])


def test_server_payload_bug_becomes_error_not_a_500(srv, monkeypatch, capsys):
    monkeypatch.setitem(payloads.ROUTES, "guard", lambda st, now: 1 / 0)
    code, _h, body = req(srv[0], "GET", "/guard")
    d = json.loads(body)
    assert code == 200 and d["stale"] is True and d["error"] == "payload failure: ZeroDivisionError"
    assert "Traceback" not in body.decode()


def test_server_lone_surrogates_in_status_json_never_drop_the_connection(srv):
    port, f, _s = srv
    f.write_text(json.dumps(surrogate_status()))              # ensure_ascii: the file holds the \\udcff escape, like the real one
    for route in ROUTES + ["status"]:
        code, _h, body = req(port, "GET", f"/{route}")        # used to raise out of do_GET: no response at all for /status
        d = json.loads(body)
        assert code == 200 and (route == "status" or "error" not in d), route
    assert json.loads(req(port, "GET", "/overview")[2])["head"] == "5 warn"           # one odd name no longer blanks the widget
    status = json.loads(req(port, "GET", "/status")[2])
    assert status["tasks"]["stuck_detector"]["items"][0]["name"] == "bad?name" and status["overall"] == "warn"


def test_server_render_encodes_a_surrogate_that_slips_through_a_payload(monkeypatch):
    """Safety net independent of payloads._t: whatever a payload function returns, render() must hand back bytes."""
    monkeypatch.setitem(payloads.ROUTES, "guard", lambda st, now: {"head": "x\udcffy", "k\udcff": ["\udcff"]})
    body = server.render("guard", server.StatusSource(WIDGETS / "sample-status.json", now=NOW))
    assert json.loads(body)["head"] == "x?y"
    monkeypatch.setitem(payloads.ROUTES, "guard", lambda st, now: 1 / 0)
    assert json.loads(server.render("guard", server.StatusSource(WIDGETS / "sample-status.json", now=NOW)))["stale"] is True


def test_server_cache_follows_file_changes(srv):
    port, f, _s = srv
    assert json.loads(req(port, "GET", "/overview")[2])["level"] == "warn"
    f.write_text(json.dumps(ms.build("crit")))
    assert json.loads(req(port, "GET", "/overview")[2])["level"] == "crit"


def test_server_preview_clock_never_goes_stale(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(ms.build("stale")))
    src = server.StatusSource(f, preview=True)
    body = json.loads(server.render("overview", src))
    assert body["stale"] is False and body["age_min"] == 3
    live = json.loads(server.render("overview", server.StatusSource(f)))              # real clock: fixture is long gone
    assert live["stale"] is True


def test_server_refuses_non_loopback_bind(tmp_path):
    for addr in ("0.0.0.0", "192.168.1.5", "::"):
        with pytest.raises(ValueError):
            server.make_server(addr, 0, server.StatusSource(tmp_path / "x"))
    assert server.main(["--bind", "0.0.0.0", "--port", "0"]) == 2


def test_server_port_comes_from_config(monkeypatch):
    monkeypatch.setattr(server, "load_toml", lambda p: {"global": {"www_port": 9222}})
    assert server.configured_port() == 9222
    monkeypatch.setattr(server, "load_toml", lambda p: {"global": {"www_port": "x"}})
    assert server.configured_port() == server.DEFAULT_PORT


# =========================================================================== payload <-> widget contract (the v2 definitions: tests/test_widgets_v2.py)
def test_sample_status_file_is_current_and_previewable(tmp_path):
    assert json.loads((WIDGETS / "sample-status.json").read_text()) == json.loads(json.dumps(ms.build("warn")))
    src = server.StatusSource(WIDGETS / "sample-status.json", preview=True)
    assert json.loads(server.render("overview", src))["head"] == "5 warn"


def test_fixture_states_cover_ok_warn_crit_stale():
    got = {s: payloads.overview(ms.build(s), NOW) for s in ms.STATES}
    assert [(got[s]["level"], got[s]["stale"]) for s in ms.STATES] == [("ok", False), ("warn", False), ("crit", False), ("warn", True)]


def test_jobs_payload_tells_no_data_from_no_cleaners():
    none = payloads.jobs({"generated_at": NOW - 60, "overall": "ok", "tasks": {}}, NOW)
    assert (none["head"], none["nt"], none["rows"]) == ("no data", 0, [])
    ok = ms.build("ok")
    c0 = {k: v for k, v in ok["tasks"].items() if v["klass"] == "C0"}
    checks = payloads.jobs(dict(ok, tasks=c0), NOW)
    assert (checks["head"], checks["nt"], checks["rows"]) == ("no cleaners", len(c0), []) and len(c0) > 0
    assert payloads.jobs(ok, NOW)["nt"] == len(ok["tasks"]) and payloads.jobs({}, NOW)["nt"] == 0


def test_payload_keys_are_flat_json_scalars_and_lists():
    """Templates read data.state.<key>: no nested objects (a method call on a missing one would kill the tile), only scalars and lists of flat rows."""
    for state in ms.STATES:
        for route in ROUTES:
            for key, v in payloads.build(route, ms.build(state), NOW).items():
                assert isinstance(v, (str, int, float, bool, type(None), list)), (route, key, type(v))
                assert all(isinstance(r, (str, int, float, bool, type(None), dict)) for r in v) if isinstance(v, list) else True


def test_payload_fields_avoid_names_the_v2_runtime_drops():
    """The v2 binding sanitiser drops the keys constructor / __proto__ / prototype from every object, and reflective names are unreadable."""
    bad = {"constructor", "__proto__", "prototype", "bind", "call", "apply", "arguments", "caller", "callee"}
    def keys(v):
        if isinstance(v, dict):
            for k, x in v.items():
                yield k
                yield from keys(x)
        elif isinstance(v, list):
            for x in v:
                yield from keys(x)
    for state in ms.STATES:
        for route in ROUTES:
            assert not bad & set(keys(payloads.build(route, ms.build(state), NOW))), (state, route)
