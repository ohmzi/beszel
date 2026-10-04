"""jobs: the external-job adapter (SPEC4 S8). It knows HOW to run one legacy command; scheduler.py decides WHEN.

A job is a `[[job]]` table in /etc/homelab-maint/jobs.toml (see etc/jobs.toml for the shipped, annotated inventory). The adapter
never changes what a legacy command does, only how it is launched and observed: same argv, same user, same environment
(the environment a systemd unit / the user manager / cron would have given it), same nice/ionice. What it adds is control.

  * LAUNCH   scheduler.tick() writes a run spec and starts `python3 -m homelab_maint.jobs supervise SPEC` detached (own session).
  * SUPERVISE  the supervisor first makes sure the job's `requires_mounts` are mounted (RequiresMountsFor= parity: one
             `systemctl start X.mount`, else the run fails loudly), then runs the command in ITS OWN process group, captures
             stdout+stderr to LOG_DIR/jobs/<job>/<run>.log
             (secrets scrubbed, size capped head+tail), enforces the timeout (TERM to the whole group, then KILL after the
             grace), kills leftovers of the group when the main process exits (systemd KillMode=control-group parity), runs the
             optional on-failure hook, and writes `<run>.done.json` LAST. The tick reaps that file on a later minute.
  * OBSERVE  `make_result()` turns exit code + done record + the job's own status JSON / touch file / log line into a core.Result.

Everything that touches the host is behind a seam (spawn, kill, user lookup, clock) so tests run with tmp dirs and mocks.
Python 3.12 stdlib only. No secrets are read, stored or printed here: command lines and environment values never reach the
log header, and the log is scrubbed line by line.
"""
from __future__ import annotations

import collections
import errno
import json
import os
import pwd
import re
import secrets
import select
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import core, schedule
from .core import Result

NAME_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
USER_RX = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
CLASSES = ("P0", "P1", "P2", "P3")
MODES = ("managed", "observe", "retired")
FAIL_STATUS = ("crit", "warn", "error")
RETRY_ON = ("failed", "timeout", "lost")          # "lost" = host rebooted, supervisor killed or told to stop (shutdown)

# What the scheduler (and so every job) uses when jobs.toml does not say otherwise. Every key can be overridden in [scheduler].
SCHED_DEFAULTS: dict[str, Any] = {
    "max_concurrent": 3,              # simultaneous non-monitor jobs
    "tick_budget_s": 40,              # stop evaluating further jobs after this long (they wait for the next tick)
    "catchup_hours": 6.0,             # a missed/deferred occurrence older than this is skipped, not run
    "defer_retry_s": 120,             # re-probe expensive admission checks (gates, interlock) at most this often
    "late_s": 300,                    # started this late = a catch-up run (windows then apply)
    "interlock_cache_s": 600,         # "legacy unit is gone" answers are cached this long
    "gate_timeout_s": 20,             # a gate probe that takes longer is BUSY
    "pressure_stale_min": 45,         # pressure_state older than this is "unknown" (treated as level 0, noted)
    "status_refresh_s": 300,          # re-merge job rows into status.json at least this often
    "keep_logs": 20,                  # run logs kept per job
    "log_head_kib": 512, "log_tail_kib": 256,
    "max_retries_per_day": 6,         # cap on RETRY starts (attempt 2, 3, ...) of one job per local day. The first start of every
                                      # occurrence is never capped: a 1-minute monitor legitimately starts 1440 times a day
    "forced_pressure_max": 1,         # a start forced after max_defer_hours still waits while pressure is above this (default: never at >= 2)
    "mount_timeout_s": 60,            # `systemctl start X.mount` for a job's requires_mounts
    "runuser": "/usr/sbin/runuser", "ionice": "/usr/bin/ionice", "systemctl": "/usr/bin/systemctl",
    "self_cmd": "/usr/local/sbin/homelab-maint", "python": "/usr/bin/python3",
    "drain_s": 5,                     # after the main process exits, wait this long for the output pipe to close
    "hook_timeout_s": 120,
    "freeze": ["18:00-23:30"],        # used only when routine.toml has no [freeze] (disruptive jobs wait out these)
    "pressure_max": {"P0": 5, "P1": 5, "P2": 1, "P3": 1},   # highest pressure level at which a class may START
    "env_root": {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin", "LANG": "en_US.UTF-8",
                 "USER": "root", "XDG_DATA_DIRS": "/var/lib/flatpak/exports/share:/usr/local/share/:/usr/share/"},
    "env_user": {"PATH": "{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin",
                 "LANG": "en_US.UTF-8"},
}
SCHED_KEYS = set(SCHED_DEFAULTS)

_JOB_KEYS = {
    "name", "title", "command", "user", "schedule", "class", "source", "mode", "heavy", "monitor", "disruptive", "backup",
    "timeout_s", "kill_grace_s", "jitter_s", "catchup_hours", "max_defer_hours", "force_after_defer", "gates", "after",
    "window", "avoid", "pressure_max", "env", "workdir", "nice", "ionice_class", "ionice_prio", "oom_score_adj", "success",
    "notify", "hooks", "tee_to", "retire", "self_notifies", "fail_status", "max_attempts", "retry_on", "retry_backoff_s",
    "keep_logs", "note", "replaced_by", "enabled", "pausable", "requires_mounts", "quiet",
}
_SUCCESS_KEYS = {"exit_codes", "warn_exit_codes", "status_json", "result_key", "ok_values", "finished_key", "started_key",
                 "warn_key", "reason_key", "time_format", "summary", "fail_summary", "max_age_hours", "touch_file",
                 "summary_file", "summary_regex", "exit_regex"}
_NOTIFY_KEYS = {"on_failure", "on_success", "on_expire", "detail_file", "significant", "confirm", "title"}
_ASCII = re.compile(r"[^\x20-\x7e]")


# --------------------------------------------------------------------------- the job model
@dataclass
class Job:
    name: str
    title: str = ""
    command: list[str] = field(default_factory=list)
    user: str = "root"
    schedule: str = ""                    # "" = never scheduled (manual `run` only)
    cls: str = "P3"                       # service class: P2/P3 yield to pressure, P0/P1 do not
    source: str = "adapter"               # adapter (legacy command) | native (umbrella tier) | task (registry task)
    mode: str = "observe"                 # managed (tick launches it) | observe (legacy still drives it) | retired
    heavy: bool = False                   # serialised through one mutex; never starts while a backup runs
    monitor: bool = False                 # read-only observer: ignores PAUSE, freeze, pressure, concurrency, mutex
    disruptive: bool = False              # restarts something: waits out freeze windows
    backup: bool = False                  # while running (or its lock held) heavy jobs wait
    pausable: bool = True
    quiet: bool = False                   # successful runs are not written to audit.jsonl / history.jsonl (default: = monitor)
    timeout_s: int = 0                    # 0 = none (systemd TimeoutStartSec=infinity)
    kill_grace_s: int = 20
    jitter_s: int = 0                     # RandomizedDelaySec equivalent (deterministic per occurrence)
    catchup_hours: float = 6.0
    max_defer_hours: float | None = None  # None: catchup_hours
    force_after_defer: bool = False       # after the defer limit run anyway (gates/pressure/freeze only; never PAUSE/mutex)
    gates: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    requires_mounts: list[str] = field(default_factory=list)   # RequiresMountsFor=: started (systemctl start X.mount) before the command
    window: str = ""                      # catch-up runs of this job start only inside this window
    avoid: list[str] = field(default_factory=list)
    pressure_max: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    nice: int | None = None
    ionice_class: int | None = None       # 1 realtime, 2 best-effort, 3 idle
    ionice_prio: int | None = None
    oom_score_adj: int | None = None
    success: dict[str, Any] = field(default_factory=dict)
    notify: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    tee_to: str = ""
    retire: list[str] = field(default_factory=list)
    self_notifies: bool = False           # the legacy script alerts by itself: the umbrella sends nothing (no double sends)
    fail_status: str = "crit"
    max_attempts: int = 1
    retry_on: list[str] = field(default_factory=list)
    retry_backoff_s: list[int] = field(default_factory=lambda: [300, 900, 3600])
    keep_logs: int = 20
    note: str = ""
    replaced_by: str = ""
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.title or self.name

    def sig(self) -> str:
        """Changes when the schedule is edited, so a new schedule never triggers a catch-up of the old one."""
        return f"{self.schedule}|{self.jitter_s}"


@dataclass
class JobsConfig:
    sched: dict[str, Any]
    jobs: dict[str, Job] = field(default_factory=dict)
    external: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    modes: dict[str, str] = field(default_factory=dict)      # job-modes.json overrides actually applied


def _ascii(s: Any, n: int = 140) -> str:
    return _ASCII.sub("?", str(s).replace("\n", " ").replace("\r", " "))[:n]


def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool) or v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _strs(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []


# --------------------------------------------------------------------------- loading and validation
def modes_path() -> Path:
    return core.STATE_DIR / "job-modes.json"


def load_modes() -> dict[str, str]:
    """Per-job mode overrides written by `homelab-maint migrate cutover|rollback` (or `scheduler mode`). JSON, atomic."""
    d = core.read_json(modes_path(), {})
    return {k: v for k, v in d.items() if isinstance(k, str) and v in MODES} if isinstance(d, dict) else {}


def set_mode(name: str, mode: str | None) -> dict[str, str]:
    """Persist (mode in MODES) or clear (None) one override. The cutover/rollback glue calls this; nothing else does."""
    if not NAME_RX.match(name) or (mode is not None and mode not in MODES):
        raise ValueError(f"bad job name or mode: {name!r} {mode!r}")
    cur = load_modes()
    if mode is None:
        cur.pop(name, None)
    else:
        cur[name] = mode
    core.write_json_atomic(modes_path(), cur, 0o644)
    return cur


def _parse_job(raw: dict, defaults: dict, errs: list[str], sched: dict) -> Job | None:
    name = str(raw.get("name", ""))
    tag = f"job {name or '?'}"
    if not NAME_RX.match(name):
        errs.append(f"{tag}: bad name (want [a-z0-9][a-z0-9._-]*)")
        return None
    merged = {**defaults, **raw}
    for k in merged:
        if k not in _JOB_KEYS:
            errs.append(f"{tag}: unknown key {k!r} ignored")
    cmd = _strs(merged.get("command"))
    if not cmd or not all(isinstance(x, str) for x in merged.get("command", [])):
        errs.append(f"{tag}: command must be a non-empty list of strings")
        return None
    user = str(merged.get("user", "root"))
    if not USER_RX.match(user):
        errs.append(f"{tag}: bad user {user!r}")
        return None
    cls = str(merged.get("class", "P3")).upper()
    mode = str(merged.get("mode", "observe"))
    if cls not in CLASSES or mode not in MODES:
        errs.append(f"{tag}: bad class {cls!r} or mode {mode!r}")
        return None
    j = Job(name=name, command=cmd, user=user, cls=cls, mode=mode)
    j.title = _ascii(merged.get("title") or name, 80)
    j.schedule = str(merged.get("schedule", "") or "")
    if j.schedule:
        bad = schedule.validate(j.schedule)
        if bad:
            errs.append(f"{tag}: {bad}")
            return None
    for k in ("source", "window", "workdir", "tee_to", "note", "replaced_by"):
        setattr(j, k, str(merged.get(k, getattr(j, k)) or ""))
    for k in ("heavy", "monitor", "disruptive", "backup", "pausable", "force_after_defer", "self_notifies", "enabled", "quiet"):
        v = merged.get(k, getattr(j, k))
        if not isinstance(v, bool):
            errs.append(f"{tag}: {k} must be true/false")
            v = getattr(j, k)
        setattr(j, k, v)
    if "quiet" not in merged:
        j.quiet = j.monitor                  # a read-only observer is not "maintenance done": successes stay out of the audit trail
    j.timeout_s = max(0, int(_num(merged.get("timeout_s"), 0) or 0))
    j.kill_grace_s = max(1, int(_num(merged.get("kill_grace_s"), 20) or 20))
    j.jitter_s = max(0, int(_num(merged.get("jitter_s"), 0) or 0))
    j.catchup_hours = float(_num(merged.get("catchup_hours"), sched["catchup_hours"]))
    j.max_defer_hours = _num(merged.get("max_defer_hours"))
    j.pressure_max = None if merged.get("pressure_max") is None else int(_num(merged["pressure_max"], 1))
    j.gates, j.after, j.avoid, j.retire = (_strs(merged.get(k)) for k in ("gates", "after", "avoid", "retire"))
    j.requires_mounts = _strs(merged.get("requires_mounts"))
    if any(not m.startswith("/") or ".." in m.split("/") or "\0" in m for m in j.requires_mounts):
        errs.append(f"{tag}: requires_mounts must be absolute paths without '..'")
        return None
    env = merged.get("env", {})
    j.env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    for k in ("nice", "ionice_class", "ionice_prio", "oom_score_adj"):
        v = _num(merged.get(k))
        setattr(j, k, None if v is None else int(v))
    if j.ionice_class not in (None, 1, 2, 3):
        errs.append(f"{tag}: ionice_class must be 1, 2 or 3")
        j.ionice_class = None
    for blk, keys in (("success", _SUCCESS_KEYS), ("notify", _NOTIFY_KEYS)):
        v = merged.get(blk, {})
        v = dict(v) if isinstance(v, dict) else {}
        errs.extend(f"{tag}: unknown {blk} key {k!r} ignored" for k in v if k not in keys)
        setattr(j, blk, v)
    hk = merged.get("hooks", {})
    j.hooks = {k: _strs(v) for k, v in hk.items() if isinstance(v, list)} if isinstance(hk, dict) else {}
    j.fail_status = str(merged.get("fail_status", "crit"))
    if j.fail_status not in FAIL_STATUS:
        errs.append(f"{tag}: fail_status must be one of {FAIL_STATUS}")
        j.fail_status = "crit"
    j.max_attempts = max(1, int(_num(merged.get("max_attempts"), 1) or 1))
    j.retry_on = [x for x in _strs(merged.get("retry_on")) if x in RETRY_ON]
    bo = [int(x) for x in merged.get("retry_backoff_s", j.retry_backoff_s) if isinstance(x, (int, float)) and x >= 0]
    j.retry_backoff_s = bo or [300]
    j.keep_logs = max(1, int(_num(merged.get("keep_logs"), sched["keep_logs"])))
    for w in [j.window, *j.avoid]:
        if w:
            try:
                schedule.parse_window(w)
            except schedule.ScheduleError as exc:
                errs.append(f"{tag}: {exc}")
                return None
    if not (j.command[0].startswith("/") or j.command[0] in ("{self}", "{python}")):
        errs.append(f"{tag}: command[0] must be an absolute path (or {{self}} / {{python}}), got {j.command[0]!r}")
        return None
    if j.max_attempts > 1 and not j.retry_on:
        j.retry_on = ["lost", "timeout"]
    return j


def untrusted_reason(p: Path) -> str:
    """Why `p` may not be believed ('' = it may). jobs.toml names commands the tick runs as ROOT, so only a file owned by root (or
    by us), writable by nobody else, inside a directory nobody else can write, is trusted (the probe engine applies the same rule)."""
    try:
        st, dst = p.stat(), p.parent.stat()
    except OSError:
        return "cannot be examined"
    me = (0, os.geteuid())
    if st.st_uid not in me or dst.st_uid not in me:
        return "the file or its directory is not owned by root"
    if st.st_mode & 0o022 or dst.st_mode & 0o022:
        return "the file or its directory is group/world writable"
    return ""


def load(path: Path | None = None, mcfg: dict | None = None, apply_modes: bool = True, enforce_trust: bool | None = None) -> JobsConfig:
    """jobs.toml + registry tasks that carry a cron `schedule` in maint.toml. Invalid entries are reported in `errors` and SKIPPED
    (an unparsable schedule is never 'always'); a missing file yields an empty, error-free config (a fresh install). Running as root
    (the tick) an untrusted jobs.toml is IGNORED as a whole: fail closed, nothing runs from a file anybody else could have edited."""
    p = path or core.CONF_DIR / "jobs.toml"
    errs: list[str] = []
    if (os.geteuid() == 0 if enforce_trust is None else enforce_trust) and p.exists():
        why = untrusted_reason(p)
        if why:
            return JobsConfig(dict(SCHED_DEFAULTS), errors=[f"jobs.toml ignored, nothing will be scheduled from it: {why}"])
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raw = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return JobsConfig(dict(SCHED_DEFAULTS), errors=[f"jobs.toml unreadable: {type(exc).__name__}: {_ascii(exc, 100)}"])
    sched = dict(SCHED_DEFAULTS)
    user_sched = raw.get("scheduler", {}) if isinstance(raw.get("scheduler"), dict) else {}
    if "max_attempts_per_day" in user_sched:         # the old name: it capped FIRST starts too (it switched off every job due > N times a day)
        user_sched = {("max_retries_per_day" if k == "max_attempts_per_day" else k): v for k, v in user_sched.items()}
    for k, v in user_sched.items():
        if k in SCHED_KEYS:
            sched[k] = {**sched[k], **v} if isinstance(sched[k], dict) and isinstance(v, dict) else v
        else:
            errs.append(f"scheduler: unknown key {k!r} ignored")
    defaults = raw.get("defaults", {}) if isinstance(raw.get("defaults"), dict) else {}
    cfg = JobsConfig(sched, errors=errs)
    for r in raw.get("job", []) if isinstance(raw.get("job"), list) else []:
        if not isinstance(r, dict):
            errs.append("job: entry is not a table")
            continue
        j = _parse_job(r, defaults, errs, sched)
        if j is None:
            continue
        if j.name in cfg.jobs:
            errs.append(f"job {j.name}: duplicate name, second one ignored")
            continue
        cfg.jobs[j.name] = j
    for e in raw.get("external", []) if isinstance(raw.get("external"), list) else []:
        if isinstance(e, dict) and NAME_RX.match(str(e.get("name", ""))):
            cfg.external.append({k: _ascii(v, 160) if isinstance(v, str) else v for k, v in e.items()})
    for name, j in task_entries(mcfg, sched, errs).items():
        if name in cfg.jobs:
            errs.append(f"task {name}: a job with that name exists, the task schedule is ignored")
        else:
            cfg.jobs[name] = j
    if apply_modes:
        cfg.modes = {n: m for n, m in load_modes().items() if n in cfg.jobs}
        for n, m in cfg.modes.items():
            cfg.jobs[n].mode = m
    for j in cfg.jobs.values():
        if not j.enabled and j.mode == "managed":
            j.mode = "observe"
    return cfg


def task_entries(mcfg: dict | None, sched: dict, errs: list[str]) -> dict[str, Job]:
    """Registry tasks scheduled by cron (`[tasks.NAME] schedule = "..."` in maint.toml). The tick runs them exactly like the tier
    timers did: `homelab-maint run --task NAME [--apply]`; `--apply` only when the task's own mode is "apply" (the runner still
    needs mode = apply AND the flag). Knowing nothing about the task class without importing it, heavy/gates come from config."""
    mc = mcfg if mcfg is not None else core.load_config()
    out: dict[str, Job] = {}
    for name, t in (mc.get("tasks", {}) or {}).items():
        if not isinstance(t, dict) or not t.get("schedule") or t.get("enabled", True) is False:
            continue
        if not NAME_RX.match(str(name).replace("_", "-")):
            errs.append(f"task {name}: unusable name for the scheduler")
            continue
        raw = {"name": str(name).replace("_", "-"), "title": t.get("title", name), "source": "task",
               # --scheduled: the tick is never the owner's `run --task` override, so routine.RunGuard holds it to windows and freeze
               "command": ["{self}", "run", "--task", name, "--scheduled"] + (["--apply"] if t.get("mode") == "apply" else []),
               "schedule": t["schedule"], "mode": "managed", "class": t.get("class", "P3"),
               # An apply-mode task is a cleaner: serialised with the other heavy work and kept out of the freeze windows by default
               # (`run --task X` is a manual override for the routine guard, so the tick itself must honour the freeze).
               "heavy": bool(t.get("heavy", t.get("mode") == "apply")), "monitor": bool(t.get("monitor", False)),
               "disruptive": bool(t.get("disruptive", t.get("mode") == "apply" and not t.get("monitor", False))),
               "gates": t.get("gates", []), "timeout_s": t.get("timeout_s", 1800), "jitter_s": t.get("jitter_s", 0),
               "notify": {"on_failure": "none"}}          # the runner's Notifier already alerts on task results
        j = _parse_job(raw, {}, errs, sched)
        if j:
            out[j.name] = j
    return out


# --------------------------------------------------------------------------- command, environment, user switching
def user_info(user: str) -> tuple[int, int, str] | None:
    try:
        p = pwd.getpwnam(user)
        return p.pw_uid, p.pw_gid, p.pw_dir
    except KeyError:
        return None


def expand(arg: str, sched: dict, home: str = "") -> str:
    return (arg.replace("{self}", sched["self_cmd"]).replace("{python}", sched["python"])
            .replace("{home}", home))


def build_env(job: Job, sched: dict, lookup: Callable[[str], tuple[int, int, str] | None] = user_info,
              exists: Callable[[str], bool] = os.path.exists) -> dict[str, str] | None:
    """The environment the unit/cron would have given the command (NOT the tick's own environment). None: unknown user."""
    if job.user == "root":
        env = dict(sched["env_root"])
    else:
        ui = lookup(job.user)
        if ui is None:
            return None
        uid, _gid, home = ui
        env = {k: v.replace("{home}", home) for k, v in sched["env_user"].items()}
        env.update(HOME=home, USER=job.user, LOGNAME=job.user)
        rd = f"/run/user/{uid}"
        if exists(rd):                              # the user manager's runtime dir: `systemctl --user` works through it
            env["XDG_RUNTIME_DIR"] = rd
            if exists(f"{rd}/bus"):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={rd}/bus"
    home = env.get("HOME", "")
    env.update({str(k): expand(str(v), sched, home) for k, v in job.env.items()})
    return env


def build_argv(job: Job, sched: dict, home: str = "") -> list[str]:
    argv = [expand(a, sched, home) for a in job.command]
    if job.ionice_class:
        io = [sched["ionice"], "-c", str(job.ionice_class)]
        if job.ionice_class != 3 and job.ionice_prio is not None:
            io += ["-n", str(job.ionice_prio)]
        argv = io + argv
    if job.user != "root":
        argv = [sched["runuser"], "-u", job.user, "--"] + argv
    return argv


def build_spec(job: Job, sched: dict, run_id: str, attempt: int, lookup=user_info, exists=os.path.exists,
               now: float | None = None) -> dict | None:
    """Everything the supervisor needs. None when the job cannot be launched (unknown user): the caller records a failure."""
    env = build_env(job, sched, lookup, exists)
    if env is None:
        return None
    home = env.get("HOME", "")
    ui = lookup(job.user) if job.user != "root" else None
    base = core.STATE_DIR / "jobruns" / job.name
    logd = core.LOG_DIR / "jobs" / job.name
    hook = [expand(a, sched, home) for a in job.hooks.get("on_failure", [])]
    secrets_ = [v for k, v in env.items() if re.search(r"(?i)pass|secret|token|key|cred", k) and len(v) >= 6]
    return {
        "job": job.name, "run_id": run_id, "attempt": attempt, "argv": build_argv(job, sched, home), "env": env,
        "cwd": job.workdir or (home if ui and os.path.isdir(home) else "/"), "timeout_s": job.timeout_s,
        "grace_s": job.kill_grace_s, "nice": job.nice, "oom_score_adj": job.oom_score_adj,
        "log_path": str(logd / f"{run_id}.log"), "log_head": int(sched["log_head_kib"]) * 1024,
        "log_tail": int(sched["log_tail_kib"]) * 1024, "tee_to": job.tee_to,
        "tee_owner": [ui[0], ui[1]] if ui else None, "literals": secrets_,
        "run_path": str(base / f"{run_id}.run.json"), "done_path": str(base / f"{run_id}.done.json"),
        "hook": hook, "user": job.user, "hook_timeout_s": int(sched["hook_timeout_s"]),
        "drain_s": float(sched["drain_s"]),
        "hook_argv": ([sched["runuser"], "-u", job.user, "--"] + hook) if hook and job.user != "root" else hook,
        "mounts": list(job.requires_mounts), "mount_timeout_s": int(sched["mount_timeout_s"]), "systemctl": sched["systemctl"],
    }


def new_run_id(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(2)


def mount_unit(path: str) -> str:
    """The systemd mount unit of a path, like `systemd-escape --path --suffix=mount`: '/' becomes '-', every character outside
    [A-Za-z0-9:_.] (and a leading '.') becomes \\xNN per UTF-8 byte."""
    segs = [x for x in path.split("/") if x]
    if not segs:
        return "-.mount"
    esc = "-".join("".join(c if c.isascii() and (c.isalnum() or c in ":_.") else "".join(f"\\x{b:02x}" for b in c.encode()) for c in seg)
                   for seg in segs)
    return (esc.replace(".", "\\x2e", 1) if esc.startswith(".") else esc) + ".mount"


def ensure_mounts(paths: list[str], systemctl: str, timeout_s: int, ismount: Callable[[str], bool] = os.path.ismount,
                  run: Callable[..., Any] = subprocess.run) -> tuple[bool, str]:
    """RequiresMountsFor= parity: a required path that is not a mountpoint gets one `systemctl start <unit>.mount` (a no-op when
    the unit is already mounted), then it must BE a mountpoint. -> (ok, why). The legacy unit failed its start the same way."""
    for p in paths:
        if ismount(p):
            continue
        unit = mount_unit(p)
        try:
            rc = run([systemctl, "start", "--", unit], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{p} is not mounted and `systemctl start {unit}` could not run ({type(exc).__name__})"
        if rc != 0 or not ismount(p):
            return False, f"{p} is not mounted and `systemctl start {unit}` did not mount it (rc={rc})"
    return True, ""


# --------------------------------------------------------------------------- process helpers (also used by the scheduler)
def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def read_stat(pid: int) -> tuple[str, int, int, int] | None:
    """(state, ppid, pgrp, start ticks since boot) from /proc/<pid>/stat; None when the process does not exist."""
    try:
        t = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    j = t.rfind(")")
    f = t[j + 2:].split() if j > 0 else []
    try:
        return f[0], int(f[1]), int(f[2]), int(f[19])
    except (IndexError, ValueError):
        return None


def proc_start(pid: int) -> int | None:
    s = read_stat(pid)
    return s[3] if s else None


def proc_alive(pid: int | None, start: int | None) -> bool:
    """Alive = exists, not a zombie, and (when a start time was recorded) the SAME process: a recycled pid has another start
    time, so a stale record can never keep a dead run 'running' nor get an innocent process killed."""
    if not pid:
        return False
    s = read_stat(pid)
    return bool(s and s[0] != "Z" and (start is None or s[3] == start))


def group_alive(pgid: int | None) -> bool:
    if not pgid or pgid < 2:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid: int | None, sig: int, leader_start: int | None = None) -> bool:
    """Signal a whole process group. Refuses when the group leader's pid now belongs to a different process (pid reuse)."""
    if not pgid or pgid < 2:
        return False
    s = read_stat(pgid)
    if s is not None and leader_start is not None and s[3] != leader_start:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# --------------------------------------------------------------------------- scrubbing and the capped log
# Every pattern below is LINEAR on any 4 KiB line: it starts from a literal anchor (a keyword, a flag, `://`), every repetition
# that could run away is bounded ({0,40}) or possessive (`++`, `{n,}+`, Python >= 3.11), and nothing ever backs off through a long
# run of word characters. (The first version had a leading `[\w.-]*` before the keyword: 13 s per line on "x.pass" repeated, which
# froze the supervisor and with it the job's timeout.) tests/test_jobs.py times the worst shapes.
_KW = r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth)"
_RUN = "A-Za-z0-9+_="                                                  # the characters of an opaque token run (plus "-", added where used)
_SCRUB = [
    # Authorization headers, also JSON-quoted ("Authorization": "Bearer x")
    (re.compile(r"(?i)\b(authorization|proxy-authorization)(['\"]?\s*[:=]\s*['\"]?)(?:(?:bearer|basic|token)\s+)?[^\s,;'\"]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b((?:set-)?cookie['\"]?\s*[:=]\s*)\S.*"), r"\1[redacted]"),            # Cookie / Set-Cookie: the rest of the line
    # name=value, name: value, "name": "value", 'name': 'value' (a closing quote may sit between the name and the separator)
    (re.compile(r"(?i)(" + _KW + r"[\w.-]{0,40}+['\"]?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"), r"\1[redacted]"),
    # --password hunter2 / --token abc (the value is the next word; `--password-stdin` carries none)
    (re.compile(r"(?i)((?<![\w-])--?(?:password|passwd|pass|pwd|token|secret|api[_-]?key|access[_-]?key|auth|credentials?)[=\s]++)(?!-)(\S+)"),
     r"\1[redacted]"),
    # `-p SECRET` only where -p IS a password: docker/podman login, sshpass, the mysql family (not `docker run -p 80:80`, `mkdir -p`)
    (re.compile(r"((?i:\b(?:login|sshpass|mysql\w*|mariadb\w*)\b)[^\n]{0,200}?\s-p\s?)(?!-)(\S+)"), r"\1[redacted]"),
    # curl -u user:pw, --user user:pw (not `docker run -u 1000:1000`)
    (re.compile(r"((?i:\b(?:curl|wget)\b)[^\n]{0,300}?\s(?:-u|--user|--proxy-user)[\s=]?[^\s:]{1,64}+:)(\S+)"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}+"), r"\1[redacted]"),                    # a bearer token without its header
    (re.compile(r"(\b[a-z][a-z0-9+.-]{0,30}+://[^/\s:@]{1,100}+:)[^@\s/]{1,200}+@"), r"\1[redacted]@"),   # URL user:password@
    (re.compile(r"(?i)([?&](?:token|key|api_?key|apikey|secret|password|sig|signature|access_token)=)[^&\s#]+"), r"\1[redacted]"),
    # well-known token shapes: AWS key id, GitHub token, Slack token, JWT
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}+|\bxox[abprs]-[A-Za-z0-9-]{10,}+|\beyJ[\w-]{8,}+(?:\.[\w-]{3,}+){1,2}+"),
     "[redacted]"),
    # last: an opaque token-like run (needs letters AND digits). A maximal run only: it starts where no run character precedes it.
    (re.compile(r"(?<![" + _RUN + r"-])[" + _RUN + r"-]{40,}+"),
     lambda m: "[redacted]" if re.search(r"\d", m[0]) and re.search(r"[A-Za-z]", m[0]) else m[0]),
]
# Cheap pre-filter: most log lines contain none of these and skip every pattern above.
_KEYWORDS = re.compile(r"(?i)pass|secret|token|key|cred|auth|://|cookie|bearer|login|sshpass|mysql|mariadb|curl|wget|eyJ|AKIA|ASIA|gh[pousr]_|xox|"
                       r"[A-Za-z0-9+_=-]{40}")


def scrub_line(s: str, literals: tuple[str, ...] | list[str] = ()) -> str:
    for lit in literals:
        if lit and lit in s:
            s = s.replace(lit, "[redacted]")
    if not _KEYWORDS.search(s):
        return s
    for rx, rep in _SCRUB:
        s = rx.sub(rep, s)
    return s


def _open_append_as(path: str, owner) -> int:
    """Open `path` for append (creating it 0644) with the effective identity of `owner` [uid, gid] when running as root, and
    never through a symlink: a log file in a user-writable directory must not become a way to make root append to /etc/something.
    -1 when it cannot be opened (the tee is optional)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    switch = bool(owner) and os.geteuid() == 0
    try:
        if switch:
            os.setegid(owner[1])
            os.seteuid(owner[0])
        try:
            return os.open(path, flags, 0o644)
        finally:
            if switch:
                os.seteuid(0)
                os.setegid(0)
    except OSError:
        return -1


class LogWriter:
    """Scrubbing, size-capped run log: the first `head` bytes verbatim, then only the last `tail` bytes survive (the part that
    says why it failed), with an explicit marker for what was dropped. Optionally mirrors the head to `tee_to` (the legacy log).

    A log that cannot be written (disk full, EIO, read-only filesystem, unopenable path) NEVER stops the run: `err` records the
    first failure ('ENOSPC'), every later write is skipped or retried quietly, and the caller keeps draining the job's output
    (a closed pipe would SIGPIPE a backup mid-rsync; the legacy unit logged to the journal and ran on). The last lines are
    still kept in memory for the done record."""

    def __init__(self, path: str, head: int, tail: int, literals=(), tee_to: str = "", tee_owner=None, keep_lines: int = 40):
        self.err = ""
        self.fd = -1
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        except OSError as exc:
            self._fail(exc)
        self.head, self.tail_cap, self.lit = head, tail, tuple(literals)
        self.written = self.dropped = self.tail_bytes = 0
        self.tailq: collections.deque[bytes] = collections.deque()
        self.last: collections.deque[str] = collections.deque(maxlen=keep_lines)
        self.buf = b""
        self.tee = -1
        self.tee_written = 0
        if tee_to:
            self.tee = _open_append_as(tee_to, tee_owner)
        self.truncated = False

    def _fail(self, exc: OSError) -> None:
        if not self.err:
            self.err = errno.errorcode.get(exc.errno or 0, type(exc).__name__)

    def _w(self, fd: int, data: bytes) -> bool:
        """write() all of `data`; False (and `err` set) instead of an exception when the log cannot take it."""
        if fd < 0:
            return False
        try:
            while data:
                n = os.write(fd, data)
                if n <= 0:
                    raise OSError(errno.EIO, "short write")
                data = data[n:]
            return True
        except OSError as exc:
            self._fail(exc)
            return False

    def raw(self, text: str) -> None:
        """Supervisor's own lines (header/trailer): never counted against the cap, never scrubbed (they carry no command text)."""
        self._w(self.fd, (text + "\n").encode("ascii", "replace"))

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                if len(self.buf) > 65536:                # a line without end (progress bar): cut it
                    i = len(self.buf) - 1
                else:
                    return
            line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
            self._line(line)

    def _line(self, line: bytes) -> None:
        t = line.decode("utf-8", "replace").rstrip("\r\n").split("\r")[-1]
        t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", t)
        t = scrub_line(t[:4096] + (" [line cut]" if len(t) > 4096 else ""), self.lit)     # bounded work per line, whatever it holds
        self.last.append(t[:300])
        out = (t + "\n").encode("utf-8", "replace")
        if self.tee >= 0 and self.tee_written < self.head:
            try:
                os.write(self.tee, out)
                self.tee_written += len(out)
            except OSError:
                self.tee = -1
        if self.written < self.head:
            self._w(self.fd, out)
            self.written += len(out)
            return
        self.truncated = True
        self.tailq.append(out)
        self.tail_bytes += len(out)
        while self.tail_bytes > self.tail_cap and self.tailq:
            self.tail_bytes -= len(self.tailq.popleft())
            self.dropped += 1

    def close(self) -> None:
        if self.buf:
            self._line(self.buf + b"\n")
            self.buf = b""
        if self.truncated:
            self._w(self.fd, f"[... {self.dropped} lines omitted by homelab-maint (log cap) ...]\n".encode())
            self._w(self.fd, b"".join(self.tailq))
        for fd in (self.fd, self.tee):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def rotate_logs(job: str, keep: int) -> int:
    """Keep the newest `keep` run logs (and their .done/.run records) of one job. Returns how many files were removed."""
    n = 0
    for d, pat in ((core.LOG_DIR / "jobs" / job, "*.log"), (core.STATE_DIR / "jobruns" / job, "*.done.json")):
        try:
            files = sorted(d.glob(pat), key=lambda p: p.name)
        except OSError:
            continue
        for p in files[:-keep] if keep > 0 else files:
            try:
                p.unlink()
                n += 1
                if pat != "*.log":
                    (d / (p.name[:-len(".done.json")] + ".run.json")).unlink(missing_ok=True)
            except OSError:
                pass
    return n


# --------------------------------------------------------------------------- the supervisor (runs detached, as root)
def _private_dir(p: Path) -> None:
    """jobruns/<job>/ holds run specs (environment values) and run records (the last 30 output lines): root only."""
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


def _atomic(path: str, obj: dict, mode: int = 0o600) -> None:
    """Write a run record. 0600, not 0644: the done record carries the tail of the job's output, which no local user may read
    (the log itself is 0640). Only the tick (root) reads it."""
    p = Path(path)
    _private_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)      # born with the final mode: no 0644 window
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, sort_keys=True, default=str))
    os.chmod(tmp, mode)
    os.replace(tmp, p)


_STOP = {"flag": False}


def _on_term(_sig, _frm) -> None:
    _STOP["flag"] = True


def _wait_group_gone(pgid: int, secs: float) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not group_alive(pgid):
            return True
        time.sleep(0.1)
    return not group_alive(pgid)


def _stop_group(proc: subprocess.Popen | None, pgid: int | None, grace_s: float) -> None:
    """The supervisor itself broke while the job runs: end the job (TERM, then KILL after the grace) and reap it BEFORE the done
    record is written. Letting go of a live job would close its stdout pipe (SIGPIPE at its next write, possibly mid-rsync), leave
    remnants running after the tick has reaped the run as finished, and let a retry start next to them."""
    if proc is None or not pgid:
        return
    for sig, wait in ((signal.SIGTERM, max(float(grace_s), 1.0)), (signal.SIGKILL, 5.0)):
        proc.poll()                                         # reaps the leader: a zombie would keep the group "alive" for ever
        if proc.returncode is not None and not group_alive(pgid):
            return
        kill_group(pgid, sig)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            proc.poll()
            if proc.returncode is not None and not group_alive(pgid):
                return
            time.sleep(0.1)
    proc.poll()


def supervise(spec_path: str) -> int:
    """Run one job to completion. Always ends by writing the done file (even on internal errors) and returns 0: the verdict
    lives in the done record, a non-zero exit here would only add systemd noise. A log that cannot be written is NOT an internal
    error (the job runs on, `log_error` says so); any other internal error ends the job first, then reports."""
    with open(spec_path) as f:
        spec = json.load(f)
    try:
        os.unlink(spec_path)                             # the spec may carry environment values: do not leave it around
    except OSError:
        pass
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    os.umask(0o022)                                      # systemd's default UMask=0022, whatever umask the tick or a shell had
    t0 = time.time()
    done: dict[str, Any] = {"rc": None, "signal": None, "timed_out": False, "cancelled": False, "leftover_killed": False,
                            "t_start": t0, "log": spec["log_path"], "attempt": spec.get("attempt", 1)}
    log: LogWriter | None = None
    pgid = None
    proc: subprocess.Popen | None = None
    try:
        try:                                           # absolute, like systemd's Nice=; unset means 0 (the tick unit itself runs at 10)
            os.setpriority(os.PRIO_PROCESS, 0, int(spec["nice"]) if spec.get("nice") is not None else 0)
        except OSError:
            pass
        if spec.get("oom_score_adj") is not None:
            try:
                Path("/proc/self/oom_score_adj").write_text(str(int(spec["oom_score_adj"])))
            except OSError:
                pass
        log = LogWriter(spec["log_path"], spec["log_head"], spec["log_tail"], spec.get("literals", ()), spec.get("tee_to", ""),
                        spec.get("tee_owner"))
        log.raw(f"# homelab-maint job={spec['job']} run={spec['run_id']} attempt={spec.get('attempt', 1)} "
                f"started={time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(t0))} user={spec['user']}")
        run = {"sup_pid": os.getpid(), "sup_start": proc_start(os.getpid()), "boot": boot_id(), "job_pid": None,
               "job_pgid": None, "job_start": None, "t0": t0}
        _atomic(spec["run_path"], run)
        mounted, why = ensure_mounts(spec.get("mounts") or [], spec.get("systemctl", "/usr/bin/systemctl"),
                                     int(spec.get("mount_timeout_s", 60)))
        if not mounted:                                  # the unit would not have started: fail loudly, never run on the wrong disk
            log.raw(f"# not started: {why}")
            done.update(rc=125, error=f"required mount missing: {why[:70]}")
        elif _STOP["flag"]:
            log.raw("# cancelled before the command started")
            done.update(cancelled=True)
        else:
            try:
                proc = subprocess.Popen(spec["argv"], env=spec["env"], cwd=spec["cwd"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            except OSError as exc:
                log.raw(f"# could not start the command: {type(exc).__name__}: {_ascii(exc, 120)}")
                done.update(rc=127, error=f"exec failed: {type(exc).__name__}")
        if proc is not None:
            pgid = proc.pid
            run.update(job_pid=proc.pid, job_pgid=pgid, job_start=proc_start(proc.pid))
            _atomic(spec["run_path"], run)
            deadline = t0 + spec["timeout_s"] if spec["timeout_s"] > 0 else None
            term_at = None
            exit_t = None
            eof = False
            fd = proc.stdout.fileno()
            while True:
                now = time.time()
                if not term_at and (_STOP["flag"] or (deadline and now > deadline)) and proc.poll() is None:
                    done["cancelled"] = _STOP["flag"]
                    done["timed_out"] = not _STOP["flag"]
                    kill_group(pgid, signal.SIGTERM)
                    term_at = now
                if term_at and now > term_at + spec["grace_s"]:
                    kill_group(pgid, signal.SIGKILL)
                    term_at = now + 3600                 # KILL sent; do not repeat every loop
                if not eof:
                    r, _, _ = select.select([fd], [], [], 0.5)
                    if r:
                        try:
                            data = os.read(fd, 65536)
                        except OSError:                  # EIO on the pipe: nothing more will come
                            data = b""
                        if data:
                            log.feed(data)
                        else:
                            eof = True
                else:
                    time.sleep(0.1)
                if proc.poll() is not None:
                    exit_t = exit_t or time.time()
                    if eof or time.time() - exit_t > spec["drain_s"]:
                        break
            rc = proc.wait()
            if rc < 0:
                done.update(signal=-rc, rc=128 - rc)
            else:
                done["rc"] = rc
            if group_alive(pgid) and not _wait_group_gone(pgid, 0.5):    # leftovers of the run: KillMode=control-group parity
                done["leftover_killed"] = True
                kill_group(pgid, signal.SIGTERM)
                if not _wait_group_gone(pgid, 5):
                    kill_group(pgid, signal.SIGKILL)
            try:
                os.close(fd)
            except OSError:
                pass
    except Exception as exc:                              # noqa: BLE001 - the done file must exist whatever happened
        done.update(rc=done.get("rc") if done.get("rc") is not None else 255, error=f"supervisor: {type(exc).__name__}: {_ascii(exc, 100)}")
        try:
            _stop_group(proc, pgid, spec.get("grace_s", 20))     # never leave the job running unobserved, with a closed pipe
        except Exception:                                 # noqa: BLE001
            pass
    finally:
        failed = done.get("rc") not in (0,) or done.get("error")
        if log is not None:
            try:
                dur = time.time() - t0
                log.raw(f"# exit rc={done.get('rc')} signal={done.get('signal')} timed_out={done['timed_out']} duration={dur:.1f}s")
                if failed and spec.get("hook_argv") and not done.get("cancelled"):   # a stop (shutdown) is not a failure to announce now
                    try:
                        _run_hook(spec, log, done)
                    except Exception as exc:              # noqa: BLE001 - a broken hook must not cost us the tail and the done record
                        done["hook"] = f"error: {type(exc).__name__}"
                log.close()
                done["tail"] = list(log.last)[-30:]
                done["log_truncated"] = log.truncated
                if log.err:
                    done["log_error"] = log.err
            except Exception as exc:                      # noqa: BLE001
                done.setdefault("error", f"log: {type(exc).__name__}")
        done["t_end"] = time.time()
        _atomic(spec["done_path"], done)
    return 0


def _run_hook(spec: dict, log: LogWriter, done: dict) -> None:
    """The on-failure hook (e.g. backup-failed.sh, the legacy OnFailure= unit's command) with its own timeout; its output goes
    to the same log under a marker. Runs in its own process group so a hung hook cannot outlive the supervisor."""
    log.raw("# --- on_failure hook ---")
    try:
        p = subprocess.Popen(spec["hook_argv"], env=spec["env"], cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except OSError as exc:
        log.raw(f"# hook could not start: {type(exc).__name__}")
        done["hook"] = "start-failed"
        return
    end = time.time() + spec["hook_timeout_s"]
    fd = p.stdout.fileno()
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            log.feed(data)
    if p.poll() is None:
        kill_group(p.pid, signal.SIGTERM)
        time.sleep(1)
        kill_group(p.pid, signal.SIGKILL)
        done["hook"] = "timeout"
    else:
        done["hook"] = f"rc={p.wait()}"
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------------- spawning (scheduler side)
_KEEP: list[subprocess.Popen] = []


def spawn_supervisor(spec: dict) -> int:
    """Write the run spec (0600, root only: it carries the environment) and start the supervisor detached in its own session.
    Returns the supervisor pid. Raises OSError when it cannot be started."""
    base = core.STATE_DIR / "jobruns" / spec["job"]
    _private_dir(base.parent)
    _private_dir(base)
    sp = base / f"{spec['run_id']}.spec.json"
    fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(spec, f)
    env = {"PATH": SCHED_DEFAULTS["env_root"]["PATH"], "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    for k in ("HOMELAB_MAINT_STATE", "HOMELAB_MAINT_LOG", "HOMELAB_MAINT_CONF", "HOMELAB_MAINT_RUN", "HOMELAB_MAINT_TZ", "TZ"):
        if k in os.environ:
            env[k] = os.environ[k]
    p = subprocess.Popen([sys.executable, "-B", "-m", "homelab_maint.jobs", "supervise", str(sp)], env=env, cwd="/",
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    _KEEP.append(p)
    return p.pid


# --------------------------------------------------------------------------- results
class _Safe(dict):
    def __missing__(self, key):
        return "?"


def fmt(template: str, data: dict) -> str:
    try:
        return template.format_map(_Safe({k: v for k, v in data.items() if not isinstance(v, (dict, list))}))
    except (ValueError, IndexError, KeyError, AttributeError):
        return ""


def parse_time(v: Any, fmt_: str = "%Y-%m-%d %H:%M:%S") -> float | None:
    """A status file's timestamp (host-local wall clock like `date '+%F %T'`, or ISO with an offset, or epoch) -> epoch."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str):
        return None
    for f in (fmt_, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            import datetime as _dt
            d = _dt.datetime.strptime(v.strip(), f)
            return d.timestamp() if d.tzinfo else d.replace(tzinfo=schedule.host_tz()).timestamp()
        except ValueError:
            continue
    return None


def status_ctx(data: dict, job: Job) -> dict:
    """The keys a summary template may use: the job's own status JSON plus `job` and `space` ("used 130G" / "freed 512G": the
    backup scripts report used_gb negative when a run freed space, which reads badly as "used -512G")."""
    ctx = {**data, "job": data.get("job", job.name)}
    used = data.get("used_gb")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        ctx["space"] = f"used {int(used)}G" if used >= 0 else f"freed {int(-used)}G"
    return ctx


def observed(job: Job, now: float | None = None) -> dict[str, Any]:
    """What the job's OWN artefacts say about its last run, without running anything: {"finished": epoch|None, "result": str|None,
    "summary": str, "age_h": float|None, "stale": bool|None}. Used for observe-mode display and for freshness limits."""
    s = job.success
    out: dict[str, Any] = {"finished": None, "result": None, "summary": "", "age_h": None, "stale": None}
    now = now if now is not None else time.time()
    data = None
    if s.get("status_json"):
        data = core.read_json(Path(s["status_json"]), None)
        if isinstance(data, dict):
            out["finished"] = parse_time(data.get(s.get("finished_key", "finished")), s.get("time_format", "%Y-%m-%d %H:%M:%S"))
            out["result"] = str(data.get(s.get("result_key", "result")))
            tpl = s.get("summary") if str(out["result"]) in [str(x) for x in s.get("ok_values", ["ok"])] else s.get("fail_summary")
            out["summary"] = _ascii(fmt(tpl or "", status_ctx(data, job)))
    if out["finished"] is None and s.get("touch_file"):
        try:
            out["finished"] = os.stat(s["touch_file"]).st_mtime
        except OSError:
            pass
    if out["finished"] is not None:
        out["age_h"] = round((now - out["finished"]) / 3600, 1)
        lim = _num(s.get("max_age_hours"))
        out["stale"] = bool(lim and out["age_h"] > lim)
    return out


def _findall(pattern: str, text: str) -> list:
    try:
        return re.findall(pattern, text, re.M)
    except re.error:
        return []


def _summary_text(s: dict, done: dict, started: float, fresh_only: bool = False) -> str:
    """What summary_regex / exit_regex are matched against: the last 8 KiB of `summary_file` (strftime-expanded with the run's
    start, so "logs/sync-%Y-%m-%d.log" works) or, without one, the run's own captured output. With fresh_only a file this run did
    not touch reads as empty: an old run's marker is not this run's verdict."""
    path = s.get("summary_file")
    if not path:
        return "\n".join(done.get("tail") or [])
    try:
        path = time.strftime(path, time.localtime(started)) if "%" in path else path
        with open(path, "rb") as f:
            if fresh_only and os.fstat(f.fileno()).st_mtime < started - 5:
                return ""
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            return f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def make_result(job: Job, done: dict, started: float, now: float) -> Result:
    """core.Result for one finished run: exit code first, then the job's own status JSON / touch file / summary pattern."""
    s = job.success
    ok_codes = [int(c) for c in s.get("exit_codes", [0])]
    warn_codes = [int(c) for c in s.get("warn_exit_codes", [])]
    rc, sig = done.get("rc"), done.get("signal")
    fail = job.fail_status
    hard = "error" if fail == "crit" else fail              # infrastructure failures (lost, timeout) read as "error" when crit
    last = next((ln for ln in reversed(done.get("tail") or []) if ln.strip() and not ln.startswith("#")), "")
    m: dict[str, Any] = {"rc": rc if rc is not None else -1, "attempt": int(done.get("attempt", 1)),
                         "duration_s": round(float(done.get("t_end", now)) - float(done.get("t_start", started)), 1)}
    bad = True                                              # every branch below that is not a success says so explicitly
    if done.get("lost"):
        status, summ = "error", f"run lost: {done.get('lost_why', 'supervisor vanished')}"
    elif done.get("cancelled"):
        status, summ = "error", "cancelled (SIGTERM to the supervisor)"
    elif done.get("timed_out"):
        status, summ = hard, f"timed out after {job.timeout_s}s and was killed"
    elif done.get("error"):
        status, summ = hard, _ascii(done["error"], 100)
    elif sig:
        status, summ = fail, f"killed by signal {sig}"
    elif rc in ok_codes:
        status, summ, bad = "ok", f"exit {rc}" + (f": {last}" if last else ""), False
    elif rc in warn_codes:
        status, summ, bad = "warn", f"exit {rc} (tolerated)" + (f": {last}" if last else ""), False
    else:
        status, summ = fail, f"exit {rc}" + (f": {last}" if last else "")
    clean = not bad and status == "ok"
    if s.get("status_json") and not (done.get("lost") or done.get("cancelled")):
        obs = observed(job, now)
        data = core.read_json(Path(s["status_json"]), None)
        fresh = obs["finished"] is not None and obs["finished"] >= started - 5 and isinstance(data, dict)
        if not fresh:
            if clean:
                status, summ = "warn", "finished with exit 0 but its status file was not refreshed by this run"
        else:
            okv = [str(x) for x in s.get("ok_values", ["ok"])]
            res = str(data.get(s.get("result_key", "result")))
            m["result"] = res
            warns = _num(data.get(s.get("warn_key", "warnings")), 0) or 0
            reason = _ascii(data.get(s.get("reason_key", "reason"), ""), 100)
            d2 = status_ctx(data, job)
            if res in okv:
                if clean:                                   # the script's own verdict agrees with the exit code
                    status = "warn" if warns > 0 else "ok"
                    summ = _ascii(fmt(s.get("summary", ""), d2)) or summ
                    if warns > 0 and reason:
                        summ = _ascii(f"{summ} ({int(warns)} warning(s): {reason})")
            else:                                           # the script itself says it failed, whatever its exit code was
                if not bad:
                    status, bad = fail, True
                summ = _ascii(fmt(s.get("fail_summary", ""), {**d2, "reason": reason or "see the log"})) or f"result {res}: {reason}"
    elif s.get("touch_file") and clean:
        obs = observed(job, now)
        if obs["finished"] is None or obs["finished"] < started - 5:
            status, summ = "warn", "finished with exit 0 but its success marker was not updated"
    if s.get("exit_regex") and status in ("ok", "warn"):
        # A wrapper that swallows the real exit code (tunarr's run.sh logs "exit=$?" and exits with `ls | xargs`'s status) writes
        # it into a log: the LAST match of exit_regex in this run's summary_file (or captured output) is the verdict.
        hits = _findall(s["exit_regex"], _summary_text(s, done, started, fresh_only=True))
        code = None
        if hits:
            try:
                code = int(hits[-1] if isinstance(hits[-1], str) else hits[-1][0])
            except ValueError:
                code = None
        if code is None:
            if status == "ok":
                status, summ = "warn", "finished but its exit marker was not found in this run's log"
        else:
            m["inner_rc"] = code
            if code in warn_codes:
                status, summ = "warn", f"inner command exit {code} (tolerated)"
            elif code not in ok_codes:
                status, summ, bad = fail, f"inner command exit {code} (the wrapper itself exited {rc})", True
    m["failed"] = 1 if bad else 0
    if s.get("summary_regex") and status in ("ok", "warn"):
        hits = _findall(s["summary_regex"], _summary_text(s, done, started))
        if hits:
            h = hits[-1]
            summ = _ascii(h if isinstance(h, str) else h[0])
    if done.get("log_error") and status in ("ok", "warn"):
        # The run itself was fine (it ran on, its output was drained): only the supervisor could not write the whole log. A warning,
        # not a failure: no alert, no retry, but the owner sees the log is incomplete.
        m["log_degraded"] = 1
        status, summ = "warn", f"{_ascii(summ, 105)} [log incomplete: {_ascii(done['log_error'], 20)}]"
    return Result(status, _ascii(summ), metrics={k: v for k, v in m.items() if isinstance(v, (int, float, str, bool))})


def read_done(path: str | Path) -> dict | None:
    d = core.read_json(Path(path), None)
    return d if isinstance(d, dict) and "t_end" in d else None


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) == 2 and a[0] == "supervise":
        return supervise(a[1])
    print("usage: python3 -m homelab_maint.jobs supervise SPEC.json   (started by the scheduler; not for humans)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
