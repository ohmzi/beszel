"""Tests for tasks/self_health.py: the pipeline-health task (C0, check tier) and the public self.json.

Tmp dirs only, a fake HTTP server on 127.0.0.1, a scripted `sh` (docker and systemctl), a fake /proc: nothing real is asked or sent.
The clock is NOW (2023), far from the real time, so any use of time.time() in a decision shows up as a wrong verdict.
"""
import hashlib
import io
import json
import os
import socket
import subprocess
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core
from homelab_maint.core import Ctx
from homelab_maint.tasks import self_health as sh_mod

NOW = 1_700_000_000.0
H = 3600.0


# --------------------------------------------------------------------------- scripted host
class FakeSh:
    """`systemctl is-active <unit>`: the exit code follows systemd's (0 active, 3 inactive/failed, 4 = no such unit, which is-active
    prints as "inactive"). Every other command is 'not found'."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.active = "active"                                     # beszel-hub.service, by default running

    def __call__(self, cmd, timeout=60, **kw):
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            rc = 4 if self.active == "unknown" else 0 if self.active == "active" else 3
            out = "inactive" if self.active == "unknown" else self.active
            return subprocess.CompletedProcess(cmd, rc, out + "\n", "")
        return subprocess.CompletedProcess(cmd, 127, "", "not found")

    def service_calls(self):
        return [c for c in self.calls if c[:2] == ["systemctl", "is-active"]]


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Web:
    """A fake dashboard on 127.0.0.1: answers GET /api/health with whatever .code/.body/.delay say."""

    def __init__(self):
        outer = self
        self.code, self.body, self.delay, self.paths = 200, b'{"status":"ok"}', 0.0, []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.code)
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class World:
    """A fully deployed, healthy host at NOW. Tests break one thing and look at one row."""

    def __init__(self, tmp: Path, mp, web: Web):
        self.tmp, self.web, self.fake = tmp, web, FakeSh()
        for attr, name in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("RUN_DIR", "run"), ("CONF_DIR", "conf")):
            d = tmp / name
            d.mkdir()
            mp.setattr(core, attr, d)
        self.state, self.log, self.run, self.conf = core.STATE_DIR, core.LOG_DIR, core.RUN_DIR, core.CONF_DIR
        self.pub = self.state / "public"
        self.units = tmp / "units"                                    # stands in for /etc/systemd/system (read-only stats of unit files)
        self.units.mkdir()
        mp.setattr(sh_mod, "UNIT_DIR", self.units)
        self.opts = {"web_port": web.port}
        mp.setitem(sh_mod.DEFAULTS, "web_port", web.port)           # export() reads maint.toml options: never reach for the real :8088
        mp.setattr(sh_mod, "sh", self.fake)
        self.build()

    # -- builders
    def jwrite(self, path: Path, obj, mtime: float | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def status(self, check_age=300.0, daily_age=6 * H, weekly_age=2 * 86400.0, extra_tasks=None, **top):
        tr = {"check": {"last_run": NOW - check_age}}
        if daily_age is not None:
            tr["daily"] = {"last_run": NOW - daily_age}
        if weekly_age is not None:
            tr["weekly"] = {"last_run": NOW - weekly_age}
        doc = {"schema": 1, "generated_at": NOW - check_age, "overall": "ok", "paused": False, "tier_runs": tr,
               "tick": {"last_run": NOW - 30},
               "tasks": {"disk_forecast": {"status": "ok", "tier": "check", "klass": "C0", "last_run": NOW - check_age},
                         **(extra_tasks or {})}}
        doc.update(top)
        self.jwrite(self.state / "status.json", doc)

    def history(self, n_ok=200, n_err=0, task="disk_forecast", age_h=12.0):
        lines = []
        for i in range(n_ok + n_err):
            t = NOW - age_h * H * (i + 1) / (n_ok + n_err + 1)
            st = "error" if i < n_err else "ok"
            lines.append(json.dumps({"t": t, "kind": "task", "task": task, "status": st, "alert": True, "reclaimed": 0, "dur": 0.1,
                                     "metrics": {}}, separators=(",", ":")))
        (self.state / "history.jsonl").write_text("\n".join(lines) + "\n")

    def registry(self, synced_age=3600.0):
        rd = self.conf / "rules.d"
        rd.mkdir(exist_ok=True)
        files = {"00-baseline.toml": b'[meta]\ncategory = "safety"\n', "10-checks.toml": b'[[rule]]\nid = "check.disk"\n'}
        for n, data in files.items():
            (rd / n).write_bytes(data)
            os.utime(rd / n, (NOW - 2 * 86400, NOW - 2 * 86400))
        self.jwrite(self.state / "rules" / "current.json",
                    {"hash": "deadbeef", "synced_at": NOW - synced_age, "rules_count": 42,
                     "files": [{"name": n, "sha": hashlib.sha256(d).hexdigest()} for n, d in files.items()]})
        self.rd = rd

    def build(self):
        self.status()
        self.jwrite(self.run / "tick.json", {"t": NOW - 20, "ms": 12})
        self.jwrite(self.pub / "manifest.json", {"schema": 1, "generated_at": NOW - 30, "registry_hash": "deadbeef"})
        self.jwrite(self.pub / "overview.json", {"schema": 1, "generated_at": NOW - 300, "export_errors": []}, NOW - 30)
        self.jwrite(self.pub / "live.json", {"generated_at": NOW - 3})
        self.jwrite(self.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW - 20}})
        self.history()
        (self.state / "ack" / "inbox").mkdir(parents=True, exist_ok=True)
        self.registry()
        k = self.conf / "kuma.toml"
        k.write_text('[push]\n"tier-check" = "AbCdEf0123456789"\n"umbrella-probes" = "ZyXwVu9876543210"\n')
        os.chmod(k, 0o600)

    def unit(self, cid: str, age: float):
        """Install the systemd unit file of a part `age` seconds before NOW (what install.sh does; a stat is all self_health does)."""
        f = self.units / sh_mod.UNIT_FILES[cid]
        f.write_text("[Unit]\nDescription=x\n")
        os.utime(f, (NOW - age, NOW - age))

    def notify_state(self, where=None, **doc):
        d = where or self.state
        d.mkdir(parents=True, exist_ok=True)
        (d / "notify-state.json").write_text(json.dumps({"v": 1, "sent": [], "dedupe": {}, "esc": {}, **doc}))

    # -- running
    def assess(self, state=None, quick=False, now=NOW, mode="export", **over):
        o = {**self.opts, **over}
        st = {} if state is None else state
        return sh_mod.assess(now, lambda k, d=None: o.get(k, d), st, quick=quick, mode=mode)


@pytest.fixture
def web():
    w = Web()
    yield w
    w.close()


@pytest.fixture
def w(tmp_path, monkeypatch, web):
    return World(tmp_path, monkeypatch, web)


def row(rep, cid):
    return next(r for r in rep["rows"] if r["id"] == cid)


def effective_ok(doc, now):
    """The level a CONSUMER of self.json must show at `now` (the page rule)."""
    return sh_mod.effective(doc, now)["level"]


def ctx_for(w: World, now=NOW, **opts) -> Ctx:
    cfg = {"tasks": {"self_health": {**w.opts, **opts}}, "protected": {}}
    return Ctx(cfg, "self_health", False, now=now)


# =========================================================================== registration and the healthy baseline
def test_registered_as_c0_check_task():
    t = core.REGISTRY["self_health"]
    assert (t.klass, t.tier, t.name) == ("C0", "check", "self_health") and t.title and t.timeout >= 10


def test_cli_loader_discovers_the_module():
    from homelab_maint import cli
    cli.load_tasks()
    assert "self_health" in core.REGISTRY and not [n for n in cli.IMPORT_ERRORS if "self_health" in n]


def test_healthy_deployed_host_is_ok(w):
    rep = w.assess()
    assert rep["level"] == "ok" and rep["reasons"] == [] and rep["bad"] == []
    assert {r["id"]: r["state"] for r in rep["rows"]} == {c: "ok" for c in sh_mod.TITLES}
    assert [r["id"] for r in rep["rows"]] == [c for c, _ in sh_mod.COMPONENTS]       # fixed order


def test_task_result_ok_shape(w):
    res = sh_mod.run(ctx_for(w))
    assert res.status == "ok" and res.alert is True and res.summary.startswith("ok:")
    assert len(res.summary) <= 140 and res.summary.isascii()
    assert all(isinstance(v, (int, float, str, bool)) for v in res.metrics.values())
    assert len(res.items) <= 12 and res.metrics["level"] == "ok" and res.metrics["errors_24h"] == 0
    assert res.issue_key == ""


# =========================================================================== runner (check tier cadence)
@pytest.mark.parametrize("age,state", [(300, "ok"), (1470, "ok"), (1471, "degraded"), (2700, "degraded"), (2701, "down")])
def test_runner_thresholds_follow_the_check_interval(w, age, state):
    w.status(check_age=age)
    r = row(w.assess(), "runner")
    assert r["state"] == state
    if state != "ok":
        assert "check run" in r["reason"] and r["hint"] and r["age_s"] == age


def test_runner_down_makes_the_export_verdict_down(w):
    w.status(check_age=3 * H)
    for mode in ("export", "cli"):                                                        # independent evidence: the strict judgement
        rep = w.assess(mode=mode)
        assert rep["level"] == "down" and "runner stopped" in rep["reasons"][0] and "3.0 h" in rep["reasons"][0]
    doc = sh_mod.export(NOW)
    assert doc["level"] == "down" and doc["headline"].startswith("Monitoring pipeline: DOWN: runner stopped")


def test_a_down_part_makes_the_task_crit(w):
    w.history(n_ok=40, n_err=60)                                                           # 60 % of the task runs failed
    res = sh_mod.run(ctx_for(w))
    assert res.status == "crit" and res.summary.startswith("DOWN: 60% of task runs failed") and "Try: " in res.summary
    assert len(res.summary) <= 140 and res.issue_key == "errors"


def test_runner_interval_is_configurable(w):
    w.status(check_age=3000)
    assert row(w.assess(), "runner")["state"] == "down"
    assert row(w.assess(check_interval_s=3600), "runner")["state"] == "ok"
    assert row(w.assess(check_interval_s="junk", late_factor=True), "runner")["state"] == "down"        # wrong types: the defaults


def test_runner_falls_back_to_check_task_rows_without_tier_runs(w):
    w.status()
    doc = json.loads((w.state / "status.json").read_text())
    del doc["tier_runs"]
    doc["tasks"]["disk_forecast"]["last_run"] = NOW - 5000
    w.jwrite(w.state / "status.json", doc)
    assert row(w.assess(), "runner")["state"] == "down"                                   # older install: row times used
    doc["tasks"]["disk_forecast"]["last_run"] = NOW - 100
    w.jwrite(w.state / "status.json", doc)
    assert row(w.assess(), "runner")["state"] == "ok"


def test_runner_missing_status_is_first_run_then_a_fault(w):
    (w.state / "status.json").unlink()
    assert row(w.assess(), "runner")["state"] == "info"
    st = {"first_seen": NOW - 5000}
    assert row(w.assess(state=st), "runner")["state"] == "degraded"


def test_runner_corrupt_status_and_clock_in_the_future(w):
    (w.state / "status.json").write_text("{not json")
    assert row(w.assess(), "runner")["state"] == "degraded"
    w.status(check_age=-1000)                                                             # stamped 1000 s ahead
    r = row(w.assess(), "runner")
    assert r["state"] == "degraded" and "future" in r["detail"]


# =========================================================================== publish
def test_publish_age_from_manifest(w):
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW - 2000})
    r = row(w.assess(), "publish")
    assert r["state"] == "degraded" and "publishing stopped" in r["reason"]
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW - 60})
    assert row(w.assess(), "publish")["state"] == "ok"


def test_publish_mentions_when_the_website_numbers_are_old_but_publishing_runs(w):
    w.jwrite(w.pub / "overview.json", {"generated_at": NOW - 5000, "export_errors": []}, NOW - 20)      # runner stalled, publish alive
    rep = w.assess()
    r = row(rep, "publish")
    assert r["state"] == "ok" and "the numbers shown are 83 min old" in r["detail"] and rep["metrics"]["web_data_age_s"] == 5000


def test_publish_a_dead_publisher_is_degraded_never_down(w):
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW - 10 * 86400})
    assert row(w.assess(), "publish")["state"] == "degraded"


def test_publish_falls_back_to_overview_mtime_without_a_manifest(w):
    (w.pub / "manifest.json").unlink()
    assert row(w.assess(), "publish")["state"] == "ok"                                     # overview.json written 30 s ago
    os.utime(w.pub / "overview.json", (NOW - 5000, NOW - 5000))
    assert row(w.assess(), "publish")["state"] == "degraded"


def test_publish_ignores_a_manifest_stamped_in_the_future(w):
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW + 86400})
    assert row(w.assess(), "publish")["state"] == "ok"                                     # the overview mtime decides


def test_publish_reports_export_errors(w):
    w.jwrite(w.pub / "overview.json", {"generated_at": NOW, "export_errors": ["slo.json", "bad name/../x"]}, NOW - 30)
    r = row(w.assess(), "publish")
    assert r["state"] == "degraded" and "slo.json" in r["detail"] and "/" not in r["detail"]


def test_publish_not_deployed_vs_gone(w):
    import shutil
    shutil.rmtree(w.pub)
    st: dict = {}
    assert row(w.assess(state=st), "publish")["state"] == "info"                           # older install: never published
    w.build()
    assert row(w.assess(state=st), "publish")["state"] == "ok"                             # seen now
    shutil.rmtree(w.pub)
    assert row(w.assess(state=st), "publish")["state"] == "degraded"                       # it existed and is gone


def test_publish_empty_public_dir_waits_then_complains(w):
    for f in w.pub.iterdir():
        f.unlink()
    assert row(w.assess(), "publish")["state"] == "info"
    assert row(w.assess(state={"first_seen": NOW - 9000}), "publish")["state"] == "degraded"


# =========================================================================== tick, daily, weekly
@pytest.mark.parametrize("age,state", [(20, "ok"), (300, "ok"), (301, "degraded"), (3600, "degraded")])
def test_tick_heartbeat_age(w, age, state):
    w.jwrite(w.run / "tick.json", {"t": NOW - age})
    w.status(tick={"last_run": NOW - 99999})                                               # the stale persistent copies must not win
    r = row(w.assess(), "tick")
    assert r["state"] == state and (state == "ok" or "scheduler tick stopped" in r["reason"])


def test_tick_falls_back_to_status_and_sched_json(w):
    (w.run / "tick.json").unlink()                                                         # tmpfs after a reboot
    assert row(w.assess(), "tick")["state"] == "ok"                                        # status.json tick.last_run = 30 s ago
    w.status(tick={"last_run": NOW - 5000})
    w.jwrite(w.state / "sched.json", {"schema": 1, "jobs": {}, "meta": {}}, NOW - 100)
    assert row(w.assess(), "tick")["state"] == "ok"                                        # sched.json rewritten 100 s ago
    os.utime(w.state / "sched.json", (NOW - 5000, NOW - 5000))
    assert row(w.assess(), "tick")["state"] == "degraded"


def test_tick_not_installed_on_an_older_install_then_gone_once_seen(w):
    (w.run / "tick.json").unlink()
    w.status()
    doc = json.loads((w.state / "status.json").read_text())
    del doc["tick"]
    w.jwrite(w.state / "status.json", doc)
    st: dict = {}
    assert row(w.assess(state=st), "tick")["state"] == "info"
    w.jwrite(w.run / "tick.json", {"t": NOW - 5})
    assert row(w.assess(state=st), "tick")["state"] == "ok"
    (w.run / "tick.json").unlink()
    r = row(w.assess(state=st), "tick")
    assert r["state"] == "degraded" and "stopped" in r["reason"]


def test_tick_future_stamps_are_not_evidence(w):
    w.jwrite(w.run / "tick.json", {"t": NOW + 10 * H})
    w.status(tick={"last_run": NOW + 10 * H})
    r = row(w.assess(), "tick")
    assert r["state"] == "degraded" and "future" in r["detail"]                              # only future stamps: clock trouble, not freshness
    w.jwrite(w.state / "sched.json", {"schema": 1}, NOW - 50)
    assert row(w.assess(), "tick")["state"] == "ok"                                          # one honest stamp is enough


@pytest.mark.parametrize("tier,age,state", [("daily", 29 * H, "ok"), ("daily", 31 * H, "degraded"),
                                            ("weekly", 8 * 86400, "ok"), ("weekly", 10 * 86400, "degraded")])
def test_daily_and_weekly_vs_cadence(w, tier, age, state):
    w.status(**{f"{tier}_age": age})
    r = row(w.assess(), tier)
    assert r["state"] == state and (state == "ok" or "last ran" in r["reason"])


def test_tier_never_ran_is_quiet_on_a_young_install(w):
    w.status(weekly_age=None)
    assert row(w.assess(), "weekly")["state"] == "info"
    r = row(w.assess(state={"first_seen": NOW - 10 * 86400}), "weekly")
    assert r["state"] == "degraded" and "never run" in r["reason"]


def test_tier_falls_back_to_task_rows_without_tier_runs(w):
    w.status(extra_tasks={"apt_clean": {"status": "ok", "tier": "daily", "klass": "C1", "last_run": NOW - 40 * H},
                          "scheduler": {"status": "ok", "tier": "job", "klass": "J", "last_run": NOW}})
    doc = json.loads((w.state / "status.json").read_text())
    del doc["tier_runs"]["daily"]
    w.jwrite(w.state / "status.json", doc)
    assert row(w.assess(), "daily")["state"] == "degraded"


# =========================================================================== live and sensor ring
@pytest.mark.parametrize("age,state", [(3, "ok"), (90, "ok"), (91, "degraded"), (4000, "degraded")])
def test_live_json_age(w, age, state):
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - age})
    r = row(w.assess(), "live")
    assert r["state"] == state and (state == "ok" or "live monitor stopped" in r["reason"])


def test_live_not_deployed_gone_and_mtime_fallback(w):
    (w.pub / "live.json").unlink()
    st: dict = {}
    assert row(w.assess(state=st), "live")["state"] == "info"
    w.jwrite(w.pub / "live.json", {"no_stamp": 1}, NOW - 2)                                # no generated_at: the file time
    assert row(w.assess(state=st), "live")["state"] == "ok"
    (w.pub / "live.json").unlink()
    assert row(w.assess(state=st), "live")["state"] == "degraded"
    w.jwrite(w.pub / "live.json", {"generated_at": NOW + 3 * H})
    r = row(w.assess(state=st), "live")
    assert r["state"] == "degraded" and "future" in r["detail"]


@pytest.mark.parametrize("age,state", [(20, "ok"), (300, "ok"), (301, "degraded")])
def test_metrics_ring_age(w, age, state):
    w.jwrite(w.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW - age}})
    r = row(w.assess(), "metrics")
    assert r["state"] == state and (state == "ok" or "sensor sampler stopped" in r["reason"])


def test_metrics_ring_absent_info_then_gone(w):
    (w.state / "metrics-ring.json").unlink()
    st: dict = {}
    assert row(w.assess(state=st), "metrics")["state"] == "info"
    w.jwrite(w.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW - 5}})
    w.assess(state=st)
    (w.state / "metrics-ring.json").unlink()
    assert row(w.assess(state=st), "metrics")["state"] == "degraded"


# =========================================================================== registry in sync
def test_registry_absent_is_info_then_a_fault_if_it_disappears(w):
    import shutil
    shutil.rmtree(w.conf / "rules.d")
    shutil.rmtree(w.state / "rules")
    st: dict = {}
    assert row(w.assess(state=st), "registry")["state"] == "info"
    w.registry()
    assert row(w.assess(state=st), "registry")["state"] == "ok"
    shutil.rmtree(w.conf / "rules.d")
    shutil.rmtree(w.state / "rules")
    assert row(w.assess(state=st), "registry")["state"] == "degraded"


def test_registry_in_sync_reports_count_and_remembers_the_hash_scheme(w):
    st: dict = {}
    r = row(w.assess(state=st), "registry")
    assert r["state"] == "ok" and "42 rules" in r["detail"] and "not verified" not in r["detail"]


def test_registry_edit_inside_the_grace_is_pending_then_a_fault(w):
    f = w.rd / "10-checks.toml"
    f.write_bytes(b'[[rule]]\nid = "check.changed"\n')
    os.utime(f, (NOW - 100, NOW - 100))
    r = row(w.assess(), "registry")
    assert r["state"] == "info" and "applies it within a minute" in r["detail"]
    os.utime(f, (NOW - 2000, NOW - 2000))                                                  # edited 33 min ago, still not applied
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "10-checks.toml" in r["detail"] and r["hint"]


def test_registry_added_and_removed_files_are_out_of_sync(w):
    (w.rd / "20-new.toml").write_text("[meta]\n")
    os.utime(w.rd / "20-new.toml", (NOW - 3000, NOW - 3000))
    assert row(w.assess(), "registry")["state"] == "degraded"
    (w.rd / "20-new.toml").unlink()
    (w.rd / "00-baseline.toml").unlink()
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "00-baseline.toml" in r["detail"]


def test_registry_accepts_sha_prefixes_dict_form_and_other_algorithms(w):
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["files"] = {f"rules.d/{n}": hashlib.sha1((w.rd / n).read_bytes()).hexdigest()[:12] for n in ("00-baseline.toml", "10-checks.toml")}
    w.jwrite(w.state / "rules" / "current.json", cur)
    st: dict = {}
    assert row(w.assess(state=st), "registry")["state"] == "ok" and st["sha_alg"] == "sha1"


def test_registry_unrecognisable_hashes_are_unknown_never_in_sync(w):
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["files"] = [{"name": f["name"], "sha": "zz" * 8} for f in cur["files"]]
    w.jwrite(w.state / "rules" / "current.json", cur)
    rep = w.assess()
    r = row(rep, "registry")
    assert r["state"] == "unknown" and "not recognisable" in r["detail"] and rep["level"] == "degraded"    # was: "ok (content not verified)"


def test_registry_touch_without_content_change_is_not_a_fault(w):
    for p in w.rd.glob("*.toml"):
        os.utime(p, (NOW - 100, NOW - 100))                                                  # newer than the sync, same bytes
    assert row(w.assess(), "registry")["state"] == "ok"


def test_registry_never_synced(w):
    (w.state / "rules" / "current.json").unlink()
    st: dict = {}
    assert row(w.assess(state=st), "registry")["state"] == "info"                             # first sight: the tick has a minute
    r = row(w.assess(state=st, now=NOW + 1200), "registry")
    assert r["state"] == "degraded" and "never synced" in r["reason"]


def reject(w, content=b'[[rule]]\nid = "check.bad"\n', kind="invalid"):
    """What registry.py leaves behind for a refused edit: rules.d holds the bad content, current.json keeps the LAST GOOD file list and
    gets `invalid`, history.jsonl gets one valid:false record."""
    f = w.rd / "10-checks.toml"
    f.write_bytes(content)
    os.utime(f, (NOW - 50, NOW - 50))
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["invalid"] = {"hash": "badbad", "ts": NOW - 40, "kind": kind, "errors": ["a", "b"], "code": "x"}
    w.jwrite(w.state / "rules" / "current.json", cur)
    (w.state / "rules" / "history.jsonl").write_text(json.dumps({"ts": NOW - 40, "valid": kind != "invalid", "errors": ["a", "b"], "applied": False}) + "\n")


def test_registry_rejected_change_is_degraded_until_a_good_one(w):
    reject(w)
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "rejected" in r["detail"] and "2 error(s)" in r["detail"] and "last good config" in r["detail"]
    f = w.rd / "10-checks.toml"                                                              # the owner fixes it: the registry applies it
    f.write_bytes(b'[[rule]]\nid = "check.good"\n')
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["invalid"] = None
    cur["files"] = [{"name": n, "sha": hashlib.sha256((w.rd / n).read_bytes()).hexdigest()} for n in ("00-baseline.toml", "10-checks.toml")]
    w.jwrite(w.state / "rules" / "current.json", cur)
    assert row(w.assess(), "registry")["state"] == "ok"


def test_registry_blocked_change_is_named_blocked(w):
    reject(w, kind="blocked")
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "blocked" in r["detail"]


def test_registry_generated_file_edited_by_hand(w):
    g = w.conf / "maint.toml"
    g.write_text("# GENERATED from rules.d: edit the registry, not this file\n[global]\n")
    os.utime(g, (NOW - 100, NOW - 100))                                                     # synced 3600 s ago, edited after
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "maint.toml" in r["detail"]
    assert row(w.assess(registry_check_generated=False), "registry")["state"] == "ok"
    os.utime(g, (NOW - 7200, NOW - 7200))
    assert row(w.assess(), "registry")["state"] == "ok"                                      # written before the sync: fine
    (w.conf / "notify.toml").write_text("[routes]\n")                                         # no GENERATED header: the owner's file
    assert row(w.assess(), "registry")["state"] == "ok"


def test_registry_published_hash_differs(w):
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW - 30, "registry_hash": "oldhash"})
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "published registry" in r["detail"]


# =========================================================================== runner error rate
def test_error_rate_thresholds(w):
    w.history(n_ok=97, n_err=3)
    assert row(w.assess(), "errors")["state"] == "ok"                                         # 3 %
    w.history(n_ok=90, n_err=10)
    r = row(w.assess(), "errors")
    assert r["state"] == "degraded" and "10%" in r["reason"]
    w.history(n_ok=40, n_err=60)
    assert row(w.assess(), "errors")["state"] == "down"
    assert w.assess()["level"] == "down"


def test_error_rate_needs_enough_runs_and_ignores_old_records(w):
    w.history(n_ok=2, n_err=8)                                                                # 10 runs: not a rate
    assert row(w.assess(), "errors")["state"] == "ok"
    (w.state / "history.jsonl").write_text("\n".join(json.dumps({"t": NOW - 30 * H - i, "kind": "task", "task": "a", "status": "error"},
                                                              separators=(",", ":")) for i in range(300)) + "\n")     # all older than 24 h
    r = row(w.assess(), "errors")
    assert r["state"] == "ok" and "0 of 0" in r["detail"]


def test_tasks_in_error_now(w):
    err = {f"t{i}": {"status": "error", "tier": "check", "klass": "C0", "last_run": NOW - 100} for i in range(2)}
    w.status(extra_tasks=err)
    assert row(w.assess(), "errors")["state"] == "ok"                                         # two failing tasks page by themselves
    w.status(extra_tasks={**err, "t3": {"status": "error", "tier": "check", "klass": "C0", "last_run": NOW}})
    r = row(w.assess(), "errors")
    assert r["state"] == "degraded" and "3 tasks" in r["detail"]
    w.status(extra_tasks={"module_probes": {"status": "error", "tier": "check", "klass": "C0", "last_run": NOW}})
    r = row(w.assess(), "errors")
    assert r["state"] == "degraded" and "module_probes" in r["detail"] and "import" in r["reason"]


def test_history_regex_matches_what_core_appends(w):
    core.append_history({"t": NOW - 10, "kind": "task", "task": "x_task", "status": "error", "alert": True, "reclaimed": 0, "dur": 1.2,
                         "metrics": {"a": 1}})
    core.append_history({"t": NOW - 5, "kind": "disk", "mount": "/", "free": 1})
    (w.state / "history.jsonl").write_text((w.state / "history.jsonl").read_text())          # keep the appended lines
    h = sh_mod.history_errors(NOW)
    assert h["errors"] == 1 and h["by"] == {"x_task": 1}


def test_history_tail_window_is_bounded_and_survives_a_cut_first_line(w, monkeypatch):
    monkeypatch.setattr(sh_mod, "HIST_TAIL", 20_000)
    big = [json.dumps({"t": NOW - 100 - i, "kind": "task", "task": f"t{i}", "status": "error"}, separators=(",", ":")) for i in range(2000)]
    (w.state / "history.jsonl").write_text("\n".join(big) + "\n")
    h = sh_mod.history_errors(NOW)
    assert 0 < h["total"] < 2000 and h["total"] == h["errors"]                                # only the tail, no crash on the cut line
    assert sh_mod.history_errors(NOW, 1)["total"] == 0                                        # window respected
    (w.state / "history.jsonl").unlink()
    assert sh_mod.history_errors(NOW)["total"] == 0


def test_history_of_a_large_file_is_cheap(w):
    line = json.dumps({"t": NOW - 60, "kind": "sample", "c": {"x": "y" * 400}}, separators=(",", ":"))
    with open(w.state / "history.jsonl", "w") as f:
        for _ in range(20000):                                                                # ~9 MiB of spike samples
            f.write(line + "\n")
        for i in range(300):
            f.write(json.dumps({"t": NOW - 30, "kind": "task", "task": "a", "status": "ok"}, separators=(",", ":")) + "\n")
    t0 = time.perf_counter()
    h = sh_mod.history_errors(NOW)
    assert h["total"] == 300 and time.perf_counter() - t0 < 0.3


# =========================================================================== state dir: space, size, growth
def fake_vfs(free_b: int, total_b: int = 100 * 2 ** 30):
    return lambda p: SimpleNamespace(f_frsize=1, f_blocks=total_b, f_bavail=free_b)


@pytest.mark.parametrize("free_gib,state", [(50, "ok"), (1.5, "degraded"), (0.1, "down")])
def test_state_free_space(w, monkeypatch, free_gib, state):
    monkeypatch.setattr(sh_mod, "_statvfs", fake_vfs(int(free_gib * 2 ** 30)))
    r = row(w.assess(), "state")
    assert r["state"] == state
    assert (w.assess()["level"] == "down") == (state == "down")


def test_state_low_percent_is_degraded_even_with_gibs_free(w, monkeypatch):
    monkeypatch.setattr(sh_mod, "_statvfs", fake_vfs(4 * 2 ** 30, 100 * 2 ** 30 * 10))        # 4 GiB of 1 TiB = 0.4 %
    assert row(w.assess(), "state")["state"] == "degraded"


def test_state_size_and_growth(w):
    big = w.state / "big.bin"
    with open(big, "wb") as f:
        f.truncate(20 * 2 ** 20)                                                              # sparse 20 MiB
    assert row(w.assess(), "state")["state"] == "ok"
    assert row(w.assess(state_max_gib=0.011), "state")["state"] == "degraded"                  # 11 MiB limit
    st = {"size": [[NOW - 86400, 1000]]}
    r = row(w.assess(state=st, state_growth_warn_mib_day=1), "state")
    assert r["state"] == "degraded" and "growing fast" in r["reason"] and "MiB/day" in r["detail"]
    st = {"size": [[NOW - 3600, 1000]]}                                                        # only 1 h apart: no rate yet
    assert row(w.assess(state=st, state_growth_warn_mib_day=1), "state")["state"] == "ok"


def test_state_samples_are_kept_hourly_and_pruned(w):
    st: dict = {"size": [[NOW - 10 * 86400, 5], [NOW - 600, 7]]}
    w.assess(state=st)
    assert [s[0] for s in st["size"]] == [NOW - 10 * 86400, NOW - 600]                        # a sample 10 min ago blocks a new one (no append, no prune)
    st = {"size": [[NOW - 10 * 86400, 5], [NOW - 7200, 7]]}
    w.assess(state=st)
    assert [s[0] for s in st["size"]] == [NOW - 7200, int(NOW)]


def test_tree_size_budget_and_symlinks(tmp_path):
    (tmp_path / "a").mkdir()
    for i in range(300):
        (tmp_path / "a" / f"f{i}").write_bytes(b"x" * 10)
    os.symlink("/etc", tmp_path / "link")                                                      # never followed (only its own 4 bytes count)
    assert sh_mod.tree_size(tmp_path) == (3000 + len("/etc"), True)
    assert sh_mod.tree_size(tmp_path, max_entries=5)[1] is False
    assert sh_mod.tree_size(tmp_path, budget_s=-1)[1] is False                                  # out of time: incomplete, never trusted


def test_quick_mode_reuses_a_recent_size_and_error_rate(w, monkeypatch):
    st: dict = {}
    w.assess(state=st)                                                                         # a full run fills the caches
    assert st["hist"]["total"] == 200 and st["size"]
    monkeypatch.setattr(sh_mod, "tree_size", lambda *a, **k: pytest.fail("walked the state dir"))
    monkeypatch.setattr(sh_mod, "history_errors", lambda *a, **k: pytest.fail("re-read the history"))
    assert w.assess(state=st, quick=True, now=NOW + 60)["level"] == "ok"
    with pytest.raises(pytest.fail.Exception):
        w.assess(state=st, quick=True, now=NOW + 2 * 3600)                                     # cache expired: it reads again


# =========================================================================== acknowledge inbox
def test_inbox_backlog(w):
    inbox = w.state / "ack" / "inbox"
    assert row(w.assess(), "acks")["state"] == "ok"
    for i in range(3):
        (inbox / f"{i}-abc.json").write_text("{}")
        os.utime(inbox / f"{i}-abc.json", (NOW - 30, NOW - 30))
    assert row(w.assess(), "acks")["state"] == "ok"
    os.utime(inbox / "0-abc.json", (NOW - 700, NOW - 700))
    r = row(w.assess(), "acks")
    assert r["state"] == "degraded" and "3 acknowledgement" in r["reason"] and "not processing" in r["reason"]
    os.utime(inbox / "0-abc.json", (NOW - 30, NOW - 30))
    for i in range(30):
        (inbox / f"x{i}.json").write_text("{}")
        os.utime(inbox / f"x{i}.json", (NOW - 5, NOW - 5))
    assert row(w.assess(), "acks")["state"] == "degraded"                                      # count over the cap


def test_inbox_rejected_dir_is_counted_not_judged(w):
    rej = w.state / "ack" / "inbox" / "rejected"
    rej.mkdir()
    (rej / "a.json").write_text("{}")
    r = row(w.assess(), "acks")
    assert r["state"] == "ok" and "1 rejected" in r["detail"]


def test_inbox_absent_then_gone(w):
    import shutil
    shutil.rmtree(w.state / "ack")
    st: dict = {}
    assert row(w.assess(state=st), "acks")["state"] == "info"
    (w.state / "ack" / "inbox").mkdir(parents=True)
    assert row(w.assess(state=st), "acks")["state"] == "ok"
    shutil.rmtree(w.state / "ack")
    assert row(w.assess(state=st), "acks")["state"] == "degraded"


# =========================================================================== website: /api/health + beszel-hub.service
def test_website_healthy(w):
    r = row(w.assess(), "website")
    assert r["state"] == "ok" and "200" in r["detail"] and "beszel-hub.service active" in r["detail"]
    assert w.web.paths == ["/api/health"] and w.fake.service_calls()[-1][-1] == "beszel-hub.service"


def test_website_service_is_asked_as_one_argv_element_never_a_shell(w):
    w.assess(web_service="bad name; rm -rf /")
    assert w.fake.service_calls()[-1] == ["systemctl", "is-active", "beszel-hub.service"]     # a bad name falls back, never reaches a shell


def test_website_says_it_is_unhealthy(w):
    w.web.code, w.web.body = 503, b'{"ok":false,"reason":"overview.json is 50 min old \xc3\xa9"}'
    r = row(w.assess(), "website")
    assert r["state"] == "degraded" and "503" in r["reason"] and "overview.json is 50 min old" in r["reason"] and r["reason"].isascii()
    w.web.code, w.web.body = 404, b"nope"
    assert "404" in row(w.assess(), "website")["reason"]


def test_website_not_deployed_is_info_not_an_error(w):
    w.opts["web_port"] = closed_port()
    w.fake.active = "unknown"                                       # is-active prints "inactive" (rc 4) for a unit that does not exist
    rep = w.assess()
    r = row(rep, "website")
    assert r["state"] == "info" and "not deployed" in r["detail"] and rep["level"] == "ok" and rep["metrics"]["web"] == "not deployed"


def test_website_gone_after_it_was_seen(w):
    st: dict = {}
    assert row(w.assess(state=st), "website")["state"] == "ok"
    w.opts["web_port"] = closed_port()
    w.fake.active = "unknown"
    r = row(w.assess(state=st), "website")
    assert r["state"] == "degraded" and "disappeared" in r["reason"]


def test_website_service_states_without_an_answer(w):
    w.opts["web_port"] = closed_port()
    w.fake.active = "failed"
    assert "is failed" in row(w.assess(), "website")["reason"]
    w.fake.active = "inactive"
    assert "is inactive" in row(w.assess(), "website")["reason"]
    w.fake.active = "activating"
    r = row(w.assess(), "website")
    assert r["state"] == "info" and "is starting" in r["detail"]


def test_website_answering_while_the_unit_is_down_is_a_fault(w):
    w.fake.active = "inactive"                                      # a stray server on the port: /api/health answers but the unit is not running
    r = row(w.assess(), "website")
    assert r["state"] == "degraded" and "website answers but beszel-hub.service is inactive" in r["reason"]


def test_website_service_up_but_no_answer_is_a_fault(w):
    w.opts["web_port"] = closed_port()
    r = row(w.assess(), "website")
    assert r["state"] == "degraded" and "website service runs but /api/health does not answer (refused)" in r["reason"]


def test_website_unreachable_without_a_unit_after_it_was_seen(w):
    st: dict = {}
    w.assess(state=st)
    w.opts["web_port"] = closed_port()
    w.fake.active = "unknown"
    r = row(w.assess(state=st), "website")
    assert r["state"] == "degraded" and "disappeared" in r["reason"]


def test_website_hang_is_bounded_by_the_timeout(w):
    w.web.delay = 1.2
    t0 = time.perf_counter()
    r = row(w.assess(web_timeout_s=0.3), "website")
    assert time.perf_counter() - t0 < 1.1
    assert r["state"] == "degraded" and "timeout" in r["reason"]                                    # something listens: it is not 'not deployed'


def test_website_only_ever_talks_to_loopback_and_caps_the_timeout(w, monkeypatch):
    seen = []

    def fake_get(host, port, path, timeout):
        seen.append((host, port, path, timeout))
        return 200, b"{}", "", 1
    monkeypatch.setattr(sh_mod, "http_get", fake_get)
    w.assess(web_host="evil.example.com", web_timeout_s=60, web_port=8123)
    assert seen == [("127.0.0.1", 8123, "/api/health", 3.0)]                                        # loopback only, the fixed path, the timeout capped
    seen.clear()
    w.assess(web_host="localhost")
    assert seen[0][0] == "localhost" and seen[0][2] == "/api/health"


def test_website_path_and_service_fall_back_when_malformed(w, monkeypatch):
    seen = []
    monkeypatch.setattr(sh_mod, "http_get", lambda host, port, path, timeout: seen.append((host, port, path)) or (200, b"{}", "", 1))
    w.assess(web_path="not-a-path", web_service="a b")
    assert seen == [("127.0.0.1", w.web.port, "/api/health")] and w.fake.service_calls()[-1][-1] == "beszel-hub.service"


def test_website_check_can_be_switched_off(w):
    r = row(w.assess(web_check=False), "website")
    assert r["state"] == "info" and w.web.paths == [] and w.fake.calls == []


# =========================================================================== Kuma heartbeat config
def test_kuma_configured_ok_and_never_leaks_tokens(w):
    rep = w.assess()
    r = row(rep, "kuma")
    assert r["state"] == "ok" and "2/2" in r["detail"]
    blob = json.dumps(sh_mod.public(rep, NOW)) + sh_mod.summary(rep)
    assert "AbCdEf0123456789" not in blob and "ZyXwVu9876543210" not in blob


def test_kuma_absent_is_optional_unless_required(w):
    (w.conf / "kuma.toml").unlink()
    assert row(w.assess(), "kuma")["state"] == "info"
    assert row(w.assess(kuma_required=True), "kuma")["state"] == "degraded"


def test_kuma_missing_key_malformed_token_and_bad_toml(w):
    k = w.conf / "kuma.toml"
    k.write_text('[push]\n"tier-check" = "AbCdEf0123456789"\n')
    r = row(w.assess(), "kuma")
    assert r["state"] == "info" and "umbrella-probes" in r["detail"]
    assert row(w.assess(kuma_required=True), "kuma")["state"] == "degraded"
    k.write_text('[push]\n"tier-check" = "AbCdEf0123456789"\n"umbrella-probes" = "bad token!"\n')
    r = row(w.assess(), "kuma")
    assert r["state"] == "degraded" and "malformed" in r["detail"] and "bad token" not in json.dumps(r)
    k.write_text("[push\nbroken")
    r = row(w.assess(), "kuma")
    assert r["state"] == "degraded" and "not valid TOML" in r["detail"]


def test_kuma_file_permissions_are_noted(w):
    os.chmod(w.conf / "kuma.toml", 0o644)
    r = row(w.assess(), "kuma")
    assert r["state"] == "ok" and "chmod 600" in r["detail"]


def test_kuma_oversized_file_is_refused(w):
    (w.conf / "kuma.toml").write_text("# " + "x" * 70000)
    assert row(w.assess(), "kuma")["state"] == "degraded"


def test_kuma_keys_option_is_validated(w):
    assert row(w.assess(kuma_keys=["tier-check"]), "kuma")["detail"].startswith("1/1")
    assert row(w.assess(kuma_keys=["bad key", 5]), "kuma")["detail"].startswith("2/2")             # invalid list: the defaults


# =========================================================================== the verdict, summary and `since`
def test_level_is_the_worst_row_and_reasons_are_ordered_worst_first(w):
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})                                      # degraded
    w.status(check_age=4000)                                                                        # down (and runner is first in order)
    w.jwrite(w.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW - 4000}})                    # degraded
    rep = w.assess()
    assert rep["level"] == "down"
    assert [r["id"] for r in rep["bad"]] == ["runner", "live", "metrics"]
    assert rep["reasons"][0].startswith("runner stopped")
    assert sh_mod.headline(rep).startswith("Monitoring pipeline: DOWN: runner stopped")


def test_degraded_headline_and_status_mapping(w):
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    rep = w.assess()
    assert rep["level"] == "degraded" and sh_mod.headline(rep) == "Monitoring pipeline: degraded: live monitor stopped 15 min ago"
    res = sh_mod.run(ctx_for(w))
    assert res.status == "warn" and res.alert is True and res.summary.startswith("DEGRADED: live monitor stopped 15 min ago")
    assert "Try: systemctl status homelab-maint-live" in res.summary and res.issue_key == "live"
    assert res.items[0]["component"] == "Live monitor" and res.items[0]["state"] == "degraded" and res.items[0]["hint"]


def test_summary_stays_within_140_ascii_chars_with_many_problems(w):
    w.status(check_age=9000)
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    w.jwrite(w.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW - 4000}})
    w.jwrite(w.run / "tick.json", {"t": NOW - 9999})
    rep = w.assess()
    s = sh_mod.summary(rep)
    assert len(s) <= 140 and s.isascii() and s.startswith("DOWN: runner stopped") and "(+" in s


def test_summary_mentions_parts_that_are_not_deployed(w):
    import shutil
    shutil.rmtree(w.pub)
    (w.run / "tick.json").unlink()
    rep = w.assess()
    s = sh_mod.summary(rep)
    assert rep["level"] == "ok" and "not deployed" in s and rep["reasons"] == []


def test_since_is_kept_while_the_level_holds_and_reset_when_it_changes(w):
    st: dict = {}
    w.assess(state=st, now=NOW)
    assert st["level"] == "ok" and st["since"] == NOW
    w.assess(state=st, now=NOW + 60)
    assert st["since"] == NOW                                                                         # still ok: unchanged
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    rep = w.assess(state=st, now=NOW + 120)
    assert rep["level"] == "degraded" and rep["since"] == NOW + 120
    w.assess(state=st, now=NOW + 180)
    assert st["since"] == NOW + 120
    st["since"] = NOW + 10 ** 6                                                                       # a stamp from the future (clock stepped back)
    assert w.assess(state=st, now=NOW + 240)["since"] == NOW + 240


def test_first_seen_is_reset_when_the_clock_stepped_back(w):
    st = {"first_seen": NOW + 10 ** 6}
    w.assess(state=st)
    assert st["first_seen"] == NOW


def test_a_row_that_crashes_is_unknown_and_degrades_the_verdict(w, monkeypatch):
    def boom(cx):
        raise PermissionError("denied: /secret/path")
    monkeypatch.setattr(sh_mod, "COMPONENTS", tuple((c, boom if c == "live" else f) for c, f in sh_mod.COMPONENTS))
    rep = w.assess()
    r = row(rep, "live")
    assert r["state"] == "unknown" and "PermissionError" in r["detail"] and rep["level"] == "degraded"
    assert row(rep, "runner")["state"] == "ok"                                                         # the others still ran
    assert sh_mod.run(ctx_for(w)).status == "warn"


def test_the_task_never_raises(w, monkeypatch):
    monkeypatch.setattr(sh_mod, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug in assess")))
    res = sh_mod.run(ctx_for(w))
    assert res.status == "warn" and "could not run" in res.summary and res.summary.isascii() and len(res.summary) <= 140


def test_a_hostile_status_file_cannot_break_it(w):
    w.jwrite(w.state / "status.json", {"generated_at": "x", "tier_runs": [1, 2], "tasks": {"a": 5, "b": {"status": ["error"], "tier": {}}},
                                      "tick": "no"})
    rep = w.assess()
    assert rep["level"] in ("degraded", "down", "ok") and rep["rows"]
    w.jwrite(w.state / "status.json", [1, 2, 3])
    assert row(w.assess(), "runner")["state"] == "degraded"
    (w.state / "status.json").write_bytes(b"\xff\xfe\x00")
    assert row(w.assess(), "runner")["state"] == "degraded"


# =========================================================================== C0 purity, cost, the clock
def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def test_run_is_read_only_except_its_own_state_file(w, monkeypatch):
    monkeypatch.setattr(Ctx, "act", lambda *a, **k: pytest.fail("a C0 task must never call act"))
    before = {n: tree(d) for n, d in (("state", w.state), ("conf", w.conf), ("log", w.log), ("run", w.run))}
    ctx = ctx_for(w)
    ctx.apply = True                                                                                   # even if somebody forces it
    sh_mod.run(ctx)
    ctx.save_state()
    after = {n: tree(d) for n, d in (("state", w.state), ("conf", w.conf), ("log", w.log), ("run", w.run))}
    assert after["conf"] == before["conf"] and after["log"] == before["log"] and after["run"] == before["run"]
    changed = {k for k in set(after["state"]) | set(before["state"]) if after["state"].get(k) != before["state"].get(k)}
    assert changed == {"tasks/self_health.json"}
    assert oct(os.stat(w.state / "tasks" / "self_health.json").st_mode & 0o777) == "0o600"


def test_run_stays_under_300_ms_on_a_deployed_host(w):
    sh_mod.run(ctx_for(w))                                                                             # warm imports and the thread pool
    t0 = time.perf_counter()
    res = sh_mod.run(ctx_for(w))
    dt = time.perf_counter() - t0
    assert res.status == "ok" and dt < 0.3, dt


def test_the_clock_is_only_ever_now(w):
    """The world is at NOW (2023); the real clock is years later. Any use of time.time() would make everything stale."""
    assert abs(time.time() - NOW) > 86400 * 365
    assert w.assess()["level"] == "ok"
    assert w.assess(now=NOW + 5 * H)["level"] == "down"                                               # five hours later nothing was refreshed


# =========================================================================== export / self.json
def test_export_document_shape_and_size(w):
    doc = sh_mod.export(NOW)
    assert set(doc) == {"schema", "generated_at", "valid_until", "level", "headline", "verdict", "ttl", "limits", "checks", "metrics"}
    assert doc["schema"] == 2 and doc["generated_at"] == NOW and doc["level"] == "ok"
    assert doc["verdict"] == {"level": "ok", "reasons": [], "since": NOW}
    assert doc["ttl"] == {"refresh_s": 60, "degraded_after_s": 180, "down_after_s": 600} and doc["valid_until"] == NOW + 180     # FILE freshness
    assert doc["limits"] == {"runner_late_s": 1470, "runner_down_s": 2700}                                                      # the runner's own limits
    assert [c["id"] for c in doc["checks"]] == [c for c, _ in sh_mod.COMPONENTS]
    assert all({"id", "title", "state", "detail"} <= set(c) for c in doc["checks"])
    assert len(json.dumps(doc)) < 8000 and doc["metrics"]["level"] == "ok"


def test_export_carries_reasons_and_hints_when_degraded(w):
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    doc = sh_mod.export(NOW)
    assert doc["level"] == "degraded" and doc["verdict"]["reasons"] == ["live monitor stopped 15 min ago"]
    live = next(c for c in doc["checks"] if c["id"] == "live")
    assert live["state"] == "degraded" and live["hint"].startswith("systemctl status")


def test_export_survives_publish_scrubbing_unchanged(w):
    publish = pytest.importorskip("homelab_maint.publish")
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    w.opts["web_port"] = closed_port()
    doc = sh_mod.export(NOW)
    scrubbed = publish._public_doc(json.loads(json.dumps(doc)), NOW)
    assert scrubbed == json.loads(json.dumps(doc))                                                     # nothing was redacted or cut


def test_export_of_a_worst_case_document_survives_publish_scrubbing_unchanged(w):
    """Every new row (alerts, kuma, unit evidence, registry rejection) with its hints must pass publish's redactor untouched."""
    publish = pytest.importorskip("homelab_maint.publish")
    reject(w)
    w.notify_state(outbox=[box_entry(7 * H)], breaker={"until": NOW + 100, "n": 4, "why": "smtp: connection refused"})
    for i in range(3):
        sh_mod.note_kuma("tier-check", False, "curl exit 7", now=NOW - 10 + i)
    (w.pub / "live.json").unlink()
    w.unit("live", 2 * H)
    w.status(check_age=3 * H)
    w.opts["web_port"] = closed_port()
    doc = sh_mod.export(NOW)
    assert doc["level"] == "down" and {c["id"] for c in doc["checks"] if c["state"] != "ok"} >= {"runner", "alerts", "kuma", "registry", "live"}
    rt = json.loads(json.dumps(doc))
    assert publish._public_doc(rt, NOW) == rt


def test_export_persist_flag(w):
    p = w.state / "tasks" / "self_health.json"
    sh_mod.export(NOW, persist=False)
    assert not p.exists()
    sh_mod.export(NOW)
    st = json.loads(p.read_text())
    assert st["level"] == "ok" and st["since"] == NOW and "first_seen" in st and oct(p.stat().st_mode & 0o777) == "0o600"
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    assert sh_mod.export(NOW + 60)["verdict"]["since"] == NOW + 60
    assert sh_mod.export(NOW + 90)["verdict"]["since"] == NOW + 60                                     # persisted between exports


def test_export_and_task_share_the_since_state(w):
    ctx = ctx_for(w)
    sh_mod.run(ctx)
    ctx.save_state()
    assert sh_mod.export(NOW + 30)["verdict"]["since"] == NOW                                          # the task's ok-since carries over


def test_export_of_a_crashing_self_check_says_so_instead_of_staying_ok(w, monkeypatch):
    monkeypatch.setattr(sh_mod, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    doc = sh_mod.export(NOW)
    assert doc["level"] == "degraded" and doc["verdict"]["reasons"] == ["the self-check could not run: RuntimeError"]
    assert doc["generated_at"] == NOW and doc["checks"] == [] and doc["ttl"]["down_after_s"] == 600 and doc["valid_until"] == NOW + 180
    assert doc["limits"]["runner_down_s"] == 2700 and effective_ok(doc, NOW + 700) == "down"                                     # same page rule


def test_export_with_a_broken_maint_toml_still_works(w):
    (w.conf / "maint.toml").write_text("[tasks.self_health\nbroken")
    assert sh_mod.export(NOW)["level"] == "ok"


def test_export_reads_options_from_maint_toml(w):
    (w.conf / "maint.toml").write_text("[tasks.self_health]\ncheck_interval_s = 3600\n")
    doc = sh_mod.export(NOW)
    assert doc["limits"]["runner_down_s"] == 10800 and doc["ttl"]["down_after_s"] == 600           # the runner limits move, the file ttl does not
    (w.conf / "maint.toml").write_text("[tasks.self_health]\nrefresh_s = 120\nstale_factor = 2.5\n")
    assert sh_mod.export(NOW)["ttl"] == {"refresh_s": 120, "degraded_after_s": 300, "down_after_s": 1200}
    (w.conf / "maint.toml").write_text("[tasks.self_health]\nrefresh_s = 3000\n")                  # absurd: capped, the file cannot turn the rule off
    assert sh_mod.export(NOW)["ttl"]["degraded_after_s"] == 900 and sh_mod.export(NOW)["ttl"]["down_after_s"] == 3600


def test_write_export_only_into_an_existing_public_dir(w):
    import shutil
    assert sh_mod.write_export(NOW) is True
    f = w.pub / "self.json"
    assert json.loads(f.read_text())["level"] == "ok" and oct(f.stat().st_mode & 0o777) == "0o644"
    assert not list(w.pub.glob("*.tmp"))
    shutil.rmtree(w.pub)
    assert sh_mod.write_export(NOW) is False and not w.pub.exists()                                    # never creates public/


# =========================================================================== CLI
def test_cli_table_json_and_exit_codes(w, capsys):
    assert sh_mod.main(["--now", str(NOW)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Monitoring pipeline: healthy") and "Runner (check tier)" in out
    assert sh_mod.main(["--json", "--now", str(NOW)]) == 0
    assert json.loads(capsys.readouterr().out)["level"] == "ok"
    assert sh_mod.main(["--check", "--now", str(NOW)]) == 0
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    assert sh_mod.main(["--check", "--now", str(NOW)]) == 1
    assert sh_mod.main(["--check", "--now", str(NOW + 6 * H)]) == 2
    assert "try:" in capsys.readouterr().out


def test_cli_write_persists_and_publishes(w):
    buf = io.StringIO()
    with redirect_stdout(buf):
        sh_mod.main(["--write", "--now", str(NOW)])
    assert (w.pub / "self.json").exists() and (w.state / "tasks" / "self_health.json").exists()


# =========================================================================== integration seams: the runner core and the publisher
def test_runs_under_the_runner_core_with_its_state_and_alarm(w, monkeypatch):
    """core.run_task = Ctx + SIGALRM + save_state, exactly what cmd_run does for every task (clock pinned to the world)."""
    monkeypatch.setattr(time, "time", lambda: NOW)
    cfg = {"global": {}, "tasks": {"self_health": dict(w.opts)}, "caps": {}, "protected": {"patterns": [".*"]}}
    res, dur = core.run_task(core.REGISTRY["self_health"], cfg, apply=True)                 # apply=True is forced off for a C0 task
    assert res.status == "ok" and dur < 1.0 and res.summary.startswith("ok: pipeline healthy")
    st = json.loads((w.state / "tasks" / "self_health.json").read_text())
    assert st["level"] == "ok" and st["first_seen"] == NOW and set(st["seen"]) >= {"publish", "tick", "live", "metrics", "registry", "acks", "website"}
    w.history(n_ok=40, n_err=60)                                                             # a real "down" part reaches the runner core as crit
    res, _ = core.run_task(core.REGISTRY["self_health"], cfg, apply=False)
    assert res.status == "crit" and res.summary.startswith("DOWN: 60% of task runs failed")
    assert json.loads((w.state / "tasks" / "self_health.json").read_text())["level"] == "down"
    w.history()
    w.status(check_age=4000)                                                                 # runner gap: the run itself is the proof of life
    res, _ = core.run_task(core.REGISTRY["self_health"], cfg, apply=False)
    assert res.status == "ok" and "gap" in res.summary


def test_the_publish_glue_writes_self_json(w, monkeypatch):
    """The exact glue proposed for publish.py (an OPTIONAL_SOURCES entry and a BUILDERS row) produces public/self.json."""
    publish = pytest.importorskip("homelab_maint.publish")
    if not all(hasattr(publish, a) for a in ("OPTIONAL_SOURCES", "BUILDERS", "_optional_builder", "_trim_lists")):
        pytest.skip("publish.py internals changed")
    monkeypatch.setattr(publish, "OPTIONAL_SOURCES", {**publish.OPTIONAL_SOURCES, "self.json": (("tasks.self_health", "export"),)})
    monkeypatch.setattr(publish, "BUILDERS", [("self.json", publish._optional_builder("self.json", publish._trim_lists(("checks",))))])
    monkeypatch.setattr(publish, "_list_timers", lambda: None, raising=False)
    status = json.loads((w.state / "status.json").read_text())
    assert "self.json" in publish.publish(status, NOW)                                      # (other streams' builders may write more files)
    doc = json.loads((w.pub / "self.json").read_text())
    assert doc["schema"] == 2 and doc["level"] == "ok" and doc["verdict"]["since"] == NOW and len(doc["checks"]) == len(sh_mod.COMPONENTS) == 14
    assert oct((w.pub / "self.json").stat().st_mode & 0o777) == "0o644"


# =========================================================================== REVIEW FIXES (independent review, verdict "unsafe")
# 1 critical  nothing refreshed self.json: a dead runner left `level: ok` in the file
# 2 high      "never ran since install" read healthy in the CLI and doctor
# 3 medium    no alert-delivery row (dead transport = blind monitoring that read ok)
# 4 medium    inside the check run the runner/publish rows were tautological (false DOWN after every long outage)
# 5 medium    registry row: a revert stayed "rejected", dotfiles were "not applied", an edited sole file read ok
# 6 medium    "never seen" was "not deployed" even when the part's unit is installed
def selfjson(level="ok", age=0.0, **kw):
    """A self.json as the refresher writes it, `age` seconds old at NOW."""
    return {"schema": 2, "generated_at": NOW - age, "level": level, "headline": "x",
            "verdict": {"level": level, "reasons": kw.pop("reasons", []), "since": NOW - 5 * H},
            "ttl": kw.pop("ttl", {"refresh_s": 60, "degraded_after_s": 180, "down_after_s": 600}), "checks": [], "metrics": {}, **kw}


# --------------------------------------------------------------------------- 1. the file ages by itself; a refresher keeps it honest
def test_self_json_ttl_is_the_files_freshness_not_the_runners_lateness(w):
    doc = sh_mod.export(NOW)
    assert doc["ttl"]["degraded_after_s"] == 180 and doc["ttl"]["down_after_s"] == 600 and doc["ttl"]["refresh_s"] == 60
    assert doc["limits"] == {"runner_late_s": 1470, "runner_down_s": 2700}
    assert doc["ttl"]["degraded_after_s"] < doc["limits"]["runner_late_s"]                      # a stale ok is NOT tolerated for 24.5 min


@pytest.mark.parametrize("age,level,stale", [(0, "ok", False), (60, "ok", False), (180, "ok", False), (181, "degraded", True),
                                             (600, "degraded", True), (601, "down", True), (86400, "down", True)])
def test_page_rule_a_self_ok_file_with_an_old_generated_at_renders_degraded_or_down(age, level, stale):
    eff = sh_mod.effective(selfjson("ok", age), NOW)
    assert eff["level"] == level and eff["stale"] is stale and eff["age_s"] == age
    if stale:
        assert "self-health data is" in eff["reasons"][0] and eff["headline"].startswith(f"Monitoring pipeline: {'DOWN' if level == 'down' else 'degraded'}: self-health data is")
    else:
        assert eff["headline"] == "Monitoring pipeline: healthy" and eff["reasons"] == []


def test_page_rule_keeps_the_files_own_verdict_and_reasons_when_it_is_worse(w):
    eff = sh_mod.effective(selfjson("down", 10, reasons=["runner stopped: no check run for 3.0 h"]), NOW)
    assert eff["level"] == "down" and eff["reasons"] == ["runner stopped: no check run for 3.0 h"] and not eff["stale"]
    eff = sh_mod.effective(selfjson("degraded", 200, reasons=["live monitor stopped 15 min ago"]), NOW)
    assert eff["level"] == "degraded" and eff["stale"] and eff["reasons"][1] == "live monitor stopped 15 min ago" and "refresher stopped" in eff["reasons"][0]
    assert sh_mod.effective(selfjson("down", 200), NOW)["level"] == "down"                         # stale never improves a verdict


@pytest.mark.parametrize("doc", [None, [], "x", {}, {"level": "ok"}, {"generated_at": "now", "level": "ok"}, {"generated_at": True}])
def test_page_rule_missing_or_unreadable_file_is_degraded_never_healthy(doc):
    eff = sh_mod.effective(doc, NOW)
    assert eff["level"] == "degraded" and eff["stale"] and "homelab-maint-selfhealth.timer" in eff["reasons"][0] and eff["age_s"] is None


def test_page_rule_a_file_from_the_future_is_clock_trouble_not_freshness():
    eff = sh_mod.effective(selfjson("ok", -3 * H), NOW)
    assert eff["level"] == "degraded" and "future" in eff["reasons"][0]


@pytest.mark.parametrize("ttl", [{"degraded_after_s": 10 ** 9, "down_after_s": 10 ** 10}, {"degraded_after_s": "x"}, {"degraded_after_s": -5, "down_after_s": 0},
                                 {}, "junk", None, {"degraded_after_s": True}])
def test_page_rule_a_tampered_or_missing_ttl_cannot_switch_the_rule_off(ttl):
    doc = selfjson("ok", 2 * H)
    doc["ttl"] = ttl
    assert sh_mod.effective(doc, NOW)["level"] == "down"                                           # capped / defaulted: two hours old is down
    doc = selfjson("ok", 1000)
    doc["ttl"] = ttl
    assert sh_mod.effective(doc, NOW)["level"] in ("degraded", "down")                             # 1000 s: beyond the 900 s cap whatever the file says


def test_runner_dead_only_the_refresher_alive_the_file_flips_to_down_within_one_refresh(w):
    """The runner last ran at NOW-300 and never runs again. Nothing but the 1-minute refresher ticks; it must rewrite self.json so
    that the verdict flips on the very next tick, and the page must never show a stale ok."""
    f = w.pub / "self.json"
    seen = []
    for dt, level in [(0, "ok"), (60, "ok"), (1140, "ok"), (1200, "degraded"), (2400, "degraded"), (2460, "down"), (2520, "down")]:
        w.jwrite(w.run / "tick.json", {"t": NOW + dt - 5})                                          # the tick, live monitor and sampler are separate
        w.jwrite(w.pub / "live.json", {"generated_at": NOW + dt - 2})                                # units: they keep running while the check tier is dead
        w.jwrite(w.state / "metrics-ring.json", {"v": 1, "last": {"t": NOW + dt - 10}})
        assert sh_mod.main(["--refresh", "--now", str(NOW + dt)]) == 0
        doc = json.loads(f.read_text())
        assert doc["level"] == level and doc["generated_at"] == NOW + dt, (dt, doc["level"])        # rewritten every tick, verdict current
        assert sh_mod.effective(doc, NOW + dt)["level"] == level and not sh_mod.effective(doc, NOW + dt)["stale"]
        seen.append(doc["level"])
    assert seen == ["ok", "ok", "ok", "degraded", "degraded", "down", "down"]
    assert "runner stopped" in doc["verdict"]["reasons"][0]
    # ... and WITHOUT the refresher (it died too): the file left by the last tier run (an ok) is judged by its age alone
    stale = selfjson("ok", 0)
    assert effective_ok(stale, NOW + 100) == "ok" and effective_ok(stale, NOW + 200) == "degraded" and effective_ok(stale, NOW + 700) == "down"


def test_refresh_is_silent_and_reports_failure_through_the_exit_code(w, capsys, monkeypatch):
    assert sh_mod.main(["--refresh", "--now", str(NOW)]) == 0
    assert capsys.readouterr().out == "" and (w.pub / "self.json").exists() and (w.state / "tasks" / "self_health.json").exists()
    monkeypatch.setattr(sh_mod, "_atomic_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert sh_mod.main(["--refresh", "--now", str(NOW)]) == 1                                       # public/ exists but the write failed: the unit fails
    import shutil
    shutil.rmtree(w.pub)
    assert sh_mod.main(["--refresh", "--now", str(NOW)]) == 0 and not w.pub.exists()                 # publishing not enabled: nothing to refresh, never created


def test_refresh_uses_the_cache_for_the_expensive_rows(w, monkeypatch):
    sh_mod.refresh(NOW)                                                                              # fills the caches (history rate, dir size)
    monkeypatch.setattr(sh_mod, "tree_size", lambda *a, **k: pytest.fail("walked the state dir every minute"))
    monkeypatch.setattr(sh_mod, "history_errors", lambda *a, **k: pytest.fail("re-read 6 MiB of history every minute"))
    assert sh_mod.refresh(NOW + 60) is True


def test_the_refresher_unit_command_runs_end_to_end_in_a_subprocess(tmp_path):
    """The exact ExecStart of the proposed homelab-maint-selfhealth.service, in a fresh interpreter on scratch dirs (website check
    off: no docker, no systemctl, no network)."""
    import sys
    dirs = {k: tmp_path / k.lower() for k in ("STATE", "LOG", "RUN", "CONF", "UNITS")}
    for d in dirs.values():
        d.mkdir()
    (dirs["STATE"] / "public").mkdir()
    (dirs["CONF"] / "maint.toml").write_text("[tasks.self_health]\nweb_check = false\n")
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
           **{f"HOMELAB_MAINT_{k}": str(d) for k, d in dirs.items()}}
    t0 = time.time()
    r = subprocess.run([sys.executable, "-B", "-m", "homelab_maint.tasks.self_health", "--refresh"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
    doc = json.loads((dirs["STATE"] / "public" / "self.json").read_text())
    assert doc["schema"] == 2 and abs(doc["generated_at"] - time.time()) < 60 and doc["generated_at"] >= t0 - 1 and doc["ttl"]["degraded_after_s"] == 180
    assert oct((dirs["STATE"] / "public" / "self.json").stat().st_mode & 0o777) == "0o644"
    assert not list((dirs["STATE"] / "public").glob(".*tmp")) and (dirs["STATE"] / "tasks" / "self_health.json").exists()
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(dirs["STATE"] / "public")                                                              # no public/: exit 0, nothing created
    assert subprocess.run([sys.executable, "-B", "-m", "homelab_maint.tasks.self_health", "--refresh"], env=env, capture_output=True, timeout=60).returncode == 0
    assert not (dirs["STATE"] / "public").exists()


def test_concurrent_writers_never_leave_a_truncated_self_json(w):
    """The refresher, a tier run's publish and a human can write the same file: a shared tmp name could publish a half-written one."""
    target = w.pub / "self.json"
    stop, bad = threading.Event(), []

    def writer(n):
        doc = selfjson(**{"reasons": [f"w{n}" * 400]})
        while not stop.is_set():
            sh_mod._atomic_json(target, doc, 0o644)

    ts = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(3)]
    for t in ts:
        t.start()
    end = time.monotonic() + 0.6
    reads = 0
    while time.monotonic() < end:
        try:
            json.loads(target.read_text())
            reads += 1
        except FileNotFoundError:
            continue
        except ValueError as e:
            bad.append(str(e))
    stop.set()
    for t in ts:
        t.join(2)
    assert reads > 20 and bad == []
    assert not [p for p in w.pub.iterdir() if p.name.endswith(".tmp")] or all(p.name.startswith(".pub-self-") for p in w.pub.iterdir() if p.name.endswith(".tmp"))


def test_atomic_writer_uses_a_unique_hidden_tmp_name_per_write(w, monkeypatch):
    """Deterministic twin of the thread test: core.write_json_atomic shares ONE tmp name per target (two writers can clobber each other)."""
    srcs = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (srcs.append(Path(a).name), real(a, b))[1])
    for _ in range(3):
        sh_mod._atomic_json(w.pub / "self.json", selfjson(), 0o644)
    assert len(set(srcs)) == 3 and all(n.startswith(".pub-self-") and n.endswith(".tmp") for n in srcs)     # swept by publish's .pub-*.tmp cleanup
    assert [p.name for p in w.pub.iterdir() if p.name.endswith(".tmp")] == []


def test_published_cli_applies_the_age_rule_to_the_file_on_disk(w, capsys):
    w.jwrite(w.pub / "self.json", selfjson("ok", 0))
    assert sh_mod.main(["--published", "--check", "--now", str(NOW + 30)]) == 0
    assert capsys.readouterr().out.startswith("Monitoring pipeline: healthy")
    assert sh_mod.main(["--published", "--check", "--now", str(NOW + 400)]) == 1
    assert "refresher stopped" in capsys.readouterr().out
    assert sh_mod.main(["--published", "--check", "--json", "--now", str(NOW + 4000)]) == 2
    assert json.loads(capsys.readouterr().out)["level"] == "down"
    (w.pub / "self.json").unlink()
    assert sh_mod.main(["--published", "--check", "--now", str(NOW)]) == 1                           # missing: degraded, not healthy


def test_doctor_flags_a_missing_or_stale_published_file(w):
    ok, why = sh_mod.doctor(NOW)
    assert not ok and "published self.json" in why and "selfhealth.timer" in why                    # public/ exists, the refresher never wrote
    sh_mod.refresh(NOW)
    assert sh_mod.doctor(NOW + 60) == (True, "pipeline healthy")
    ok, why = sh_mod.doctor(NOW + 400)                                                               # refresher died: the live verdict is fine, the file is old
    assert not ok and "refresher stopped" in why


# --------------------------------------------------------------------------- 2. "never ran since install" is not healthy
def test_cli_with_an_old_timer_unit_and_no_status_is_degraded_and_writes_nothing(w, capsys):
    """Reproduces the review: install.sh finished, the timer never ran, no state file, the CLI persists nothing."""
    (w.state / "status.json").unlink()
    w.unit("check", 3 * H)
    assert not (w.state / "tasks" / "self_health.json").exists()
    assert sh_mod.main(["--check", "--now", str(NOW)]) == 1
    out = capsys.readouterr().out
    assert "degraded" in out and "never produced status.json" in out and "systemctl status homelab-maint-check.timer" in out
    assert not (w.state / "tasks" / "self_health.json").exists()                                    # a human asking leaves no trace
    ok, why = sh_mod.doctor(NOW)
    assert not ok and "check tier has never produced status.json" in why


def test_cli_with_no_sign_of_an_install_gives_no_benefit_of_the_doubt(w):
    (w.state / "status.json").unlink()
    r = row(w.assess(mode="cli"), "runner")
    assert r["state"] == "degraded" and "no check timer unit" in r["detail"] and "never run" in r["reason"]
    assert row(w.assess(mode="export"), "runner")["state"] == "info"                                  # the website's refresher: first run still gets the grace


def test_cli_with_a_timer_installed_minutes_ago_still_waits_for_the_first_run(w):
    (w.state / "status.json").unlink()
    w.unit("check", 120)
    r = row(w.assess(mode="cli"), "runner")
    assert r["state"] == "info" and "waiting for the first check run" in r["detail"]
    w.unit("check", 1600)                                                                             # > 1.5 x 15 min + 2 min
    assert row(w.assess(mode="cli"), "runner")["state"] == "degraded"


def test_the_grace_is_anchored_on_the_installed_unit_not_on_the_state_file(w):
    (w.state / "status.json").unlink()
    w.unit("check", 3 * H)
    for mode in ("export", "cli"):
        r = row(w.assess(state={}, mode=mode), "runner")                                              # fresh state: first_seen = now
        assert r["state"] == "degraded" and "3.0 h after install" in r["detail"] or mode == "cli"
    st = {"first_seen": NOW - 3 * H}
    assert row(w.assess(state=st), "runner")["state"] == "degraded"                                   # the persisted anchor still works too
    (w.units / sh_mod.UNIT_FILES["check"]).unlink()
    assert row(w.assess(state={}), "runner")["state"] == "info"                                       # no evidence at all: the old behaviour


def test_never_ran_tiers_anchor_on_the_installed_unit(w):
    w.status(weekly_age=None, daily_age=None)
    assert row(w.assess(), "weekly")["state"] == "info"
    w.unit("check", 40 * 86400)
    rep = w.assess(state={})
    assert row(rep, "weekly")["state"] == "degraded" and row(rep, "daily")["state"] == "degraded"


def test_a_unit_stamped_in_the_future_counts_as_just_installed(w):
    (w.state / "status.json").unlink()
    w.unit("check", -10 * H)
    assert row(w.assess(mode="cli"), "runner")["state"] == "info"


# --------------------------------------------------------------------------- 3. can an alert actually be delivered?
def box_entry(age, n=1, **kw):
    return {"kind": "alert", "key": "k", "snap": {"title": "SECRET TITLE xyz", "kind": "alert"}, "ts": NOW - age, "last": NOW - age, "n": n, "why": "smtp down", **kw}


def test_alerts_no_state_or_idle_state_is_ok(w):
    assert row(w.assess(), "alerts")["state"] == "ok"
    w.notify_state()
    r = row(w.assess(), "alerts")
    assert r["state"] == "ok" and "no undelivered" in r["detail"]


def test_alerts_open_breaker_is_degraded_and_the_verdict_follows(w):
    w.notify_state(breaker={"until": NOW + 200, "n": 2, "why": "smtp: connection refused"})
    rep = w.assess()
    r = row(rep, "alerts")
    assert r["state"] == "degraded" and "circuit is open" in r["detail"] and "smtp: connection refused" in r["detail"] and r["hint"]
    assert rep["level"] == "degraded" and rep["metrics"]["breaker_open"] is True
    w.notify_state(breaker={"until": NOW + 10 ** 6, "n": 1})                                          # implausibly far ahead: notify ignores it, so do we
    assert row(w.assess(), "alerts")["state"] == "ok"


def test_alerts_repeated_failures_without_a_delivery_since(w):
    w.notify_state(breaker={"until": NOW - 100, "n": 3, "why": "timeout"})                            # closed again, but 3 failures in a row and no success
    r = row(w.assess(), "alerts")
    assert r["state"] == "degraded" and "3 alert transport failures in a row" in r["detail"]
    w.notify_state(breaker={"until": NOW - 100, "n": 1})
    assert row(w.assess(), "alerts")["state"] == "ok"                                                 # one blip
    w.notify_state(breaker={"until": NOW - 3 * H, "n": 9})
    assert row(w.assess(), "alerts")["state"] == "ok"                                                 # old leftover: nothing has been sent since, nothing is stuck


@pytest.mark.parametrize("age,state", [(60, "ok"), (899, "ok"), (900, "degraded"), (3 * H, "degraded"), (6 * H - 1, "degraded"), (6 * H, "down")])
def test_alerts_outbox_age_thresholds(w, age, state):
    w.notify_state(outbox=[box_entry(age)])
    r = row(w.assess(), "alerts")
    assert r["state"] == state, r
    if state != "ok":
        assert "critical page" in r["detail"] and r["hint"] and r["age_s"] == age
    assert (w.assess()["level"], ) == ({"ok": "ok", "degraded": "degraded", "down": "down"}[state], )


def test_alerts_outbox_thresholds_follow_notify_toml(w):
    (w.conf / "notify.toml").write_text("[retry]\noutbox_ttl_s = 3600\noutbox_max = 3\n")
    w.notify_state(outbox=[box_entry(1900)])                                                          # 31 min > ttl/2
    assert row(w.assess(), "alerts")["state"] == "down"
    w.notify_state(outbox=[box_entry(30), box_entry(20), box_entry(10)])                              # fresh, but the outbox is at its cap: pages are dropped
    r = row(w.assess(), "alerts")
    assert r["state"] == "down" and "dropped" in r["reason"]
    (w.conf / "notify.toml").write_text("[retry\nbroken")                                             # unreadable: notify's own defaults
    w.notify_state(outbox=[box_entry(1900)])
    assert row(w.assess(), "alerts")["state"] == "degraded"


def test_alerts_fresh_outbox_entries_are_just_a_retry_in_progress(w):
    w.notify_state(outbox=[box_entry(120, n=0), box_entry(30)])
    r = row(w.assess(), "alerts")
    assert r["state"] == "ok" and "2 critical page(s) waiting for a retry" in r["detail"]


def test_alerts_outbox_dates_are_read_the_way_notify_reads_them(w):
    w.notify_state(outbox=[box_entry(-5 * H)])                                                        # first attempt in the future: brand new, not old
    assert row(w.assess(), "alerts")["state"] == "ok"
    w.notify_state(outbox=[{"kind": "alert", "key": "k", "snap": {}, "ts": "x"}])                      # undated: old enough to complain
    assert row(w.assess(), "alerts")["state"] == "degraded"


def test_alerts_reads_the_newest_state_between_the_state_dir_and_the_run_dir(w):
    w.notify_state(where=w.state, outbox=[])
    w.notify_state(where=w.run, outbox=[box_entry(7 * H)])                                            # the tmpfs fallback written during an outage is newer
    os.utime(w.state / "notify-state.json", (NOW - 100, NOW - 100))
    assert row(w.assess(), "alerts")["state"] == "down"


def test_alerts_corrupt_state_is_degraded_and_never_leaks_page_content(w):
    (w.state / "notify-state.json").write_text("{not json")
    assert row(w.assess(), "alerts")["state"] == "degraded"
    w.notify_state(outbox=[box_entry(7 * H)], breaker={"until": NOW + 100, "n": 1, "why": "x"})
    rep = w.assess()
    assert "SECRET TITLE" not in json.dumps(sh_mod.public(rep, NOW)) + sh_mod.summary(rep)             # only counts and ages


def test_kuma_failing_pushes_degrade_the_row_and_a_success_clears_it(w):
    for i in range(2):
        sh_mod.note_kuma("tier-check", False, "curl exit 7", now=NOW - 100 + i)
    assert row(w.assess(), "kuma")["state"] == "ok"                                                   # two: below kuma_fail_n
    sh_mod.note_kuma("tier-check", False, "curl exit 7", now=NOW - 50)
    r = row(w.assess(), "kuma")
    assert r["state"] == "degraded" and "failed 3 times in a row" in r["detail"] and "curl exit 7" in r["detail"] and "AbCdEf0123456789" not in json.dumps(r)
    assert row(w.assess(kuma_fail_n=5), "kuma")["state"] == "ok"
    sh_mod.note_kuma("tier-check", True, now=NOW - 10)
    assert row(w.assess(), "kuma")["state"] == "ok"
    doc = json.loads((w.state / "kuma-state.json").read_text())
    assert doc["keys"]["tier-check"]["n"] == 0 and oct((w.state / "kuma-state.json").stat().st_mode & 0o777) == "0o600"


def test_note_kuma_stores_no_secret_and_never_raises(w, monkeypatch):
    sh_mod.note_kuma("umbrella-probes", False, "http://x/api/push/AbCdEf0123456789ZyXwVu?status=down", now=NOW)
    raw = (w.state / "kuma-state.json").read_text()
    assert "AbCdEf0123456789ZyXwVu" not in raw and "umbrella-probes" in raw
    sh_mod.note_kuma("bad key!", False)                                                               # not a key name: ignored
    assert "bad key" not in (w.state / "kuma-state.json").read_text()
    monkeypatch.setattr(core, "STATE_DIR", Path("/proc/nonexistent/x"))
    sh_mod.note_kuma("tier-check", False)                                                             # unwritable: swallowed (a heartbeat result never breaks a push)


# --------------------------------------------------------------------------- 4. inside the check run: no tautological DOWN
def test_in_runner_a_long_gap_is_info_not_a_crit_sample(w):
    """Reproduces the review: after a reboot / outage the first run reads the previous run's status.json, which is old by definition."""
    w.status(check_age=8 * H)
    res = sh_mod.run(ctx_for(w))
    assert res.status == "ok" and res.summary.startswith("ok: pipeline healthy (a 8.0 h gap before this run has ended)")
    assert "not deployed" not in res.summary and res.issue_key == "" and res.metrics["gap_s"] == 8 * H and res.metrics["level"] == "ok"
    gap = next(i for i in res.items if i["component"] == "Runner (check tier)")
    assert gap["state"] == "info" and "a monitoring gap that has ended" in gap["detail"]
    import homelab_maint.cli as cli
    assert cli.overall({"self_health": {"status": res.status, "alert": res.alert}}) == "ok"            # status.overall stays ok: Kuma/page/SLO see no crit sample
    kuma_status = "up" if cli.overall({"self_health": {"status": res.status, "alert": res.alert}}) in ("ok", "info", "skipped") else "down"
    assert kuma_status == "up"


def test_the_strict_judgement_of_the_same_evidence_is_kept_outside_the_runner(w):
    w.status(check_age=8 * H)
    for mode in ("export", "cli"):
        r = row(w.assess(mode=mode), "runner")
        assert r["state"] == "down" and "no check run for 8.0 h" in r["detail"]
    assert sh_mod.export(NOW)["level"] == "down"                                                        # the website's strip says so until a run has landed
    w.status(check_age=0)                                                                                # ... and the run that just landed clears it
    assert sh_mod.export(NOW)["level"] == "ok"


def test_in_runner_the_publish_row_only_complains_when_publishing_fell_behind_the_runner(w):
    w.status(check_age=8 * H)
    w.jwrite(w.pub / "manifest.json", {"generated_at": NOW - 8 * H + 5, "registry_hash": "deadbeef"})   # published right after the previous run
    w.jwrite(w.pub / "overview.json", {"generated_at": NOW - 8 * H, "export_errors": []}, NOW - 8 * H)
    r = row(w.assess(mode="runner"), "publish")
    assert r["state"] == "info" and "the gap has ended" in r["detail"]
    assert row(w.assess(mode="export"), "publish")["state"] == "degraded"                               # the website's view: it IS old
    w.status(check_age=300)                                                                              # the runner ran 5 min ago but the last publish is 8 h old
    r = row(w.assess(mode="runner"), "publish")
    assert r["state"] == "degraded" and "publishing stopped" in r["reason"]                              # a dead publisher is still caught inside the run


def test_in_runner_real_clock_trouble_and_other_rows_are_unchanged(w):
    w.status(check_age=-1000)
    assert row(w.assess(mode="runner"), "runner")["state"] == "degraded"                                 # a stamp from the future is not a gap
    w.status(check_age=8 * H)
    w.jwrite(w.pub / "live.json", {"generated_at": NOW - 900})
    rep = w.assess(mode="runner")
    assert row(rep, "live")["state"] == "degraded" and rep["level"] == "degraded"                        # everything else still counts


# --------------------------------------------------------------------------- 5. registry row against the real registry.py
def test_registry_a_revert_clears_the_rejection_even_though_history_keeps_the_old_record(w):
    """Reproduces the review: the owner restores the applied content; sync takes the 'unchanged' path, clears current.json.invalid and
    appends NO history record, so the stale valid:false record stays last."""
    (w.state / "rules" / "history.jsonl").write_text(json.dumps({"ts": NOW - 50, "valid": True, "applied": True}) + "\n"
                                                     + json.dumps({"ts": NOW - 10, "valid": False, "errors": ["a"], "applied": False}) + "\n")
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["invalid"] = None
    w.jwrite(w.state / "rules" / "current.json", cur)
    assert row(w.assess(), "registry")["state"] == "ok"
    (w.state / "rules" / "history.jsonl").write_text(json.dumps({"ts": NOW - 10, "valid": True, "applied": False, "errors": []}) + "\n")
    assert row(w.assess(), "registry")["state"] == "ok"                                                  # (the old valid:true/applied:false reading is gone too)


def test_registry_reverted_but_not_yet_cleared_waits_for_the_tick_then_complains(w):
    reject(w)
    f = w.rd / "10-checks.toml"
    f.write_bytes(b'[[rule]]\nid = "check.disk"\n')                                                       # the applied content is back ...
    os.utime(f, (NOW - 30, NOW - 30))                                                                     # ... 30 s ago, `invalid` not cleared yet
    r = row(w.assess(), "registry")
    assert r["state"] == "info" and "reverted" in r["detail"]
    os.utime(f, (NOW - 1000, NOW - 1000))
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["invalid"]["ts"] = NOW - 1000
    w.jwrite(w.state / "rules" / "current.json", cur)
    r = row(w.assess(), "registry")
    assert r["state"] == "degraded" and "tick is not syncing" in r["detail"]


def test_registry_ignores_dotfiles_and_non_regular_files_like_scan_registry(w):
    (w.rd / ".10-checks.toml").write_text('[[rule]]\nid = "stray"\n')                                     # an editor's hidden copy
    os.utime(w.rd / ".10-checks.toml", (NOW - 3000, NOW - 3000))
    (w.rd / "30-dir.toml").mkdir()
    os.symlink(w.rd / "10-checks.toml", w.rd / "40-link.toml")
    r = row(w.assess(), "registry")
    assert r["state"] == "ok", r                                                                          # none of them is part of the registry
    (w.rd / "20-new.toml").write_text("[meta]\n")                                                         # a REAL new file still is
    os.utime(w.rd / "20-new.toml", (NOW - 3000, NOW - 3000))
    assert row(w.assess(), "registry")["state"] == "degraded"


def test_registry_an_edited_sole_file_is_caught_with_no_remembered_hash_scheme(w):
    """Reproduces the review: the shipped state has only 00-baseline-invariants.toml; edited before the first observation, no sha_alg."""
    (w.rd / "10-checks.toml").unlink()
    cur = json.loads((w.state / "rules" / "current.json").read_text())
    cur["files"] = [f for f in cur["files"] if f["name"] == "00-baseline.toml"]
    w.jwrite(w.state / "rules" / "current.json", cur)
    st: dict = {}
    assert row(w.assess(state=st), "registry")["state"] == "ok" and "sha_alg" not in st                  # verified with sha256: no detection step
    f = w.rd / "00-baseline.toml"
    f.write_bytes(b'[meta]\ncategory = "safety"\n# edited\n')
    os.utime(f, (NOW - 3000, NOW - 3000))
    r = row(w.assess(state={}), "registry")                                                              # fresh state, nothing remembered
    assert r["state"] == "degraded" and "00-baseline.toml" in r["detail"] and "not verified" not in r["detail"]


def test_registry_every_file_edited_is_degraded_not_ok(w):
    for n in ("00-baseline.toml", "10-checks.toml"):
        (w.rd / n).write_bytes(b"# changed\n")
        os.utime(w.rd / n, (NOW - 3000, NOW - 3000))
    r = row(w.assess(state={}), "registry")
    assert r["state"] == "degraded" and "00-baseline.toml" in r["detail"] and "10-checks.toml" in r["detail"]


# --------------------------------------------------------------------------- 6. installed but never started is not "not deployed"
def test_live_unit_installed_but_no_output_is_degraded_even_with_no_memory(w):
    """Reproduces the review: the live service crash-loops from the first boot, live.json never exists, fresh state."""
    (w.pub / "live.json").unlink()
    assert row(w.assess(state={}), "live")["state"] == "info" and "not deployed" in row(w.assess(state={}), "live")["detail"]
    w.unit("live", 2 * H)
    rep = w.assess(state={})
    r = row(rep, "live")
    assert r["state"] == "degraded" and "never run" in r["reason"] and "2.0 h ago" in r["detail"] and r["hint"] and rep["level"] == "degraded"
    w.unit("live", 100)                                                                                   # just installed: it owes nothing yet
    r = row(w.assess(state={}), "live")
    assert r["state"] == "info" and "waiting for its first run" in r["detail"]
    w.unit("live", 90 + 180 + 1)
    assert row(w.assess(state={}), "live")["state"] == "degraded"                                         # past its own limit + 3 min


def test_tick_and_metrics_units_installed_but_silent_are_degraded(w):
    (w.run / "tick.json").unlink()
    doc = json.loads((w.state / "status.json").read_text())
    del doc["tick"]
    w.jwrite(w.state / "status.json", doc)
    (w.state / "metrics-ring.json").unlink()
    assert row(w.assess(state={}), "tick")["state"] == "info" and row(w.assess(state={}), "metrics")["state"] == "info"
    w.unit("tick", 3600)
    w.unit("metrics", 3600)
    rep = w.assess(state={})
    assert row(rep, "tick")["state"] == "degraded" and "scheduler tick has produced nothing" in row(rep, "tick")["detail"]
    assert row(rep, "metrics")["state"] == "degraded" and "sensor sampler" in row(rep, "metrics")["reason"]
    assert [r["id"] for r in rep["bad"]] == ["tick", "metrics"]


def test_a_state_wipe_does_not_turn_a_dead_part_back_into_not_deployed(w):
    st: dict = {}
    w.unit("live", 5 * H)
    w.assess(state=st)
    assert "live" in st["seen"]
    (w.pub / "live.json").unlink()
    assert row(w.assess(state=st), "live")["state"] == "degraded"                                          # remembered
    assert row(w.assess(state={}), "live")["state"] == "degraded"                                          # state file lost: the unit still says it is owed


def test_units_that_are_not_installed_stay_not_deployed_and_are_counted_as_such(w):
    import shutil
    shutil.rmtree(w.pub)
    (w.run / "tick.json").unlink()
    rep = w.assess(state={})
    assert rep["level"] == "ok" and rep["metrics"]["not_deployed"] >= 2 and "not deployed" in sh_mod.summary(rep)
    w.unit("live", 100)                                                                                    # waiting-for-first-run is NOT "not deployed"
    nd = w.assess(state={})["metrics"]["not_deployed"]
    assert nd == rep["metrics"]["not_deployed"] - 1


def test_publish_never_happened_though_the_runner_ran(w):
    """A public dir that is absent or empty because publish crashes on every run, with no memory and no unit."""
    import shutil
    shutil.rmtree(w.pub)
    w.status(check_age=2400)                                                                                # a run completed 40 min ago and should have published
    r = row(w.assess(state={}), "publish")
    assert r["state"] == "degraded" and "nothing was ever published" in r["detail"]
    w.status(check_age=300)                                                                                 # 5 min ago: the first publish may still be on its way
    assert row(w.assess(state={}), "publish")["state"] == "info"
    w.pub.mkdir()                                                                                           # exists but empty
    w.status(check_age=2400)
    assert row(w.assess(state={}), "publish")["state"] == "degraded"
    w.status(check_age=300)
    assert row(w.assess(state={}), "publish")["state"] == "info"
