"""homelab-maint command line: the ONE entry point of the umbrella (runner, scheduler tick, notifications, monitoring, migration).

  homelab-maint run --tier check|daily|weekly|monthly [--task NAME] [--dry-run | --apply] [--override | --force]
  homelab-maint status | plan [TASK] | approve TASK HASH | pause|resume [TASK] | gate NAME | doctor | serve
  homelab-maint tick [--dry-run]          the per-minute scheduler tick (homelab-maint-tick.timer): starts what is due
  homelab-maint schedule [--json]         every job, task and timer with its next run (the one place to see the routine)
  homelab-maint job run|mode|status|validate|health|export ...   external-job adapter (see: job --help)
  homelab-maint routine ...               maintenance windows, freeze, change log (see: routine --help)
  homelab-maint incidents list|show|playbook|slo|export|update       incident ledger and SLOs
  homelab-maint report daily|weekly|index [--print]                  generate a report
  homelab-maint notify send|test|route|render|export|flush|doctor ...  the one notification path; notify-test = notify test
  homelab-maint probes [run|validate|export|...]                     monitoring probes
  homelab-maint live [--once]             the 5 s live monitor (the unit runs python3 -m homelab_maint.live)
  homelab-maint metrics-sample | metrics-export | publish           sensor ring, public export for the website
  homelab-maint migrate status|plan|check|cutover|rollback ...       retire legacy jobs, one reversible step at a time
  homelab-maint new task|job|probe NAME | plugins                    scaffolding and plugin discovery
  homelab-maint ack list|add|remove|process|issue-token|export|explain|init|validate|doctor   acknowledged known issues (SPEC5)
  homelab-maint web bootstrap             show the website's first-run login secret once (root terminal only; SPEC6 s7)
  homelab-maint swap [status] | relieve [--apply]   who holds the swap, is it in use, and a guarded way to empty it
  homelab-maint rules list|show|check|diff|sync|history|rollback|export|migrate|explain|where|orphans   what the script does, defined once (SPEC6)
  homelab-maint self-health [--json] [--check] [--write] [--published]   health of the monitoring pipeline itself
  homelab-maint smart-event ...           smartd -M exec hook (prints nothing, never raises)

Import cost matters: the scheduler tick imports this module every minute, so everything but core is imported lazily.
Subcommands with free-form flags are handed to their module's own parser BEFORE argparse sees them (PASS below).
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import pkgutil
import socket
import sys
import time
from types import SimpleNamespace

from . import core
from .core import (GIB, Locked, Result, audit, human, kuma_push, load_config, paused, plan_hash, read_json, run_task,
                   tier_lock, write_json_atomic)
from .core import CONF_DIR, STATE_DIR  # noqa: F401 - kept as attributes: tests patch cli.STATE_DIR; the code reads core.* so patches reach it

TIERS = ("check", "daily", "weekly", "monthly")      # monthly has no timer: the tick runs it in the routine's monthly window
EXTRA_TASK_MODULES = ("reports", "routine")          # register tasks from outside tasks/: report_daily/weekly, routine_*
# `homelab-maint NAME ...rest` -> module.fn([*lead, *rest]): these modules parse their own flags (REMAINDER cannot take leading options).
PASS = {"routine": ("routine", "main", ()), "incidents": ("incidents", "main", ()), "report": ("reports", "main", ()),
        "notify": ("notify", "main", ()), "migrate": ("legacy", "main", ()), "probes": ("probes", "main", ()),
        "job": ("scheduler", "main", ()), "live": ("live", "main", ()), "serve": ("server", "main", ()),   # serve: --port/--bind/--status
        "new": ("scaffold", "main", ("new",)), "plugins": ("scaffold", "main", ("plugins",)),
        "ack": ("acks", "main", ()), "web": ("acks", "web_main", ()), "swap": ("swapwatch", "swap_main", ()), "rules": ("registry", "main", ()),
        "self-health": ("tasks.self_health", "main", ())}
DIGEST_KINDS = {"report_daily": "daily", "report_weekly": "weekly"}


# --------------------------------------------------------------------------- task discovery
IMPORT_ERRORS: list[str] = []          # names of the placeholder `error` tasks load_tasks registered (doctor lists them)


def _import_error_task(mod: str, exc: BaseException) -> None:
    """A module that cannot be imported must not blind the others: it becomes an `error` check that says so (and pages)."""
    msg = f"import failed: {type(exc).__name__}: {exc}"[:130]
    name = "module_" + mod.rsplit(".", 1)[-1]
    core.REGISTRY[name] = core.Task(name, "C0", "check", lambda ctx, m=msg: Result("error", m), f"Module {mod.rsplit('.', 1)[-1]}", 30)
    if name not in IMPORT_ERRORS:                      # load_tasks runs once per routine step: list the module once
        IMPORT_ERRORS.append(name)


def load_tasks() -> None:
    """Register every task: homelab_maint/tasks/*, reports and routine (outside tasks/), then the owner's plugins.d."""
    from . import tasks as pkg
    mods = [f"{pkg.__name__}.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)] + [f"{__package__}.{m}" for m in EXTRA_TASK_MODULES]
    for mod in mods:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            _import_error_task(mod, exc)
    try:
        from . import scaffold
        rep = scaffold.load_plugins(disabled=tuple(load_config().get("global", {}).get("disabled_plugins", [])))   # never raises
        IMPORT_ERRORS.extend(n for n in rep.error_tasks if n not in IMPORT_ERRORS)    # a refused plugin is an error task too
    except Exception as exc:  # noqa: BLE001
        _import_error_task("plugins", exc)
    try:
        from . import registry
        registry.register_tasks()                          # the C0 check `rules_registry`: valid, applied, in sync (SPEC6)
    except Exception as exc:  # noqa: BLE001
        _import_error_task("registry", exc)


def overall(tasks: dict) -> str:
    """The host's colour: an acknowledged problem keeps its true status in status.json but is not the colour (SPEC5)."""
    worst = 0
    for t in tasks.values():
        if t.get("alert", True) and not t.get("acked"):
            worst = max(worst, core.LEVELS.get(t.get("status", "ok"), 0))
    return {0: "ok", 1: "warn", 2: "crit"}[worst]


def _scalars(metrics: dict) -> dict:
    """History only needs small scalars; lists of rows belong in status.json, not in the time series."""
    return {k: v for k, v in metrics.items() if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 40)}


class _state_lock:
    """Short exclusive lock around the read-merge-write of status.json/alerts.json.

    Tiers run concurrently (check every 15 min while daily/weekly are mid-run), each under its own
    tier lock, so the shared files need their own lock or one tier overwrites another's results.
    NOTHING slow may happen while it is held (no sending, no publishing): see cmd_run.
    """
    def __enter__(self):
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(core.STATE_DIR / "state.lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# --------------------------------------------------------------------------- run
class _ReportOnlyGuard:
    """Stand-in when routine.py cannot be loaded: every task still runs (the checks keep working), nothing may mutate."""
    def order(self, tasks):
        return sorted(tasks, key=lambda t: ({"C0": 0, "C1": 1, "C2": 2}.get(t.klass, 3), t.name))

    def begin(self, t, apply, manual=False, force=False):
        return SimpleNamespace(run=True, apply=False)

    def task_cfg(self, cfg, t, d):
        return cfg

    def after(self, *_a):
        return None


def _guard():
    """(routine module | None, guard). routine.RunGuard = windows, freeze, busy gates, canary caps, post-check; it fails closed."""
    try:
        from . import routine
        return routine, routine.RunGuard()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] routine guard unavailable, running report-only: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None, _ReportOnlyGuard()


def _ack_mark(entry: dict, name: str, now: float) -> None:
    """SPEC5: fingerprint one status entry (entry["fp"]) and mark it acknowledged (entry["acked"]) when an ack covers it. Fails closed: no
    acks module, an unreadable store or any exception means the entry is simply not acknowledged (the alert goes out, the colour stays)."""
    try:
        from . import acks
        acks.mark_entry(name, entry, now)
    except Exception:  # noqa: BLE001
        entry.pop("acked", None)


def _ack_apply(status: dict, now: float) -> None:
    """Mark every entry of status (also those this tier did not run: an ack added or ended since their last run) and set status["acked_n"]."""
    try:
        from . import acks
        acks.apply_to_status(status, now)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] acknowledgements unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)


def _notifier(cfg: dict):
    """notify.HermesNotifier(defer=True): the debounce state machine of core.Notifier with delivery through notify.send.
    evaluate() only QUEUES (a short durable write), deliver() sends after the state lock is released, so a dead SMTP/SMS
    transport (up to 90 s a try) never holds the lock every tier needs. core.Notifier (same adapter, inline) is the fallback."""
    try:
        from .notify import HermesNotifier
        return HermesNotifier(cfg, defer=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] notify unavailable, alerting inline: {type(exc).__name__}: {exc}", file=sys.stderr)
        return core.Notifier(cfg)


def _publish(status: dict) -> None:
    """Incident ledger first (the export reads it), then the public files the website reads. Optional: maintenance never depends on them."""
    try:
        from . import incidents
        incidents.update(status, None, time.time())       # never raises; history=None: it reads history.jsonl itself
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] incidents update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        from . import publish
        written = publish.publish(status)                  # never raises, < 1 s; builds routine.json from the same run
    except Exception as exc:  # noqa: BLE001
        written = []
        print(f"[warn] publish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    if "routine.json" not in written:                      # fallback: the routine's own writer (no scrubbing, one more systemctl call)
        try:
            from . import routine
            routine.write_export()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] routine export failed: {exc}", file=sys.stderr)


def _notable(doc: dict) -> bool:
    """Is a DAILY report worth an email on a day that is not Monday? (SPEC4 S7: digest_daily only when something happened.)"""
    h, inc, act = (doc.get(k) if isinstance(doc.get(k), dict) else {} for k in ("health", "incidents", "actions"))
    return bool(inc.get("opened") or inc.get("open_now") or h.get("monitoring_gap") or str(h.get("grade") or "A")[:1] not in "AB"
                or (act.get("freed_bytes") or 0) >= GIB)


def _send_digests(done: list) -> None:
    """Email the report a report_* task just wrote (reports.py sends nothing itself): digest_daily when notable or on Mondays,
    report_weekly with the weekly report (the Wednesday window; the report is the week that just ended). Once per report id.
    An ungraded report (no history yet) is skipped unless it says monitoring is blind. Never raises."""
    try:
        sent_p = core.STATE_DIR / "digest-sent.json"
        sent = read_json(sent_p, {}) or {}
        for t, res, *_rest in done:
            kind, rid = DIGEST_KINDS.get(t.name), res.metrics.get("id")
            if not kind or not rid or res.status == "error" or sent.get(kind) == rid:
                continue
            doc = read_json(core.STATE_DIR / "public" / "reports" / f"{rid}.json")
            h = (doc or {}).get("health") if isinstance((doc or {}).get("health"), dict) else {}
            if not isinstance(doc, dict) or (h.get("score") is None and not h.get("monitoring_gap")):
                continue
            if kind == "daily" and not _notable(doc) and time.localtime().tm_wday != 0:
                continue
            from . import notify
            d = notify.send(notify.report_event(doc))
            if d.ok or d.handled:                           # a failed send is retried by the next run of the report task
                sent[kind] = rid
                write_json_atomic(sent_p, sent, 0o600)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] report digest failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_run(a) -> int:
    cfg = load_config()
    load_tasks()
    apply = bool(a.apply) and not a.dry_run
    selected = [t for t in core.REGISTRY.values()
                if (a.task and t.name == a.task) or (not a.task and t.tier == a.tier)]
    if a.task and not selected:
        print(f"unknown task {a.task}", file=sys.stderr)
        return 2
    selected = [t for t in selected if cfg.get("tasks", {}).get(t.name, {}).get("enabled", True)]
    tier = a.tier if not a.task else selected[0].tier if selected else "check"
    try:
        lock = tier_lock(tier)
        lock.__enter__()
    except Locked:
        print(f"tier {tier} already running; skipping", file=sys.stderr)
        return 0
    try:
        routine, guard = _guard()
        # manual = the owner's `run --task` (--override/--force or a terminal), never a timer, the scheduler or the tick (--scheduled)
        manual = bool(routine and routine.is_manual(a))
        done: list[tuple] = []
        # Tasks run WITHOUT the state lock (they can take minutes); only the merge below is locked.
        # guard.order: routine order for managed daily/weekly/monthly tasks (verify and report last), the rest C0 < C1 < C2 by name
        # (spike_sampler runs before stuck_detector, which reads its sample).
        for t in guard.order(selected):
            d = guard.begin(t, apply, manual=manual, force=bool(getattr(a, "force", False)))
            if not d.run:
                continue                # not this task's moment (window, freeze, busy gate, done): keep its last result
            res, dur = run_task(t, guard.task_cfg(cfg, t, d), apply and d.apply)
            guard.after(t, d, res, dur)
            done.append((t, res, dur, time.time(), apply and d.apply))
        enabled = {n for n in core.REGISTRY if cfg.get("tasks", {}).get(n, {}).get("enabled", True)}
        with _state_lock():
            status = read_json(core.STATE_DIR / "status.json", {}) or {}
            status.setdefault("tasks", {})
            notifier = _notifier(cfg)
            for t, res, dur, ts, applied in done:
                tcfg = cfg.get("tasks", {}).get(t.name, {})
                entry = {
                    "title": t.title, "klass": t.klass, "tier": t.tier, "status": res.status,
                    "summary": res.summary, "last_run": ts, "duration_s": round(dur, 2),
                    "reclaimed_bytes": res.reclaimed_bytes, "metrics": res.metrics,
                    "items": res.items[:12], "alert": res.alert,
                    "mode": "apply" if (applied and tcfg.get("mode") == "apply" and t.klass != "C0"
                                        and not paused(t.name)) else ("check" if t.klass == "C0" else "dry-run"),
                }
                if res.plan is not None:
                    entry["plan"] = res.plan
                    entry["plan_hash"] = plan_hash(res.plan)
                if getattr(res, "issue_key", None):
                    entry["issue_key"] = res.issue_key           # SPEC5: the task knows better than its summary which error this is
                _ack_mark(entry, t.name, ts)                     # entry["fp"] (failing) and entry["acked"] (covered by an acknowledgement)
                status["tasks"][t.name] = entry
                # "alert" per run: incidents/SLOs/reports then honour alert=False findings (stuck_detector, orphan_report) in history too
                core.append_history({"t": ts, "kind": "task", "task": t.name, "status": res.status, "alert": res.alert,
                                     "reclaimed": res.reclaimed_bytes, "dur": round(dur, 2),
                                     "metrics": _scalars(res.metrics) if t.klass == "C0" else {},
                                     **({"acked": True} if entry.get("acked") else {})})       # reports: an acknowledged result is informational
                if t.klass != "C0" and res.reclaimed_bytes:
                    status.setdefault("reclaimed_log", []).append({"t": ts, "task": t.name, "bytes": res.reclaimed_bytes})
                if not res.metrics.get("self_notifies"):    # probes in alert_mode = "events" page per probe themselves
                    notifier.evaluate(t.name, t.title, res, ts)
            # Entries for tasks that were removed or disabled must not count as failing forever. Not the scheduler's rows (klass J):
            # the tick owns those and would only have to put them back within a minute.
            for name in [n for n, e in status["tasks"].items() if n not in enabled and e.get("klass") != "J"]:
                del status["tasks"][name]
            now = time.time()
            status["reclaimed_log"] = [r for r in status.get("reclaimed_log", []) if now - r["t"] < 90 * 86400]
            _ack_apply(status, now)                              # before overall(): an acknowledged task never colours the hero
            notifier.save()
            status.update(schema=1, generated_at=now, host=socket.gethostname(), paused=paused(),
                          overall=overall(status["tasks"]))
            status.setdefault("tier_runs", {})[tier] = {"last_run": now, "dry_run": not apply}
            write_json_atomic(core.STATE_DIR / "status.json", status)
        # ---- everything below runs WITHOUT the state lock: it can wait on the network
        _publish(status)
        kuma_push(cfg, f"tier-{tier}", status["overall"], f"{tier}: {status['overall']} ({len(selected)} tasks)")
        if hasattr(notifier, "deliver"):
            notifier.deliver()                              # sends what evaluate() queued + retries earlier failures; never raises
        if not a.dry_run and not manual:
            _send_digests(done)
        failed = [(t.name, r.summary) for t, r, *_ in done if r.status == "error"]
        for name, summary in failed:
            print(f"[error] {name}: {summary}", file=sys.stderr)
        return 1 if failed else 0       # non-zero makes the systemd unit visibly fail (failed_units sees it)
    finally:
        lock.__exit__(None, None, None)


def cmd_status(_a) -> int:
    st = read_json(core.STATE_DIR / "status.json", {}) or {}
    if not st:
        print("no status yet; run: homelab-maint run --tier check")
        return 0
    age = time.time() - st.get("generated_at", 0)
    print(f"overall={st.get('overall')} paused={st.get('paused')} updated {age / 60:.0f} min ago")
    for name, e in sorted(st.get("tasks", {}).items(), key=lambda kv: (str(kv[1].get("tier", "")), kv[0])):
        freed = f" freed={human(e['reclaimed_bytes'])}" if e.get("reclaimed_bytes") else ""
        print(f"  {e.get('status', '?'):<7} {e.get('klass', '?')} {str(e.get('tier', '')):<7} {name:<22} {e.get('summary', '')}{freed}")
    return 0


def cmd_plan(a) -> int:
    st = read_json(core.STATE_DIR / "status.json", {}) or {}
    for name, e in st.get("tasks", {}).items():
        if e.get("klass") == "C2" and (not a.task or a.task == name):
            print(f"{name}: hash={e.get('plan_hash')}  {e['summary']}")
            print(json.dumps(e.get("plan"), indent=1)[:4000])
    return 0


def cmd_approve(a) -> int:
    cfg = load_config()
    load_tasks()
    t = core.REGISTRY.get(a.task)
    if not t or t.klass != "C2":
        print("not a C2 task", file=sys.stderr)
        return 2
    if paused(a.task):
        print("paused", file=sys.stderr)
        return 3
    # Re-plan now; refuse if it differs from what was approved (plan grew or changed).
    res, _ = run_task(t, cfg, apply=False)
    if res.plan is None or plan_hash(res.plan) != a.hash:
        print(f"plan changed (now {plan_hash(res.plan) if res.plan else None}); review with: homelab-maint plan {a.task}",
              file=sys.stderr)
        return 4
    ap = core.STATE_DIR / "approvals"
    ap.mkdir(parents=True, exist_ok=True)
    (ap / f"{a.task}.{a.hash}").write_text(str(time.time()))
    audit(a.task, "approve", a.hash, 0, "approved")
    # Apply with the approval present: the task checks core.approved(task, hash) itself.
    cfg.setdefault("tasks", {}).setdefault(a.task, {})["mode"] = "apply"
    res, _ = run_task(t, cfg, apply=True)
    print(f"{res.status}: {res.summary} (freed {human(res.reclaimed_bytes)})")
    try:
        from . import routine
        routine.record_approved(a.task, res)               # change log + post-check of the applied plan
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] change log: {exc}", file=sys.stderr)
    return 0


def cmd_pause(a, on: bool) -> int:
    p = core.CONF_DIR / (f"PAUSE.{a.task}" if a.task else "PAUSE")
    if on:
        p.write_text(time.strftime("%FT%T") + "\n")
    elif p.exists():
        p.unlink()
    print(("paused " if on else "resumed ") + (a.task or "everything"))
    return 0


def cmd_gate(a) -> int:
    """Exit 0 = fine to proceed, 1 = busy (systemd ExecCondition skips the unit quietly).

    Imports only tasks.gates so a broken unrelated module cannot make the gate defer forever. Any
    internal error exits 255: for ExecCondition that is a unit FAILURE, which failed_units reports.
    """
    try:
        from .tasks import gates
        return int(gates.cli_gate(a.name))
    except Exception as exc:  # noqa: BLE001
        print(f"gate error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 255


# --------------------------------------------------------------------------- the umbrella's other entry points
def _flush_notify() -> None:
    """Replay pages the transport could not take earlier, once a tick: the delay after the circuit closes is a minute, not the
    next 15-minute check run. With nothing queued it is one short locked read."""
    try:
        from . import notify
        notify.flush_pending()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] notify flush failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _rules_sync() -> None:
    """SPEC6: rules.d is the source of the config files the scheduler reads next (jobs.toml, probes.toml, ...), so a changed registry is
    validated and compiled BEFORE the jobs are looked at. About a millisecond when nothing changed; nothing is imported until a registry
    exists (rules.d is created by `rules migrate`). A broken registry keeps the last good files; the line printed here goes to the journal."""
    try:
        if not (core.CONF_DIR / "rules.d").is_dir():
            return
        from . import registry
        msg = registry.tick()                              # never raises, never waits for the sync lock; '' when there was nothing to do
        if msg:
            print(msg, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] rules sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _ack_tick() -> None:
    """SPEC5, every minute: apply the acknowledge requests the website and the e-mail link queued (the inbox), end acknowledgements that
    ran out and hand their expiry notices to notify. Idle = one directory listing and one read; nothing is imported (or created) until the
    feature is installed (acks.json or ack/ exists)."""
    try:
        if not ((core.STATE_DIR / "acks.json").exists() or (core.STATE_DIR / "ack").is_dir()):
            return
        from . import acks
        acks.run_once()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] acks tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_tick(a) -> int:
    from . import scheduler
    if not a.dry_run:
        _rules_sync()
    rc = int(scheduler.main(["tick", *(["--dry-run"] if a.dry_run else []), *(["--now", str(a.now)] if a.now is not None else [])]) or 0)
    if not a.dry_run:
        _flush_notify()                                    # after the jobs were started: a slow transport delays nothing else
        _ack_tick()
    return rc


def cmd_schedule(a) -> int:
    from . import scheduler
    return int(scheduler.main(["explain", *(["--json"] if a.json else [])]) or 0)


def cmd_notify_test(a) -> int:
    from . import notify
    return int(notify.main(["test", *a.kinds, *(["--dry-run"] if a.dry_run else [])]) or 0)


def cmd_publish(_a) -> int:
    from . import publish
    return int(publish.main())


def cmd_metrics_sample(a) -> int:
    """One sensor sample into the ring, then refresh the public export so metrics.json is at most a minute old."""
    from . import metrics_ring
    rc = int(metrics_ring.main(["sample", *(["-v"] if a.verbose else [])]) or 0)
    try:
        from . import publish
        publish.publish()                                  # never raises
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] publish failed: {exc}", file=sys.stderr)
    return rc


def cmd_metrics_export(a) -> int:
    from . import metrics_ring
    return int(metrics_ring.main(["export", *(["--pretty"] if a.pretty else [])]) or 0)


def cmd_doctor(_a) -> int:
    cfg = load_config()
    load_tasks()
    ok = True

    def chk(label, cond, hint=""):
        nonlocal ok
        print(f"[{'ok' if cond else 'FAIL'}] {label}" + (f"  ({hint})" if not cond and hint else ""))
        ok = ok and cond

    def probe(label, fn):
        """One self-check; a check that itself crashes is a FAIL line, never a traceback that hides the rest."""
        try:
            cond, hint = fn()
        except Exception as exc:  # noqa: BLE001
            cond, hint = False, f"{type(exc).__name__}: {exc}"
        chk(label, cond, hint)

    def routine_cfg():
        from . import routine
        rc = routine.load_config()
        return rc.valid, "; ".join(rc.errors)[:200]

    def routine_steps():
        """Steps that name a task nobody registered (a typo, a removed module) and cleaners no routine names (the guard holds
        those to report-only: a stale installed routine.toml lacks the newer ones; merge routine.toml.dist)."""
        from . import routine
        rc = routine.load_config()
        steps = {s.task for e in rc.entries for s in e.steps}
        gone = sorted(steps - set(core.REGISTRY))
        loose = sorted(t.name for t in core.REGISTRY.values() if t.klass in ("C1", "C2") and t.tier != "check" and t.name not in steps)
        why = (f"unregistered: {', '.join(gone[:4])}. " if gone else "") + (f"not in routine.toml (report-only): {', '.join(loose[:4])}"
                                                                          + (f" +{len(loose) - 4}" if len(loose) > 4 else "") if loose else "")
        return not gone and not loose, why

    def tick_enabled():
        from . import routine
        s = routine.tick_state()
        return s != "missing", "enable homelab-maint-tick.timer: without it the routine never retries, the monthly window never opens"

    def scheduler_ok():
        from . import scheduler
        probs = scheduler.validate()
        return not probs, f"{len(probs)} problem(s), first: {probs[0] if probs else ''}"

    def scheduler_alive():
        from . import scheduler
        st, summ = scheduler.health()
        return st == "ok", f"{st}: {summ}"

    def probes_ok():
        from . import probes
        _d, rows, errs = probes.load_probes()
        return not errs and bool(rows), f"{len(errs)} problem(s), first: {errs[0] if errs else 'no probe defined'}"

    def inventory_ok():
        from . import legacy
        return len(legacy.load_inventory().items) > 0, "empty legacy inventory"

    def sampler_ok():
        from . import metrics_ring
        e = metrics_ring.export()
        return not e.get("stale"), "no fresh sample: enable homelab-maint-metrics.timer"

    def live_ok():
        d = read_json(core.STATE_DIR / "public" / "live.json", {}) or {}
        if not isinstance(d.get("generated_at"), (int, float)):
            return False, "no live.json yet: enable homelab-maint-live.service"
        age = time.time() - float(d["generated_at"])
        return age < 60, f"live.json is {age:.0f} s old: enable homelab-maint-live.service"

    def registry_ok():
        from . import registry
        s = registry.status()
        if not s["present"]:
            return True, ""                                # rules.d not set up yet: the config files are hand-maintained
        first_hour = "not adopted yet: homelab-maint rules diff, then sudo homelab-maint rules sync --adopt" if s.get("adopted") is False else ""
        why = first_hour or (s["unsafe"][:1] or s["errors"][:1] or [None])[0] or ("last sync failed: " + str((s["last_error"] or {}).get("error")) if s["last_error"]
                                                                  else "unsynced: run homelab-maint rules sync" if s["pending"] else f"drift: {s['drift']}")
        return bool(s["valid"] and s["safe"] and s["in_sync"]), str(why)[:200]

    def pipeline_ok():
        from .tasks import self_health
        return self_health.doctor()

    def ack_policy_ok():
        """The pager (notify.toml [ack]) and the dashboard/inbox (ack.toml [ack]) must agree on which tasks may be acknowledged."""
        from . import acks, notify
        a, n = acks.load_config()["ack"], notify.ack_cfg(notify.load_config(cfg))
        mine = {"require_rule": a.get("require_rule", True) is not False, "allow_tasks": sorted(a.get("allow_tasks") or []),
                "deny_tasks": sorted(a.get("deny_tasks") or []), "deny_prefixes": sorted(a.get("deny_prefixes") or [])}
        theirs = {"require_rule": n["require_rule"], "allow_tasks": sorted(n["allow"]), "deny_tasks": sorted(n["deny"]), "deny_prefixes": sorted(n["deny_prefix"])}
        bad = [k for k in mine if mine[k] != theirs[k]]
        return not bad, f"{', '.join(bad)} differ between ack.toml and notify.toml [ack]: ack.toml decides for the pager too (notify.toml's copy is deprecated and ignored); make them equal or delete the notify.toml keys"

    site: dict = {}

    def site_health():
        """GET http://127.0.0.1:<port>/healthz once (loopback only, 3 s): {"answered", "code", "doc"}. Nothing deployed = refused connection."""
        if not site:
            from .tasks import self_health
            o = cfg.get("tasks", {}).get("self_health", {}) or {}
            port = o.get("web_port") if isinstance(o.get("web_port"), int) and 0 < o.get("web_port") < 65536 else 8098
            code, body, err, _ms = self_health.http_get("127.0.0.1", port, "/healthz", 3.0)
            try:
                doc = json.loads(body) if code is not None else None
            except ValueError:
                doc = None
            site.update(answered=code is not None, code=code, err=err, port=port, doc=doc if isinstance(doc, dict) else {})
        return site

    def website_ok():
        """SPEC6 s6/s7: what the site itself says is wrong ('warnings' of /healthz: no login yet, unreadable auth.json, nothing in front of
        the login, inbox not writable). Not deployed (nothing listens) is fine here; the pipeline row says if it should be."""
        s = site_health()
        if not s["answered"]:
            return True, ""
        warns = [str(w) for w in (s["doc"].get("warnings") or []) if isinstance(w, str)]
        if s["code"] != 200:
            warns.insert(0, f"/healthz answered HTTP {s['code']}: {str(s['doc'].get('reason') or '?')[:60]}")
        return not warns, "; ".join(w[:110] for w in warns[:3]) + (f" (+{len(warns) - 3} more)" if len(warns) > 3 else "")

    def login_ok():
        """The acknowledge login: auth.json present, valid and readable by the site's group. Absent is only a problem once the site is up."""
        from . import acks
        if not (core.STATE_DIR / "ack").is_dir():
            return True, ""
        state, why = acks.auth_state()
        if state == "ok":
            return True, ""
        if state == "absent":
            return not site_health()["answered"], "first-run setup pending: sudo homelab-maint web bootstrap, open the site, enter the secret"
        from . import acks_auth
        return False, (f"auth.json {state}: {why}; fix: sudo chgrp {acks_auth.web_gid(core.STATE_DIR / 'ack')} {core.STATE_DIR / 'ack' / 'auth.json'} "
                       f"(mode 0640), or remove it and run: sudo homelab-maint web bootstrap")

    chk("config readable", bool(cfg.get("global")), str(core.CONF_DIR / "maint.toml"))
    chk("state dir writable", os.access(core.STATE_DIR, os.W_OK), str(core.STATE_DIR))
    try:
        from . import notify
        for label, ok_, hint in notify.doctor(cfg):        # the delivery path: transport user, Hermes scripts, breaker, outbox
            chk(label, ok_, hint)
    except Exception as exc:  # noqa: BLE001
        chk("notify module importable", False, f"{type(exc).__name__}: {exc}")
    try:
        from . import acks
        for label, ok_, hint in acks.doctor():             # SPEC5: ack.toml, acks.json, the inbox and its permissions
            chk(label, ok_, hint)
    except Exception as exc:  # noqa: BLE001
        chk("acks module importable", False, f"{type(exc).__name__}: {exc}")
    chk("tasks registered", len(core.REGISTRY) > 0, f"{len(core.REGISTRY)} tasks")
    chk("no duplicate task names", not core.DUPLICATES, ", ".join(f"{n} ({a} vs {b})" for n, a, b in core.DUPLICATES[:3]))
    chk("every module and plugin imported", not IMPORT_ERRORS, ", ".join(IMPORT_ERRORS))
    probe("routine.toml valid", routine_cfg)
    probe("routine steps and cleaners match the registry", routine_steps)
    probe("routine tick enabled", tick_enabled)
    probe("jobs.toml valid", scheduler_ok)
    probe("scheduler tick alive", scheduler_alive)
    probe("probes.toml valid", probes_ok)
    probe("legacy inventory valid", inventory_ok)
    probe("metrics sampler running", sampler_ok)
    probe("live monitor running", live_ok)
    probe("ack policy equal in ack.toml and notify.toml", ack_policy_ok)
    probe("website /healthz (when deployed) has no warnings", website_ok)
    probe("website login set up and readable by the site (ack/auth.json)", login_ok)
    probe("rules registry valid and in sync", registry_ok)
    probe("monitoring pipeline healthy, self.json fresh", pipeline_ok)
    chk("kill switch absent", not paused(), "global PAUSE present")
    for n in sorted(core.REGISTRY):
        t = core.REGISTRY[n]
        print(f"    {t.klass} {t.tier:<7} {n}")
    return 0 if ok else 1


def _passthrough(name: str, rest: list[str]) -> int:
    mod, fn, lead = PASS[name]
    return int(getattr(importlib.import_module(f"{__package__}.{mod}"), fn)([*lead, *rest]) or 0)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["smart-event"]:
        # smartd hook, handled before argparse and every other import: smartd treats ANY output as a hook failure, so a usage
        # error is as bad as a crash. Exit 0 = delivered or held by policy, 1 = the owner was not told (the stub then falls back).
        try:
            from . import smart_hook
            return int(smart_hook.smart_event_main(args[1:]))
        except Exception:  # noqa: BLE001
            return 1
    if args and args[0] in PASS:
        return _passthrough(args[0], args[1:])
    ap = argparse.ArgumentParser(prog="homelab-maint", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    r = sub.add_parser("run", help="run a tier (or one task)")
    r.add_argument("--tier", choices=TIERS, default="check")
    r.add_argument("--task")
    r.add_argument("--override", action="store_true", help="owner override of the routine window and done-state for --task (a freeze or a halt still holds)")
    r.add_argument("--force", action="store_true", help="with --task: also override a freeze or a halt (implies --override)")
    r.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)     # the tick/routine: held to every window and freeze
    g = r.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    sub.add_parser("status", help="human summary of the last results")
    p = sub.add_parser("plan", help="show C2 plans (with the hash you must approve)"); p.add_argument("task", nargs="?")
    q = sub.add_parser("approve", help="apply an approved C2 plan"); q.add_argument("task"); q.add_argument("hash")
    pa = sub.add_parser("pause", help="kill switch"); pa.add_argument("task", nargs="?")
    re_ = sub.add_parser("resume", help="lift the kill switch"); re_.add_argument("task", nargs="?")
    ga = sub.add_parser("gate", help="exit 0 = idle (proceed), 1 = busy (skip); for systemd ExecCondition"); ga.add_argument("name")
    sub.add_parser("doctor", help="self-check of paths, config, notification path, scheduler, probes")
    t = sub.add_parser("tick", help="the per-minute scheduler tick: start what is due, reap what finished")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--now", type=float, help=argparse.SUPPRESS)
    sc = sub.add_parser("schedule", help="the unified schedule: every job, task and timer with its next run")
    sc.add_argument("--json", action="store_true")
    nt = sub.add_parser("notify-test", help="send one clearly labelled TEST per notification kind (run as root)")
    nt.add_argument("kinds", nargs="*", metavar="KIND[.SEV]")
    nt.add_argument("--dry-run", action="store_true", help="render and route, send nothing")
    sub.add_parser("publish", help="write the public JSON files the website reads")
    ms = sub.add_parser("metrics-sample", help="take one sensor sample into the 7-day ring (then publish)")
    ms.add_argument("-v", "--verbose", action="store_true", dest="verbose")
    me = sub.add_parser("metrics-export", help="print the sensor ring export as JSON")
    me.add_argument("--pretty", action="store_true")
    # Listed for --help only; main() hands these to their own parser before this one runs (see PASS).
    sub.add_parser("routine", help="maintenance windows, freeze, change log (routine --help)")
    sub.add_parser("incidents", help="incident ledger and SLOs: list | show ID | playbook TASK | slo | export | update")
    sub.add_parser("report", help="daily|weekly|index [--print]: generate a report")
    sub.add_parser("notify", help="the notification path: send | test | route | render | export | flush | doctor")
    sub.add_parser("migrate", help="retire legacy jobs: status | plan | check | cutover | rollback | journal | audit")
    sub.add_parser("probes", help="monitoring probes: run | validate | export | forget")
    sub.add_parser("job", help="external jobs: run | mode | status | validate | health | export")
    sub.add_parser("live", help="the live monitor (--once prints one sample, writes nothing)")
    sub.add_parser("serve", help="the Homarr widget server (127.0.0.1:9111); --port/--bind/--status")
    sub.add_parser("new", help="scaffold a task, job or probe: new task|job|probe NAME")
    sub.add_parser("plugins", help="load plugins.d as the runner would and report what was loaded or refused")
    sub.add_parser("ack", help="acknowledged known issues: list | add | remove | process | issue-token | export | explain | init | validate | doctor")
    sub.add_parser("web", help="the maintenance website: bootstrap = show the first-run login secret once (root terminal)")
    sub.add_parser("swap", help="who holds the swap and whether it can be emptied safely: [status] | relieve [--apply]")
    sub.add_parser("rules", help="the rules registry (rules.d): list | show | check | diff | sync | history | rollback | export | migrate | explain | where | orphans")
    sub.add_parser("self-health", help="health of the monitoring pipeline itself: [--json] [--check] [--write] [--published]")
    sub.add_parser("smart-event", help="smartd -M exec hook: prints nothing, exit 0 = delivered")
    a = ap.parse_args(args)
    return {"run": cmd_run, "status": cmd_status, "plan": cmd_plan, "approve": cmd_approve,
            "pause": lambda x: cmd_pause(x, True), "resume": lambda x: cmd_pause(x, False),
            "gate": cmd_gate, "doctor": cmd_doctor, "tick": cmd_tick, "schedule": cmd_schedule,
            "notify-test": cmd_notify_test, "publish": cmd_publish, "metrics-sample": cmd_metrics_sample,
            "metrics-export": cmd_metrics_export}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
