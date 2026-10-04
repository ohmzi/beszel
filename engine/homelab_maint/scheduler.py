"""scheduler: the ONE tick of homelab-maint (SPEC4 S8). Everything that runs on a schedule on this host is started from here.

`homelab-maint-tick.timer` runs `homelab-maint tick` every minute (Type=oneshot, flock'd so ticks never overlap). A tick:
  1. REAPS finished jobs (done files written by the detached supervisors, see jobs.py), kills overdue ones (TERM, then KILL of the
     process group), detects lost runs (supervisor gone, host rebooted) by pid + start-time + boot id, never by pid alone;
  2. SEEDS and EVALUATES every managed job: an occurrence is due when the cron engine (schedule.py, host TZ, DST-correct) says so;
     missed occurrences fold into ONE catch-up run within `catchup_hours`; disruptive jobs catch up only inside their window;
  3. ADMITS it or defers it with a recorded reason: PAUSE, legacy-unit interlock, `after`, concurrency cap, heavy mutex (one heavy
     job at a time), no heavy / disruptive / gated job while a backup runs (ours or anyone's), pressure (the pressure_state
     `gate_level`: memory and cpu, never io-only or gpu-only), freeze windows, busy gates. Deferrals expire (`max_defer_hours`,
     then skip) except for `force_after_defer` jobs (backups): those start anyway once the limit passes, counted from when the
     tick first could act (downtime is not deferral). Force beats a busy gate, a freeze and a window, but never PAUSE, the mutex,
     a running backup, the interlock or pressure above `forced_pressure_max` (default 1: heavy work never starts at level >= 2); a forced job that is
     still blocked at the limit alerts once instead of waiting silently;
  4. LAUNCHES it detached (own session, own process group) and returns. It costs ~tens of ms when nothing is due.

State is STATE_DIR/sched.json (tick-owned); a run's own files live in STATE_DIR/jobruns/<job>/. The tick writes the state file only
when something changed. Results reach status.json as klass "J" rows (same shape as tasks), history.jsonl (kind "job") and, via
notify.py, as alerts / recoveries / maintenance updates (unless the legacy script still notifies itself: `self_notifies`).

Safety properties (each is tested):
  * a job never starts while its previous run is alive; the intent is persisted BEFORE the spawn, so a crash cannot double-run;
  * `max_retries_per_day` caps RETRIES of one occurrence only; an occurrence's first start is never capped (a 1-minute monitor
    starts 1440 times a day);
  * `mode` decides who drives a job: "observe" (default shipped state) = the legacy unit/cron still runs it, the tick only shows it;
    "managed" = the tick runs it, and refuses while the legacy unit/timer/cron line is still enabled or active (interlock, fail closed);
  * every error, timeout, unreadable probe or unknown state defers or blocks, it never starts something heavy;
  * PAUSE (and PAUSE.<job>) stops launches of everything except read-only monitors.

CLI (also what the `homelab-maint tick|schedule|job` glue calls):
  python3 -m homelab_maint.scheduler tick [--dry-run] | explain [--json] | run JOB [--force] | mode JOB managed|observe|retired|reset
                                     | status | export | validate | health
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import core, jobs, schedule
from .core import Locked, Result

SCHEMA = 1
SELF_ROW = "scheduler"                  # the pseudo-task in status.json that carries the tick's own health
BIG = 9_999_999_999


# --------------------------------------------------------------------------- seams: everything that touches the host
class Env:
    """Injectable edges. Defaults talk to the real host; tests replace any attribute with a fake."""

    def __init__(self) -> None:
        self.now: Callable[[], float] = time.time
        self.spawn: Callable[[dict], int] = jobs.spawn_supervisor
        self.proc_start = jobs.proc_start
        self.alive = jobs.proc_alive
        self.group_alive = jobs.group_alive
        self.kill_group = jobs.kill_group
        self.boot_id = jobs.boot_id
        self.lookup_user = jobs.user_info
        self.exists = os.path.exists
        self.busy: Callable[[str, dict], tuple[bool, str]] = default_busy
        self.backup_running: Callable[[dict], tuple[bool, str]] = default_backup_running
        self.pressure: Callable[[float, dict], tuple[int | None, str]] = default_pressure
        self.legacy_active: Callable[[list[str], dict], tuple[bool, str]] = default_legacy_active
        self.freeze: Callable[[float, dict], tuple[bool, str]] = default_freeze
        self.paused: Callable[[str | None], bool] = core.paused
        self.notify: Callable[..., Any] = default_notify
        self.audit: Callable[..., Any] = core.audit
        self.history: Callable[[dict], Any] = core.append_history


# --------------------------------------------------------------------------- default probes (read-only)
def default_busy(name: str, sched: dict) -> tuple[bool, str]:
    """tasks.gates.busy(name) with a hard time limit: a probe that hangs is BUSY (fail closed). Imported lazily: gates pulls in
    urllib/concurrent.futures, which an idle tick never needs."""
    box: dict[str, tuple[bool, str]] = {}

    def run() -> None:
        try:
            from .tasks import gates
            box["r"] = gates.busy(name)
        except Exception as exc:                     # noqa: BLE001
            box["r"] = (True, f"gate error: {type(exc).__name__}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(float(sched["gate_timeout_s"]))
    return box.get("r", (True, f"gate {name}: probe timed out"))


def locked_files(patterns: tuple[str, ...] = ("/run/lock/backup-*.lock",)) -> tuple[list[str], str]:
    """Lock files currently flock()ed by someone, from /proc/locks (read-only: taking the lock ourselves could make a backup
    that is just starting fail its own `flock -n`). -> (held paths, error text; '' when readable)."""
    import glob
    try:
        text = Path("/proc/locks").read_text()
    except OSError as exc:
        return [], f"cannot read /proc/locks ({type(exc).__name__})"
    held = {tok for ln in text.splitlines() for tok in ln.split() if re.fullmatch(r"[0-9a-f]+:[0-9a-f]+:\d+", tok)}
    out = []
    for pat in patterns:
        for p in glob.glob(pat):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}" in held:
                out.append(p)
    return out, ""


def default_backup_running(sched: dict) -> tuple[bool, str]:
    """Is a backup (or docker-prune) running that the scheduler did not start itself: a legacy unit, or a script run by hand?"""
    held, err = locked_files()
    if err:
        return True, err
    if held:
        return True, f"backup lock held: {os.path.basename(held[0])}"
    return default_busy("backup", sched)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def gate_level_of(d: dict) -> tuple[int, str] | None:
    """The level to GATE on, from a pressure_state record: its `gate_level` (memory and cpu only), else max(mem, cpu) of its `dims`
    (a record written before gate_level existed), else its overall `level` as a last resort. NOT `level` first: that one also
    counts io and gpu, and this host holds io PSI at level 3 for hours every night (a `find` on a spinning disk) while memory is
    fine; gating on it deferred every P2/P3 job and every backup (pressure.py: "nobody waits on io"). None: nothing usable."""
    if _int(d.get("gate_level")):
        return int(d["gate_level"]), f"gate level {d['gate_level']}"
    dims = d.get("dims")
    if isinstance(dims, dict) and dims:
        lv = [(x.get("level") if isinstance(x, dict) else x) for x in (dims.get("mem"), dims.get("cpu"))]
        lv = [int(x) for x in lv if _int(x)]
        return max(lv, default=0), f"gate level {max(lv, default=0)} (mem/cpu)"
    if _int(d.get("level")):
        return int(d["level"]), f"level {d['level']} (no gate_level in the record)"
    return None


def default_pressure(now: float, sched: dict) -> tuple[int | None, str]:
    """Current pressure level 0-5 to gate on, from the pressure_state task's own state file; None when missing or stale (unknown).
    See gate_level_of: io-only or gpu-only pressure is not a reason to defer maintenance."""
    d = core.read_json(core.STATE_DIR / "tasks" / "pressure_state.json", {})
    if not isinstance(d, dict) or not isinstance(d.get("t"), (int, float)):
        return None, "pressure_state has not run"
    age = (now - d["t"]) / 60
    if age > float(sched["pressure_stale_min"]):
        return None, f"pressure_state is {age:.0f} min old"
    got = gate_level_of(d)
    if got is None:
        return None, "pressure_state has no level"
    return got


def _window_values(v: Any) -> list[str]:
    return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def default_freeze(now: float, sched: dict) -> tuple[bool, str]:
    """Freeze windows for disruptive jobs: the routine's own [freeze] table (one place), else [scheduler].freeze, plus the
    CONF_DIR/FREEZE file. Dated freezes the routine understands but this parser does not are the routine's business."""
    if (core.CONF_DIR / "FREEZE").exists():
        return True, "FREEZE file present"
    specs: list[tuple[str, str]] = []
    rt = core.load_toml(core.CONF_DIR / "routine.toml") if (core.CONF_DIR / "routine.toml").exists() else {}
    fz = rt.get("freeze") if isinstance(rt.get("freeze"), dict) else None
    if fz is not None:
        specs = [(k, w) for k, v in fz.items() for w in _window_values(v)]
    else:
        specs = [("freeze", w) for w in sched.get("freeze", [])]
    for name, spec in specs:
        try:
            if schedule.in_window(spec, now):
                return True, f"freeze window {name} {spec}"
        except schedule.ScheduleError:
            continue
    return False, ""


def _unit_state(out: str) -> str:
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def default_legacy_active(specs: list[str], sched: dict) -> tuple[bool, str]:
    """Is ANY legacy driver of this job still enabled or active? specs: "system:UNIT", "user:USER:UNIT", "cron:USER:TAG".
    True also when it cannot be verified (fail closed: two schedulers must never both run a backup)."""
    for spec in specs:
        kind, _, rest = spec.partition(":")
        try:
            if kind == "system":
                act = _unit_state(core.sh(["systemctl", "is-active", rest], timeout=10).stdout)
                en = _unit_state(core.sh(["systemctl", "is-enabled", rest], timeout=10).stdout)
                if act in ("active", "activating", "reloading", "deactivating") or en in ("enabled", "enabled-runtime"):
                    return True, f"legacy {rest} is {act or 'inactive'}/{en or 'disabled'}"
                if not act and not en:
                    return True, f"cannot verify legacy {rest}"
            elif kind == "user":
                user, _, unit = rest.partition(":")
                ui = jobs.user_info(user)
                if ui is None:
                    return True, f"cannot verify legacy {unit}: no user {user}"
                for d in (f"{ui[2]}/.config/systemd/user", "/etc/systemd/user"):
                    for w in Path(d).glob(f"*.wants/{unit}"):
                        if w.exists():
                            return True, f"legacy user unit {unit} is enabled"
                rd = f"/run/user/{ui[0]}"
                if os.path.exists(f"{rd}/bus"):
                    r = core.sh([sched["runuser"], "-u", user, "--", "env", f"XDG_RUNTIME_DIR={rd}", "systemctl", "--user",
                                 "is-active", unit], timeout=10)
                    if _unit_state(r.stdout) in ("active", "activating"):
                        return True, f"legacy user unit {unit} is active"
            elif kind == "cron":
                user, _, tag = rest.partition(":")
                r = core.sh(["crontab", "-l", "-u", user], timeout=10)
                if r.returncode == 127:
                    return True, "cannot verify cron (crontab missing)"
                if any(tag in ln and not ln.lstrip().startswith("#") for ln in r.stdout.splitlines()):
                    return True, f"legacy cron line {tag} is active"
            else:
                return True, f"unknown retire spec {spec!r}"
        except Exception as exc:                     # noqa: BLE001
            return True, f"cannot verify {spec}: {type(exc).__name__}"
    return False, ""


def default_notify(kind: str, job: jobs.Job, res: Result, **ctx) -> Any:
    """Build the event through notify.py's helpers and send it. kind: failure | recovery | maintenance | expire. Imported lazily;
    any failure here is swallowed by the caller (a notification problem must never change a job's recorded result)."""
    from . import notify
    title = str(job.notify.get("title") or job.label)
    key = f"job:{job.name}"
    done = ctx.get("done") or {}
    facts: dict[str, Any] = {"Job": job.name, "Class": job.cls, "Schedule": job.schedule or "manual"}
    if ctx.get("rc") is not None:
        facts["Exit code"] = ctx["rc"]
    if ctx.get("duration_s") is not None:
        facts["Duration"] = f"{int(ctx['duration_s'] // 60)} min {int(ctx['duration_s'] % 60)} s"
    if ctx.get("attempt", 1) > 1 or job.max_attempts > 1:
        facts["Attempt"] = f"{ctx.get('attempt', 1)}/{job.max_attempts}"
    if kind == "failure":
        details: dict[str, Any] = {"log": (done.get("tail") or [])[-20:]}
        text = read_detail(job)
        if text:
            details["text"] = text
        details["todo"] = [f"Read the run log: {ctx.get('log', 'LOG_DIR/jobs/' + job.name)}",
                           f"Re-run by hand when fixed: homelab-maint job run {job.name}"]
        ev = notify.alert_event(key, title, res.status, res.summary, facts=facts, details=details)
    elif kind == "expire":
        ev = notify.alert_event(key, title, "warn", res.summary, facts=facts)
    elif kind == "recovery":
        ev = notify.recovery_event(key, title, res.summary, was=ctx.get("was", "crit"), facts=facts)
    else:
        text = read_detail(job)
        ev = notify.maintenance_event(key, title, res.summary, done=[res.summary], facts=facts,
                                      significant=bool(job.notify.get("significant")))
        if text and isinstance(ev.details, dict):
            ev.details["text"] = text
    return notify.send(ev)


def read_detail(job: jobs.Job, limit: int = 6000) -> str:
    p = job.notify.get("detail_file")
    if not p:
        return ""
    try:
        with open(p, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- state files and locks
def state_path() -> Path:
    return core.STATE_DIR / "sched.json"


def load_state() -> dict:
    d = core.read_json(state_path(), None)
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        d = {"schema": SCHEMA, "jobs": {}, "meta": {}}
    d.setdefault("jobs", {})
    d.setdefault("meta", {})
    return d


@contextlib.contextmanager
def tick_lock(wait_s: float = 0.0):
    """One tick (or manual `job run`) at a time. wait_s > 0 retries for that long; otherwise raises Locked at once."""
    core.RUN_DIR.mkdir(parents=True, exist_ok=True)
    f = open(core.RUN_DIR / "tick.lock", "w")
    end = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= end:
                f.close()
                raise Locked("tick.lock") from None
            time.sleep(0.2)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


@contextlib.contextmanager
def state_lock():
    """The same short lock cli.cmd_run holds around its read-merge-write of status.json."""
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(core.STATE_DIR / "state.lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def iso(t: float | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else "-"


def hhmm(t: float | None, tz=None) -> str:
    if not t:
        return "-"
    import datetime as dt
    return dt.datetime.fromtimestamp(t, tz or schedule.host_tz()).strftime("%a %m-%d %H:%M")


def _overall(tasks: dict) -> str:
    """The host's colour for status.json. An acknowledged task (SPEC5) is not the colour: acks.overall is the one rule (cli.cmd_run uses it
    too), so merge_status cannot turn the hero yellow a minute after the 15-minute run made it green. Without the module: the true colour."""
    try:
        from . import acks
        return acks.overall(tasks)
    except Exception:  # noqa: BLE001
        pass
    worst = 0
    for t in tasks.values():
        if isinstance(t, dict) and t.get("alert", True):
            worst = max(worst, core.LEVELS.get(t.get("status", "ok"), 0))
    return {0: "ok", 1: "warn", 2: "crit"}[worst]


def eff_jitter(job: jobs.Job, due: float) -> int:
    """RandomizedDelaySec for one occurrence: deterministic, and never more than half the job's own period."""
    j = job.jitter_s
    if j and job.schedule:
        gap = schedule.parse(job.schedule).min_gap_s(due - 1, 2)
        if gap:
            j = int(min(j, gap / 2))
    return schedule.jitter(job.name, due, j)


# --------------------------------------------------------------------------- the scheduler
class Scheduler:
    def __init__(self, env: Env, cfg: jobs.JobsConfig, st: dict, now: float, dry_run: bool = False):
        self.env, self.cfg, self.st, self.now, self.dry = env, cfg, st, now, dry_run
        self.sched = cfg.sched
        self.dirty = False
        self.status_dirty = False
        self.rep: dict[str, Any] = {"started": [], "reaped": [], "deferred": {}, "expired": [], "errors": []}
        self.status_t = 0.0
        self.status_sig: list[int] | None = None
        self._freeze: tuple[bool, str] | None = None
        self._pressure: tuple[int | None, str] | None = None

    # ---- small accessors
    def js(self, name: str) -> dict:
        return self.st["jobs"].setdefault(name, {})

    def job_of(self, name: str) -> jobs.Job:
        return self.cfg.jobs.get(name) or jobs.Job(name=name, title=name, mode="retired")

    def running(self) -> list[tuple[str, dict]]:
        return [(n, j) for n, j in self.st["jobs"].items() if isinstance(j.get("running"), dict)]

    def save(self) -> None:
        if self.dry or not self.dirty:
            return
        live = set(self.cfg.jobs)
        if live or not self.cfg.errors:       # a jobs.toml that could not be read/trusted must not wipe the schedule state (catch-up!)
            self.st["jobs"] = {n: j for n, j in self.st["jobs"].items() if n in live or j.get("running")}
        core.write_json_atomic(state_path(), self.st, 0o644)
        self.dirty = False

    # ---- 1. reaping
    def reap_all(self) -> None:
        for name, js in self.running():
            try:
                self.reap(self.job_of(name), js)
            except Exception as exc:                         # noqa: BLE001 - one broken record must not stop the tick
                self.rep["errors"].append(f"reap {name}: {type(exc).__name__}: {str(exc)[:80]}")

    def reap(self, job: jobs.Job, js: dict) -> None:
        r, now, env = js["running"], self.now, self.env
        done = jobs.read_done(r["done"])
        if done:
            self.finalize(job, js, done)
            return
        run = core.read_json(Path(r["run"]), None) if r.get("run") else None
        run = run if isinstance(run, dict) else {}
        rebooted = bool(r.get("boot")) and r["boot"] != env.boot_id()
        jpgid = run.get("job_pgid")
        spid, sstart = _sup_of(r, run)
        sup = (not rebooted) and env.alive(spid, sstart)
        grp = (not rebooted) and bool(jpgid) and env.group_alive(jpgid)
        if spid is None and now - r["started"] < 30 and not rebooted:
            return                                              # between "intent saved" and "spawned"
        if sup or grp:
            self._enforce_deadline(job, js, r, run)
            return
        why = "host rebooted while it ran" if rebooted else ("never started" if spid is None else
                                                              "supervisor vanished without a result (killed?)")
        done = {"rc": None, "lost": True, "lost_why": why, "t_start": r["started"], "t_end": now, "log": r.get("log"),
                "attempt": r.get("attempt", 1), "tail": _tail_of(r.get("log"))}
        self.finalize(job, js, done)

    def _enforce_deadline(self, job: jobs.Job, js: dict, r: dict, run: dict) -> None:
        hard = r.get("hard_deadline")
        if not hard or self.now <= hard or self.dry:
            return
        k = r.setdefault("kill", {})
        spid, sstart = _sup_of(r, run)
        self.dirty = True
        if not k.get("term_at"):
            k["term_at"] = self.now
            self.env.kill_group(spid, signal.SIGTERM, sstart)
            self.env.kill_group(run.get("job_pgid"), signal.SIGTERM, run.get("job_start"))
            self.env.audit("sched", "job-term", job.name, 0, f"past its deadline (pid {spid})")
        elif self.now > k["term_at"] + max(int(job.kill_grace_s), 20) and not k.get("kill_at"):
            k["kill_at"] = self.now
            self.env.kill_group(spid, signal.SIGKILL, sstart)
            self.env.kill_group(run.get("job_pgid"), signal.SIGKILL, run.get("job_start"))
            self.env.audit("sched", "job-kill", job.name, 0, "still alive after TERM + grace")

    def finalize(self, job: jobs.Job, js: dict, done: dict) -> None:
        r, now = js["running"], self.now
        ended = float(done.get("t_end", now))
        res = jobs.make_result(job, done, r["started"], now)
        attempt = int(r.get("attempt", 1))
        kind = "lost" if done.get("lost") or done.get("cancelled") else "timeout" if done.get("timed_out") else "failed"
        bad = bool(res.metrics.get("failed"))
        js.update(last_start=r["started"], last_end=ended, last_rc=done.get("rc"), last_status=res.status,
                  last_summary=res.summary, last_duration=round(ended - float(done.get("t_start", r["started"])), 1),
                  last_log=done.get("log"), last_run_id=r.get("run_id"), last_attempt=attempt, last_bad=bad)
        js.pop("running", None)
        js.pop("skipped_reason", None)
        self.dirty = self.status_dirty = True
        self.rep["reaped"].append((job.name, res.status))
        try:
            if bad or not job.quiet:      # "done" / "failed: why": the audit contract, so the website's "what was done" shows runs
                self.env.audit(job.name, "run", job.label, 0, f"failed: {res.summary[:100]}" if bad else "done")
                self.env.history({"t": ended, "kind": "job", "task": job.name, "status": res.status, "rc": done.get("rc"),
                                  "dur": js["last_duration"], "class": job.cls, "attempt": attempt})
        except Exception:                                    # noqa: BLE001
            pass
        retry = (bad and attempt < job.max_attempts and kind in job.retry_on and not r.get("manual")
                 and self._retries_today(js) < int(self.sched["max_retries_per_day"]))
        if retry:
            back = job.retry_backoff_s[min(attempt - 1, len(job.retry_backoff_s) - 1)]
            lab = f"retry {attempt + 1}/{job.max_attempts} after {kind}"
            js["pending"] = {"due": r.get("due", ended), "first_due": r.get("first_due", r.get("due", ended)), "since": now,
                             "attempt": attempt + 1, "retry_at": now + back, "label": lab, "reason": f"{lab}: waiting until {hhmm(now + back)}"}
            return
        self._streak_and_notify(job, js, res, done, bad, attempt)
        jobs.rotate_logs(job.name, job.keep_logs)

    def _retries_today(self, js: dict) -> int:
        """Retry starts (attempt 2, 3, ...) of this job today. First starts are never counted: a job that is simply due more than
        N times a day (a 1-minute monitor, the routine every 15 min) must not be switched off after its Nth start."""
        rd = js.get("retries_day") or {}
        return int(rd.get("n", 0)) if rd.get("day") == time.strftime("%Y-%m-%d", time.localtime(self.now)) else 0

    def _streak_and_notify(self, job: jobs.Job, js: dict, res: Result, done: dict, bad: bool, attempt: int) -> None:
        """Failure streak, then at most one alert per streak (+ a daily reminder), one recovery, optional maintenance update.
        A self-notifying legacy script is trusted for ordinary failures (no double sends) but NOT for abnormal ends it cannot
        report: lost (host rebooted / supervisor killed), timed out, cancelled, could not start."""
        ctx = {"done": done, "rc": done.get("rc"), "attempt": attempt, "duration_s": js.get("last_duration"),
               "log": done.get("log", "")}
        abnormal = bool(done.get("lost") or done.get("timed_out") or done.get("cancelled") or done.get("error"))
        mode = job.notify.get("on_failure", "alert")
        if not bad:
            was, lvl = js.pop("alerted", None), js.pop("alerted_level", "crit")
            js.pop("alerted_at", None)
            js["fail_streak"] = 0
            if was and mode != "none":
                self._send("recovery", job, res, was=lvl, **ctx)
            if job.notify.get("on_success") == "maintenance" and not job.self_notifies and res.status in ("ok", "warn"):
                self._send("maintenance", job, res, **ctx)
            return
        js["fail_streak"] = int(js.get("fail_streak", 0)) + 1
        confirm = int(job.notify.get("confirm", 2 if job.monitor else 1))
        if mode == "none" or (job.self_notifies and not abnormal) or js["fail_streak"] < confirm:
            return
        if not js.get("alerted") or self.now - float(js.get("alerted_at", 0)) >= 24 * 3600:
            if self._send("failure", job, res, **ctx):
                js["alerted"], js["alerted_at"] = True, self.now
                js["alerted_level"] = "warn" if res.status == "warn" else "crit"

    def _send(self, kind: str, job: jobs.Job, res: Result, **ctx) -> bool:
        if self.dry:
            return False
        try:
            d = self.env.notify(kind, job, res, **ctx)
            if d is not None and not getattr(d, "handled", True):          # notify.Delivery: not delivered and not intentionally dropped
                self.rep["errors"].append(f"notify {job.name}: not delivered ({str(getattr(d, 'note', ''))[:60]})")
                return False
            return True
        except Exception as exc:                             # noqa: BLE001 - never lose a result because the pager failed
            self.rep["errors"].append(f"notify {job.name}: {type(exc).__name__}")
            try:
                self.env.audit("sched", "notify-failed", job.name, 0, f"{kind}: {type(exc).__name__}")
            except Exception:                                # noqa: BLE001
                pass
            return False

    # ---- 2. evaluation
    def order(self) -> list[jobs.Job]:
        pri = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted((j for j in self.cfg.jobs.values() if j.mode == "managed"),
                      key=lambda j: (not j.monitor, pri[j.cls], j.name))

    def evaluate_all(self, t0: float, budget_s: float) -> None:
        for job in self.order():
            if time.monotonic() - t0 > budget_s:
                self.rep["errors"].append("tick budget exhausted; the rest waits for the next tick")
                break
            try:
                self.evaluate(job)
            except Exception as exc:                         # noqa: BLE001
                self.rep["errors"].append(f"{job.name}: {type(exc).__name__}: {str(exc)[:80]}")

    def evaluate(self, job: jobs.Job) -> None:
        js = self.js(job.name)
        now = self.now
        sch = schedule.parse(job.schedule) if job.schedule else None
        if js.get("running"):
            # Overlap prevention: the previous run is alive, so an occurrence that comes due now is DROPPED (a timer whose
            # service is still running skips that elapse too); the next one after `now` is the next candidate.
            if sch is not None and js.get("next_due") is not None and now >= js["next_due"]:
                js["last_due"] = sch.prev_before(now + 1) or js["next_due"]
                js["next_due"] = sch.next_after(now) or BIG
                js["overlap_skipped"] = int(js.get("overlap_skipped", 0)) + 1
                self.dirty = True
            return
        if sch is not None:
            if js.get("sig") != job.sig() or js.get("next_due") is None:
                self.reseed(job, js, sch)
            elif now >= js["next_due"]:
                latest = sch.prev_before(now + 1) or js["next_due"]
                pend = js.get("pending")
                if pend is None or not pend.get("manual"):
                    first = (pend or {}).get("first_due", latest)         # one chain of unresolved occurrences: expiry/force clock
                    js["pending"] = {"due": latest, "first_due": first, "since": (pend or {}).get("since", now), "attempt": 1,
                                     "superseded": int((pend or {}).get("superseded", 0)) + (1 if pend else 0),
                                     "held_alert": bool((pend or {}).get("held_alert"))}
                js["last_due"] = latest
                js["next_due"] = sch.next_after(now) or BIG
                self.dirty = True
        pend = js.get("pending")
        if pend:
            self.try_start(job, js, pend)

    def reseed(self, job: jobs.Job, js: dict, sch: schedule.Schedule) -> None:
        """First sight of a job, or its schedule was edited: the most recent occurrence counts as handled (no catch-up of
        something the legacy driver already ran or of an old schedule); the next one is the first we act on."""
        js["sig"] = job.sig()
        js["last_due"] = sch.prev_before(self.now + 1)
        js["next_due"] = sch.next_after(self.now) or BIG
        if not (js.get("pending") or {}).get("manual"):
            js.pop("pending", None)
        self.dirty = True

    def jitter(self, job: jobs.Job, pend: dict) -> int:
        return eff_jitter(job, pend["due"])

    def try_start(self, job: jobs.Job, js: dict, pend: dict) -> bool:
        now = self.now
        manual = bool(pend.get("manual"))
        due = float(pend["due"])
        first = float(pend.get("first_due", due))
        defer_s = float(job.max_defer_hours if job.max_defer_hours is not None else job.catchup_hours) * 3600
        # Two clocks. EXPIRY runs from the missed due time (a run older than catchup_hours is not worth running). The DEFERRAL clock
        # runs from when this tick could first act: a host that was off for a day did not "defer" its catch-up, so a forced
        # start (force_after_defer) is never the first thing it does after boot.
        seen = max(first, float(pend.get("since", first)))
        expire_at = first + max(float(job.catchup_hours) * 3600, defer_s)
        if job.force_after_defer:
            expire_at = max(expire_at, seen + defer_s + 3600)
        if not manual and now > expire_at:
            return self.expire(job, js, pend)
        start_at = pend.get("retry_at") or (due + (0 if manual else self.jitter(job, pend)))
        if now < start_at:
            lab = f"{pend['label']}: " if pend.get("label") else ""
            return self.defer(job, js, pend, f"{lab}waiting until {hhmm(start_at)}" + ("" if pend.get("label") else " (jitter)"), quiet=True)
        if pend.get("probe_at", 0) > now:
            return False                                      # an expensive probe said "busy" a moment ago
        auto = bool(job.force_after_defer and not manual and now - seen >= defer_s)
        ok, why, expensive = self.admit(job, js, pend, bool(pend.get("forced") or auto), auto)
        if not ok:
            if expensive:
                pend["probe_at"] = now + float(self.sched["defer_retry_s"])
            if auto and not pend.get("held_alert") and job.notify.get("on_expire") == "alert" and not self.dry:
                # Even a forced start cannot pass this blocker (PAUSE, the heavy mutex, a legacy driver, a real stall): say so once
                # now, not when the occurrence finally expires days later.
                pend["held_alert"] = True
                self.dirty = True
                self._send("expire", job, Result("warn", f"still waiting after {defer_s / 3600:g} h: {why}"[:140]))
            return self.defer(job, js, pend, why)
        return self.launch(job, js, pend)

    def defer(self, job: jobs.Job, js: dict, pend: dict, why: str, quiet: bool = False) -> bool:
        if pend.get("reason") != why:
            pend["reason"] = why
            self.dirty = True
            if not quiet:
                js["skipped_reason"] = why
                try:
                    self.env.audit("sched", "job-deferred", job.name, 0, why[:120])
                except Exception:                            # noqa: BLE001
                    pass
        self.rep["deferred"][job.name] = why
        return False

    def expire(self, job: jobs.Job, js: dict, pend: dict) -> bool:
        why = f"expired: could not start within {max(job.catchup_hours, job.max_defer_hours or 0):g} h ({pend.get('reason') or 'no reason recorded'})"
        js.pop("pending", None)
        js["skipped_reason"], js["skipped_at"] = why, self.now
        self.dirty = True
        self.rep["expired"].append(job.name)
        res = Result("warn", why[:140])
        try:
            self.env.audit("sched", "job-expired", job.name, 0, why[:120])
            self.env.history({"t": self.now, "kind": "job", "task": job.name, "status": "skipped", "rc": None, "dur": 0,
                              "class": job.cls})
        except Exception:                                    # noqa: BLE001
            pass
        if job.notify.get("on_expire", "none") == "alert" and not self.dry:
            self._send("expire", job, res)
        return False

    # ---- 3. admission control (cheap checks first, probes last)
    def pressure_now(self) -> tuple[int | None, str]:
        if self._pressure is None:
            self._pressure = self.env.pressure(self.now, self.sched)
        return self._pressure

    def admit(self, job: jobs.Job, js: dict, pend: dict, forced: bool, auto_forced: bool = False) -> tuple[bool, str, bool]:
        now, env, sched = self.now, self.env, self.sched
        if not job.monitor and job.pausable and env.paused(job.name):
            return False, "paused (kill switch)", False
        if job.retire:
            cache = self.st["meta"].setdefault("interlock", {})
            key = "|".join(job.retire)
            hit = cache.get(key)
            ttl = 30.0 if (job.backup or job.heavy) else float(sched["interlock_cache_s"])    # never launch a backup on an old "all clear"
            if not (hit and not hit.get("active") and now - hit.get("t", 0) < ttl):
                active, why = env.legacy_active(job.retire, sched)
                cache[key] = {"t": now, "active": bool(active), "why": why[:100]}
                self.dirty = True
                if active:
                    return False, f"interlock: {why}; run `migrate cutover` first", True
        run = dict(self.running())
        for a in job.after:
            if a in run:
                return False, f"waiting for {a} to finish", False
        if not job.monitor:
            n = sum(1 for nm, _ in run.items() if not self.job_of(nm).monitor)
            if n >= int(sched["max_concurrent"]):
                return False, f"{n} jobs already running (limit {sched['max_concurrent']})", False
            if job.heavy:
                for nm in run:
                    if self.job_of(nm).heavy:
                        return False, f"heavy job {nm} is running", False
            if job.heavy or job.backup or job.disruptive or job.gates:
                # A backup this tick started runs in the TICK unit's cgroup, not in backup-*.service, so the gates' `systemctl
                # is-active backup-*.service` (immich-recycle, backup) cannot see it: ask the scheduler's own running set first, then
                # the lock files / units for a backup started by anything else. Like the heavy mutex this is not bypassed by a
                # forced start: restarting immich_server in the middle of a pg_dump is exactly what the gate existed to prevent.
                for nm in run:
                    if self.job_of(nm).backup:
                        return False, f"backup {nm} is running", False
                busy, why = env.backup_running(sched)
                if busy:
                    return False, f"a backup is running ({why})", True
        if auto_forced and not job.monitor:                          # forced by the deferral limit, not by a human (`run --force`)
            lvl, pwhy = self.pressure_now()
            cap = max(int(sched["forced_pressure_max"]), job.pressure_max if job.pressure_max is not None else 0)
            if lvl is not None and lvl > cap:
                return False, f"pressure {pwhy}: even a forced start waits above level {cap}", False
        if not forced:
            if not job.monitor:
                pmax = job.pressure_max if job.pressure_max is not None else int(sched["pressure_max"].get(job.cls, 1))
                if job.heavy:
                    pmax = min(pmax, 1)                              # heavy work never starts at level >= 2, whatever its class
                lvl, pwhy = self.pressure_now()
                if lvl is not None and lvl > pmax:
                    return False, f"pressure {pwhy} > {pmax} allowed for {job.cls}", False
                if job.disruptive:
                    if self._freeze is None:
                        self._freeze = env.freeze(now, sched)
                    if self._freeze[0]:
                        return False, self._freeze[1], False
            for w in job.avoid:
                if schedule.in_window(w, now):
                    return False, f"inside avoid window {w}", False
            late = now - float(pend.get("first_due", pend["due"]))
            if job.window and late > float(sched["late_s"]) and not schedule.in_window(job.window, now):
                return False, f"catch-up waits for its window {job.window}", False
            for g in job.gates:
                busy, why = env.busy(g, sched)
                if busy:
                    return False, f"gate {g}: {why}", True
        return True, "", False

    # ---- 4. launching
    def launch(self, job: jobs.Job, js: dict, pend: dict) -> bool:
        now, env = self.now, self.env
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        attempt = int(pend.get("attempt", 1))
        retry = attempt > 1                                  # only restarts of one occurrence count against the daily cap
        cap = int(self.sched["max_retries_per_day"])
        if retry and self._retries_today(js) >= cap and not pend.get("manual"):
            return self.defer(job, js, pend, f"daily retry cap {cap} reached", quiet=True)
        run_id = jobs.new_run_id(now)
        spec = jobs.build_spec(job, self.sched, run_id, attempt, env.lookup_user, env.exists)
        if self.dry:
            self.rep["started"].append(job.name)
            return True
        if spec is None:
            js.pop("pending", None)
            res = Result("error", f"cannot launch: user {job.user!r} does not exist")
            js.update(last_start=now, last_end=now, last_status="error", last_summary=res.summary, last_rc=None, last_bad=True)
            self._streak_and_notify(job, js, res, {"tail": []}, True, attempt)
            self.dirty = self.status_dirty = True
            return False
        hard = None
        if job.timeout_s:
            hard = now + job.timeout_s + job.kill_grace_s + (int(self.sched["hook_timeout_s"]) if job.hooks else 0) + 60
        js["running"] = {"run_id": run_id, "started": now, "attempt": attempt, "due": pend["due"], "boot": env.boot_id(),
                         "first_due": pend.get("first_due", pend["due"]), "pid": None, "sup_start": None, "hard_deadline": hard, "log": spec["log_path"],
                         "run": spec["run_path"], "done": spec["done_path"], "manual": bool(pend.get("manual"))}
        js.pop("pending", None)
        js.pop("skipped_reason", None)
        if retry:
            js["retries_day"] = {"day": day, "n": self._retries_today(js) + 1}
        self.dirty = True
        self.save()                                          # intent on disk BEFORE the spawn: a crash cannot double-run
        try:
            pid = env.spawn(spec)
        except Exception as exc:                             # noqa: BLE001
            js.pop("running", None)
            res = Result("error", f"launch failed: {type(exc).__name__}: {str(exc)[:80]}")
            js.update(last_start=now, last_end=now, last_status="error", last_summary=res.summary, last_rc=None, last_bad=True)
            self._streak_and_notify(job, js, res, {"tail": []}, True, attempt)
            self.status_dirty = True
            self.rep["errors"].append(f"{job.name}: {res.summary}")
            self.dirty = True
            self.save()
            return False
        js["running"]["pid"] = pid
        js["running"]["sup_start"] = env.proc_start(pid)
        self.rep["started"].append(job.name)
        try:
            if not job.quiet:
                env.audit("sched", "job-start", job.name, 0, f"pid {pid} run {run_id} attempt {attempt}")
        except Exception:                                    # noqa: BLE001
            pass
        self.status_dirty = self.dirty = True
        return True

    # ---- status.json rows
    def entry(self, job: jobs.Job, js: dict) -> dict | None:
        if not js.get("last_status") and not js.get("running"):
            return None
        ok = js.get("last_status") or "info"
        e = {"title": job.label, "klass": "J", "tier": "job", "status": ok if js.get("last_status") else "info",
             "summary": js.get("last_summary") or "first run in progress", "last_run": js.get("last_end") or js.get("last_start"),
             "duration_s": js.get("last_duration", 0), "reclaimed_bytes": 0,
             "metrics": {"rc": js.get("last_rc") if isinstance(js.get("last_rc"), int) else -1, "attempt": js.get("last_attempt", 1),
                         "fail_streak": js.get("fail_streak", 0), "running": 1 if js.get("running") else 0},
             "items": [], "alert": True, "mode": "managed", "schedule": job.schedule or "manual", "class": job.cls,
             "source": job.source, "next_due": js.get("next_due")}
        return e

    def merge_status(self) -> None:
        if self.dry:
            return
        env_now = self.now
        with state_lock():
            status = core.read_json(core.STATE_DIR / "status.json", {}) or {}
            if not status:
                return                       # the runner has not written status.json yet; it owns schema/generated_at, we only merge rows
            tasks = status.setdefault("tasks", {})
            managed = {n for n, j in self.cfg.jobs.items() if j.mode == "managed"}
            for n in [n for n, e in tasks.items() if isinstance(e, dict) and e.get("klass") == "J" and n not in managed | {SELF_ROW}]:
                del tasks[n]
            for n in managed:
                e = self.entry(self.cfg.jobs[n], self.st["jobs"].get(n, {}))
                if e:
                    tasks[n] = e
                else:
                    tasks.pop(n, None)
            tasks[SELF_ROW] = self.self_entry()
            status["overall"] = _overall(tasks)
            status["tick"] = {"last_run": env_now, "managed": len(managed), "running": len(self.running())}
            core.write_json_atomic(core.STATE_DIR / "status.json", status)
            self.status_sig = _status_sig()                  # inside the lock: nobody can have rewritten it in between
        self.status_t = env_now
        self.status_dirty = False

    def self_entry(self) -> dict:
        errs = len(self.cfg.errors)
        stuck = [n for n, j in self.running() if (j["running"].get("kill") or {}).get("kill_at")]
        status = "crit" if stuck else "warn" if errs else "ok"
        managed = sum(1 for j in self.cfg.jobs.values() if j.mode == "managed")
        summ = (f"{managed} managed job(s), {len(self.running())} running" + (f", {errs} config problem(s)" if errs else "")
                + (f", UNKILLABLE: {', '.join(stuck)}" if stuck else ""))
        return {"title": "Scheduler tick", "klass": "J", "tier": "job", "status": status, "summary": summ[:140],
                "last_run": self.now, "duration_s": 0, "reclaimed_bytes": 0, "metrics": {"managed": managed, "config_problems": errs},
                "items": [{"problem": e[:100]} for e in self.cfg.errors[:8]], "alert": True, "mode": "managed",
                "schedule": "every minute", "class": "P0", "source": "native"}


def _sup_of(r: dict, run: dict) -> tuple[int | None, int | None]:
    """(pid, start ticks) of the supervisor: what the tick saved at spawn, else what the supervisor wrote itself into run.json
    (the tick can die between spawn and its final save; the supervisor's own record then keeps the run from reading as lost)."""
    if r.get("pid"):
        return r["pid"], r.get("sup_start")
    return run.get("sup_pid"), run.get("sup_start")


def _status_sig() -> list[int] | None:
    """[mtime_ns, size] of status.json: when it differs from what our last merge left, the runner rewrote it (and dropped our rows)."""
    try:
        st = os.stat(core.STATE_DIR / "status.json")
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def _tail_of(path: str | None, n: int = 20) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [ln[:300] for ln in lines[-n:]]


# --------------------------------------------------------------------------- public API
def tick(env: Env | None = None, cfg: jobs.JobsConfig | None = None, now: float | None = None, dry_run: bool = False) -> dict:
    """One scheduler pass. Returns a report {started, reaped, deferred, expired, errors, ms, locked}. Never raises for a job-level
    problem; raises Locked-free: another tick in progress returns {"locked": True} immediately."""
    env = env or Env()
    t0 = time.monotonic()
    now = env.now() if now is None else now
    cfg = cfg or jobs.load()
    ctx: contextlib.AbstractContextManager = contextlib.nullcontext() if dry_run else tick_lock()
    try:
        with ctx:
            sc = Scheduler(env, cfg, load_state(), now, dry_run)
            sc.reap_all()
            sc.evaluate_all(t0, float(sc.sched.get("tick_budget_s", 40)))
            hb = core.read_json(core.RUN_DIR / "tick.json", {}) or {}
            hb = hb if isinstance(hb, dict) else {}
            sc.status_t, sc.status_sig = float(hb.get("status_t", 0)), hb.get("status_sig")
            refresh = now - sc.status_t >= float(sc.sched["status_refresh_s"])
            rewritten = _status_sig() != sc.status_sig         # the runner (cmd_run) wrote status.json since our last merge
            if (sc.status_dirty or refresh or rewritten) and not dry_run:
                sc.merge_status()
            sc.save()
            sc.rep["ms"] = round((time.monotonic() - t0) * 1000, 1)
            if cfg.errors and not cfg.jobs:         # a broken jobs.toml: make the tick unit FAIL (failed_units sees it), not just beat
                sc.rep["errors"].append(f"jobs.toml: no job loaded, {len(cfg.errors)} problem(s): {cfg.errors[0][:100]}")
            if not dry_run:
                _heartbeat(now, sc)
            return sc.rep
    except Locked:
        return {"locked": True, "started": [], "reaped": [], "deferred": {}, "expired": [], "errors": [],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


def _heartbeat(now: float, sc: Scheduler) -> None:
    """Dead-man's switch on tmpfs (no disk write per minute): when did the last tick run, and what did it see."""
    try:
        (core.RUN_DIR / "tick.json").write_text(json.dumps({"t": now, "ms": sc.rep["ms"], "running": len(sc.running()),
                                                            "status_t": sc.status_t, "status_sig": sc.status_sig,
                                                            "errors": len(sc.rep["errors"]), "config_problems": len(sc.cfg.errors),
                                                            "jobs": len(sc.cfg.jobs)}))       # 0: the beat is fresh but nothing is scheduled
    except OSError:
        pass


def health(now: float | None = None, max_age_s: int = 300) -> tuple[str, str]:
    """(status, summary) for a C0 check / the dead-man's switch: the tick must have run within `max_age_s`."""
    now = time.time() if now is None else now
    hb = core.read_json(core.RUN_DIR / "tick.json", None)
    if not isinstance(hb, dict):
        return "warn", "no scheduler tick recorded since boot"
    age = now - float(hb.get("t", 0))
    if age > max_age_s:
        return "crit", f"scheduler tick last ran {age / 60:.0f} min ago"
    if hb.get("jobs") == 0:                     # a jobs.toml typo: the tick beats (so the beat probe stays green) but starts nothing at all
        return "crit", f"the tick loaded no jobs ({hb.get('config_problems') or 0} problem(s) in jobs.toml): nothing is scheduled"
    if hb.get("config_problems"):
        return "warn", f"{hb['config_problems']} problem(s) in jobs.toml"
    return "ok", f"tick ran {age:.0f}s ago in {hb.get('ms', '?')} ms"


def run_job(name: str, env: Env | None = None, cfg: jobs.JobsConfig | None = None, now: float | None = None,
            force: bool = False, ignore_mode: bool = False, wait_s: float = 5.0) -> dict:
    """Start one job NOW, outside its schedule (`homelab-maint job run NAME`). It still honours the interlock, overlap, the heavy
    mutex and PAUSE; `force` additionally skips pressure/freeze/gates/windows. A job in observe mode needs ignore_mode (the
    parity check runs the adapter once while the legacy unit is still the driver; the interlock then still applies)."""
    env = env or Env()
    now = env.now() if now is None else now
    cfg = cfg or jobs.load()
    job = cfg.jobs.get(name)
    if job is None:
        return {"started": False, "reason": f"unknown job {name!r}"}
    if job.mode == "retired":
        return {"started": False, "reason": "job is retired"}
    if job.mode != "managed" and not ignore_mode:
        return {"started": False, "reason": f"job is in {job.mode} mode (the legacy driver runs it); use --ignore-mode for a parity run"}
    try:
        with tick_lock(wait_s):
            sc = Scheduler(env, cfg, load_state(), now)
            js = sc.js(name)
            sc.reap_all()
            if js.get("running"):
                return {"started": False, "reason": "already running"}
            pend = {"due": now, "since": now, "attempt": 1, "manual": True, "forced": force}
            js["pending"] = pend
            ok = sc.try_start(job, js, pend)
            if not ok:
                js.pop("pending", None)
            if sc.status_dirty:
                sc.merge_status()
            sc.dirty = True
            sc.save()
            return {"started": ok, "reason": sc.rep["deferred"].get(name, ""), "run_id": (js.get("running") or {}).get("run_id")}
    except Locked:
        return {"started": False, "reason": "another tick is running; try again"}


# --------------------------------------------------------------------------- explain / export (the unified schedule)
def _jobrow(job: jobs.Job, js: dict, now: float, cfg: jobs.JobsConfig, env: Env | None = None) -> dict:
    sch = schedule.parse(job.schedule) if job.schedule else None
    pend = js.get("pending")
    r = js.get("running")
    nxt: float | None = None
    why = ""
    if job.mode == "retired":
        why = job.note or "retired: documented only, never runs"
    elif job.mode == "observe":
        nxt = sch.next_after(now) if sch else None
        why = (f"observe: {', '.join(job.retire)} still drives it until cutover" if job.retire else
               f"observe: nothing schedules it yet; `job mode {job.name} managed` (or its cutover) turns it on")
    elif r:
        nxt = js.get("next_due")
        why = f"running since {hhmm(r.get('started'))} (run {r.get('run_id')})"
    elif pend:
        nxt = pend.get("retry_at") or pend["due"] + (0 if pend.get("manual") else eff_jitter(job, pend["due"]))
        why = pend.get("reason") or "due; starts on the next tick"
    else:
        nxt = js.get("next_due") if js.get("next_due") not in (None, BIG) else (sch.next_after(now) if sch else None)
        if nxt and job.jitter_s:
            nxt += eff_jitter(job, nxt)
        why = "waiting for its schedule" if nxt else "manual only"
        if js.get("skipped_reason"):
            why = f"last occurrence skipped: {js['skipped_reason']}"
    row = {"job": job.name, "title": job.label, "source": job.source, "mode": job.mode, "class": job.cls, "heavy": job.heavy,
           "monitor": job.monitor, "user": job.user, "schedule": job.schedule or "manual", "jitter_s": job.jitter_s,
           "next_due": nxt, "next_due_h": hhmm(nxt), "last_start": js.get("last_start"), "last_end": js.get("last_end"),
           "last_status": js.get("last_status"), "last_summary": js.get("last_summary", ""), "why": why[:200],
           "self_notifies": job.self_notifies, "replaced_by": job.replaced_by}
    if job.mode != "managed" and (job.success.get("status_json") or job.success.get("touch_file")):
        o = jobs.observed(job, now)
        row.update(last_end=o["finished"], last_status=o["result"], last_summary=o["summary"], last_age_h=o["age_h"],
                   stale=o["stale"])
    return row


def explain(now: float | None = None, cfg: jobs.JobsConfig | None = None, st: dict | None = None) -> list[dict]:
    """The unified schedule: [{job, next_due, why, ...}] for every job (managed, observed, retired) and every externally managed
    timer (OS and app services the umbrella only observes), soonest first. The single place to see everything that runs."""
    now = time.time() if now is None else now
    cfg = cfg or jobs.load()
    st = st if st is not None else load_state()
    rows = [_jobrow(j, st["jobs"].get(j.name, {}), now, cfg) for j in cfg.jobs.values()]
    for e in cfg.external:
        rows.append({"job": e["name"], "title": e.get("title", e["name"]), "source": "os" if e.get("kind", "os") == "os" else "external",
                     "mode": "external", "class": e.get("class", "P3"), "heavy": False, "monitor": False, "user": e.get("user", "root"),
                     "schedule": e.get("schedule", ""), "jitter_s": 0, "next_due": None, "next_due_h": "-", "last_start": None,
                     "last_end": None, "last_status": None, "last_summary": "", "why": "managed externally: " + e.get("note", e.get("unit", "")),
                     "self_notifies": False, "replaced_by": ""})
    rows.sort(key=lambda r: (r["next_due"] is None, r["next_due"] or 0, r["job"]))
    return rows


def export(now: float | None = None, cfg: jobs.JobsConfig | None = None, st: dict | None = None) -> dict:
    """jobs.json for the public website: no commands, no paths, no environment, no log text."""
    now = time.time() if now is None else now
    cfg = cfg or jobs.load()
    rows = explain(now, cfg, st)
    keep = ("job", "title", "source", "mode", "class", "heavy", "monitor", "schedule", "next_due", "last_start", "last_end", "last_status")
    nopath = lambda v, n: re.sub(r"/\S+", "...", str(v or ""))[:n]            # noqa: E731 - no file paths in a public file
    pub = [{k: r.get(k) for k in keep} | {"last_summary": nopath(r.get("last_summary"), 120), "why": nopath(r.get("why"), 160)}
           for r in rows]
    h = health(now)
    return {"generated_at": now, "tick": {"status": h[0], "summary": h[1]}, "jobs": pub,
            "counts": {m: sum(1 for r in rows if r["mode"] == m) for m in ("managed", "observe", "retired", "external")},
            "config_problems": len(cfg.errors)}


def format_table(rows: list[dict], now: float | None = None) -> str:
    out = [f"{'NEXT':<16} {'MODE':<8} {'CLS':<3} {'HVY':<3} {'JOB':<24} {'SCHEDULE':<26} LAST / WHY"]
    for r in rows:
        last = (r.get("last_status") or "-") + (f" {hhmm(r['last_end'])}" if r.get("last_end") else "")
        out.append(f"{r['next_due_h']:<16} {r['mode']:<8} {r['class']:<3} {'*' if r['heavy'] else ' ':<3} {r['job'][:24]:<24} "
                   f"{str(r['schedule'])[:26]:<26} {last} | {r['why'][:90]}")
    return "\n".join(out)


# --------------------------------------------------------------------------- lint
def validate(cfg: jobs.JobsConfig | None = None, check_paths: bool = True) -> list[str]:
    """Problems that would bite at launch time (the loader already rejected unparsable schedules and bad names)."""
    cfg = cfg or jobs.load()
    probs = list(cfg.errors)
    gate_names: set[str] = set()
    try:
        from .tasks import gates
        gate_names = set(gates._PROBES) | set(gates.ALIASES) | {"any"}
    except Exception:                                        # noqa: BLE001
        pass
    for j in cfg.jobs.values():
        if check_paths and j.mode != "retired":
            argv0 = jobs.expand(j.command[0], cfg.sched)
            if not os.access(argv0, os.X_OK):
                probs.append(f"job {j.name}: {argv0} is not an executable file")
            if j.user != "root" and jobs.user_info(j.user) is None:
                probs.append(f"job {j.name}: user {j.user} does not exist")
        for g in j.gates:
            if gate_names and g not in gate_names:
                probs.append(f"job {j.name}: unknown gate {g!r}")
        for a in j.after:
            if a not in cfg.jobs:
                probs.append(f"job {j.name}: after = {a!r} is not a job")
        for r in j.retire:
            if not re.fullmatch(r"(system:[\w@.-]+|user:[a-z_][a-z0-9_-]*:[\w@.-]+|cron:[a-z_][a-z0-9_-]*:\S+)", r):
                probs.append(f"job {j.name}: bad retire spec {r!r}")
        if j.mode == "managed" and j.source == "adapter" and not j.retire and j.schedule:
            probs.append(f"job {j.name}: managed legacy job without a `retire` interlock")
    return probs


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="homelab-maint scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--now", type=float)
    e = sub.add_parser("explain")
    e.add_argument("--json", action="store_true")
    e.add_argument("--at", type=float)
    r = sub.add_parser("run")
    r.add_argument("job")
    r.add_argument("--force", action="store_true", help="skip pressure/freeze/gates/windows (never PAUSE, mutex or interlock)")
    r.add_argument("--ignore-mode", action="store_true", help="allow a parity run of a job still in observe mode")
    m = sub.add_parser("mode")
    m.add_argument("job")
    m.add_argument("mode", choices=[*jobs.MODES, "reset"])
    sub.add_parser("status")
    sub.add_parser("export")
    sub.add_parser("validate")
    sub.add_parser("health")
    a = ap.parse_args(argv)
    if a.cmd == "tick":
        try:
            rep = tick(now=a.now, dry_run=a.dry_run)
        except Exception as exc:                             # noqa: BLE001
            print(f"tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if a.dry_run or rep.get("started") or rep.get("reaped") or rep.get("errors"):
            print(json.dumps(rep, default=str))
        return 1 if rep.get("errors") and any(not x.startswith("tick budget") for x in rep["errors"]) else 0
    if a.cmd in ("explain", "status"):
        rows = explain(a.at if a.cmd == "explain" else None)
        print(json.dumps(rows, indent=1, default=str) if getattr(a, "json", False) else format_table(rows))
        return 0
    if a.cmd == "run":
        res = run_job(a.job, force=a.force, ignore_mode=a.ignore_mode)
        print(json.dumps(res))
        return 0 if res["started"] else 1
    if a.cmd == "mode":
        if a.job not in jobs.load(apply_modes=False).jobs:
            print(f"unknown job {a.job}", file=sys.stderr)
            return 2
        jobs.set_mode(a.job, None if a.mode == "reset" else a.mode)
        print(f"{a.job}: mode override {'cleared' if a.mode == 'reset' else a.mode}")
        return 0
    if a.cmd == "export":
        print(json.dumps(export(), default=str))
        return 0
    if a.cmd == "validate":
        probs = validate()
        for p in probs:
            print(p)
        print(f"{len(probs)} problem(s)")
        return 1 if probs else 0
    st, summ = health()
    print(f"{st}: {summ}")
    return 0 if st == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
