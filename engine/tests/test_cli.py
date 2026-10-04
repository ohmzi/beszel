"""Tests for the integration glue: cli.py (every subcommand, dispatch, the run loop), core.Notifier's notify adapter, the
widget server's extra routes and the metrics contracts payloads/publish read from the checks.

Nothing touches the host: every directory is a tmp dir, `core.sh` is stubbed (no `logger`, no curl), the notify transport is
never reached (the notifier and the publisher are replaced by recorders), and the subprocess tests only import or `--help`."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)

import argparse
import fcntl
import http.client
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from homelab_maint import cli, core, payloads
from homelab_maint.core import Result

ROOT = Path(__file__).resolve().parent.parent
cli.load_tasks()                       # every real task registers ONCE, into the real registry, before a fixture swaps core.REGISTRY:
REAL_REGISTRY = dict(core.REGISTRY)    # (a module first imported while it is swapped would register into the swap and never again)
# every word `homelab-maint --help` must list (the module docstring lists them too)
SUBCOMMANDS = ("run status plan approve pause resume gate doctor serve tick schedule notify-test publish metrics-sample metrics-export "
               "routine incidents report notify migrate probes job live new plugins smart-event ack web rules self-health").split()
HEAVY = {"notify", "notify_templates", "routine", "publish", "scheduler", "jobs", "schedule", "incidents", "reports", "live", "legacy",
         "probes", "scaffold", "metrics_ring", "server", "payloads", "payloads_metrics", "inuse", "smart_hook", "acks", "registry"}
NOW = 1_800_000_000.0


def child_env(tmp_path: Path) -> dict:
    """A subprocess that can only see tmp dirs (never the live state, config or notification transport)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HOMELAB_MAINT_")}
    for k in ("state", "log", "run", "conf"):
        (tmp_path / k).mkdir(exist_ok=True)
        env[f"HOMELAB_MAINT_{k.upper()}"] = str(tmp_path / k)
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", HOMELAB_MAINT_LIB=str(ROOT))
    return env


def py(tmp_path, code: str, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", "-c", code, *args], capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT, env=child_env(tmp_path), stdin=subprocess.DEVNULL)


# =========================================================================== a tiny world for cmd_run
class World:
    def __init__(self, root: Path, mp):
        self.root, self.mp = root, mp
        self.state, self.log, self.conf, self.run = (root / n for n in ("state", "log", "conf", "run"))
        for d in (self.state, self.log, self.conf, self.run):
            d.mkdir(parents=True, exist_ok=True)
        for k, d in (("STATE_DIR", self.state), ("LOG_DIR", self.log), ("CONF_DIR", self.conf), ("RUN_DIR", self.run)):
            mp.setattr(core, k, d)
        mp.setattr(core, "sh", lambda cmd, *a, **k: CompletedProcess(cmd, 0, "", ""))          # no logger, no curl, no runuser
        from homelab_maint.tasks import self_health
        mp.setattr(self_health, "http_get", lambda *a, **k: (None, b"", "refused", 1))         # doctor asks the website's /healthz: nothing listens in a test
        from homelab_maint import registry
        mp.setattr(registry, "register_tasks", lambda: None)        # load_tasks would add `rules_registry` to this fake registry (a real load: see below)
        (self.conf / "protected.toml").write_text('patterns = ["^never-matches$"]\n')
        self.events: list[tuple] = []                       # (what, state lock held?) in call order
        self.registry({})

    def registry(self, spec: dict):
        reg = {}
        for name, row in spec.items():
            klass, tier, res = row
            reg[name] = core.Task(name, klass, tier, (lambda ctx, r=res: r(ctx) if callable(r) else r), title=name)
        self.mp.setattr(core, "REGISTRY", reg)

    def maint(self, text: str):
        (self.conf / "maint.toml").write_text(text)

    def status(self) -> dict:
        return json.loads((self.state / "status.json").read_text())

    def history(self) -> list[dict]:
        p = self.state / "history.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def held(self) -> bool:
        """Is the global status lock held right now? (flock conflicts between open file descriptions, even in one process.)"""
        with open(self.state / "state.lock", "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def wire(self):
        """Replace the notifier, the publisher and the guard with recorders; return the notifier class used."""
        w = self

        class Rec:
            def __init__(self, cfg, *a, **k):
                self.cfg = cfg

            def evaluate(self, name, title, res, now):
                w.events.append((f"evaluate:{name}", w.held()))

            def save(self):
                w.events.append(("save", w.held()))

            def deliver(self):
                w.events.append(("deliver", w.held()))

        self.mp.setattr(cli, "_notifier", lambda cfg: Rec(cfg))
        self.mp.setattr(cli, "_publish", lambda status: w.events.append(("publish", w.held())))
        self.mp.setattr(cli, "kuma_push", lambda *a, **k: w.events.append(("kuma", w.held())))
        return Rec


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"tier": "check", "task": None, "apply": False, "dry_run": False, **kw})


# =========================================================================== the command line
def test_help_lists_every_subcommand_and_the_docstring_names_the_umbrella_ones(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert [c for c in SUBCOMMANDS if c not in out] == []
    assert [c for c in ("tick", "schedule", "job", "routine", "incidents", "report", "notify", "probes", "live", "metrics-sample",
                        "metrics-export", "publish", "migrate", "new", "smart-event", "ack", "web", "rules", "self-health") if c not in cli.__doc__] == []
    assert cli.TIERS == ("check", "daily", "weekly", "monthly")


def test_run_accepts_every_tier_and_the_routine_flags(monkeypatch):
    """`run --tier monthly` and the three owner/scheduler flags parse (--scheduled is what the tick adds to `run --task`)."""
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda a: seen.append(vars(a)) or 0)
    for argv in (["run", "--tier", "monthly", "--dry-run"], ["run", "--task", "x", "--override"], ["run", "--task", "x", "--force", "--apply"],
                 ["run", "--task", "x", "--scheduled", "--apply"]):
        assert cli.main(argv) == 0
    assert [a["tier"] for a in seen] == ["monthly", "check", "check", "check"]
    assert [(a["override"], a["force"], a["scheduled"]) for a in seen] == [(False, False, False), (True, False, False), (False, True, False),
                                                                           (False, False, True)]
    with pytest.raises(SystemExit):
        cli.main(["run", "--tier", "hourly"])
    with pytest.raises(SystemExit):
        cli.main(["run", "--apply", "--dry-run"])


def test_every_passthrough_target_exists_and_is_callable():
    for name, (mod, fn, lead) in cli.PASS.items():
        m = importlib.import_module(f"homelab_maint.{mod}")
        assert callable(getattr(m, fn)), (name, mod, fn)
        assert isinstance(lead, tuple)


@pytest.mark.parametrize("argv,mod,fn,expect", [
    (["routine", "--now", "2026-10-03 08:00", "due"], "routine", "main", ["--now", "2026-10-03 08:00", "due"]),   # leading option survives
    (["incidents", "list"], "incidents", "main", ["list"]),
    (["report", "daily", "--print"], "reports", "main", ["daily", "--print"]),
    (["notify", "send", "alert", "crit", "T", "--task", "x"], "notify", "main", ["send", "alert", "crit", "T", "--task", "x"]),
    (["migrate", "status"], "legacy", "main", ["status"]),
    (["probes", "run", "--force"], "probes", "main", ["run", "--force"]),
    (["job", "mode", "backup-system", "observe"], "scheduler", "main", ["mode", "backup-system", "observe"]),
    (["live", "--once"], "live", "main", ["--once"]),
    (["serve"], "server", "main", []),                                                    # server.main must get [], not sys.argv: argparse would reject "serve"
    (["serve", "--port", "9999"], "server", "main", ["--port", "9999"]),
    (["new", "task", "foo"], "scaffold", "main", ["new", "task", "foo"]),               # the verb is part of scaffold's own argv
    (["plugins"], "scaffold", "main", ["plugins"]),
    (["ack", "list"], "acks", "main", ["list"]),                                          # SPEC5
    (["ack", "add", "0123456789abcdef", "--days", "30"], "acks", "main", ["add", "0123456789abcdef", "--days", "30"]),
    (["rules", "sync", "--no-notify"], "registry", "main", ["sync", "--no-notify"]),     # SPEC6
    (["rules", "check", "--todo"], "registry", "main", ["check", "--todo"]),
    (["self-health", "--json", "--check"], "tasks.self_health", "main", ["--json", "--check"]),
])
def test_passthrough_hands_the_raw_arguments_to_the_modules_own_parser(monkeypatch, argv, mod, fn, expect):
    got = []
    monkeypatch.setattr(importlib.import_module(f"homelab_maint.{mod}"), fn, lambda a: got.append(list(a)) or 7)
    assert cli.main(argv) == 7 and got == [expect]


def test_notify_test_and_schedule_and_tick_are_thin_wrappers(monkeypatch):
    from homelab_maint import notify, scheduler
    calls = []
    monkeypatch.setattr(notify, "main", lambda a: calls.append(("notify", list(a))) or 0)
    monkeypatch.setattr(scheduler, "main", lambda a: calls.append(("sched", list(a))) or 0)
    monkeypatch.setattr(notify, "flush_pending", lambda *a, **k: calls.append(("flush",)))
    assert cli.main(["notify-test", "alert.crit", "recovery", "--dry-run"]) == 0
    assert cli.main(["schedule", "--json"]) == 0
    assert cli.main(["tick"]) == 0
    assert cli.main(["tick", "--dry-run"]) == 0
    assert calls == [("notify", ["test", "alert.crit", "recovery", "--dry-run"]), ("sched", ["explain", "--json"]),
                     ("sched", ["tick"]), ("flush",),                                    # a real tick replays queued pages afterwards
                     ("sched", ["tick", "--dry-run"])]                                  # a dry run changes nothing, not even the outbox


def test_smart_event_is_handled_before_argparse_and_never_prints(monkeypatch, capsys):
    from homelab_maint import smart_hook
    seen = []
    monkeypatch.setattr(smart_hook, "smart_event_main", lambda a: seen.append(list(a)) or 0)
    assert cli.main(["smart-event", "--not-a-flag", "x"]) == 0 and seen == [["--not-a-flag", "x"]]     # argparse would exit 2 here
    monkeypatch.setattr(smart_hook, "smart_event_main", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["smart-event"]) == 1                                                                # crash: exit 1 so the stub falls back
    monkeypatch.setattr(smart_hook, "smart_event_main", lambda a: 1)
    assert cli.main(["smart-event"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""                                                               # smartd treats any output as a failure


def test_metrics_publish_wrappers(monkeypatch, capsys):
    from homelab_maint import metrics_ring, publish
    calls = []
    monkeypatch.setattr(metrics_ring, "main", lambda a: calls.append(("ring", list(a))) or 0)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: calls.append(("publish",)) or ["metrics.json"])
    monkeypatch.setattr(publish, "main", lambda: calls.append(("publish-main",)) or 0)
    assert cli.main(["metrics-sample", "-v"]) == 0
    assert cli.main(["metrics-export", "--pretty"]) == 0
    assert cli.main(["publish"]) == 0
    assert calls == [("ring", ["sample", "-v"]), ("publish",),                           # the sampler refreshes metrics.json (<= 1 min old)
                     ("ring", ["export", "--pretty"]), ("publish-main",)]
    monkeypatch.setattr(metrics_ring, "main", lambda a: 1)                               # a failed sample is still reported, publish still runs
    monkeypatch.setattr(publish, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cli.main(["metrics-sample"]) == 1                                             # publish never decides the exit code
    assert "publish failed" in capsys.readouterr().err


@pytest.mark.parametrize("sub", ["run", "status", "plan", "approve", "pause", "resume", "gate", "doctor", "serve", "tick", "schedule",
                                 "notify-test", "publish", "metrics-sample", "metrics-export", "routine", "incidents", "report", "notify",
                                 "migrate", "job", "live", "new", "plugins", "ack", "rules", "self-health"])
def test_every_subcommand_answers_help_in_a_fresh_interpreter(tmp_path, sub):
    """The tick imports this module every minute and the units call these names: `--help` must exit 0 for each, importing only
    what it needs (a subcommand that cannot import its module would fail here, not at 3 a.m.)."""
    r = subprocess.run([sys.executable, "-B", "-m", "homelab_maint.cli", sub, "--help"], capture_output=True, text=True, timeout=60,
                       cwd=ROOT, env=child_env(tmp_path), stdin=subprocess.DEVNULL)
    said = bool((r.stdout + r.stderr).strip())                     # incidents.py, notify.py and acks.py have no -h flag: they print usage, exit 2
    assert "Traceback" not in r.stderr and (r.returncode == 0 or (r.returncode == 2 and said and sub in ("incidents", "notify", "ack"))), \
        (r.stdout[-300:], r.stderr[-600:])


def test_the_wrapper_script_runs(tmp_path):
    r = subprocess.run([str(ROOT / "homelab-maint"), "--help"], capture_output=True, text=True, timeout=60, env=child_env(tmp_path),
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and "COMMAND" in r.stdout


# =========================================================================== import cost, cycles, the registry
def test_importing_cli_is_cheap_and_lazy(tmp_path):
    """The per-minute tick imports cli: under 300 ms, and none of the big modules (they load when their subcommand runs)."""
    code = ("import sys, time, json; t = time.perf_counter(); import homelab_maint.cli; dt = time.perf_counter() - t; "
            "print(json.dumps({'dt': dt, 'mods': sorted(m.split('.')[1] for m in sys.modules if m.startswith('homelab_maint.') and m.count('.') == 1)}))")
    r = py(tmp_path, code)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["dt"] < 0.3, d
    assert not HEAVY & set(d["mods"]), sorted(HEAVY & set(d["mods"]))


def test_every_module_imports_alone_in_a_fresh_interpreter(tmp_path):
    """No import cycle and no module that only works because another one was imported first."""
    mods = sorted(p.stem for p in (ROOT / "homelab_maint").glob("*.py") if p.stem != "__init__")
    mods += [f"tasks.{p.stem}" for p in sorted((ROOT / "homelab_maint" / "tasks").glob("*.py")) if p.stem != "__init__"]
    bad = {m: r.stderr[-300:] for m in mods if (r := py(tmp_path, f"import homelab_maint.{m}")).returncode != 0}
    assert not bad, bad
    assert len(mods) > 30


def test_load_tasks_registers_every_module_once(tmp_path):
    """The real registry in a fresh interpreter: reports/routine (outside tasks/), native, monitors, pressure, guard, plugins; no clash,
    no placeholder `module_*` error task, a monthly tier, and a second load_tasks() (the routine calls cmd_run once per step) is a no-op."""
    code = ("import json; from homelab_maint import cli, core\n"
            "cli.load_tasks(); n = len(core.REGISTRY); cli.load_tasks()\n"
            "print(json.dumps({'n': n, 'n2': len(core.REGISTRY), 'dups': core.DUPLICATES, 'errs': cli.IMPORT_ERRORS,\n"
            " 'names': sorted(core.REGISTRY), 'tiers': sorted({t.tier for t in core.REGISTRY.values()})}))")
    r = py(tmp_path, code)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["dups"] == [] and d["errs"] == [] and d["n"] == d["n2"] and d["n"] > 40, {k: d[k] for k in ("dups", "errs", "n", "n2")}
    for name in ("disk_forecast", "report_daily", "report_weekly", "routine_verify_daily", "routine_rotate", "surrealdb_health", "probes",
                 "pressure_state", "pressure_response", "bulkhead_check", "legacy_audit", "docker_prune_parity", "os_jobs",
                 "self_health", "rules_registry"):                                  # SPEC6: the pipeline's own check and the registry check
        assert name in d["names"], name
    assert d["tiers"] == ["check", "daily", "monthly", "weekly"] and not any(n.startswith("module_") for n in d["names"])


def test_a_module_that_cannot_import_becomes_one_error_task_not_a_blind_runner(tmp_path):
    """A fresh interpreter (modules other tests imported would not re-register): tasks/gates.py fails to import. The runner still loads every
    other module, registers ONE `module_gates` error task (the page says why) and does not list it again when the routine calls load_tasks()
    once per step."""
    code = ("import importlib, json\n"
            "from homelab_maint import cli, core\n"
            "real = importlib.import_module\n"
            "def fake(name, *a, **k):\n"
            "    if name == 'homelab_maint.tasks.gates':\n"
            "        raise ImportError('broken on purpose')\n"
            "    return real(name, *a, **k)\n"
            "importlib.import_module = fake\n"
            "cli.load_tasks(); cli.load_tasks()\n"
            "res = core.REGISTRY['module_gates'].run(None)\n"
            "print(json.dumps({'errs': cli.IMPORT_ERRORS, 'status': res.status, 'summary': res.summary, 'ok': 'disk_forecast' in core.REGISTRY}))")
    r = py(tmp_path, code)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["errs"] == ["module_gates"] and d["status"] == "error" and "broken on purpose" in d["summary"] and d["ok"]


def test_plugin_discovery_is_called_with_the_configured_disabled_list(w, monkeypatch):
    from homelab_maint import scaffold
    w.maint('[global]\ndisabled_plugins = ["noisy"]\n')
    seen = []
    monkeypatch.setattr(scaffold, "load_plugins", lambda disabled=(): seen.append(tuple(disabled)) or SimpleNamespace(error_tasks=["plugin_x"]))
    monkeypatch.setattr(cli, "IMPORT_ERRORS", [])
    cli.load_tasks()
    assert seen == [("noisy",)] and cli.IMPORT_ERRORS == ["plugin_x"]


# =========================================================================== cmd_run: the glue between the pieces
def test_run_defers_every_send_until_the_state_lock_is_released(w):
    """evaluate() and save() run INSIDE the lock (short writes); the publisher, the Kuma heartbeat and deliver() (network, up to 90 s
    on a dead transport) run after it, in that order, so one slow SMTP never blocks the other tiers."""
    w.registry({"a": ("C0", "check", Result("crit", "a is bad")), "b": ("C0", "check", Result("ok", "fine"))})
    w.wire()
    assert cli.cmd_run(ns()) == 0
    assert w.events == [("evaluate:a", True), ("evaluate:b", True), ("save", True), ("publish", False), ("kuma", False), ("deliver", False)]


def test_run_writes_status_history_and_the_alert_flag(w):
    w.registry({"loud": ("C0", "check", Result("warn", "w", {"n": 3, "big": [1, 2]})),
                "quiet": ("C0", "check", Result("warn", "w", alert=False)), "ok": ("C0", "check", Result("ok", "o"))})
    w.wire()
    assert cli.cmd_run(ns()) == 0
    st = w.status()
    assert st["overall"] == "warn" and set(st["tasks"]) == {"loud", "quiet", "ok"}                 # a quiet warn still shows, only `loud` colours overall
    assert st["tasks"]["quiet"]["alert"] is False and st["tier_runs"]["check"]["dry_run"] is True
    h = {r["task"]: r for r in w.history() if r.get("kind") == "task"}
    assert h["loud"]["alert"] is True and h["quiet"]["alert"] is False                              # incidents/SLO/reports read this per run
    assert h["loud"]["metrics"] == {"n": 3}                                                          # scalars only
    w.registry({"quiet": ("C0", "check", Result("warn", "w", alert=False))})
    assert cli.cmd_run(ns()) == 0 and w.status()["overall"] == "ok"


def test_run_keeps_the_schedulers_rows_and_drops_removed_tasks(w):
    """The tick owns klass J rows (it would only have to put them back); a task that was removed must not fail forever."""
    w.registry({"a": ("C0", "check", Result("ok", "fine"))})
    w.wire()
    (w.state / "status.json").write_text(json.dumps({"tasks": {"backup-system": {"klass": "J", "status": "ok", "summary": "ran"},
                                                               "gone": {"klass": "C0", "status": "crit", "summary": "old"}}}))
    assert cli.cmd_run(ns()) == 0
    assert set(w.status()["tasks"]) == {"a", "backup-system"}


def test_run_errors_exit_nonzero_and_unknown_tasks_exit_2(w, capsys):
    def boom(ctx):
        raise RuntimeError("kaput")
    w.registry({"x": ("C0", "check", boom)})
    w.wire()
    assert cli.cmd_run(ns()) == 1 and "[error] x:" in capsys.readouterr().err
    assert cli.cmd_run(ns(task="nope")) == 2


def test_run_in_the_monthly_tier_selects_monthly_tasks_only(w):
    ran = []
    w.registry({"m": ("C0", "monthly", lambda ctx: ran.append("m") or Result("ok", "m")), "d": ("C0", "daily", lambda ctx: ran.append("d") or Result("ok", "d"))})
    w.wire()
    assert cli.cmd_run(ns(tier="monthly")) == 0
    assert ran == ["m"] and w.status()["tier_runs"]["monthly"]["dry_run"] is True


def test_run_fails_closed_without_a_routine_config(w):
    """RunGuard: no valid routine.toml means no window to apply in, so a C1 cleaner set to mode=apply is held to report-only and
    the C0 checks still run. (The same guard with a real routine is covered by tests/test_routine.py.)"""
    acted = []
    def clean(ctx):
        ctx.act("rm", "thing", 1, lambda: acted.append(1))
        return Result("ok", "cleaned" if acted else "dry-run")
    w.registry({"clean": ("C1", "daily", clean), "chk": ("C0", "daily", Result("ok", "fine"))})
    w.maint('[tasks.clean]\nmode = "apply"\n')
    w.wire()
    assert cli.cmd_run(ns(tier="daily", apply=True)) == 0
    st = w.status()["tasks"]
    assert acted == [] and st["clean"]["mode"] == "dry-run" and st["chk"]["status"] == "ok"


def test_run_applies_when_the_guard_allows_and_the_task_is_in_apply_mode(w, monkeypatch):
    """The guard's verdict is what reaches run_task: `apply and d.apply`, per task, and `after` sees every result."""
    acted, seen = [], []
    def clean(ctx):
        ctx.act("rm", "thing", 5, lambda: acted.append(1))
        return Result("ok", "cleaned")
    w.registry({"clean": ("C1", "daily", clean), "held": ("C1", "daily", clean)})
    w.maint('[tasks.clean]\nmode = "apply"\n[tasks.held]\nmode = "apply"\n')
    w.wire()

    class Guard:
        def order(self, tasks):
            return sorted(tasks, key=lambda t: t.name)

        def begin(self, t, apply, manual=False, force=False):
            return SimpleNamespace(run=t.name != "skipped", apply=t.name != "held", manual=manual, force=force)

        def task_cfg(self, cfg, t, d):
            return cfg

        def after(self, t, d, res, dur):
            seen.append((t.name, res.status))
    monkeypatch.setattr(cli, "_guard", lambda: (None, Guard()))
    assert cli.cmd_run(ns(tier="daily", apply=True)) == 0
    st = w.status()["tasks"]
    assert acted == [1] and st["clean"]["mode"] == "apply" and st["clean"]["reclaimed_bytes"] == 5
    assert st["held"]["mode"] == "dry-run" and sorted(seen) == [("clean", "ok"), ("held", "ok")]


def test_a_guard_that_cannot_load_degrades_to_report_only_not_to_apply(w, monkeypatch, capsys):
    from homelab_maint import routine
    monkeypatch.setattr(routine, "RunGuard", lambda: (_ for _ in ()).throw(RuntimeError("no guard")))
    r, g = cli._guard()
    assert r is None and "report-only" in capsys.readouterr().err
    assert g.begin(None, True).apply is False and g.begin(None, True).run is True


def test_the_notifier_in_cmd_run_is_the_deferring_notify_backed_one(monkeypatch):
    from homelab_maint import notify
    n = cli._notifier({"global": {"alert_confirm_runs": 2}})
    assert isinstance(n, notify.HermesNotifier) and isinstance(n, core.Notifier) and n.defer is True and hasattr(n, "deliver")
    monkeypatch.setattr(notify, "HermesNotifier", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no notify")))
    assert type(cli._notifier({})) is core.Notifier                                         # a broken notify module still alerts (inline)


def test_publish_step_order_and_failures_never_break_the_run(w, monkeypatch, capsys):
    from homelab_maint import incidents, publish, routine
    calls = []
    monkeypatch.setattr(incidents, "update", lambda st, hist, now: calls.append("incidents") or {"ok": True})
    monkeypatch.setattr(publish, "publish", lambda st: calls.append("publish") or ["overview.json", "routine.json"])
    monkeypatch.setattr(routine, "write_export", lambda: calls.append("routine-export"))
    cli._publish({"tasks": {}})
    assert calls == ["incidents", "publish"]                                                # publish builds routine.json itself
    calls.clear()
    monkeypatch.setattr(publish, "publish", lambda st: calls.append("publish") or [])      # ... and when it did not, the routine's own writer runs
    cli._publish({"tasks": {}})
    assert calls == ["incidents", "publish", "routine-export"]
    monkeypatch.setattr(incidents, "update", lambda *a: (_ for _ in ()).throw(RuntimeError("i")))
    monkeypatch.setattr(publish, "publish", lambda st: (_ for _ in ()).throw(RuntimeError("p")))
    monkeypatch.setattr(routine, "write_export", lambda: (_ for _ in ()).throw(RuntimeError("r")))
    cli._publish({})                                                                          # three failures: warnings only
    assert capsys.readouterr().err.count("[warn]") == 3


# =========================================================================== the weekly / daily digest
def put_report(w, rid, health, **doc):
    d = w.state / "public" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({"id": rid, "kind": "weekly" if "W" in rid else "daily", "health": health, **doc}))


def digest_run(w, monkeypatch, name, rid, status="ok", wday=2):
    from homelab_maint import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: sent.append(ev.kind) or SimpleNamespace(ok=True, handled=True))
    monkeypatch.setattr(cli.time, "localtime", lambda *a: SimpleNamespace(tm_wday=wday))
    t = core.Task(name, "C0", "daily", lambda ctx: None)
    cli._send_digests([(t, Result(status, "x", {"id": rid}), 1.0, NOW, False)])
    return sent


def test_the_weekly_report_is_emailed_once_and_a_blind_week_still_is(w, monkeypatch):
    put_report(w, "2026-W40", {"score": 97, "grade": "A"})
    assert digest_run(w, monkeypatch, "report_weekly", "2026-W40") == ["report_weekly"]
    assert digest_run(w, monkeypatch, "report_weekly", "2026-W40") == []                    # once per report id
    put_report(w, "2026-W41", {"score": None, "grade": "n/a"})
    assert digest_run(w, monkeypatch, "report_weekly", "2026-W41") == []                    # nothing to grade yet: no mail
    put_report(w, "2026-W42", {"score": None, "grade": "n/a", "monitoring_gap": True})
    assert digest_run(w, monkeypatch, "report_weekly", "2026-W42") == ["report_weekly"]    # "monitoring is blind" is the one thing to say
    assert digest_run(w, monkeypatch, "report_weekly", "2026-W43", status="error") == []   # a failed report task sends nothing


def test_the_daily_digest_only_when_notable_or_on_mondays(w, monkeypatch):
    put_report(w, "2026-10-01", {"score": 99, "grade": "A"})
    assert digest_run(w, monkeypatch, "report_daily", "2026-10-01", wday=2) == []          # quiet Wednesday
    assert digest_run(w, monkeypatch, "report_daily", "2026-10-01", wday=0) == ["digest_daily"]   # Monday: always
    put_report(w, "2026-10-02", {"score": 80, "grade": "C"})
    assert digest_run(w, monkeypatch, "report_daily", "2026-10-02", wday=2) == ["digest_daily"]
    put_report(w, "2026-10-03", {"score": 99, "grade": "A"}, actions={"freed_bytes": 3 * core.GIB})
    assert digest_run(w, monkeypatch, "report_daily", "2026-10-03", wday=2) == ["digest_daily"]


# =========================================================================== doctor
def test_doctor_runs_end_to_end_in_an_empty_world(w, monkeypatch, capsys):
    from homelab_maint import routine
    from homelab_maint.tasks import self_health
    monkeypatch.setattr(routine, "tick_state", lambda rc=None: "missing")
    monkeypatch.setattr(self_health, "doctor", lambda *a, **k: (False, "no check run yet"))      # (it reads docker and the website: not in a unit test)
    monkeypatch.setattr(core, "REGISTRY", dict(REAL_REGISTRY))
    monkeypatch.setattr(core, "DUPLICATES", [])                                              # (other tests record clashes on purpose)
    monkeypatch.setattr(cli, "IMPORT_ERRORS", [])
    rc = cli.cmd_doctor(ns())
    out = capsys.readouterr().out
    assert rc == 1                                                                           # an empty world is not healthy ...
    for label in ("config readable", "tasks registered", "no duplicate task names", "every module and plugin imported",
                  "routine.toml valid", "routine steps and cleaners match the registry", "routine tick enabled", "jobs.toml valid", "scheduler tick alive", "probes.toml valid",
                  "legacy inventory valid", "metrics sampler running", "live monitor running", "kill switch absent",
                  "ack.toml valid", "acks.json readable", "ack policy equal in ack.toml and notify.toml", "rules registry valid and in sync", "monitoring pipeline healthy, self.json fresh"):
        assert label in out, label
    assert "[ok] tasks registered" in out and "[ok] no duplicate task names" in out and "no live.json yet" in out                  # ... but it says why instead of crashing
    assert "[FAIL] routine tick enabled" in out
    assert "[ok] ack policy equal in ack.toml and notify.toml" in out                        # shipped defaults: both lists empty


def test_doctor_flags_an_ack_policy_that_differs_between_the_pager_and_the_dashboard(w, monkeypatch, capsys):
    from homelab_maint import routine
    from homelab_maint.tasks import self_health
    monkeypatch.setattr(routine, "tick_state", lambda rc=None: "missing")
    monkeypatch.setattr(self_health, "doctor", lambda *a, **k: (True, ""))
    (core.CONF_DIR / "ack.toml").write_text('[ack]\nallow_tasks = ["X"]\n')                  # the dashboard follows ack.toml, the pager notify.toml
    cli.cmd_doctor(ns())
    out = capsys.readouterr().out
    assert "[FAIL] ack policy equal in ack.toml and notify.toml  (allow_tasks differ" in out
    (core.CONF_DIR / "notify.toml").write_text('[ack]\nallow_tasks = ["X"]\n')
    cli.cmd_doctor(ns())
    assert "[ok] ack policy equal in ack.toml and notify.toml" in capsys.readouterr().out


def doctor_out(w, monkeypatch, capsys, healthz=None, ack=None):
    """Run cmd_doctor in the tiny world with the pipeline row stubbed; `healthz` = (status, body dict) of the website or None (nothing listens)."""
    from homelab_maint import routine
    from homelab_maint.tasks import self_health
    monkeypatch.setattr(routine, "tick_state", lambda rc=None: "missing")
    monkeypatch.setattr(self_health, "doctor", lambda *a, **k: (True, ""))
    asked = []
    if healthz is not None:
        monkeypatch.setattr(self_health, "http_get", lambda host, port, path, timeout: asked.append((host, port, path)) or (healthz[0], json.dumps(healthz[1]).encode(), "", 3))
    if ack:
        (w.state / "ack").mkdir(exist_ok=True)
        for name, text in ack.items():
            (w.state / "ack" / name).write_text(text)
    cli.cmd_doctor(ns())
    return capsys.readouterr().out, asked


WEB_ROWS = ("website /healthz (when deployed) has no warnings", "website login set up and readable by the site (ack/auth.json)")


def test_doctor_does_not_mind_a_website_that_is_not_deployed(w, monkeypatch, capsys):
    out, _ = doctor_out(w, monkeypatch, capsys)
    for row in WEB_ROWS:
        assert f"[ok] {row}" in out, row


def test_doctor_repeats_the_warnings_of_the_websites_healthz_when_it_is_deployed(w, monkeypatch, capsys):
    warns = ["ack: first-run setup is not complete (open the site)", "ack: nothing in front of the owner login (set BASIC_AUTH_FILE)", "x3", "x4"]
    out, asked = doctor_out(w, monkeypatch, capsys, healthz=(200, {"ok": True, "reason": "", "warnings": warns}))
    assert asked == [("127.0.0.1", 8098, "/healthz")]                                        # loopback only, GET /healthz and nothing else
    assert "[FAIL] website /healthz (when deployed) has no warnings  (ack: first-run setup is not complete (open the site); ack: nothing in front" in out and "(+1 more)" in out
    ok, _ = doctor_out(w, monkeypatch, capsys, healthz=(200, {"ok": True, "warnings": []}))
    assert "[ok] website /healthz (when deployed) has no warnings" in ok
    stale, _ = doctor_out(w, monkeypatch, capsys, healthz=(503, {"ok": False, "reason": "overview.json is stale", "warnings": []}))
    assert "[FAIL] website /healthz (when deployed) has no warnings  (/healthz answered HTTP 503: overview.json is stale" in stale


def test_doctor_asks_the_port_the_self_health_task_is_configured_for(w, monkeypatch, capsys):
    (w.conf / "maint.toml").write_text("[global]\nnotify_handle = 'x'\n[tasks.self_health]\nweb_port = 9099\n")
    _out, asked = doctor_out(w, monkeypatch, capsys, healthz=(200, {"warnings": []}))
    assert asked == [("127.0.0.1", 9099, "/healthz")]


def test_doctor_says_what_to_do_about_the_login_only_once_the_site_is_up(w, monkeypatch, capsys):
    out, _ = doctor_out(w, monkeypatch, capsys, ack={"web.key": "k" * 64})                    # ack/ exists, no auth.json, no site: first install, nothing to do yet
    assert "[ok] website login set up" in out
    out, _ = doctor_out(w, monkeypatch, capsys, healthz=(200, {"warnings": []}), ack={})
    assert "[FAIL] website login set up and readable by the site (ack/auth.json)  (first-run setup pending: sudo homelab-maint web bootstrap" in out
    good = {"v": 1, "pw": {"alg": "pbkdf2-sha256", "iter": 600000, "salt": "ab" * 16, "hash": "cd" * 32}}
    (w.state / "ack" / "auth.json").write_text(json.dumps(good))
    os.chmod(w.state / "ack" / "auth.json", 0o644)
    out, _ = doctor_out(w, monkeypatch, capsys, healthz=(200, {"warnings": []}))
    assert "[ok] website login set up" in out
    os.chmod(w.state / "ack" / "auth.json", 0o600)                                           # the site's group could not read it
    out, _ = doctor_out(w, monkeypatch, capsys)
    assert "[FAIL] website login set up" in out and "unreadable" in out and str(w.state / "ack" / "auth.json") in out
    assert f"sudo chgrp {os.getgid()} " in out                                               # the site's gid is the one web.key has (here: this user's group)
    (w.state / "ack" / "auth.json").write_text("{}")
    os.chmod(w.state / "ack" / "auth.json", 0o644)
    out, _ = doctor_out(w, monkeypatch, capsys)
    assert "[FAIL] website login set up" in out and "unusable" in out and "web bootstrap" in out


def test_doctor_without_the_ack_postbox_has_nothing_to_say_about_the_login(w, monkeypatch, capsys):
    assert not (w.state / "ack").exists()
    out, _ = doctor_out(w, monkeypatch, capsys, healthz=(200, {"warnings": []}))
    assert "[ok] website login set up" in out


# =========================================================================== core.Notifier -> notify adapter
def test_core_notifier_sends_through_notify_and_keeps_its_debounce(w, monkeypatch):
    from homelab_maint import notify
    sent = []
    monkeypatch.setattr(notify, "notifier_send", lambda cfg, name, title, status, summary, now, **kw: sent.append((name, status, kw.get("recovery"), kw.get("was"))) or True)
    n = core.Notifier({"global": {"alert_confirm_runs": 2}})
    n.evaluate("t", "Title", Result("crit", "bad"), 1000.0)
    assert sent == []                                                                        # one blip never pages
    n.evaluate("t", "Title", Result("crit", "bad"), 1900.0)
    assert sent == [("t", "crit", False, "warn")]                                            # confirmed: one page
    n.evaluate("t", "Title", Result("crit", "bad"), 2800.0)
    assert len(sent) == 1                                                                    # and no repeat before the reminder
    n.evaluate("t", "Title", Result("ok", "fine"), 3700.0)
    n.evaluate("t", "Title", Result("ok", "fine"), 4600.0)
    assert sent[-1] == ("t", "warn", True, "crit")                                           # recovery: tagged, and remembers it was critical


def test_core_notifier_falls_back_to_the_bridge_when_notify_breaks(w, monkeypatch):
    from homelab_maint import notify
    calls = []
    monkeypatch.setattr(notify, "notifier_send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("template bug")))
    monkeypatch.setattr(core, "sh", lambda cmd, *a, **k: calls.append(cmd) or CompletedProcess(cmd, 0, "", ""))
    n = core.Notifier({"global": {"alert_confirm_runs": 1, "bridge": "/bin/bridge", "notify_handle": "ohmz"}})
    n.evaluate("t", "Title", Result("crit", "bad"), 1000.0)
    assert any(c[:4] == ["runuser", "-u", "ohmz", "--"] and c[4] == "/bin/bridge" for c in calls if isinstance(c, list))   # an alert is never lost


# =========================================================================== the widget server's extra routes
@pytest.fixture
def server(w):
    from homelab_maint import server as srv
    src = srv.StatusSource(w.state / "status.json", now=NOW)
    httpd = srv.make_server("127.0.0.1", 0, src)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


@pytest.mark.parametrize("route", ["thermal", "load", "metrics", "heartbeat"])
def test_server_answers_the_metric_and_monitor_routes_with_200_json_even_with_no_data(server, route, monkeypatch):
    """Homarr shows a red triangle for any non-200: no ring, no probe state and no status.json still answer 200 with an in-band marker."""
    from homelab_maint import payloads_metrics
    monkeypatch.setattr(payloads_metrics, "load_export", lambda now=None: None)             # no ring and no published metrics.json
    code, body = get(server, f"/{route}")
    d = json.loads(body)
    assert code == 200 and isinstance(d, dict)
    if route in ("thermal", "load"):
        assert d["stale"] is True and d["error"]                                              # dimmed by the template, never an HTTP error
    if route == "metrics":
        assert d["error"]
    if route == "heartbeat":
        assert "ok" in d and "total" in d


def test_server_routes_are_get_only_and_unknown_paths_404(server):
    assert get(server, "/nope")[0] == 404 and get(server, "/overview")[0] == 200
    assert get(server, "/thermal", "POST")[0] == 405 and get(server, "/heartbeat", "DELETE")[0] == 405


def test_thermal_and_load_are_built_from_the_ring_export_at_the_servers_clock(server, monkeypatch):
    """The routes pass the server's (frozen) clock to payloads_metrics.build: with a fresh export the widgets are not stale."""
    from homelab_maint import payloads_metrics
    e = json.loads((ROOT / "widgets" / "fixtures-metrics" / "full_ring.json").read_text())
    monkeypatch.setattr(payloads_metrics, "load_export", lambda now=None: e)
    for route in ("thermal", "load"):
        code, body = get(server, f"/{route}")
        d = json.loads(body)
        assert code == 200 and d["groups"] and d["rows"] and "error" not in d, route
    assert json.loads(get(server, "/metrics")[1])["slots"] == 168                         # the raw export is served as is


# =========================================================================== cross-module contracts the glue relies on
def test_payloads_knows_the_monthly_tier():
    assert payloads.TIER_STALE_S["monthly"] >= 35 * 86400 and payloads.TIER_STALE_S["monthly"] > payloads.TIER_STALE_S["weekly"]
    assert payloads.TIER_ORDER["monthly"] > payloads.TIER_ORDER["weekly"]
    assert {"check", "daily", "weekly"} <= set(payloads.TIER_STALE_S)


def test_disk_forecast_rows_carry_the_exact_size_publish_reads(w, monkeypatch):
    from homelab_maint.tasks import checks_basic
    monkeypatch.setattr(checks_basic, "_disk_usage", lambda m: {"used": 60 * core.GIB, "free": 40 * core.GIB})
    res = checks_basic.disk_forecast(core.Ctx({"tasks": {"disk_forecast": {"watch": ["/", "/data"], "info_only": ["/x"]}}}, "disk_forecast", False, NOW))
    rows = res.metrics["mounts"]
    assert [r["mount"] for r in rows] and all(r["size"] == 100 * core.GIB for r in rows)       # used + free, so storage.json needs no statvfs


def test_audit_keeps_every_row_but_only_forks_logger_for_real_attempts(tmp_path, monkeypatch):
    """A report-mode daily run audits one "dry-run" row per would-delete file (201 on this host): the rows stay in audit.jsonl (reports
    count them), but only attempts (done, refused, failed) go to syslog. 233 `logger` forks a run were pure noise."""
    forks = []
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: forks.append(cmd) or CompletedProcess(cmd, 0, "", ""))
    core.audit("retention", "delete", "/x/a", 5, "dry-run")
    core.audit("retention", "delete", "/x/b", 5, "refused-protected")
    core.audit("retention", "delete", "/x/c", 5, "done")
    rows = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["dry-run", "refused-protected", "done"]
    assert [c[-1].split()[2] for c in forks] == ["refused-protected", "done"] and all(c[0] == "logger" for c in forks)


def test_sh_skips_logger_only_while_no_syslog_is_set(monkeypatch):
    """HOMELAB_MAINT_NO_SYSLOG keeps tests and scratch runs out of the host journal: only a `logger` argv is skipped, nothing else."""
    ran = []
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setenv("HOMELAB_MAINT_NO_SYSLOG", "1")
    assert core.sh(["logger", "-t", "x", "y"]).returncode == 0 and core.sh(["true"]).returncode == 0
    assert ran == [["true"]]
    monkeypatch.delenv("HOMELAB_MAINT_NO_SYSLOG")
    core.sh(["logger", "-t", "x", "y"])
    assert ran[-1][0] == "logger"


# =========================================================================== SPEC5 acknowledgements in the run loop
def test_overall_skips_acknowledged_tasks_but_not_the_others():
    tasks = {"a": {"status": "warn", "acked": {"until": NOW}}, "b": {"status": "ok"}}
    assert cli.overall(tasks) == "ok"                                                  # acknowledged: the true status stays, the colour does not
    assert cli.overall({**tasks, "c": {"status": "crit"}}) == "crit"
    assert cli.overall({"a": {"status": "crit", "alert": False}}) == "ok"


def test_an_acknowledged_issue_keeps_its_status_but_not_the_hero_colour(w):
    """Real acks module over the tmp state: warn -> `ack add` -> the next run (any tier) marks the entry, the hero is green again, the history
    record says `acked` (reports score it as informational) and a task of ANOTHER tier that this run did not touch is marked too."""
    from homelab_maint import acks
    w.registry({"disk": ("C0", "check", Result("warn", "/ is 9% free", issue_key="disk:/")),
                "backup": ("C0", "daily", Result("warn", "stack-backup is late", issue_key="backup:stack")), "fine": ("C0", "check", Result("ok", "fine"))})
    w.wire()
    assert cli.cmd_run(ns()) == 0 and cli.cmd_run(ns(tier="daily")) == 0
    st = w.status()
    e = st["tasks"]["disk"]
    assert e["issue_key"] == "disk:/" and re.fullmatch(r"[0-9a-f]{16}", e["fp"]) and "acked" not in e                 # the id the Acknowledge button posts
    assert st["overall"] == "warn" and st["acked_n"] == 0 and "fp" not in st["tasks"]["fine"]
    acks.add(e["fp"], days=30, by="cli")
    acks.add(st["tasks"]["backup"]["fp"], days=30, by="cli")
    assert cli.cmd_run(ns()) == 0                                                      # the check tier: `backup` (daily) is not run, only re-marked
    st = w.status()
    assert st["tasks"]["disk"]["status"] == "warn" and st["tasks"]["disk"]["acked"]["fp"] == st["tasks"]["disk"]["fp"]
    assert st["tasks"]["backup"]["acked"]["by"] == "cli" and st["acked_n"] == 2 and st["overall"] == "ok"
    h = [r for r in w.history() if r.get("task") == "disk"]
    assert "acked" not in h[0] and h[1]["acked"] is True                               # only the run covered by an ack is informational
    acks.remove(st["tasks"]["disk"]["fp"])
    assert cli.cmd_run(ns()) == 0 and w.status()["overall"] == "warn" and "acked" not in w.status()["tasks"]["disk"]      # un-acknowledge: red at once


def test_a_broken_acks_module_changes_nothing_the_colour_and_the_alert_stay(w, monkeypatch, capsys):
    from homelab_maint import acks
    monkeypatch.setattr(acks, "mark_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unreadable")))
    monkeypatch.setattr(acks, "apply_to_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unreadable")))
    w.registry({"disk": ("C0", "check", Result("crit", "full", issue_key="disk:/"))})
    w.wire()
    assert cli.cmd_run(ns()) == 0
    st = w.status()
    assert st["overall"] == "crit" and "acked" not in st["tasks"]["disk"] and "acknowledgements unavailable" in capsys.readouterr().err
    assert ("evaluate:disk", True) in w.events                                       # the notifier still saw the true result


def test_the_core_notifier_bridge_fallback_keeps_an_acknowledged_alert_silent(w, monkeypatch):
    """notify.send holds acknowledged alerts itself; only when notify is broken does core.Notifier use the old bridge, and it asks too."""
    from homelab_maint import acks, notify
    (core.CONF_DIR / "ack.toml").write_text('[ack]\nseverities = ["warn", "crit"]\n')   # this test acknowledges a crit issue on purpose
    monkeypatch.setattr(notify, "notifier_send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("template bug")))
    calls = []
    monkeypatch.setattr(core, "sh", lambda cmd, *a, **k: calls.append(cmd) or CompletedProcess(cmd, 0, "", ""))
    core.write_json_atomic(w.state / "status.json", {"tasks": {
        "t": {"title": "T", "status": "crit", "summary": "bad", "issue_key": "k1", "alert": True},
        "u": {"title": "U", "status": "crit", "summary": "bad", "issue_key": "k2", "alert": True}}})
    acks.add("t", days=30, by="cli")                                                 # only t is acknowledged (acks run on the real clock)
    now = time.time()
    n = core.Notifier({"global": {"alert_confirm_runs": 1, "bridge": "/bin/bridge", "notify_handle": "ohmz"}})
    n.evaluate("t", "T", Result("crit", "bad", issue_key="k1"), now)
    assert not any(c[:1] == ["runuser"] for c in calls if isinstance(c, list))      # held: nothing on the wire ...
    assert n.s["tasks"]["t"]["alerted"] == 2                                         # ... but the debounce state is the true one (reminders keep their cadence)
    rows = [json.loads(x) for x in (w.log / "audit.jsonl").read_text().splitlines()]
    assert any(r["task"] == "notify" and r["action"] == "suppressed" and r["outcome"] == "acknowledged" for r in rows)
    n.evaluate("u", "U", Result("crit", "bad", issue_key="k2"), now)
    assert any(c[:4] == ["runuser", "-u", "ohmz", "--"] for c in calls if isinstance(c, list))      # an issue nobody acknowledged still pages
    n.evaluate("t", "T", Result("crit", "worse", issue_key="k1-other"), now + 99999)                 # another exact error: not covered
    assert sum(1 for c in calls if isinstance(c, list) and c[:1] == ["runuser"]) == 2


# =========================================================================== SPEC5 / SPEC6 in the tick
def tick_world(w, monkeypatch):
    """Everything the tick touches replaced by recorders; returns the call log. The three feature directories exist."""
    from homelab_maint import acks, notify, registry, scheduler
    calls = []
    for d in (w.conf / "rules.d", w.state / "ack"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "tick", lambda *a: calls.append("rules") or "rules sync: applied 3 rule(s)")
    monkeypatch.setattr(scheduler, "main", lambda a: calls.append(("sched", list(a))) or 0)
    monkeypatch.setattr(notify, "flush_pending", lambda *a, **k: calls.append("flush"))
    monkeypatch.setattr(acks, "run_once", lambda *a, **k: calls.append("acks") or {})
    return calls


def test_the_tick_syncs_the_registry_before_the_scheduler_and_runs_the_acknowledge_inbox_after_notify(w, monkeypatch, capsys):
    calls = tick_world(w, monkeypatch)
    assert cli.main(["tick"]) == 0
    assert calls == ["rules", ("sched", ["tick"]), "flush", "acks"]                    # the scheduler reads the files the sync may have rewritten
    assert "rules sync: applied 3 rule(s)" in capsys.readouterr().err                  # one line for the journal, nothing when nothing changed
    calls.clear()
    assert cli.main(["tick", "--dry-run"]) == 0
    assert calls == [("sched", ["tick", "--dry-run"])]                                  # a dry run changes nothing: no sync, no inbox


def test_each_tick_hook_is_skipped_until_its_feature_exists_and_never_breaks_the_tick(w, monkeypatch, capsys):
    calls = tick_world(w, monkeypatch)
    for d in (w.conf / "rules.d", w.state / "ack"):
        d.rmdir()
    assert cli.main(["tick"]) == 0
    assert calls == [("sched", ["tick"]), "flush"]                                      # no rules.d, no ack/: nothing runs, nothing is created
    assert not (w.state / "ack").exists()
    (w.state / "acks.json").write_text("{}")                                            # acks.json alone also means the feature is installed
    calls.clear()
    assert cli.main(["tick"]) == 0 and calls == [("sched", ["tick"]), "flush", "acks"]
    tick_world(w, monkeypatch)
    from homelab_maint import acks, registry
    for mod, fn in ((registry, "tick"), (acks, "run_once")):
        monkeypatch.setattr(mod, fn, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaput")))
    capsys.readouterr()
    assert cli.main(["tick"]) == 0                                                      # two broken hooks: two warnings, the jobs still ran
    err = capsys.readouterr().err
    assert "rules sync failed" in err and "acks tick failed" in err


def test_an_idle_tick_imports_neither_the_registry_nor_the_acknowledge_module(tmp_path):
    code = ("import sys\nfrom homelab_maint import cli\ncli._rules_sync(); cli._ack_tick()\n"
            "print(sorted(m for m in sys.modules if m in ('homelab_maint.registry', 'homelab_maint.acks')))")
    r = py(tmp_path, code)
    assert r.returncode == 0 and r.stdout.strip() == "[]", (r.stdout, r.stderr)


def test_a_rule_edit_reaches_the_runner_through_the_generated_files_alone(w, monkeypatch):
    """SPEC6 v2: core.load_config is NOT changed. rules.d -> the tick -> the generated maint.toml -> load_config, within one tick."""
    import inspect
    from homelab_maint import registry as R, scheduler
    for f in (ROOT / "etc").glob("*.toml"):
        shutil.copy(f, w.conf / f.name)
        os.chmod(w.conf / f.name, 0o644)
    os.chmod(w.conf, 0o755)
    proof, _texts = R.migrate(ROOT / "etc", w.conf / "rules.d", today="2026-10-02")
    assert proof.ok
    os.chmod(w.conf / "rules.d", 0o755)
    monkeypatch.setattr(R, "default_hooks", lambda: R.NO_HOOKS)                         # no maintenance e-mail from a unit test
    monkeypatch.setattr(scheduler, "main", lambda a: 0)
    assert cli.main(["tick"]) == 0
    head = (w.conf / "maint.toml").read_text()[:400]
    assert "GENERATED from rules.d" in head and core.load_config()["tasks"]["disk_forecast"]["warn_free_pct"] == 12
    rule = w.conf / "rules.d" / "10-checks.toml"
    rule.write_text(rule.read_text().replace("warn_free_pct = 12", "warn_free_pct = 15", 1))
    assert cli.main(["tick"]) == 0
    assert core.load_config()["tasks"]["disk_forecast"]["warn_free_pct"] == 15          # the runner reads what the sync generated
    assert cli.main(["tick"]) == 0 and json.loads((w.state / "rules" / "current.json").read_text())["rules_count"] > 100
    assert "rules" not in inspect.getsource(core.load_config).lower().replace("protected", "")      # load_config knows nothing about the registry


def test_load_tasks_names_a_registry_that_cannot_import_instead_of_hiding_it(monkeypatch):
    from homelab_maint import registry
    monkeypatch.setattr(core, "REGISTRY", dict(REAL_REGISTRY))
    monkeypatch.setattr(cli, "IMPORT_ERRORS", [])
    monkeypatch.setattr(registry, "register_tasks", lambda: (_ for _ in ()).throw(RuntimeError("broken registry")))
    cli.load_tasks()
    assert cli.IMPORT_ERRORS == ["module_registry"] and "broken registry" in core.REGISTRY["module_registry"].run(None).summary


def test_every_cleaner_has_a_report_title_and_the_v2_cleaners_a_change_kind():
    """A cleaner without an ACTION_TITLES entry shows up in a report under its raw task name; the three package cleaners are maintenance."""
    from homelab_maint import reports, routine
    missing = sorted(n for n, t in REAL_REGISTRY.items() if t.klass in ("C1", "C2") and n not in reports.ACTION_TITLES)
    assert missing == [], f"add them to reports.ACTION_TITLES: {missing}"
    assert not [n for n in reports.ACTION_TITLES if n not in REAL_REGISTRY], "a title for a task that does not exist"
    assert {routine.CHANGE_KIND[n] for n in ("stale_driver_packages", "apt_autoremove_unused", "flatpak_unused")} == {"maintenance"}


# =========================================================================== the audit trail: aggregate report-mode rows, one syslog call per run
def audit_rows(w) -> list[dict]:
    return [json.loads(x) for x in (w.log / "audit.jsonl").read_text().splitlines()]


def test_report_mode_audits_a_capped_sample_and_one_aggregate_row_per_action(w):
    """A daily report-mode run wrote ~230 `dry-run` rows (retention 201, qos_classes 29): now the first DRY_SAMPLE of each action are listed,
    the rest is ONE row that carries the count and the bytes. Real attempts and refusals are never aggregated."""
    def clean(ctx):
        for i in range(300):
            assert ctx.act("delete", f"/x/{i}", 10, lambda: None) is False
        for i in range(30):
            ctx.act("class", f"svc{i}", 0, lambda: None)
        ctx.act("delete", "/protected/y", 1, lambda: None)
        return Result("ok", "would")
    core.run_task(core.Task("t", "C1", "daily", clean), {"tasks": {"t": {}}, "protected": {"patterns": ["^/protected"]}}, False)
    rows = audit_rows(w)
    d = [r for r in rows if r["action"] == "delete" and r["outcome"] == "dry-run"]
    assert len(d) == core.DRY_SAMPLE + 1 and [r["target"] for r in d[:2]] == ["/x/0", "/x/1"] and "n" not in d[0]
    agg = d[-1]
    assert agg["n"] == 300 - core.DRY_SAMPLE and agg["bytes"] == 10 * (300 - core.DRY_SAMPLE) and "more" in agg["target"]
    assert sum(r.get("n", 1) for r in d) == 300                                      # the count is exact
    c = [r for r in rows if r["action"] == "class"]
    assert len(c) == core.DRY_SAMPLE + 1 and c[-1]["n"] == 10
    assert [r["outcome"] for r in rows if r["target"] == "/protected/y"] == ["refused-protected"]      # a refusal is a decision: its own row
    assert len(rows) < 60                                                            # 331 decisions, under 60 rows


def test_the_sample_size_is_a_config_key_and_a_small_run_is_listed_in_full(w):
    def clean(n):
        def run(ctx):
            for i in range(n):
                ctx.act("delete", f"/x/{i}", 1, lambda: None)
            return Result("ok", "would")
        return run
    core.run_task(core.Task("t", "C1", "daily", clean(core.DRY_SAMPLE)), {"tasks": {}}, False)
    assert len(audit_rows(w)) == core.DRY_SAMPLE and all("n" not in r for r in audit_rows(w))       # exactly the sample: no aggregate row
    (w.log / "audit.jsonl").unlink()
    core.run_task(core.Task("t", "C1", "daily", clean(10)), {"tasks": {}, "global": {"audit_dry_sample": 3}}, False)
    rows = audit_rows(w)
    assert len(rows) == 4 and rows[-1]["n"] == 7 and rows[-1]["target"] == "(+7 more, not listed)"
    (w.log / "audit.jsonl").unlink()
    core.run_task(core.Task("t", "C1", "daily", clean(5)), {"tasks": {}, "global": {"audit_dry_sample": "junk"}}, False)
    assert len(audit_rows(w)) == 5                                                    # a bad value is the default, not an error


def test_a_task_runs_its_attempts_through_one_logger_call(w, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: calls.append((list(cmd), kw.get("input_"), kw.get("timeout"))) or CompletedProcess(cmd, 0, "", ""))

    def clean(ctx):
        for i in range(5):
            assert ctx.act("rm", f"/x/{i}", 7, lambda: None)
        ctx.act("rm", "/protected/z", 1, lambda: None)
        ctx.act("rm", "/x/a\nFORGED homelab-maint line\x1b[31m", 1, lambda: None)             # control characters cannot start a second syslog line
        assert calls == []                                                           # nothing forked while the task runs
        return Result("ok", "cleaned")
    cfg = {"tasks": {"t": {"mode": "apply"}}, "protected": {"patterns": ["^/protected"]}}
    core.run_task(core.Task("t", "C1", "daily", clean), cfg, True)
    assert [c[0] for c in calls] == [["logger", "-t", "homelab-maint"]]               # ONE logger, the lines on its stdin
    lines = calls[0][1].splitlines()
    assert len(lines) == 7 and lines[0] == "t rm done /x/0 7" and lines[5] == "t rm refused-protected /protected/z 1"
    assert "FORGED" in lines[6] and "\x1b" not in calls[0][1] and calls[0][2] == 10
    assert len(audit_rows(w)) == 7                                                   # audit.jsonl is unchanged: one row per attempt


def test_the_syslog_batch_survives_a_failing_task_nesting_and_a_flood(w, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: calls.append((list(cmd), kw.get("input_"))) or CompletedProcess(cmd, 0, "", ""))
    cfg = {"tasks": {"t": {"mode": "apply"}, "inner": {"mode": "apply"}}, "protected": {"patterns": []}}

    def boom(ctx):
        ctx.act("rm", "/a", 1, lambda: None)
        ctx.act("rm", "/b", 1, lambda: None)
        raise RuntimeError("kaput")
    res, _ = core.run_task(core.Task("t", "C1", "daily", boom), cfg, True)
    assert res.status == "error" and calls[0][1].splitlines() == ["t rm done /a 1", "t rm done /b 1"]       # a failing run still reports its attempts
    calls.clear()

    def outer(ctx):
        ctx.act("rm", "/o1", 1, lambda: None)
        core.run_task(core.Task("inner", "C1", "daily", lambda c: c.act("rm", "/i1", 1, lambda: None) and Result("ok", "i")), cfg, True)
        ctx.act("rm", "/o2", 1, lambda: None)
        return Result("ok", "o")
    core.run_task(core.Task("t", "C1", "daily", outer), cfg, True)
    assert len(calls) == 1 and calls[0][1].splitlines() == ["t rm done /o1 1", "inner rm done /i1 1", "t rm done /o2 1"]    # nested: the outer run sends
    calls.clear()

    def flood(ctx):
        for i in range(core.SYSLOG_MAX + 50):
            ctx.act("rm", f"/f/{i}", 1, lambda: None)
        return Result("ok", "f")
    core.run_task(core.Task("t", "C1", "daily", flood), {**cfg, "caps": {"max_items_per_run": 10 ** 6}}, True)
    lines = calls[0][1].splitlines()
    assert len(calls) == 1 and len(lines) == core.SYSLOG_MAX and lines[-1] == "51 more attempts of this run: see audit.jsonl"
    assert core._syslog is None                                                       # the batch is closed: a later direct audit() forks at once
    core.audit("x", "y", "z", 0, "done")
    assert calls[-1][0][-1] == "x y done z 0"


def test_a_missing_logger_never_fails_a_task_run(w, monkeypatch):
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: (_ for _ in ()).throw(PermissionError("logger")))
    res, _ = core.run_task(core.Task("t", "C1", "daily", lambda c: c.act("rm", "/a", 1, lambda: None) and Result("ok", "x")),
                           {"tasks": {"t": {"mode": "apply"}}, "protected": {"patterns": []}}, True)
    assert res.status == "ok" and core._syslog is None


def test_the_kuma_push_result_reaches_the_self_health_check(w, monkeypatch):
    from homelab_maint.tasks import self_health
    (w.conf / "kuma.toml").write_text('[push]\ntier-check = "abcdef123456"\n')
    seen, rc = [], [0]
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: CompletedProcess(cmd, rc[0], "", ""))
    monkeypatch.setattr(self_health, "note_kuma", lambda key, ok, why="", *a: seen.append((key, ok, why)))
    core.kuma_push({"global": {}}, "tier-check", "ok", "check: ok")
    rc[0] = 22
    core.kuma_push({"global": {}}, "tier-check", "ok", "check: ok")
    core.kuma_push({"global": {}}, "no-such-key", "ok", "x")                           # no token: nothing pushed, nothing recorded
    assert seen == [("tier-check", True, "curl exit 0"), ("tier-check", False, "curl exit 22")]
    monkeypatch.setattr(self_health, "note_kuma", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    core.kuma_push({"global": {}}, "tier-check", "ok", "check: ok")                    # a broken recorder never breaks a heartbeat


# =========================================================================== doctor: the registry and the pipeline rows
REG = {"present": True, "valid": True, "safe": True, "in_sync": True, "errors": [], "unsafe": [], "last_error": None, "pending": False, "drift": []}


@pytest.mark.parametrize("over,ok,why", [
    ({"present": False, "in_sync": False}, True, ""),                                          # no rules.d yet: the hand-maintained files are in use
    ({}, True, ""),
    ({"valid": False, "in_sync": False, "errors": ["rule x: bad id"]}, False, "rule x: bad id"),
    ({"safe": False, "in_sync": False, "unsafe": ["maint.toml removes a protected pattern"]}, False, "removes a protected pattern"),
    ({"in_sync": False, "last_error": {"error": "OSError: read-only"}}, False, "last sync failed: OSError: read-only"),
    ({"in_sync": False, "pending": True}, False, "unsynced: run homelab-maint rules sync"),
    ({"adopted": False, "safe": False, "in_sync": False, "pending": True, "unsafe": ["maint.toml tasks.retention.unprotect[0]: not on the baseline allow-list"]}, False,
     "not adopted yet: homelab-maint rules diff, then sudo homelab-maint rules sync --adopt"),                      # the first hour: the way out, not the finding
    ({"in_sync": False, "drift": ["jobs.toml"]}, False, "drift: ['jobs.toml']"),
])
def test_doctor_says_why_the_registry_is_not_in_sync(w, monkeypatch, capsys, over, ok, why):
    from homelab_maint import registry, routine
    from homelab_maint.tasks import self_health
    monkeypatch.setattr(registry, "status", lambda *a, **k: {**REG, **over})
    monkeypatch.setattr(self_health, "doctor", lambda *a, **k: (True, ""))
    monkeypatch.setattr(routine, "tick_state", lambda rc=None: "ok")
    cli.cmd_doctor(ns())
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "rules registry valid and in sync" in ln)
    assert line.startswith("[ok]" if ok else "[FAIL]") and why in line


def test_a_crashing_doctor_row_is_a_fail_line_and_never_hides_the_rest(w, monkeypatch, capsys):
    from homelab_maint import registry
    from homelab_maint.tasks import self_health
    monkeypatch.setattr(registry, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no registry")))
    monkeypatch.setattr(self_health, "doctor", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no docker")))
    assert cli.cmd_doctor(ns()) == 1
    out = capsys.readouterr().out
    assert "[FAIL] rules registry valid and in sync  (RuntimeError: no registry)" in out and "[FAIL] monitoring pipeline healthy, self.json fresh  (RuntimeError: no docker)" in out
    assert "[ok] kill switch absent" in out                                                      # the checks after them still ran


def test_the_tick_ends_an_acknowledgement_and_hands_exactly_one_expiry_notice_to_notify(w, monkeypatch):
    """SPEC5 s2: expiry resumes normal alerting and sends ONE notice. The tick's hook is the only caller of acks.run_once on a host without
    the ack-process job, so it must reach notify.notify_expired once per ended acknowledgement, not once per minute."""
    from homelab_maint import acks, notify
    (core.CONF_DIR / "ack.toml").write_text('[ack]\nseverities = ["warn", "crit"]\n')   # this test acknowledges a crit issue on purpose
    core.write_json_atomic(w.state / "status.json", {"tasks": {"t": {"title": "T", "status": "crit", "summary": "bad", "issue_key": "k1", "alert": True}}})
    info = acks.add("t", days=1, by="cli", now=time.time() - 2 * 86400)                    # acknowledged 2 days ago for 1 day: over
    sent = []
    monkeypatch.setattr(notify, "notify_expired", lambda now, expired=(), **k: sent.append(list(expired)) or [])
    cli._ack_tick()
    cli._ack_tick()
    assert sent == [[info.fp]]
    assert acks.is_acked(info.fp, "crit", time.time()) is None                                 # and it silences nothing any more
