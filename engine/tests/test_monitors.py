"""Tests for tasks/monitors.py: the `probes` task, monitors.json, the dead-man's switch and the notification bridge.

Tmp dirs only, a fake HTTP server on 127.0.0.1, a scripted `sh`, and fake notify transports: nothing is ever sent.
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core, probes
from homelab_maint.tasks import monitors

NOW = 1_790_900_000.0


# --------------------------------------------------------------------------- helpers
class FakeSh:
    def __init__(self, *table):
        self.table, self.calls = list(table), []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(cmd)
        for row in self.table:
            if key.startswith(row[0]):
                return subprocess.CompletedProcess(cmd, row[2] if len(row) > 2 else 0, row[1], "")
        return subprocess.CompletedProcess(cmd, 127, "", "not found")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """STATE/LOG/RUN/CONF in tmp; no real command can run (docker, systemctl, curl all answer rc 127)."""
    for attr, name in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("RUN_DIR", "run"), ("CONF_DIR", "conf")):
        d = tmp_path / name
        d.mkdir()
        os.chmod(d, 0o755)
        monkeypatch.setattr(core, attr, d)
    monkeypatch.setattr(probes, "sh", FakeSh())
    monkeypatch.setattr(core, "sh", FakeSh())
    return tmp_path


def row(name="a", state="up", severity="warn", flapping=False, since=NOW - 600, **kw) -> dict:
    """A probes.snapshot() row."""
    r = {"name": name, "title": name.title(), "type": "http", "group": "", "class": "P2", "severity": severity, "source": "native",
         "kuma": "", "kuma_paused": False, "tags": [], "optional": False, "state": state,
         "lvl": {"up": 0, "warn": 1, "down": 2}.get(state), "since": since, "last_run": NOW - 30, "interval_s": 60, "ms": 5,
         "detail": "", "flapping": flapping, "how": "GET :80/", "slo": None, "avail_today": 100.0, "avail_7d": 100.0,
         "avail_30d": 100.0, "pending": 0, "kuma_push_key": False}
    r.update(kw)
    return r


def rep(**kw) -> probes.RunReport:
    return probes.RunReport(**kw)


def write_conf(text: str, name="probes.toml"):
    f = core.CONF_DIR / name
    f.write_text(text)
    os.chmod(f, 0o644)


# =========================================================================== registration
def test_the_probes_task_is_registered_as_c0_check():
    t = core.REGISTRY["probes"]
    assert (t.klass, t.tier, t.name) == ("C0", "check", "probes") and t.timeout >= 60 and t.title


def test_cli_loader_discovers_the_module():
    import importlib
    import pkgutil

    from homelab_maint import tasks
    assert "monitors" in [m.name for m in pkgutil.iter_modules(tasks.__path__)]
    assert importlib.import_module("homelab_maint.tasks.monitors").run is monitors.run


# =========================================================================== build_result
def ascii_ok(res):
    assert len(res.summary) <= 140 and res.summary.isascii(), res.summary
    assert len(res.items) <= 8
    json.dumps(res.metrics)
    assert all(isinstance(v, (int, float, bool, type(None))) for v in res.metrics.values())


def test_nothing_configured_yet_is_a_quiet_info():
    assert monitors.build_result([], None, NOW).summary == "no probes configured"
    r = monitors.build_result([], rep(), NOW)
    assert r.status == "info" and r.alert is False and r.metrics["blind"] == 0 and r.items == []


def test_no_probes_but_config_errors_is_monitoring_blind_an_error_that_pages():
    """FIX 1: this used to be info/alert=False ('no probes configured (1 config problems)'): all 131 probes went dark silently."""
    errs = ["probes.toml: ignored (not owned by root/runner, or it or its directory is group/world writable)"]
    r = monitors.build_result([], rep(errors=errs), NOW)
    assert r.status == "error" and r.alert is True and r.summary.startswith("MONITORING BLIND: no probes are running. probes.toml: ignored")
    assert r.metrics["blind"] == 1 and r.metrics["invalid"] == 1 and r.items[0]["sev"] == "crit" and "probes.toml" in r.items[0]["detail"]
    ascii_ok(r)
    ev = monitors.build_result([], rep(errors=errs), NOW, "events")
    assert ev.status == "error" and "self_notifies" not in ev.metrics         # even in events mode the TASK-level page must fire


def test_all_up_is_ok():
    r = monitors.build_result([row("a"), row("b"), row("c")], rep(ran=["a", "b", "c"]), NOW)
    assert r.status == "ok" and r.summary == "3/3 up" and r.items == [] and r.alert is True
    assert r.metrics["total"] == 3 and r.metrics["up"] == 3 and r.metrics["ran"] == 3 and r.metrics["avail_30d_pct"] == 100.0
    ascii_ok(r)


def test_confirmed_down_crit_probe_is_crit():
    rows = [row("docker", "down", severity="crit", detail="docker not answering"), row("b")]
    r = monitors.build_result(rows, rep(), NOW)
    assert r.status == "crit" and r.summary == "1 down: Docker; 1/2 up"
    assert r.metrics["crit_down"] == 1 and r.metrics["down"] == 1
    assert r.items[0]["name"] == "docker" and r.items[0]["state"] == "down" and r.items[0]["sev"] == "crit"
    assert r.items[0]["since_min"] == 10 and r.items[0]["detail"] == "docker not answering"


def test_confirmed_down_warn_probe_is_warn_and_sorted_after_crit():
    rows = [row("w1", "down", since=NOW - 100), row("c1", "down", severity="crit", since=NOW - 50), row("w0", "down", since=NOW - 900)]
    r = monitors.build_result(rows, rep(), NOW)
    assert r.status == "crit" and [i["name"] for i in r.items] == ["c1", "w0", "w1"]       # crit first, then longest-down first
    assert "3 down: C1, W0, W1" in r.summary


def test_warn_only_trouble_is_warn():
    r = monitors.build_result([row("a", "down"), row("b")], rep(), NOW)
    assert r.status == "warn" and r.metrics["crit_down"] == 0
    r = monitors.build_result([row("a", "warn", detail="health check starting"), row("b")], rep(), NOW)
    assert r.status == "warn" and "1 degraded: A" in r.summary and r.metrics["warn"] == 1


def test_degraded_p0_is_never_crit():
    r = monitors.build_result([row("a", "warn", severity="crit")], rep(), NOW)
    assert r.status == "warn"


def test_flapping_probe_counts_as_warn_when_up_or_degraded():
    r = monitors.build_result([row("a", flapping=True), row("b", "warn", severity="crit", flapping=True)], rep(), NOW)
    assert r.status == "warn" and "2 flapping" in r.summary and r.metrics["flapping"] == 2 and r.metrics["crit_down"] == 0
    assert {i["state"] for i in r.items} == {"flapping"}


def test_a_crit_probe_that_is_down_right_now_stays_crit_even_if_it_flapped_earlier():
    """FIX 6: a service that crash-looped and then died is the worst case, not a noisy one."""
    r = monitors.build_result([row("a", flapping=True), row("b", "down", severity="crit", flapping=True)], rep(), NOW)
    assert r.status == "crit" and r.metrics["crit_down"] == 1 and r.metrics["flapping"] == 2 and "1 down: B" in r.summary
    by = {i["name"]: i for i in r.items}
    assert (by["b"]["state"], by["b"]["sev"], by["b"]["flapping"]) == ("down", "crit", True) and by["a"]["state"] == "flapping"
    assert monitors._sev(row("c", "down", severity="warn", flapping=True)) == "warn"          # only a crit probe is crit


def test_info_severity_trouble_never_pages():
    r = monitors.build_result([row("m", "down", severity="info"), row("b")], rep(), NOW)
    assert r.status == "info" and "1 info-only" in r.summary and r.items[0]["sev"] == "info"


def test_bad_probe_definitions_make_the_plane_warn_and_name_the_first_problem():
    r = monitors.build_result([row("a")], rep(errors=["extra.toml: ignored (group/world writable)", "b: bad"]), NOW)
    assert r.status == "warn" and "2 bad probe defs (extra.toml: ignored" in r.summary and r.metrics["invalid"] == 2 and r.metrics["blind"] == 0
    cfg = [i for i in r.items if i["name"] == "probes-config"]
    assert len(cfg) == 2 and "extra.toml" in cfg[0]["detail"]               # visible even when every probe is green
    ascii_ok(r)


def test_skipped_paused_and_unknown_are_not_counted_as_up():
    rows = [row("a"), row("s", "skipped", lvl=None), row("p", "paused", lvl=None), row("u", "unknown", lvl=None)]
    r = monitors.build_result(rows, rep(), NOW)
    assert r.status == "ok" and r.summary == "1/1 up, 1 skipped, 1 paused"
    assert r.metrics["skipped"] == 1 and r.metrics["paused"] == 1 and r.metrics["unknown"] == 1 and r.metrics["counted"] == 1
    assert {i["name"] for i in r.items} == {"s", "p"}                       # nothing bad: the skipped/paused rows explain themselves


def test_summary_is_ascii_and_short_whatever_the_titles():
    rows = [row(f"n{i}", "down", title="Café ☃ " + "very long title " * 5) for i in range(30)]
    r = monitors.build_result(rows, rep(), NOW)
    ascii_ok(r)
    assert len(r.items) == 8 and r.metrics["down"] == 30


def test_cached_result_is_labelled_when_another_run_holds_the_lock():
    r = monitors.build_result([row("a")], rep(locked=True), NOW)
    assert "cached" in r.summary and r.status == "ok"


def test_metrics_overdue_availability_and_slowest():
    rows = [row("a", ms=900, avail_30d=90.0), row("b", ms=40, avail_30d=100.0, last_run=NOW - 4000, interval_s=60)]
    r = monitors.build_result(rows, rep(elapsed_s=1.234), NOW)
    assert r.metrics["slowest_ms"] == 900 and r.metrics["avail_30d_pct"] == 95.0 and r.metrics["overdue"] == 1
    assert r.metrics["run_ms"] == 1234


def test_observed_share_is_reported_and_a_future_since_never_shows_a_negative_age():
    rows = [row("a", obs_30d=80.0), row("b", obs_30d=100.0, state="down", severity="crit", since=NOW + 3 * 86400)]      # b: clock stepped back
    r = monitors.build_result(rows, rep(), NOW)
    assert r.metrics["obs_30d_pct"] == 90.0
    assert [i["since_min"] for i in r.items if i["name"] == "b"] == [0]
    assert monitors.build_result([row("a")], rep(), NOW).metrics["obs_30d_pct"] is None      # rows without the field (older snapshot): no crash


def test_alert_mode_events_keeps_status_honest_but_marks_the_task_self_notifying():
    rows = [row("a", "down", severity="crit")]
    task_mode = monitors.build_result(rows, rep(), NOW, "task")
    ev_mode = monitors.build_result(rows, rep(), NOW, "events")
    assert task_mode.alert is True and "self_notifies" not in task_mode.metrics
    assert ev_mode.alert is True and ev_mode.status == "crit" and ev_mode.metrics["self_notifies"] == 1   # incidents/dashboard still see it


# =========================================================================== the task end to end (real engine, fake server)
class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/slow":
            time.sleep(1.5)
        code = self.server.codes.get(path, 200)
        body = b"ok"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def srv():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    httpd.daemon_threads = True
    httpd.codes = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.url = lambda p="/ok": f"http://127.0.0.1:{httpd.server_address[1]}{p}"
    yield httpd
    httpd.shutdown()


def run_task(cfg=None):
    return core.run_task(core.REGISTRY["probes"], {"tasks": {"probes": {"force": True, **(cfg or {})}}, "global": {}}, False)


def test_task_run_end_to_end_down_after_two_runs_and_back(srv):
    write_conf(f'[defaults]\nattempts = 1\n[[probe]]\nname = "good"\ntitle = "Good"\ntype = "http"\ntarget = "{srv.url("/ok")}"\n'
               f'[[probe]]\nname = "flaky"\ntitle = "Flaky"\ntype = "http"\ntarget = "{srv.url("/flaky")}"\nclass = "P1"\n')
    res, _dur = run_task()
    assert res.status == "ok" and res.summary == "2/2 up"
    srv.codes["/flaky"] = 503
    res, _ = run_task()
    assert res.status == "ok"                                                # one bad run is a blip
    res, _ = run_task()
    assert res.status == "warn" and res.summary == "1 down: Flaky; 1/2 up" and res.items[0]["detail"].startswith("HTTP 503")
    srv.codes["/flaky"] = 200
    res, _ = run_task()
    assert res.status == "warn"                                              # recovery needs two good runs too
    res, _ = run_task()
    assert res.status == "ok" and res.summary == "2/2 up"
    ascii_ok(res)


def test_task_with_no_config_file_at_all_is_a_quiet_info():
    res, _ = run_task()
    assert res.status == "info" and res.summary == "no probes configured" and res.alert is False      # never installed: fine


GOOD = '[defaults]\nattempts = 1\n[[probe]]\nname = "a"\ntype = "tcp"\ntarget = "127.0.0.1:9"\n'


def test_task_group_writable_config_dir_is_blind_error_and_the_runner_pages(monkeypatch):
    """The reviewer's case: a routine `chmod g+w /etc/homelab-maint` made _trusted() fail and the plane went dark with info/alert=False."""
    write_conf(GOOD)
    os.chmod(core.CONF_DIR, 0o775)
    res, _ = run_task()
    assert res.status == "error" and res.alert is True and "MONITORING BLIND" in res.summary and "probes.toml" in res.summary
    assert "group/world writable" in res.items[0]["detail"]
    n = core.Notifier({"global": {"alert_confirm_runs": 1}})                # what cmd_run does with the result
    sent = []
    monkeypatch.setattr(n, "_send", lambda name, subject, body, now: sent.append((subject, body)) or True)
    n.evaluate("probes", "Monitoring probes", res, NOW)
    assert sent and sent[0][0].startswith("CRIT Monitoring probes") and "BLIND" in sent[0][1]          # error pages like crit
    os.chmod(core.CONF_DIR, 0o755)                                           # fixed
    res, _ = run_task()
    assert res.status != "error" and "BLIND" not in res.summary and res.metrics["total"] == 1       # one probe is running again


def test_task_present_but_empty_or_all_invalid_config_is_blind_not_quiet():
    for body in ("", "# nothing\n", "[[probe]]\nname = 'Bad Name'\ntype = 'tcp'\ntarget = 'x'\n"):
        write_conf(body)
        res, _ = run_task()
        assert res.status == "error" and res.alert is True and "BLIND" in res.summary, body


def test_task_vanished_config_after_probes_ran_is_blind_not_quiet():
    write_conf(GOOD)
    res, _ = run_task()
    assert "BLIND" not in res.summary
    (core.CONF_DIR / "probes.toml").unlink()
    res, _ = run_task()
    assert res.status == "error" and "ran before" in res.summary and res.alert is True


def test_task_one_untrusted_extra_file_warns_and_names_it_while_the_rest_keeps_running():
    write_conf(GOOD)
    (core.CONF_DIR / "probes.d").mkdir()
    write_conf('[[probe]]\nname = "evil"\ntype = "command"\ntarget = ["true"]\n', "probes.d/extra.toml")
    os.chmod(core.CONF_DIR / "probes.d" / "extra.toml", 0o666)
    res, _ = run_task()
    assert res.status == "warn" and "extra.toml: ignored" in res.summary and res.metrics["total"] == 1 and res.metrics["blind"] == 0


def test_blind_plane_is_announced_as_a_config_event_in_events_mode_and_stops_the_deadman():
    write_conf(GOOD)
    os.chmod(core.CONF_DIR, 0o775)
    res, _ = run_task({"alert_mode": "events"})
    assert res.status == "error" and "self_notifies" not in res.metrics
    ev = probes.claim_events(time.time())
    assert [(e["probe"], e["to"], e["severity"]) for e in ev] == [("*config", "config", "crit")]
    e = monitors.to_event(ev[0], FNotify())
    assert (e.kind, e.severity, e.title) == ("alert", "crit", "Probe configuration has problems") and "MONITORING BLIND" in e.summary
    assert probes.load_state()["run"] == {}                                  # no run recorded => the heartbeat goes stale on purpose


def test_task_options_override_budget_and_workers(srv):
    write_conf(f'[defaults]\nattempts = 1\nconfirm = 1\n[[probe]]\nname = "slow"\ntype = "http"\ntarget = "{srv.url("/slow")}"\ntimeout_s = 5\n')
    t0 = time.monotonic()
    res, _ = run_task({"budget_s": 0.3, "workers": 1})
    assert time.monotonic() - t0 < 1.2 and res.status == "warn" and "1 down" in res.summary
    assert "did not finish" in res.items[0]["detail"]


def test_task_alert_mode_events_flag(srv):
    write_conf(f'[defaults]\nattempts = 1\nconfirm = 1\n[[probe]]\nname = "a"\ntype = "http"\ntarget = "{srv.url("/bad")}"\n')
    srv.codes["/bad"] = 500
    res, _ = run_task({"alert_mode": "events"})
    assert res.status == "warn" and res.alert is True and res.metrics["self_notifies"] == 1


def test_task_writes_history_and_its_own_state_only(srv):
    write_conf(f'[[probe]]\nname = "a"\ntype = "http"\ntarget = "{srv.url("/ok")}"\n')
    run_task()
    names = sorted(p.name for p in core.STATE_DIR.iterdir())
    assert "probes.json" in names and "history.jsonl" in names
    assert not any(n in names for n in ("status.json", "alerts.json"))        # the runner writes those, never the task
    assert core.read_history(10 ** 9, "probe")[0]["n"] == 1


# =========================================================================== monitors.json
def seed(*rows_):
    """Run the engine over scripted probes so snapshot() has state; returns the conf."""
    box = {"r": probes.OK}
    return box


def make_state(monkeypatch, defs, results_by_run):
    """defs: probe raws; results_by_run: list of {name: (lvl, detail)} per forced run, one minute apart."""
    d, ps, errs = probes.parse({"defaults": {"attempts": 1}, "probe": defs})
    cur = {}
    monkeypatch.setitem(probes.RUNNERS, "http", lambda p, snap, now: cur.get(p.name, (probes.OK, "fine")))
    for i, res in enumerate(results_by_run):
        cur.clear()
        cur.update(res)
        probes.run_due(NOW + 60 * i, conf=(d, ps, errs), sleep=lambda s: None, pusher=lambda *a: None, force=True)
    return (d, ps, errs)


def test_export_shape_ordering_and_privacy(monkeypatch):
    defs = [{"name": "z-up", "type": "http", "target": "http://127.0.0.1:9/secret/path?token=ABC", "title": "Zed"},
            {"name": "a-down", "type": "http", "target": "http://127.0.0.1:9/x", "confirm": 1, "kuma": "A Down", "source": "kuma", "slo": 99.5},
            {"name": "m-paused", "type": "http", "target": "http://127.0.0.1:9/p", "paused": True},
            {"name": "k-push", "type": "http", "target": "http://127.0.0.1:9/k", "kuma_push_key": "k-push", "kuma_paused": True, "kuma": "K"}]
    conf = make_state(monkeypatch, defs, [{"a-down": (probes.FAIL, "HTTP 500 (want ['200-299'])")}])
    ex = monitors.export(NOW + 10, conf)
    assert ex["schema"] == 1 and ex["generated_at"] == NOW + 10 and ex["stale"] is False and ex["errors"] == 0
    assert [p["name"] for p in ex["probes"]] == ["a-down", "k-push", "z-up", "m-paused"]        # down, up (by name), then paused
    assert ex["summary"] == {"total": 4, "up": 2, "down": 1, "paused": 1} and ex["sources"] == {"native": 3, "kuma": 1}
    assert ex["kuma"] == {"ported": 2, "paused_in_kuma": ["K"], "push_ready": 1}
    down = ex["probes"][0]
    assert down["state"] == "down" and down["detail"].startswith("HTTP 500") and down["slo"] == 99.5 and down["slo_status"] == "breached"
    assert down["avail_30d"] == 0.0 and down["kuma"] == "A Down"
    up = ex["probes"][2]
    assert up["detail"] == "" and up["how"] == "GET :9/secret/path"                                # healthy rows carry no detail; the query is cut
    blob = json.dumps(ex)
    assert "ABC" not in blob and "127.0.0.1" not in blob and "token" not in blob and len(blob) < 200_000


def test_export_staleness_follows_the_last_engine_run(monkeypatch):
    conf = make_state(monkeypatch, [{"name": "a", "type": "http", "target": "http://127.0.0.1:9/"}], [{}])
    assert monitors.export(NOW + 60, conf)["stale"] is False
    assert monitors.export(NOW + monitors.STALE_S + 3600 * 24, conf)["stale"] is True
    assert monitors.export(NOW, ({}, [], []))["stale"] in (True, False)       # no probes: still a well-formed document
    core.STATE_DIR.joinpath("probes.json").unlink()
    assert monitors.export(NOW, conf)["stale"] is True and monitors.export(NOW, conf)["last_run"] is None


def test_a_power_cut_burns_the_slo_budget_in_monitors_json_and_says_how_much_was_observed(monkeypatch):
    """Review fix: 10 runs, 3 h of nothing, 10 runs used to leave availability 100 % and slo_status 'ok'."""
    d, ps, errs = probes.parse({"defaults": {"attempts": 1}, "probe": [
        {"name": "a", "type": "http", "target": "http://127.0.0.1:9/", "interval_s": 60, "slo": 99.9}]})
    monkeypatch.setitem(probes.RUNNERS, "http", lambda p, snap, now: (probes.OK, "fine"))
    tick = lambda t: probes.run_due(t, conf=(d, ps, errs), sleep=lambda s: None, pusher=lambda *a: None)       # a tick, not forced
    for i in range(10):
        tick(NOW + 60 * i)
    p = monitors.export(NOW + 600, (d, ps, errs))["probes"][0]
    assert (p["avail_30d"], p["obs_30d"], p["slo_status"]) == (100.0, 100.0, "ok")
    for i in range(10):
        tick(NOW + 3 * 3600 + 60 * i)
    p = monitors.export(NOW + 3 * 3600 + 600, (d, ps, errs))["probes"][0]
    assert p["state"] == "up" and p["slo_status"] == "breached" and p["avail_30d"] < 99.9
    assert p["obs_30d"] == p["avail_30d"] and p["obs_7d"] == p["avail_7d"] < 20                              # all of the lost availability is unobserved time
    assert monitors.build_result(probes.snapshot(NOW + 3 * 3600 + 600, (d, ps, errs)), None, NOW + 3 * 3600 + 600).metrics["obs_30d_pct"] < 20


@pytest.mark.parametrize("avail,slo,want", [(None, 99.5, None), (99.9, None, None), (99.0, 99.5, "breached"), (99.8, 99.5, "ok"),
                                            (99.4, 99.0, "at_risk"), (100.0, 99.9, "ok"), (99.95, 99.9, "ok")])
def test_slo_state(avail, slo, want):
    assert monitors.slo_state(avail, slo) == want


# =========================================================================== dead-man's switch
def test_heartbeat_ok_only_while_the_runner_and_the_probe_plane_are_fresh(monkeypatch):
    conf = make_state(monkeypatch, [{"name": "a", "type": "http", "target": "http://127.0.0.1:9/"},
                                    {"name": "b", "type": "http", "target": "http://127.0.0.1:9/", "confirm": 1, "class": "P0"}],
                      [{"b": (probes.FAIL, "x")}])
    hb = monitors.heartbeat_payload(NOW + 70, conf)
    assert hb["ok"] is False and hb["status_age_s"] is None                    # no status.json yet: the check tier has not run
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 30})
    hb = monitors.heartbeat_payload(NOW + 70, conf)
    assert hb["ok"] is True and hb["status_age_s"] == 40 and hb["probes_age_s"] < 200
    assert (hb["total"], hb["up"], hb["down"], hb["crit_down"]) == (2, 1, 1, 1)
    assert monitors.heartbeat_payload(NOW + 30 + monitors.STALE_S + 1, conf)["ok"] is False          # check tier stopped
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 10_000})
    assert monitors.heartbeat_payload(NOW + 10_000 + 60, conf)["ok"] is False                        # probe plane stopped (probes.json is old)
    blob = json.dumps(hb)
    assert "127.0.0.1" not in blob and len(blob) < 400


def test_heartbeat_is_not_ok_when_a_stamp_is_in_the_future_after_a_clock_step_back(monkeypatch):
    """Review fix: a negative age passed `age < STALE_S`, so a stepped-back clock reported ok=true."""
    conf = make_state(monkeypatch, [{"name": "a", "type": "http", "target": "http://127.0.0.1:9/"}], [{}])
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 30})
    ok = monitors.heartbeat_payload(NOW + 70, conf)
    assert ok["ok"] is True and ok["clock_skew"] is False
    hb = monitors.heartbeat_payload(NOW - 3 * 86400, conf)                              # both stamps are now 3 days in the future
    assert hb["ok"] is False and hb["clock_skew"] is True and hb["probes_age_s"] < 0 and hb["status_age_s"] < 0
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 3600})
    hb = monitors.heartbeat_payload(NOW + 70, conf)                                      # only the check tier's stamp is in the future
    assert hb["ok"] is False and hb["clock_skew"] is True and hb["probes_age_s"] >= 0
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 70 + probes.SKEW_S})
    assert monitors.heartbeat_payload(NOW + 70, conf)["ok"] is True                      # a few seconds of skew is tolerated
    assert len(json.dumps(hb)) < 400


def test_heartbeat_is_not_ok_while_the_plane_is_blind_even_if_the_files_are_fresh(monkeypatch):
    conf = make_state(monkeypatch, [{"name": "a", "type": "http", "target": "http://127.0.0.1:9/"}], [{}])
    core.write_json_atomic(core.STATE_DIR / "status.json", {"generated_at": NOW + 30})
    assert monitors.heartbeat_payload(NOW + 70, conf)["ok"] is True
    st = probes.load_state()
    st["cfg"] = {"n": 1, "blind": True, "t": NOW + 60}
    probes.save_state(st)
    hb = monitors.heartbeat_payload(NOW + 70, conf)
    assert hb["ok"] is False and hb["blind"] is True and hb["config_problems"] == 1


# =========================================================================== notifications (alert_mode = "events")
@dataclass
class FEvent:
    kind: str
    severity: str = "info"
    title: str = ""
    summary: str = ""
    details: object = None
    facts: dict | None = None
    status: str | None = None
    dedupe_key: str | None = None
    task: str | None = None


@dataclass
class FDelivery:
    ok: bool = False
    handled: bool = False


class FNotify:
    Event = FEvent

    def __init__(self, *outcomes):
        self.outcomes, self.sent = list(outcomes), []

    def send(self, ev):
        self.sent.append(ev)
        o = self.outcomes.pop(0) if self.outcomes else FDelivery(ok=True)
        if isinstance(o, Exception):
            raise o
        return o


DOWN = {"probe": "docker-daemon", "title": "Docker daemon answers", "from": "up", "to": "down", "severity": "crit", "class": "P0",
        "detail": "docker not answering", "down_s": 0, "t": NOW, "dedupe_key": "probe:docker-daemon"}
UP = {**DOWN, "from": "down", "to": "up", "severity": "recovery", "down_s": 725}


def test_to_event_maps_alerts_and_recoveries():
    e = monitors.to_event(DOWN, FNotify())
    assert (e.kind, e.severity, e.status, e.task) == ("alert", "crit", "crit", "probes")
    assert e.title == "Docker daemon answers is down" and e.summary == "docker not answering" and e.dedupe_key == "probe:docker-daemon"
    assert e.facts == {"Probe": "docker-daemon", "Class": "P0", "Now": "down"} and all(isinstance(v, str) for v in e.facts.values())
    r = monitors.to_event(UP, FNotify())
    assert (r.kind, r.severity, r.status) == ("recovery", "ok", "ok") and r.title == "Docker daemon answers recovered"
    assert r.dedupe_key == e.dedupe_key and "12 min" in r.summary and r.facts["Was down for"] == "12 min"


def test_to_event_flapping_storm_and_odd_input():
    f = monitors.to_event({**DOWN, "to": "flapping", "severity": "warn", "detail": "state keeps changing"}, FNotify())
    assert f.title.endswith("keeps flapping") and f.severity == "warn"
    s = monitors.to_event({"probe": "*", "title": "Many probes changed state", "to": "storm", "severity": "crit", "detail": "10 probes changed state in one run (host stall?)",
                           "dedupe_key": "probe:*"}, FNotify())
    assert s.title == "Many probes changed state" and s.severity == "crit" and s.facts["Class"] == "-"
    weird = monitors.to_event({"probe": "x", "title": "Café " * 40, "to": "down", "severity": "bogus", "detail": "☃ " * 100}, FNotify())
    assert weird.severity == "warn" and weird.summary.isascii() and len(weird.summary) <= 140 and weird.dedupe_key is None


def queue(*events):
    """Seed the persisted queue; each event gets an id like the engine gives it (q1, q2, ...)."""
    probes.save_state({**probes._blank(), "events": [{**e, "id": e.get("id", f"q{i}")} for i, e in enumerate(events, 1)]})


def pending():
    st = probes.load_state()
    return sorted(e["id"] for e in st["events"]), sorted(e["id"] for e in st["inflight"])


ZERO = {"sent": 0, "dropped": 0, "retry": 0, "deferred": 0, "expired": 0}


def test_notify_events_delivers_and_clears_the_queue():
    queue(DOWN, UP)
    n = FNotify()
    assert monitors.notify_events(n.send, n, NOW) == {**ZERO, "sent": 2}
    assert [e.kind for e in n.sent] == ["alert", "recovery"] and pending() == ([], [])        # alerts first, and nothing left claimed


def test_notify_events_policy_drops_are_final_but_failures_are_retried_with_backoff():
    queue(DOWN, {**DOWN, "probe": "b", "dedupe_key": "probe:b"}, UP)
    n = FNotify(FDelivery(ok=True), FDelivery(handled=True), FDelivery())        # DOWN sent, b dropped by policy (dedupe/quiet), UP failed outright
    assert monitors.notify_events(n.send, n, NOW) == {**ZERO, "sent": 1, "dropped": 1, "retry": 1}
    assert pending() == (["q3"], [])                                              # only the undelivered one comes back, not claimed
    left = probes.load_state()["events"][0]
    assert left["tries"] == 1 and left["nb"] == NOW + 60 and "ct" not in left
    assert monitors.notify_events(FNotify().send, FNotify(), NOW + 30) == ZERO    # still backing off: a dead transport is not hammered
    queue(DOWN)
    n2 = FNotify(RuntimeError("transport exploded"))
    assert monitors.notify_events(n2.send, n2, NOW)["retry"] == 1 and pending() == (["q1"], [])


def test_notify_events_drops_stale_events_instead_of_paging_about_the_past():
    queue({**DOWN, "t": NOW}, {**UP, "probe": "late", "t": NOW + 3000})
    n = FNotify()
    assert monitors.notify_events(n.send, n, NOW + 3600 + 1) == {**ZERO, "sent": 1, "expired": 1}
    assert [e.dedupe_key for e in n.sent] == ["probe:docker-daemon"] and pending() == ([], [])


def test_notify_events_never_raises_and_does_nothing_without_events(monkeypatch):
    assert monitors.notify_events(lambda e: 1 / 0, FNotify(), NOW) == ZERO
    monkeypatch.setattr(probes, "claim_events", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert monitors.notify_events() == ZERO


def test_to_event_config_problem():
    c = monitors.to_event({"probe": "*config", "title": "Probe configuration", "to": "config", "severity": "crit", "class": "-",
                           "detail": "MONITORING BLIND: probes.toml: ignored", "dedupe_key": "probe:*config"}, FNotify())
    assert (c.kind, c.severity, c.title) == ("alert", "crit", "Probe configuration has problems") and c.dedupe_key == "probe:*config"
    r = monitors.to_event({"probe": "*config", "title": "Probe configuration", "to": "up", "severity": "recovery", "down_s": 600,
                           "dedupe_key": "probe:*config"}, FNotify())
    assert r.kind == "recovery" and r.dedupe_key == c.dedupe_key


def test_events_build_real_notify_events_that_route_and_render_in_a_dry_run():
    """Contract check against the real notify.py: our events are accepted, routed and rendered (dry_run sends nothing)."""
    from homelab_maint import notify
    ev = monitors.to_event(DOWN)
    assert isinstance(ev, notify.Event)
    d = notify.send(ev, dry_run=True)
    assert d.kind == "alert" and d.severity == "crit" and d.rendered
    sms = d.rendered["sms"]
    assert sms.isascii() and len(sms) <= 130 and "http" not in sms.lower() and "Docker daemon" in sms
    assert "Docker daemon answers" in d.rendered["subject"]
    rec = notify.send(monitors.to_event(UP), dry_run=True)
    assert rec.kind == "recovery" and rec.rendered
    cfg = notify.send(monitors.to_event({"probe": "*config", "title": "Probe configuration", "to": "config", "severity": "crit",
                                         "detail": "MONITORING BLIND: probes.toml: ignored", "dedupe_key": "probe:*config"}), dry_run=True)
    assert cfg.kind == "alert" and cfg.rendered and cfg.rendered["sms"].isascii()


def test_the_whole_loop_probe_event_to_notification_and_requeue(monkeypatch):
    """down -> engine event -> notify_events -> (failed delivery) -> requeued -> delivered next time, exactly once."""
    box = {"r": (probes.OK, "fine")}
    monkeypatch.setitem(probes.RUNNERS, "http", lambda p, snap, now: box["r"])
    conf = probes.parse({"defaults": {"attempts": 1}, "probe": [{"name": "web", "title": "Web", "type": "http", "target": "http://127.0.0.1:9/", "confirm": 1}]})
    run = lambda i: probes.run_due(NOW + 60 * i, conf=conf, sleep=lambda s: None, pusher=lambda *a: None, force=True)
    run(0)
    box["r"] = (probes.FAIL, "HTTP 500")
    run(1)
    n = FNotify(FDelivery())                                                      # the transport is down this time
    assert monitors.notify_events(n.send, n, NOW + 61)["retry"] == 1
    n = FNotify(FDelivery(ok=True))
    assert monitors.notify_events(n.send, n, NOW + 125) == {**ZERO, "sent": 1}    # after the 60 s back-off
    assert n.sent[0].title == "Web is down" and n.sent[0].dedupe_key == "probe:web"
    assert monitors.notify_events(n.send, n, NOW + 130)["sent"] == 0              # exactly once
    run(2)                                                                        # still down: no repeat
    assert pending() == ([], [])


# =========================================================================== FIX 3: a delivery that dies loses nothing
def test_notify_events_delivers_the_most_urgent_first():
    queue({**UP, "id": "rec", "probe": "r"}, {**DOWN, "id": "w", "severity": "warn", "probe": "w", "t": NOW}, {**DOWN, "id": "c-old", "probe": "co", "t": NOW - 30},
          {**DOWN, "id": "c-new", "probe": "cn", "t": NOW - 1})
    n = FNotify()
    monitors.notify_events(n.send, n, NOW)
    assert [e.facts["Probe"] for e in n.sent] == ["cn", "co", "w", "r"]          # newest crit, older crit, warn, recovery


def test_notify_events_budget_defers_the_rest_untouched_instead_of_overrunning_the_job(monkeypatch):
    queue(*[{**DOWN, "probe": f"p{i}", "t": NOW - i} for i in range(6)])

    class Slow(FNotify):
        def send(self, ev):
            time.sleep(0.3)
            return super().send(ev)

    n = Slow()
    t0 = time.monotonic()
    out = monitors.notify_events(n.send, n, NOW, budget_s=1.0)
    assert time.monotonic() - t0 < 2.5 and out["sent"] >= 2 and out["deferred"] >= 1          # the pass stopped at its cap, it did not run 1.8 s
    assert out["sent"] + out["deferred"] + out["retry"] == 6                                  # every event is accounted for
    evs, inflight = pending()
    assert len(evs) == out["deferred"] and len(inflight) == out["retry"]                      # deferred = queued again, NOT claimed, NOT penalised
    assert all("nb" not in e and "tries" not in e for e in probes.load_state()["events"])
    n2 = FNotify()                                                                            # the next run (after any stale claim expires) finishes the job
    assert monitors.notify_events(n2.send, n2, NOW + probes.CLAIM_TTL_S + 1)["sent"] == out["deferred"] + out["retry"] and pending() == ([], [])


def test_notify_events_a_hung_transport_is_abandoned_and_its_event_stays_claimed_for_the_ttl(monkeypatch):
    queue(*[{**DOWN, "probe": f"p{i}", "t": NOW - i} for i in range(3)])
    gate = threading.Event()

    class Hung(FNotify):
        def send(self, ev):
            gate.wait(10)
            return FDelivery(ok=True)

    n = Hung()
    t0 = time.monotonic()
    out = monitors.notify_events(n.send, n, NOW, budget_s=0.3)
    assert time.monotonic() - t0 < 1.5 and out["retry"] == 1 and out["deferred"] == 2 and out["sent"] == 0
    evs, inflight = pending()
    assert len(evs) == 2 and len(inflight) == 1                                   # the possibly-sent one is claimed; nothing is gone
    assert len(probes.claim_events(NOW + probes.CLAIM_TTL_S + 1)) == 3            # after the TTL everything is deliverable again
    gate.set()


def test_a_delivery_process_killed_mid_send_loses_no_event(tmp_path):
    """The reviewer's experiment, for real: 6 queued events, a send that takes 2 s, SIGTERM after ~1 s. The old pop-then-send left
    0 events in the queue (about 4 pages never delivered or retried). Now all 6 are still on disk, claimed, and deliverable again."""
    queue(*[{**DOWN, "probe": f"p{i}", "dedupe_key": f"probe:p{i}", "t": NOW} for i in range(6)])
    code = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        from homelab_maint.tasks import monitors
        class D:
            ok, handled = True, False
        class N:
            class Event:
                def __init__(self, **kw): self.__dict__.update(kw)
            @staticmethod
            def send(ev):
                time.sleep(2)
                return D()
        print("go", flush=True)
        monitors.notify_events(N.send, N, %r)
    """) % (str(Path(__file__).resolve().parent.parent), NOW)
    env_ = {**os.environ, "HOMELAB_MAINT_STATE": str(core.STATE_DIR), "HOMELAB_MAINT_LOG": str(core.LOG_DIR),
            "HOMELAB_MAINT_RUN": str(core.RUN_DIR), "HOMELAB_MAINT_CONF": str(core.CONF_DIR)}
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env=env_, text=True)
    assert proc.stdout.readline().strip() == "go"
    time.sleep(0.9)                                                               # claimed, and the first send is still in flight
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGTERM
    evs, inflight = pending()
    assert len(evs) + len(inflight) == 6                                          # nothing was deleted before it was delivered
    assert probes.claim_events(NOW + 5) == []                                     # (a live deliverer's claim is respected for a while)
    again = probes.claim_events(NOW + probes.CLAIM_TTL_S + 1)
    assert sorted(e["probe"] for e in again) == [f"p{i}" for i in range(6)]      # then every one of them is delivered again
    n = FNotify()
    assert monitors.notify_events(n.send, n, NOW + probes.CLAIM_TTL_S + 2)["sent"] == 0       # (already claimed above: not double-delivered)
    probes.release_events([e["id"] for e in again], failed=False, now=NOW + probes.CLAIM_TTL_S + 2)
    assert monitors.notify_events(n.send, n, NOW + probes.CLAIM_TTL_S + 2)["sent"] == 6 and pending() == ([], [])


def test_the_default_delivery_budget_fits_inside_the_job_timeout_with_the_engine_budget():
    assert probes.DEFAULTS["budget_s"] + monitors.NOTIFY_BUDGET_S <= 90          # etc/jobs.toml probes-run timeout_s (the job TERMs at 90 s)


# =========================================================================== SPEC5 issue_key: ALL the probes that fail, by stable name
def probe_fp(res):
    from homelab_maint import acks
    fp = acks.fingerprint("probes", res, res.status)
    assert fp.mode == "explicit" and fp.ackable and res.issue_key
    return str(fp)


def test_probe_key_is_every_failing_probe_not_the_three_the_summary_clips():
    rows = [row(f"p{i}", "down", since=NOW - 600 * (i + 1)) for i in range(5)] + [row("d1", "warn"), row("f1", "up", flapping=True), row("ok1")]
    r = monitors.build_result(rows, rep(), NOW)
    assert r.issue_key == "degraded:d1;down:p0,p1,p2,p3,p4;flapping:f1" and "P0" not in r.summary           # five are down, the summary lists the three longest down
    later = [{**x, "since": x["since"] - 7200, "ms": 900, "avail_30d": 90.0} for x in rows]                  # ten minutes became two hours; slower, less available
    assert monitors.build_result(later, rep(), NOW).issue_key == r.issue_key
    assert probe_fp(monitors.build_result(later, rep(), NOW)) == probe_fp(r)
    other = [row(f"q{i}", "down", since=NOW - 600 * (i + 1)) if i == 0 else x for i, x in enumerate(rows)]    # a hidden one is another probe: same summary text
    r2 = monitors.build_result(other, rep(), NOW)
    assert r2.summary == r.summary and probe_fp(r2) != probe_fp(r)
    assert probe_fp(monitors.build_result(rows[:4] + rows[5:], rep(), NOW)) != probe_fp(r)                    # one recovered
    assert monitors.build_result([row("a"), row("b")], rep(), NOW).issue_key is None                         # all up


def test_probe_key_counts_bad_definitions_and_leaves_info_only_trouble_out():
    errs = ["probes.toml: probe x: bad interval", "probes.toml: probe y: bad type"]
    r = monitors.build_result([row("a"), row("i1", "down", severity="info")], rep(errors=errs[:1]), NOW)
    assert r.issue_key == "baddefs:1" and "info-only" in r.summary                                           # info-only probes never page
    assert monitors.build_result([row("a")], rep(errors=errs), NOW).issue_key == "baddefs:2"
    blind = monitors.build_result([], rep(errors=errs), NOW)
    assert blind.status == "error" and blind.issue_key is None                                               # MONITORING BLIND keeps its own (text, "|error") identity
