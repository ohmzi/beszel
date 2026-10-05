"""native: faithful ports of the small legacy maintenance scripts, plus the OS-timer verifier.

Registered tasks (all default to report-only when they could change anything):
  surrealdb_health        C0 check   <- /usr/local/sbin/notebook-db-alert.sh        (notebook-db-alert.timer, 15 min)
  comfyui_idle_reclaim    C1 check   <- /usr/local/bin/comfyui-idle-vram.sh          (comfyui-idle-vram.timer, 5 min)
  immich_recycle          C1 check   <- immich-server-recycle.service + gate drop-in (immich-server-recycle.timer, 2 h)
  openwebui_media_prune   C1 daily   <- /usr/local/bin/prune-openwebui-media.sh      (prune-openwebui-media.timer, 04:00)
  docker_containers_prune C1 weekly  <- step 1 of /usr/local/sbin/docker-prune.sh    (stopped-container removal)
  docker_prune_parity     C0 weekly  proves docker_cache + docker_images + docker_containers_prune cover docker-prune.sh
  os_jobs                 C0 check   verifies the OS-managed timers (apt, logrotate, fstrim, ...) ran and succeeded
  docker_prune_exposure   C0 check   what docker-prune.timer would still delete that the natives keep; PAGES at first sight
Hook entry (not a task): homelab_maint/smart_hook.py  <- /usr/local/sbin/smart-alert.sh (smartd -M exec); re-exported here.
Retired, not ported: mem-guard (see RETIRED_MEM_GUARD).

Safety model: every mutation goes through ctx.act (via _do or cleaners._Acts), so report mode only audits "dry-run".
Nothing here sends a message by itself: the only outbound path is `_notify`, a thin bridge to notify.send.
Anything unparsable, missing or erroring selects/does NOTHING (fail closed).

Each port was developed against the legacy script read end to end and is parity-tested in tests/test_native.py: the
legacy script is run in a sandbox copy (tmp dirs, fake docker/du/df/runuser/curl on PATH, nothing real touched) and its
decisions are compared with the port's on identical inputs; the decision tables are also frozen as literals in the tests.

=====================================================================================================================
PARITY.md  surrealdb_health  (notebook-db-alert.sh)
=====================================================================================================================
Checked identical (sandboxed script vs port, byte-exact problem texts, signature, alert decision, subject and body):
  * wal      = floor(sum(size of ROCKS/*.log, maxdepth 1) / 1 MiB) > wal_max_mb (1024)
  * db       = ceil(du -s allocated bytes / 1 GiB) > db_max_gb (25)     (du --block-size=1G rounds UP)
  * disk     = ceil(df avail bytes / 1 GiB) < disk_min_gb (40)          (df --block-size=1G rounds UP, so 39.5 GiB
               free is "40" and does not alert: ported as is)
  * oom_kill = memory.events oom_kill > 0;  restarts = RestartCount > restart_gt (3)
  * signature = names of the failing checks only ("wal,db,"), "OK" when clean, never values; an unchanged signature is
    suppressed, a changed one alerts again (also when the set shrinks: legacy behaviour).
  * unreadable store size is not a problem (legacy: `[ "" -gt N ]` is false); unreadable df = 0 GB free = problem.
Intentional differences:
  1. RECOVERY FIX. The script only sent RESOLVED when the previous signature ended in "|BAD", which it never writes,
     so recovery was never announced. Port: no problems and a previous signature that is not ""/"OK" = recovery.
     (A sandboxed copy of the script with the one-line fix `!= "OK"` behaves exactly like the port.)
  2. Delivery is shared (see _announce). The script alerted on FIRST SIGHT (15 min); the runner's Notifier only pages after
     alert_confirm_runs consecutive runs (30 min) and sends just "<title>: <summary>". REGRESSION fixed: the leading indicator
     paged half an hour late, a one-run blip never, and the page rode on the global daily budget. Now the TASK pages a new
     incident at once (first_sight_page, default true) with the full body (every failing check, current values, what to do);
     its dedupe key is the bare task name with the Notifier's own severity, so notify's dedupe window swallows the page the
     Notifier sends when it confirms the same level (one page, not two; this needs the notify-backed HermesNotifier, which is
     the SPEC4 glue) and a crit page bypasses the per-kind and total budgets (notify: crit_bypass). A blip the Notifier never
     confirmed gets its recovery from the task; one it paged gets its recovery from the Notifier, as before. The task also pages
     what the Notifier cannot see: the SET of failing checks changing at an unchanged severity (a check joined / cleared, or a
     different failure right after a one-run flap through OK). A page notify did not accept (handled = false) is not recorded:
     the next run retries it. Every announced change has its own dedupe key (`...:<sig>:<n>`), so notify's dedupe window
     swallows a retry of the same page but never a later return to an earlier signature. first_sight_page = false restores the
     Notifier-only first page (and the change pages then wait until it has paged: no double page).
  3. Every problem is `crit` (the script paged by SMS + email for any problem). A stopped/absent container with no
     problems is `info` (visible, never pages); the script logged "status=unknown" and said nothing.
  4. Thresholds come from [tasks.surrealdb_health] (same defaults) instead of env vars; DRY_RUN is moot (C0 sends
     nothing). /var/log/notebook-db-alert.log is no longer written: the per-run heartbeat is the history.jsonl metrics.
  5. Up to two `docker container inspect --format` calls: one (Id|Status|RestartCount) instead of three, and one
     resolving the store's host directory from the container's /mydata bind mount, because a hardcoded data dir went
     stale (config `data_dir` wins; the mount is used when docker answers and the path exists; SURREAL_DEFAULTS is the
     last fallback). The cgroup is found with gates.cg_dir (systemd and cgroupfs layouts). NEVER inspect Config.Cmd/Env
     of this container: it carries the DB password.

=====================================================================================================================
PARITY.md  comfyui_idle_reclaim  (comfyui-idle-vram.sh)
=====================================================================================================================
Checked identical (sandboxed script vs port over scripted queue/pid/nvidia-smi sequences): restart only when the queue
is empty AND the container's VRAM > threshold_mb (3000, strict) on two consecutive checks; any busy/light/error check
resets the strike; a successful restart (or attempt) resets it; queue errors, non-JSON and curl failures are busy; a
pid that does not appear in nvidia-smi holds 0 MB; a stopped/missing container holds 0 MB.
Intentional differences:
  1. A queue answer without the queue_running/queue_pending lists is treated as busy (script: `{}` counted as idle).
  2. VRAM is summed over every nvidia-smi row of the container (script: two rows for one pid, e.g. two GPUs, made
     `[ -gt ]` fail so it never restarted) and rows of ANY process in the container cgroup count, not only State.Pid
     (match_cgroup, default true), so an entrypoint that forks the real worker is still seen.
  3. Strikes live in ctx.state (not /run) and expire after strike_ttl_min (45): a strike older than that is a first
     strike again. The check tier runs every 15 min, so "two consecutive checks" is 15-30 min of idleness instead of
     5-10; give the task a 5 min schedule in the scheduler to keep the old cadence.
  3b. strike_min_gap_min (4): an idle observation less than this after the last strike is NOT a new strike (state untouched).
     The script's timer spaced its two checks 5 min apart by construction; with two runners (the check tier AND the task's own
     cron schedule, a manual `run --task`, a scheduler catch-up) one moment of idleness between two generations could count
     twice. A busy/light observation still resets at once. 0 restores the old behaviour.
  3c. Two lanes of state: a run that can act (ctx.apply) keeps n/t, every other run (mode report, a tier run without --apply,
     PAUSE) keeps rn/rt, so a report run sharing the state file can never add the strike that makes an apply run restart.
  3d. Apply mode re-probes the queue right before `docker restart`; a job (or an unreadable answer) cancels the restart and
     resets the strikes.
  4. min_gap_min (30) floor between restarts, so a model that reloads straight into VRAM cannot cause a restart loop.
  5. nvidia-smi failing = "skipped" (script: silently 0 MB). A failed restart is reported (script ignored the rc).
  6. mode = "report" by default (audit "dry-run", nothing restarted). `unprotect = ["^comfyui$"]` is needed because the
     global protected list contains "comfyui".
  7. ram_threshold_mb (0 = off, the default): when set, the task also fires on the container's RESIDENT SYSTEM RAM
     (proc_mem_mb, summing /proc/<pid>/statm over its pids), not only on VRAM. ComfyUI can hand a model back to the OS's
     VRAM while the process keeps it in RAM, and the restart frees both; the legacy script never looked at system RAM.
     0 reproduces the script's signal exactly, which is why it is the default — every parity sequence above runs with it
     off, and only a task that configures a threshold ever reads /proc.
  8. stop_idle_min (15 in the shipped task; 0 = off): a blunt idle-killer layered on top of the port (Ohmz choice,
     2026-10-04). When > 0 the VRAM/RAM logic above is bypassed entirely: a running container whose queue has been empty
     for that many minutes in a row is STOPPED (`docker stop`, not restarted), whatever it holds, so a rogue ComfyUI cannot
     sit on the GPU. The idle clock lives in its own per-lane key (is/ris, like the strikes) and any job or unreadable
     queue resets it; the queue is re-read right before the stop and a job cancels it. The owner restarts ComfyUI by hand.
     0 reproduces the legacy VRAM/RAM restart path exactly.

=====================================================================================================================
PARITY.md  immich_recycle  (immich-server-recycle.service + 10-homelab-gate.conf)
=====================================================================================================================
Checked: the action is `docker restart immich_server`; cadence is every 2 h from the last attempt (OnUnitActiveSec=2h,
a deferred tick also counts); the gate is gates.cli_gate("immich-recycle") itself (idle -> proceed, busy -> defer, any
gate error -> defer, busy for more than max_defer_hours = 12 -> proceed and restart the clock): the port calls the real
function, so the semantics cannot drift.
Intentional differences:
  1. A container that is not running is NOT started (docker restart would start it): reported as skipped.
  2. min_gap_min (30) floor, and `every_hours` self-cadence (set 0 when the scheduler owns the cadence).
  3. The restart is verified (container running afterwards); mode = "report" by default; `unprotect =
     ["^immich_server$"]` is needed because the global protected list contains "immich".
  4. Restarts are audited (ctx.act) and visible in the daily digest; no per-restart message (12 a day would be noise).
  5. Report mode only PROBES the gate (gates.busy): it never writes the deferral record in gates.json, which the legacy
     unit's ExecCondition shares until the old timer is retired. Apply mode calls gates.cli_gate (record and 12 h rule).
  6. Scheduling. `every_hours` (default 2) is the port's own cadence from the last APPLY attempt, and it also keeps two
     drivers honest: keep it at 2 whichever route starts the task, and set slack_s >= the scheduler's jitter_s for the task.
     Choose ONE route: (a) the check tier started WITH --apply (every 15 min; the cadence decides when it acts), or (b) a
     cron `schedule` in [tasks.immich_recycle] (the tick runs it with --apply) plus the cli glue that makes tier runs skip
     tasks that have their own schedule. `every_hours = 0` (the scheduler owns the cadence) is only safe with exactly one
     driver that carries --apply: with two, only min_gap_min (30) limits the rate, i.e. 4x the legacy rate.
  7. Two lanes of state: only a run that can act (ctx.apply) owns last_attempt; every other run (mode report, a tier run without
     --apply, PAUSE) keeps report_attempt, so the check tier's report-mode probe can never consume the apply run's 2 h cadence.

=====================================================================================================================
PARITY.md  openwebui_media_prune  (prune-openwebui-media.sh)
=====================================================================================================================
Checked identical (sandboxed script run on a tmp tree vs the port's selection, same surviving files): the two allow-listed
directories and their name patterns (owui_*.png, owui_vid_*.webm | *_owui_vid.webm, *_owui_vid.html,
*_generated-image.png, *_generated_image*), `find -maxdepth 1 -type f` (no recursion, regular files only, symlinks and
directories never), `-mtime +7` = age of at least 8 whole days (find ignores the fraction), `*` matches dots, files from
the future are kept, everything else (user uploads) is never touched.
Intentional differences: rules are validated (absolute real directory, no symlink, patterns need >= 4 literal chars so a
config typo cannot become "*"); per-run caps (max_items_per_run 500) and ctx.act protection (the comfyui output
directory needs `unprotect = ["^/volume1/docker/comfyui/output/owui_"]` because "comfyui" is a protected pattern);
removal re-checks the inode through a directory fd (no symlink races); mode = "report" by default; a rule whose files
the protected list forbids is reported as `info` ("N protected"), never silently skipped. Row labels are "<dir>/<file>".

=====================================================================================================================
PARITY.md  docker-prune.sh  (docker_prune_parity, docker_containers_prune)
=====================================================================================================================
docker_prune_parity re-derives the script's behaviour from the script itself (constants parsed, commands proven by the
sandboxed run in the tests) and compares it with the native tasks' configuration and with live data. Findings:
  * `docker container prune --filter until=168h` uses the container CREATION time, not the stop time: it removes any
    stopped container created more than a week ago, including one stopped an hour ago, and it ignores the protected
    list and the intentionally-stopped list (the comfyui container, created long ago and stopped on purpose, would be
    removed). Ported as docker_containers_prune with the clock the script's comment intended: time since the container
    FINISHED (created, for never-started ones), stopped_days = 7, protected names/images and expected_stopped
    containers kept, capped, no -f, no -v (volumes untouched, as in the script).
  * `docker image prune --all --filter until=168h` (image build date): covered by docker_images (ledger: unused for
    unused_days = 14). Intentionally later and safer.
  * `docker buildx prune --all --filter until=168h --max-used-space 20GB` per builder, builders discovered with
    DOCKER_CONFIG=/home/ohmz/.docker: docker_cache trims above high_gib to low_gib (stricter cap) but has NO age filter
    and, when run as root without DOCKER_CONFIG, cannot see ohmz's builder (immaculaterr-builder, where the cache grew
    to 46 GB): the parity task checks this live and reports it as a gap.
  * volumes are never pruned (both).
Verdicts: same | differs (documented, accepted) | gap (legacy does something the native tasks do not) | exposed (the legacy
timer is still on and WOULD delete something the native tasks keep).
  * EXPOSURE. While docker-prune.timer is enabled or active (or systemd cannot say: fail closed) the weekly script deletes the
    intentionally stopped container (comfyui: created long ago, stopped on purpose) and then, in step 2, the image only that
    container used (16 GB, assembled by hand, no Dockerfile). The computation (the containers the script removes that the
    native selection keeps, then the images `image prune --all --filter until=168h` frees once those containers are gone: no
    surviving container uses them, older than the cutoff, not the parent of an image that stays) is shared, but the PAGE is
    docker_prune_exposure's, a separate C0 task in the CHECK tier (15 min; read-only, ~0.2 s; one docker call less when the
    timer is off): it names both in the summary with the timer's next run and pages at FIRST SIGHT (see _announce), because a
    deliberate `docker stop` must be noticed within one tick, not at the next weekly run plus two confirming runs. crit when the
    timer fires within crit_within_h (72) or its next run is unknown, else warn; one more page when it gets imminent
    (imminent_h = 6). The exposure never touches anything: the way out is the owner's (start the container, stop the timer, or
    cut over: the page's body lists them).
  * STATUS (parity). ok = no gap, nothing exposed. info = no gap but the legacy timer is still on and would delete something the
    natives keep (alert=False; the cutover ends exactly that, so it must NOT block it). warn = a gap (alert=False: dashboard
    only). The retirement gate (`kind = "task"`, legacy.GREEN = ok/info) therefore refuses a docker-prune cutover with open
    gaps and only with open gaps. REGRESSION fixed: exposure used to be `warn` here, which made the gate unreachable while the
    exposure existed and the cutover is what ends it. metrics.cutover is "ready" | "blocked: gaps" | "blocked: natives not in
    apply mode".
  * AGE OPTION. `docker_cache.max_age_hours` counts as covering the script's age filter only when cleaners.docker_cache really
    reads it (a CAPABILITIES table in cleaners, else the function's code is inspected). An option the task ignores is a gap.
  * BUILDERS. docker_cache prunes RUNNING builders only, so builder discovery compares the owner's builders with those.

=====================================================================================================================
PARITY.md  smart_event  (smart-alert.sh)  -- lives in homelab_maint/smart_hook.py, NOT here
=====================================================================================================================
The hook is a stdlib-only module of its own so that a bad deploy of ANY other module (this one, cleaners, core, notify) can
never stop it from writing its local record and paging: see the docstring of smart_hook.py for the parity text, the exit
status contract (0 = told or held back by policy, 1 = not told, so the retirement stub can fall back to the legacy script)
and the glue. `smart_event` / `smart_event_main` are re-exported below only for old callers; the cli glue must import
homelab_maint.smart_hook directly (importing this module drags in cleaners, gates and core).

=====================================================================================================================
RETIRED  mem-guard  (mem-guard.py, mem-guard.timer every 3 h, dry-run only) -- documented, NOT ported
=====================================================================================================================
See RETIRED_MEM_GUARD. It restarted containers / SIGTERM+SIGKILLed host processes by SIZE (RSS over a threshold), which is
the failure mode SPEC3 section 0 rejects (a big process is not a problem, a stalled one is) and ran `docker stats` for
every container every 3 h. Superseded by pressure_state/pressure_response (saturation, ladder, retry budget), stuck_detector
(no-progress + growth) and orphan_report (idle daemons/orphans); its PROTECTED regex is superseded by protected.toml.

=====================================================================================================================
os_jobs  (no legacy script: OS timers are observed, never replaced)
=====================================================================================================================
Sources: `systemctl list-timers --all --output=json` (last/next as epoch microseconds; `systemctl show` prints
LastTriggerUSec as a locale/TZ dependent string), `systemctl show -p Id,LoadState,ActiveState,UnitFileState,Result,
ExecMainStatus,ActiveEnterTimestampMonotonic` for the timers and their services, snapd's /v2/system-info for the
built-in refresh timer, and the unattended-upgrades log. Verified on this host: `Result` (not ExecMainStatus) is the
verdict (fwupd-refresh exits 2 with Result=success because of SuccessExitStatus=2); unattended-upgrades.service is a
long-running shutdown helper (the upgrade itself runs inside apt-daily-upgrade.service); snapd has no systemd timer.
A job that never ran is "waiting" until its timer (or, for snapd, the host) has been up as long as its limit, then
"overdue"; absent (not installed) and waiting never alert.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import fnmatch
import hashlib
import inspect
import io
import json
import math
import os
import re
import socket
import stat
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import core
from ..core import GIB, Ctx, Result, audit, human, read_json, sh, task
# stdlib-only helpers shared with the smartd hook (which must stay importable when any sibling module is broken)
from ..smart_hook import ascii_line as _ascii, scrub as _scrub        # noqa: F401
from ..smart_hook import smart_event, smart_event_main                # noqa: F401  (re-exports for old callers)
from . import cleaners as _cleaners
from . import gates
from .cleaners import _Acts, _Changed

MIB = 1024 ** 2
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_mono_now = lambda: time.clock_gettime(time.CLOCK_MONOTONIC)   # noqa: E731  (patched in tests)

RETIRED_MEM_GUARD = {
    "name": "mem-guard", "script": "/usr/local/sbin/mem-guard.py", "units": ["mem-guard.service", "mem-guard.timer"],
    "replaced_by": ["pressure_state", "pressure_response", "stuck_detector", "orphan_report", "protected.toml"],
    "mode": "retire",
    "retire_actions": ["systemctl disable --now mem-guard.timer",
                       "mv /usr/local/sbin/mem-guard.py /usr/local/lib/homelab-maint/legacy/mem-guard/"],
    "rollback_actions": ["mv /usr/local/lib/homelab-maint/legacy/mem-guard/mem-guard.py /usr/local/sbin/",
                         "systemctl enable --now mem-guard.timer"],
    "why": "killed/restarted by size (RSS over a threshold) instead of by stall; dry-run only; superseded",
}

# The migration inventory for these ports (feeds legacy-retirement.toml / `homelab-maint migrate`): one row per legacy
# thing, what replaces it and which check proves the replacement. `check` is a task that must be green N cycles in
# status.json, or a pytest id that proves parity. Nothing here retires anything by itself.
PORTS: list[dict] = [
    {"legacy": "notebook-db-alert.timer", "location": "/usr/local/sbin/notebook-db-alert.sh", "replaced_by": "surrealdb_health",
     "mode": "port", "check": "task surrealdb_health green; tests/test_native.py -k surreal"},
    {"legacy": "comfyui-idle-vram.timer", "location": "/usr/local/bin/comfyui-idle-vram.sh", "replaced_by": "comfyui_idle_reclaim",
     "mode": "port", "check": "task comfyui_idle_reclaim green in report mode, then mode=apply + unprotect"},
    {"legacy": "immich-server-recycle.timer", "location": "/etc/systemd/system/immich-server-recycle.service",
     "replaced_by": "immich_recycle", "mode": "port", "check": "task immich_recycle green; drop-in 10-homelab-gate.conf goes with the timer"},
    {"legacy": "prune-openwebui-media.timer", "location": "/usr/local/bin/prune-openwebui-media.sh",
     "replaced_by": "openwebui_media_prune", "mode": "port", "check": "task openwebui_media_prune report == script for a week"},
    {"legacy": "docker-prune.timer", "location": "/usr/local/sbin/docker-prune.sh",
     "replaced_by": "docker_containers_prune+docker_cache+docker_images", "mode": "port",
     "check": "task docker_prune_parity ok|info (gaps == 0; exposure never blocks it); status_json tasks.docker_prune_parity.metrics.cutover == ready; "
              "task docker_prune_exposure pages while the timer would still delete a kept container"},
    {"legacy": "smart-alert.sh (smartd -M exec hook)", "location": "/usr/local/sbin/smart-alert.sh", "replaced_by": "smart-event",
     "mode": "port", "check": "tests/test_native.py -k smart_hook; a TEST alert through the hook (smartd -M test)"},
    {"legacy": "mem-guard.timer", "location": "/usr/local/sbin/mem-guard.py", "replaced_by": "pressure_state+stuck_detector",
     "mode": "retire", "check": "none (dry-run only); see RETIRED_MEM_GUARD"},
    {"legacy": "OS timers (apt-daily, logrotate, fstrim, ...)", "location": "systemd", "replaced_by": "os_jobs",
     "mode": "observe", "check": "task os_jobs green"},
]


# =========================================================================== shared helpers
def _num(v: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    """A finite real number within [lo, hi]; bools, strings, NaN and out-of-range are rejected."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def _skipped(why: str, **metrics: Any) -> Result:
    return Result("skipped", _ascii(why), {"mode": "skipped", **metrics})


class _Opts:
    """Validated task options. A bad value falls back to the default and is remembered in .bad (actions then refuse)."""

    def __init__(self, ctx: Ctx):
        self.ctx, self.bad = ctx, []

    def num(self, key: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
        n = _num(self.ctx.opt(key, default), lo, hi)
        if n is None:
            self.bad.append(key)
            return default
        return n

    def name(self, key: str, default: str) -> str:
        v = self.ctx.opt(key, default)
        if isinstance(v, str) and _NAME.fullmatch(v):
            return v
        self.bad.append(key)
        return default

    def path(self, key: str, default: str) -> str:
        v = self.ctx.opt(key, default)
        if isinstance(v, str) and os.path.isabs(v) and "\0" not in v:
            return os.path.normpath(v)
        self.bad.append(key)
        return default


def _run_ok(cmd: list[str], timeout: int) -> str:
    """Run a mutating command; raise with a short reason unless it exits 0 (ctx.act audits the failure)."""
    r = sh(cmd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} rc={r.returncode}: "
                           f"{_scrub((r.stderr or r.stdout).strip()[-100:], 100)}")
    return r.stdout


def _do(ctx: Ctx, what: str, target: str, fn: Callable[[], Any]) -> tuple[str, str]:
    """One mutation through ctx.act with an honest verdict: done | would | protected | paused | failed.
    ctx.act returns False for "dry-run", "protected" and "paused" alike, so the last two are decided first."""
    if ctx.is_protected(target):
        audit(ctx.name, what, target, 0, "refused-protected")
        return "protected", ""
    if core.paused(ctx.name):
        audit(ctx.name, what, target, 0, "refused-paused")
        return "paused", ""
    try:
        return ("done" if ctx.act(what, target, 0, fn) else "would"), ""
    except Exception as exc:  # noqa: BLE001  (core._Timeout is a BaseException and still propagates)
        return "failed", _scrub(f"{type(exc).__name__}: {exc}", 120)


def _container_state(name: str) -> str | None:
    """'running' | 'exited' | ... | 'absent' (no such container) | None (docker failed)."""
    r = sh(["docker", "container", "inspect", "--format", "{{.State.Status}}", name], timeout=20)
    if r.returncode == 0:
        return r.stdout.strip().lower() or None
    return "absent" if "No such" in (r.stderr or "") else None


def _notify(event: dict) -> dict:
    """Thin bridge to notify.send, the ONE notification path. It never sends by itself: when the notify module is
    missing or broken the event is audited as a failed send (alert_path_health reads that), so a dropped message is
    never silent and nothing bypasses the unified path.
    Returns {"ok": bool, "handled": bool, "rc": int, "note": str}: ok = delivered; handled = delivered OR intentionally
    not sent by policy (dedupe window, quiet hours, muted: notify's own `handled`), which is not a failure."""
    try:
        from .. import notify
    except Exception as exc:  # noqa: BLE001  (ImportError, or a syntax error in the sibling module)
        audit("notify", "send", str(event.get("title", ""))[:80], 0, f"failed rc=127 notify module unavailable ({type(exc).__name__})")
        return {"ok": False, "handled": False, "rc": 127, "note": "notify module unavailable"}
    try:
        cls = notify.Event
        names = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set(event)
        d = notify.send(cls(**{k: v for k, v in event.items() if k in names}))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "handled": False, "rc": 1, "note": _scrub(f"{type(exc).__name__}: {exc}")}

    def pick(key: str, default: Any = None) -> Any:
        return d.get(key, default) if isinstance(d, dict) else getattr(d, key, default)

    ok = bool(pick("ok", d if isinstance(d, bool) else False))
    note = pick("note", "") or pick("skipped", "") or ""
    return {"ok": ok, "handled": ok or bool(pick("handled", False)), "rc": 0 if ok else 1, "note": _scrub(note, 400, tail=True)}


def _ceil_div(n: int, d: int) -> int:
    return -(-n // d)


# ---- announcing a NEW or CHANGED problem ahead of core.Notifier's debounce (surrealdb_health, docker_prune_exposure)
LEAD_RETRY_S = 12 * 3600            # how long a recovery the task itself owes the owner is retried (like notify.RECOVERY_RETRY_S)


def _notifier_alerted(name: str) -> int:
    """The alert level the runner's Notifier has ALREADY paged for this task (core.Notifier state in alerts.json; 0 = nothing
    paged yet). Read at the start of a run, i.e. before the Notifier has seen this run's result."""
    st = read_json(core.STATE_DIR / "alerts.json", {}) or {}
    t = (st.get("tasks") or {}).get(name) if isinstance(st, dict) else None
    v = t.get("alerted") if isinstance(t, dict) else 0
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def _remember(st: dict, sig: str | None) -> None:
    """Store the signature the owner was last told (None = forget it)."""
    if sig is None:
        st.pop("signature", None)
    else:
        st["signature"] = sig


def _sig_transition(prev: str | None, sig: str) -> str:
    """none | alert | suppressed | recovery for a failing-set signature ("OK" = nothing failing). The legacy script's recovery
    test could never be true; here a previous signature that is neither empty nor "OK" followed by a clean run is a recovery."""
    if sig == "OK":
        return "recovery" if prev not in (None, "", "OK") else "none"
    return "suppressed" if sig == prev else "alert"


def _announce(ctx: Ctx, sig: str, trans: str, page: Callable[[int], dict],
              recovery: Callable[[], dict] | None = None) -> tuple[str | None, str]:
    """Tell the owner about a NEW or CHANGED problem now, ahead of core.Notifier's confirm-N-runs debounce, through `_notify`.
    Returns (signature to remember: None = forget it, sig_page: "" | "sent" | "retry" | "closed").

    Why the task pages at all: the Notifier needs alert_confirm_runs (2) consecutive runs, shares a daily budget, and sends only
    "<title>: <summary>". A leading indicator (SurrealDB's WAL, a prune that is about to delete a hand-built image) must page at
    the first run that sees it, with the full body, and a one-run blip must not be invisible.
    How it avoids a second page when the Notifier confirms the same problem: page(0) is the FIRST page of an incident and its
    dedupe key is the bare task name, with the severity the Notifier will use, i.e. exactly the key of the Notifier's own alert
    (notify.alert_event dedupe_key=task), so notify's dedupe window (6 h) swallows the Notifier's page (handled = true, the
    Notifier records it as sent). page(n >= 1) announces a later CHANGE of the failing set (the Notifier cannot see those: same
    level); each has its own key. State: `signature` (what the owner was last told), `announced` (change counter), `lead` (the
    task paged this incident itself).
    A page notify did not accept (handled = false) is not recorded: the next run retries it. first_sight_page = false turns the
    first page off (the Notifier pages when it confirms; change pages then wait until it has: no double page). Recovery: when the
    task paged an incident the Notifier never did (a blip), the task closes it; otherwise the Notifier's recovery speaks."""
    st, prev = ctx.state, ctx.state.get("signature")
    first_ok = ctx.opt("first_sight_page", True) is not False
    alerted = _notifier_alerted(ctx.name)
    if trans == "alert":
        if prev in (None, "", "OK") and not alerted:                  # a NEW incident: the owner has not been told about this task
            if not first_ok:
                return sig, ""
            if _notify(page(0))["handled"]:
                st["lead"] = {"t": ctx.now}
                return sig, "sent"
            return prev, "retry"
        # a later CHANGE of the failing set; also a failure that returns after a one-run flap through OK while the Notifier still
        # holds its page (it needs two clean runs to confirm a recovery): the owner has not been told THIS set yet
        if ctx.opt("signature_alerts", True) is not False and (first_ok or alerted > 0):
            seq = int(_num(st.get("announced"), 0) or 0) + 1
            if _notify(page(seq))["handled"]:
                st["announced"] = seq
                return sig, "sent"
            return prev, "retry"
        return sig, ""
    if trans == "recovery" and isinstance(st.get("lead"), dict):
        if alerted > 0 or recovery is None:                           # the Notifier paged it: its recovery message speaks
            st.pop("lead", None)
        else:
            t0 = _num(st["lead"].get("t"), 0) or 0
            if _notify(recovery())["handled"] or ctx.now - t0 > LEAD_RETRY_S:
                st.pop("lead", None)
                return sig, "closed"
            return prev, "retry"
    return sig, ""


# =========================================================================== surrealdb_health
SURREAL_DEFAULTS = {"container": "open-notebook-surrealdb-1",
                    "data_dir": "/home/ohmz/docker-container-data/open-notebook/surreal_data"}
SURREAL_MOUNT_DEST = "/mydata"                 # where the container mounts its store on the host


def surreal_mount_source(container: str) -> str | None:
    """Host path the container bind-mounts at /mydata, or None when docker cannot answer or has no such mount.
    A single `-f` inspect: NEVER read Config.Cmd/Env of this container, its command line carries the DB password."""
    r = sh(["docker", "container", "inspect", "--format",
            '{{range .Mounts}}{{if eq .Destination "%s"}}{{.Source}}{{end}}{{end}}' % SURREAL_MOUNT_DEST, container],
           timeout=20)
    src = r.stdout.strip() if r.returncode == 0 else ""
    return src or None


def surreal_data_dir(o: "_Opts", container: str) -> str:
    """Where the store lives. An explicit `data_dir` option wins; otherwise the container's actual /mydata bind mount
    when docker answers and that path exists; otherwise SURREAL_DEFAULTS["data_dir"]. Resolving it at run time is the
    fix for a stale hardcoded path that measured a directory which had moved and read as 0 GB free (a false crit)."""
    if "data_dir" in o.ctx.tcfg:
        return o.path("data_dir", SURREAL_DEFAULTS["data_dir"])
    src = surreal_mount_source(container)
    return src if src and os.path.isdir(src) else SURREAL_DEFAULTS["data_dir"]


@dataclass(frozen=True)
class Reading:
    wal_mb: int
    db_gb: int | None          # None = store size unreadable (legacy: not a problem)
    disk_free_gb: int
    oom_kill: int
    restarts: int
    state: str = "unknown"


@dataclass(frozen=True)
class Limits:
    wal_max_mb: float = 1024
    db_max_gb: float = 25
    disk_min_gb: float = 40
    restart_gt: int = 3


def _wal_mb(rocks: str) -> int:
    """find ROCKS -maxdepth 1 -name '*.log' -printf '%s\\n' | sum, integer MiB (bash `/ 1048576`)."""
    total = 0
    try:
        with os.scandir(rocks) as it:
            for e in it:
                if fnmatch.fnmatchcase(e.name, "*.log"):          # find -name: '*' also matches a leading dot
                    try:
                        total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    except OSError:
        return 0                                                  # legacy: not a directory => 0
    return total // MIB


def _du_bytes(path: str, budget_s: float = 60.0) -> int | None:
    """`du -s` equivalent: allocated bytes (st_blocks * 512) of the tree including directories, hard links once,
    symlinks counted as links and never followed. None when the walk is cut off or the root is unreadable."""
    t0, total, seen, stack = time.monotonic(), 0, set(), [path]
    try:
        st = os.lstat(path)
    except OSError:
        return None
    seen.add((st.st_dev, st.st_ino))
    total += st.st_blocks * 512
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if time.monotonic() - t0 > budget_s:
                        return None
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    key = (s.st_dev, s.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    total += s.st_blocks * 512
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(os.path.join(d, e.name))
        except OSError:
            continue
    return total


def _db_gb(rocks: str) -> int | None:
    """du -s --block-size=1G prints the size rounded UP (verified: 1.8 GiB -> 2)."""
    b = _du_bytes(rocks)
    return None if b is None else _ceil_div(b, GIB)


def _disk_free_gb(path: str) -> int:
    """df --block-size=1G --output=avail: f_bavail * f_frsize rounded UP (verified: 928.7 GiB -> 929); 0 on failure."""
    try:
        s = os.statvfs(path)
    except OSError:
        return 0
    return _ceil_div(s.f_bavail * s.f_frsize, GIB)


def _oom_kill(cid: str) -> int:
    d = gates.cg_dir(cid)
    for ln in (gates.read_text(d / "memory.events") or "").splitlines() if d else []:
        if ln.startswith("oom_kill "):
            try:
                return int(ln.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def surreal_read(rocks: str, data_dir: str, container: str) -> tuple[Reading, bool]:
    """Measure everything the script measured. Returns (reading, docker_answered)."""
    wal = _wal_mb(rocks)
    db = _db_gb(rocks) if os.path.isdir(rocks) else 0
    disk = _disk_free_gb(data_dir)
    cid, state, restarts, answered = "", "unknown", 0, False
    # NEVER add Config.Cmd or Config.Env here: this container's command line carries the database password.
    r = sh(["docker", "container", "inspect", "--format", "{{.Id}}|{{.State.Status}}|{{.RestartCount}}", container], timeout=20)
    f = r.stdout.strip().split("|") if r.returncode == 0 else []
    if len(f) == 3:
        cid, state, answered = f[0], (f[1] or "unknown"), True
        restarts = int(f[2]) if f[2].isdigit() else 0
    return Reading(wal, db, disk, _oom_kill(cid) if cid else 0, restarts, state), answered


def _lim(x: float) -> str:
    return f"{x:g}"


def surreal_problems(r: Reading, lim: Limits) -> list[tuple[str, str]]:
    """[(check key, text)] in the script's order and wording."""
    out = []
    if r.wal_mb > lim.wal_max_mb:
        out.append(("wal", f"WAL is {r.wal_mb} MB (limit {_lim(lim.wal_max_mb)} MB)"))
    if r.db_gb is not None and r.db_gb > lim.db_max_gb:
        out.append(("db", f"store is {r.db_gb} GB (limit {_lim(lim.db_max_gb)} GB)"))
    if r.disk_free_gb < lim.disk_min_gb:
        out.append(("disk", f"only {r.disk_free_gb} GB free on the store's filesystem (want >= {_lim(lim.disk_min_gb)} GB)"))
    if r.oom_kill > 0:
        out.append(("oom", f"container hit its memory cap {r.oom_kill}x (cgroup oom_kill)"))
    if r.restarts > lim.restart_gt:
        out.append(("restart", f"container has restarted {r.restarts}x"))
    return out


def surreal_signature(problems: list[tuple[str, str]]) -> str:
    """Which checks fail, never their values (a creeping WAL must not re-alert); "OK" when none."""
    return "".join(k + "," for k, _ in problems) or "OK"


def surreal_transition(prev: str | None, problems: list[tuple[str, str]]) -> str:
    """none | alert | suppressed | recovery (see _sig_transition)."""
    return _sig_transition(prev, surreal_signature(problems))


def _surreal_event(rd: Reading, lim: Limits, problems: list[tuple[str, str]], sig: str, rocks: str, cont: str,
                   prev: str | None = None, seq: int = 0) -> dict:
    lines = "\n".join(f"  - {t}" for _, t in problems)
    body = (f"SurrealDB is showing the failure mode that hard-reset this host on 2026-09-24.\n\n{lines}\n\n"
            f"Current values:\n  WAL        {rd.wal_mb} MB  (limit {_lim(lim.wal_max_mb)} MB)\n"
            f"  store      {rd.db_gb} GB  (limit {_lim(lim.db_max_gb)} GB)\n"
            f"  disk free  {rd.disk_free_gb} GB  (want >= {_lim(lim.disk_min_gb)} GB)\n"
            f"  oom_kill   {rd.oom_kill}\n  restarts   {rd.restarts}\n  state      {rd.state}\n\n"
            f"What to do: the store lives at {rocks}. If oom_kill or restarts are climbing, the container is in a "
            "restart loop and the WAL will grow until the disk fills -- the fix is reducing the dataset, not raising "
            f"the cap. Check `docker logs {cont}` and `python -m app.cli report` in the goodreads container.\n").rstrip("\n")
    short = _ascii("SurrealDB: " + "; ".join(t.split(" (")[0] for _, t in problems), 130)
    return {"kind": "alert", "severity": "crit", "title": f"Open Notebook DB pressure on {socket.gethostname()}",
            "summary": short, "details": body, "status": "crit", "task": "surrealdb_health",
            # seq 0 = the FIRST page of an incident: its key is the bare task name, the key of the Notifier's own alert, so notify's
            # dedupe window swallows the page the Notifier sends when it confirms (see _announce). A later CHANGE (seq >= 1) is its
            # own event: the window must swallow a retry of the same page, never a later flap back to an earlier signature
            # (wal -> wal,db -> wal within the window)
            "dedupe_key": "surrealdb_health" if seq == 0 else f"surrealdb_health:{sig}:{seq}",
            "facts": {"wal_mb": rd.wal_mb, "db_gb": rd.db_gb, "disk_free_gb": rd.disk_free_gb, "oom_kill": rd.oom_kill,
                      "restarts": rd.restarts, "state": rd.state, "failing": sig, "previous": prev or "OK"}}


def _surreal_recovery(rd: Reading) -> dict:
    """The recovery of an incident the task paged and the Notifier never did (a blip): same key as the Notifier's recovery."""
    base = f"WAL {rd.wal_mb} MB, store {'?' if rd.db_gb is None else rd.db_gb} GB, {rd.disk_free_gb} GB free"
    return {"kind": "recovery", "severity": "ok", "title": "SurrealDB (Open Notebook)", "status": "ok", "task": "surrealdb_health",
            "summary": _ascii(f"SurrealDB is back to normal: {base}", 130), "dedupe_key": "surrealdb_health",
            "facts": {"was": "crit", "wal_mb": rd.wal_mb, "disk_free_gb": rd.disk_free_gb, "state": rd.state}}


@task("surrealdb_health", klass="C0", tier="check", title="SurrealDB (Open Notebook)", timeout=120)
def surrealdb_health(ctx: Ctx) -> Result:
    """Leading indicators of the 2026-09-24 SurrealDB failure (see PARITY: surrealdb_health in the module docstring)."""
    o = _Opts(ctx)
    cont = o.name("container", SURREAL_DEFAULTS["container"])
    data = surreal_data_dir(o, cont)
    rocks = o.path("rocks", os.path.join(data, "mydatabase.db"))
    lim = Limits(o.num("wal_max_mb", 1024, 0), o.num("db_max_gb", 25, 0), o.num("disk_min_gb", 40, 0),
                 int(o.num("restart_gt", 3, 0)))
    rd, answered = surreal_read(rocks, data, cont)
    problems = surreal_problems(rd, lim)
    sig = surreal_signature(problems)
    st = ctx.state
    prev = st.get("signature")                             # what the owner was last told (or silently accepted)
    trans = surreal_transition(prev, problems)
    if sig != st.get("observed"):                          # when the failing set last changed, announced or not
        st["since"], st["observed"] = ctx.now, sig
    # A NEW problem is paged by this task at the first run that sees it (the script's behaviour; the Notifier would wait for
    # alert_confirm_runs), with the full body. The Notifier still owns reminders and the recovery of anything it paged; what it
    # cannot see is the SET of failing checks changing at the same (crit) level, which is also paged here. See _announce for how
    # the Notifier's own page for the same incident is swallowed by notify's dedupe window instead of repeating it.
    keep, sig_page = _announce(ctx, sig, trans, lambda seq: _surreal_event(rd, lim, problems, sig, rocks, cont, prev, seq),
                               lambda: _surreal_recovery(rd))
    _remember(st, keep)
    rows = [("wal", f"{rd.wal_mb} MB", f"{_lim(lim.wal_max_mb)} MB"),
            ("db", "?" if rd.db_gb is None else f"{rd.db_gb} GB", f"{_lim(lim.db_max_gb)} GB"),
            ("disk", f"{rd.disk_free_gb} GB free", f">= {_lim(lim.disk_min_gb)} GB"),
            ("oom", str(rd.oom_kill), "0"), ("restart", str(rd.restarts), f"<= {lim.restart_gt}")]
    bad = {k for k, _ in problems}
    items = [{"check": k, "value": v, "limit": lim_, "state": "BAD" if k in bad else "ok"} for k, v, lim_ in rows]
    metrics = {"wal_mb": rd.wal_mb, "db_gb": -1 if rd.db_gb is None else rd.db_gb, "disk_free_gb": rd.disk_free_gb,
               "oom_kill": rd.oom_kill, "restarts": rd.restarts, "container_state": rd.state, "signature": sig,
               "transition": trans, "sig_page": sig_page, "problems": len(problems), "wal_max_mb": lim.wal_max_mb,
               "db_max_gb": lim.db_max_gb, "disk_min_gb": lim.disk_min_gb, "docker_ok": answered}
    if o.bad:
        metrics["bad_config"] = ",".join(o.bad)[:60]
    if problems:
        return Result("crit", _ascii("SurrealDB: " + "; ".join(t.split(" (")[0] for _, t in problems)), metrics, items)
    base = f"WAL {rd.wal_mb} MB, store {'?' if rd.db_gb is None else rd.db_gb} GB, {rd.disk_free_gb} GB free"
    if rd.state != "running":
        return Result("info", _ascii(f"SurrealDB container {rd.state}; no growth problems ({base})"), metrics, items)
    note = f" (bad config {','.join(o.bad)}: defaults used)" if o.bad else ""
    return Result("ok", _ascii(f"SurrealDB ok: {base}, oom {rd.oom_kill}, restarts {rd.restarts}{note}"), metrics, items)


# =========================================================================== comfyui_idle_reclaim
def queue_jobs(url: str, timeout: float = 5.0) -> int | None:
    """running + pending jobs; None (= busy) on ANY error, non-JSON, or an answer without both lists."""
    try:
        q = gates.http_json(url, timeout)
    except Exception:  # noqa: BLE001  (connection refused, timeout, HTTP error, bad JSON: all busy)
        return None
    if not isinstance(q, dict):
        return None
    run, pend = q.get("queue_running"), q.get("queue_pending")
    if not isinstance(run, list) or not isinstance(pend, list):
        return None
    return len(run) + len(pend)


def _container_pids(name: str, use_cgroup: bool) -> tuple[str, set[int]] | None:
    """(state, host pids of the container) | None when docker cannot answer. A missing container is ('absent', {})."""
    r = sh(["docker", "container", "inspect", "--format", "{{.Id}}|{{.State.Status}}|{{.State.Pid}}", name], timeout=20)
    if r.returncode != 0:
        return ("absent", set()) if "No such" in (r.stderr or "") else None
    f = r.stdout.strip().split("|")
    if len(f) != 3:
        return None
    cid, state, pid = f
    pids = {int(pid)} if pid.isdigit() and int(pid) > 0 else set()
    if use_cgroup and state == "running":
        d = gates.cg_dir(cid)
        if d is not None:
            pids |= {int(x) for x in (gates.read_text(d / "cgroup.procs") or "").split() if x.isdigit()}
    return state, pids


def gpu_mem_mb(pids: set[int]) -> int | None:
    """Total VRAM (MiB) held by `pids` per nvidia-smi; None when nvidia-smi fails. Rows are "pid, used_memory"."""
    r = sh(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], timeout=20)
    if r.returncode != 0:
        return None
    total = 0
    for ln in r.stdout.splitlines():
        f = [p.strip() for p in ln.split(",")]
        if len(f) >= 2 and f[0].isdigit() and int(f[0]) in pids and f[1].isdigit():
            total += int(f[1])
    return total


def proc_mem_mb(pids: set[int]) -> int | None:
    """Total resident RAM (MiB) of `pids` from /proc/<pid>/statm; None when not one of them could be read.

    Deliberately optional: it is only called when a task configures a RAM threshold, so every parity run against the
    legacy scripts — which never looked at system RAM — reads nothing here."""
    total = 0
    seen = False
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/statm") as fh:
                resident = int(fh.read().split()[1])
        except (OSError, IndexError, ValueError):
            continue
        seen = True
        total += resident * page
    return total // (1024 * 1024) if seen else None


def comfy_step(st: dict, busy: int | None, vram_mb: int, now: float, thresh_mb: float = 3000, strikes: int = 2,
               ttl_s: float = 2700.0, min_gap_s: float = 0.0, ram_mb: int | None = None,
               ram_thresh_mb: float = 0.0) -> tuple[str, dict]:
    """The legacy decision, plus an optional system-RAM arm (PARITY: comfyui_idle_reclaim, difference 1).
    st = {"n": strikes so far, "t": time of the last one}. Returns (action, new state) with
    action reset | strike | wait | restart. Idle means busy == 0 exactly; None (probe error) is busy.
    `wait`: an idle observation less than min_gap_s after the last strike is NOT a new strike (two runners, a manual run or a
    scheduler catch-up landing seconds apart would otherwise turn one moment of idleness into "sustained"); the state is
    untouched. A busy/light observation always resets, whatever the gap (the safe direction).

    Held is the legacy test, VRAM > thresh_mb, OR — only when ram_thresh_mb > 0 — RAM > ram_thresh_mb: ComfyUI can release a
    resident model from VRAM while the process still holds it in system RAM, and the restart frees both. ram_mb None
    (unreadable) is never heavy, and ram_thresh_mb 0 (the default) reproduces the legacy script exactly."""
    heavy = vram_mb > thresh_mb or (ram_thresh_mb > 0 and ram_mb is not None and ram_mb > ram_thresh_mb)
    if busy == 0 and heavy:
        fresh = st.get("n", 0) > 0 and now - st.get("t", 0) <= ttl_s
        if fresh and now - st.get("t", 0) < min_gap_s:
            return "wait", {"n": st["n"], "t": st["t"]}
        n = st["n"] + 1 if fresh else 1
        if n >= strikes:
            return "restart", {"n": 0}
        return "strike", {"n": n, "t": now}
    return "reset", {"n": 0}


def comfy_stop_step(st: dict, busy: int | None, now: float, stop_s: float, key: str = "is") -> tuple[str, float | None]:
    """The simple idle-killer (Ohmz choice, 2026-10-04, when stop_idle_min > 0): forget VRAM and RAM entirely -- a container that
    has been idle for `stop_s` in a row is STOPPED, whatever it holds, so a rogue ComfyUI cannot sit on the GPU when nobody uses
    it. Idle is busy == 0 exactly; None (a queue probe error) and any pending job both reset the clock (fail closed: a busy or an
    unreadable queue never stops anything). Returns (action | "reset" | "start" | "wait" | "stop", idle_since)."""
    if busy != 0:
        return "reset", None
    since = _num(st.get(key))
    if since is None or since > now:
        return "start", now
    return ("wait", since) if now - since < stop_s else ("stop", since)


@task("comfyui_idle_reclaim", klass="C1", tier="check", title="ComfyUI idle VRAM", timeout=120)
def comfyui_idle_reclaim(ctx: Ctx) -> Result:
    """Reclaim the GPU from an idle ComfyUI: with stop_idle_min > 0 (the shipped default) stop it after that many idle
    minutes; with stop_idle_min = 0, restart it when it is idle yet holds VRAM/RAM across two checks (see PARITY: comfyui_idle_reclaim).

    Two LANES of state: a run that can really act (ctx.apply) keeps its strikes in n/t, every other run (report mode, a tier
    run without --apply, PAUSE) in rn/rt, so a report-mode run that happens to share the state file can never add the strike
    that makes an apply run restart (the same task may be started by the check tier AND by its own cron schedule)."""
    o = _Opts(ctx)
    name = o.name("container", "comfyui")
    thresh = o.num("threshold_mb", 3000, 0, 10 ** 6)
    ram_thresh = o.num("ram_threshold_mb", 0, 0, 10 ** 6)      # 0 = the legacy signal only (VRAM); see comfy_step
    strikes = int(o.num("strikes", 2, 1, 10))
    ttl = o.num("strike_ttl_min", 45, 1, 1440) * 60
    strike_gap = o.num("strike_min_gap_min", 4, 0, 1440) * 60
    gap = o.num("min_gap_min", 30, 1, 1440) * 60
    qto = o.num("queue_timeout_s", 5, 1, 60)
    url = ctx.opt("queue_url") or (ctx.protected.get("busy") or {}).get("comfyui_queue_url", "http://127.0.0.1:8188/queue")
    if o.bad or not isinstance(url, str):
        return _skipped("bad config " + ",".join(o.bad or ["queue_url"]) + ": nothing done")
    st = ctx.state
    kn, kt = ("n", "t") if ctx.apply else ("rn", "rt")      # this run's lane

    def reset() -> None:
        st[kn] = 0
        st.pop(kt, None)
        st.pop("is", None)                                   # the idle-stop clock (see comfy_stop_step)
        st.pop("ris", None)

    insp = _container_pids(name, bool(ctx.opt("match_cgroup", True)))
    if insp is None:
        reset()
        return _skipped("docker unavailable: nothing done")
    cstate, pids = insp
    if cstate != "running":
        reset()
        return Result("ok", _ascii(f"ComfyUI container {cstate}: no VRAM to reclaim"), {"mode": "report", "container_state": cstate})
    busy = queue_jobs(url, qto)
    stop_s = o.num("stop_idle_min", 0, 0, 1440) * 60          # Ohmz choice: 0 = off (the VRAM/RAM restart path below); >0 = a plain idle-killer
    if stop_s > 0:
        ki = "is" if ctx.apply else "ris"
        action, since = comfy_stop_step(st, busy, ctx.now, stop_s, ki)
        st[kn] = 0                                           # the idle-killer and the VRAM restart are mutually exclusive
        st.pop(kt, None)
        if since is None:
            st.pop(ki, None)
        else:
            st[ki] = since
        idle_min = 0.0 if since is None else (ctx.now - since) / 60
        mode = "apply" if ctx.apply else "report"
        m = {"mode": mode, "action": action, "busy": -1 if busy is None else busy, "container_state": cstate,
             "idle_min": round(idle_min, 1), "stop_idle_min": stop_s / 60}
        if action == "reset":
            return Result("ok", _ascii(f"ComfyUI busy ({m['busy']} jobs): idle timer reset"), m)
        if action == "start":
            return Result("ok", _ascii(f"ComfyUI idle: will stop it if it stays idle {stop_s / 60:g} min"), m)
        if action == "wait":
            return Result("ok", _ascii(f"ComfyUI idle {idle_min:.0f} min of {stop_s / 60:g}: not stopping yet"), m)
        if ctx.apply and queue_jobs(url, qto) != 0:           # a job may have been queued since the first probe: look again
            st.pop(ki, None)
            m["action"] = "cancelled"
            return Result("ok", _ascii("ComfyUI got a job just before the stop: cancelled"), m)
        verdict, err = _do(ctx, "docker-stop", name, lambda: _run_ok(["docker", "stop", name], 120))
        m["action"] = verdict
        if verdict == "done":
            st.pop(ki, None)
            st["last_stop"] = ctx.now
            return Result("ok", _ascii(f"stopped {name}: idle {idle_min:.0f} min (>= {stop_s / 60:g})"), m)
        if verdict == "failed":
            return Result("warn", _ascii(f"stop of {name} failed: {err}"), m)
        why = {"would": "report", "protected": "protected: set unprotect for this task", "paused": "paused"}[verdict]
        return Result("info", _ascii(f"{why}: would stop {name} (idle {idle_min:.0f} min)"), m)
    vram = gpu_mem_mb(pids)
    if vram is None:
        reset()
        return _skipped("nvidia-smi unavailable: nothing done")
    ram = proc_mem_mb(pids) if ram_thresh > 0 else None
    action, new = comfy_step({"n": st.get(kn, 0), "t": st.get(kt, 0)}, busy, vram, ctx.now, thresh, strikes, ttl, strike_gap,
                             ram, ram_thresh)
    st[kn] = new["n"]
    if "t" in new:
        st[kt] = new["t"]
    else:
        st.pop(kt, None)
    mode = "apply" if ctx.apply else "report"
    held = f"{vram} MB VRAM" if ram is None else f"{vram} MB VRAM / {ram} MB RAM"      # byte-identical when RAM is off
    ram_note = "" if ram is None else f" / {ram} MB RAM"
    m = {"mode": mode, "action": action, "vram_mb": vram, "ram_mb": -1 if ram is None else ram,
         "ram_threshold_mb": ram_thresh, "busy": -1 if busy is None else busy, "strikes": st[kn],
         "threshold_mb": thresh, "container_state": cstate}
    if action == "reset":
        why = "busy or queue unreadable" if busy != 0 else "VRAM light"
        return Result("ok", _ascii(f"ComfyUI {why} ({vram} MB held{ram_note}, {m['busy']} jobs): strikes reset"), m)
    if action == "strike":
        return Result("ok", _ascii(f"ComfyUI idle and holding {held}: strike {st[kn]}/{strikes}"), m)
    if action == "wait":
        return Result("ok", _ascii(f"ComfyUI idle and holding {held}: strike {st[kn]}/{strikes} is only "
                                   f"{(ctx.now - st[kt]) / 60:.0f} min old, waiting (min {strike_gap / 60:g})"), m)
    last = _num(st.get("last_restart"))
    if last is not None and ctx.now - last < gap:
        m["action"] = "suppressed"
        return Result("info", _ascii(f"restart suppressed: last one {(ctx.now - last) / 60:.0f} min ago (min gap {gap / 60:.0f})"), m)
    if ctx.apply and queue_jobs(url, qto) != 0:               # a job may have been queued since the first probe: look again
        reset()
        m["action"] = "cancelled"
        return Result("ok", _ascii(f"ComfyUI got a job just before the restart: cancelled, strikes reset "
                                   f"({vram} MB held{ram_note})"), m)
    verdict, err = _do(ctx, "docker-restart", name, lambda: _run_ok(["docker", "restart", name], 120))
    m["action"] = verdict
    if verdict == "done":
        st["last_restart"] = ctx.now
        return Result("ok", _ascii(f"restarted {name}: idle for {strikes} checks, held {held}"), m)
    if verdict == "failed":
        return Result("warn", _ascii(f"restart of {name} failed: {err}"), m)
    why = {"would": "report", "protected": "protected: set unprotect for this task", "paused": "paused"}[verdict]
    return Result("info", _ascii(f"{why}: would restart {name} (idle {strikes} checks, {held})"), m)


# =========================================================================== immich_recycle
def _gate(name: str, record: bool = True) -> tuple[int, str]:
    """gates.cli_gate with its output captured. rc 0 = proceed, anything else = defer (a broken gate defers).
    record=False only PROBES (gates.busy): report mode must not touch the deferral record in STATE_DIR/gates.json, which
    the legacy unit's ExecCondition (`homelab-maint gate immich-recycle`) shares until that timer is retired."""
    if not record:
        try:
            is_busy, why = gates.busy(name)
        except Exception as exc:  # noqa: BLE001
            return 1, f"gate error: {type(exc).__name__}"
        return (1 if is_busy else 0), _ascii(why, 100)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = int(gates.cli_gate(name))
    except Exception as exc:  # noqa: BLE001
        return 1, f"gate error: {type(exc).__name__}"
    return rc, " ".join(buf.getvalue().split())


@task("immich_recycle", klass="C1", tier="check", title="Immich server recycle", timeout=180)
def immich_recycle(ctx: Ctx) -> Result:
    """`docker restart immich_server` every 2 h behind the Immich busy gate (see PARITY: immich_recycle)."""
    o = _Opts(ctx)
    name = o.name("container", "immich_server")
    every = o.num("every_hours", 2, 0, 168) * 3600
    slack = o.num("slack_s", 600, 0, 3600)
    gap = o.num("min_gap_min", 30, 1, 1440) * 60
    gate = ctx.opt("gate", "immich-recycle")
    if o.bad or not isinstance(gate, str):
        return _skipped("bad config " + ",".join(o.bad or ["gate"]) + ": nothing done")
    st, mode = ctx.state, ("apply" if ctx.apply else "report")
    # Two LANES of state (see comfyui_idle_reclaim): only a run that can really act owns last_attempt; a report-mode / no --apply
    # run that shares the state file keeps its own report_attempt, so it can never consume the 2 h cadence of the apply run.
    k_att = "last_attempt" if ctx.apply else "report_attempt"
    last = _num(st.get(k_att))
    if every > 0 and last is not None and ctx.now - last < every - slack:
        left = (every - slack - (ctx.now - last)) / 60
        return Result("ok", _ascii(f"next recycle check in about {left:.0f} min"), {"mode": mode, "next_in_min": round(left)})
    state = _container_state(name)
    if state is None:
        return _skipped("docker unavailable: nothing done")
    if state != "running":
        return _skipped(f"{name} is {state}: not starting it", container_state=state)
    lr = _num(st.get("last_restart"))
    if lr is not None and ctx.now - lr < gap:
        return _skipped(f"restarted {(ctx.now - lr) / 60:.0f} min ago (min gap {gap / 60:.0f})")
    rc, why = _gate(gate, record=ctx.apply)
    st[k_att] = ctx.now                                    # a deferred attempt also starts the next 2 h interval
    rec = (read_json(gates.STATE_DIR / "gates.json", {}) or {}).get(gate, {})
    m = {"mode": mode, "gate_rc": rc, "deferred": int(rec.get("count", 0)) if isinstance(rec, dict) else 0,
         "container_state": state}
    if rc != 0:
        return Result("info", _ascii(f"recycle deferred: {why or 'gate busy'}"), m)
    verdict, err = _do(ctx, "docker-restart", name, lambda: _run_ok(["docker", "restart", name], 180))
    m["action"] = verdict
    if verdict == "done":
        st["last_restart"] = ctx.now
        after = _container_state(name)
        m["container_state"] = after or "unknown"
        if after != "running":
            return Result("warn", _ascii(f"{name} restarted but is {after}"), m)
        return Result("ok", _ascii(f"recycled {name} (gate idle)"), m)
    if verdict == "failed":
        return Result("warn", _ascii(f"restart of {name} failed: {err}"), m)
    word = {"would": "report", "protected": "protected: set unprotect for this task", "paused": "paused"}[verdict]
    return Result("info", _ascii(f"{word}: would restart {name} (gate idle)"), m)


# =========================================================================== openwebui_media_prune
DEFAULT_MEDIA_RULES = [
    {"path": "/volume1/docker/comfyui/output", "patterns": ["owui_*.png", "owui_vid_*.webm"]},
    {"path": "/volume1/docker/openwebui/config/uploads",
     "patterns": ["*_owui_vid.webm", "*_owui_vid.html", "*_generated-image.png", "*_generated_image*"]},
]


def old_enough(age_s: float, days: int) -> bool:
    """find -mtime +N: the age in WHOLE days (fraction ignored) is greater than N, i.e. at least N+1 days."""
    return age_s >= (days + 1) * 86400


def media_candidates(path: str, patterns: list[str], days: int, now: float) -> list[tuple[str, int, int, int, int]]:
    """[(name, size, ino, dev, mtime_ns)] of `find PATH -maxdepth 1 -type f ( -name P1 -o -name P2 ) -mtime +DAYS`."""
    out = []
    with os.scandir(path) as it:
        for e in it:
            if not any(fnmatch.fnmatchcase(e.name, p) for p in patterns):
                continue
            try:
                s = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(s.st_mode) and old_enough(now - s.st_mtime, days):     # -type f, symlinks excluded
                out.append((e.name, s.st_size, s.st_ino, s.st_dev, s.st_mtime_ns))
    return sorted(out)


def _valid_media_rule(rule: Any) -> str:
    """'' when usable, else why not. The directory must be an absolute, real (non-symlink) path; every pattern needs
    at least 4 literal characters and no '/', so a typo can never turn the allow-list into 'everything'."""
    if not isinstance(rule, dict):
        return "rule is not a table"
    path, pats = rule.get("path"), rule.get("patterns")
    if not isinstance(path, str) or not os.path.isabs(path) or path == "/" or os.path.normpath(path) != path:
        return "path must be an absolute normalised directory"
    if os.path.islink(path) or os.path.realpath(path) != path:
        return "path is or crosses a symlink"
    if not isinstance(pats, list) or not pats:
        return "no patterns"
    for p in pats:
        if not isinstance(p, str) or "/" in p or len(re.sub(r"[*?\[\]]", "", p)) < 4:
            return f"pattern {p!r} is too broad"
    return ""


def _unlink_checked(dirpath: str, name: str, ino: int, dev: int, mtime_ns: int) -> None:
    """Delete one scanned file through a directory fd, re-checking the inode (a swap to a symlink or another file
    after the scan makes this raise instead of deleting something else)."""
    fd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode) or (s.st_ino, s.st_dev, s.st_mtime_ns) != (ino, dev, mtime_ns):
            raise _Changed("changed since scan")
        os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


@task("openwebui_media_prune", klass="C1", tier="daily", title="OpenWebUI generated media", timeout=600)
def openwebui_media_prune(ctx: Ctx) -> Result:
    """Delete OpenWebUI/ComfyUI generated media older than retention_days (see PARITY: openwebui_media_prune)."""
    days = _num(ctx.opt("retention_days", 7), 0, 3650)
    rules = ctx.opt("rules", DEFAULT_MEDIA_RULES)
    if days is None or int(days) != days or not isinstance(rules, list) or not rules:
        return _skipped("bad retention_days/rules config: nothing done")
    days = int(days)
    acts, refused, rows = _Acts(ctx), [], []
    for i, rule in enumerate(rules):
        why = _valid_media_rule(rule)
        if why:
            refused.append(f"rule{i}: {why}")
            audit(ctx.name, "media-rule", str(rule.get("path") if isinstance(rule, dict) else rule)[:120], 0, f"refused: {why}")
            continue
        try:
            cands = media_candidates(rule["path"], rule["patterns"], days, ctx.now)
        except OSError:
            continue                                      # legacy: `[ -d "$dir" ] || return 0`
        rows.append(f"{rule['path']}: {len(cands)}")
        for name, size, ino, dev, mt in cands:
            acts.run("media-prune", os.path.join(rule["path"], name), size,
                     lambda d=rule["path"], n=name, a=ino, b=dev, c=mt: _unlink_checked(d, n, a, b, c),
                     label=f"{os.path.basename(rule['path'])}/{name}")      # short row label; the audit keeps the full path
    res = acts.result("files")
    n = acts.n["done"] if ctx.apply else acts.n["would"]
    res.summary = _ascii(f"{'pruned' if ctx.apply else 'report: would prune'} {n} media file(s) older than {days} day(s)"
                         f" ({human(acts.bytes['done'] if ctx.apply else acts.bytes['would'])})"
                         + "".join(f"; {acts.n[k]} {w}" for k, w in (("protected", "protected"), ("failed", "failed"),
                                                                     ("capped", "deferred by cap")) if acts.n[k])
                         + (f"; {len(refused)} rule(s) refused: {refused[0]}" if refused else ""))
    if refused and res.status == "ok":
        res.status = "warn"
    elif acts.n["protected"] and res.status == "ok":
        res.status = "info"                                # matched files the protected list forbids: show it, never page
    res.metrics.update(retention_days=days, rules=len(rules), rules_refused=len(refused))
    return res


# =========================================================================== docker-prune.sh: parity + containers
_STOPPED = {"exited", "created", "dead"}                  # what `docker container prune` considers (not running/paused)
LEGACY_DOCKER_PRUNE = {"retention_h": 168, "cache_max_bytes": 20 * 10 ** 9, "docker_config": "/home/ohmz/.docker"}
LEGACY_DOCKER_PRUNE_PATHS = ["/usr/local/sbin/docker-prune.sh",
                             "/usr/local/lib/homelab-maint/legacy/docker-prune/docker-prune.sh"]
_SIZE_UNITS = {"B": 1, "KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12}


def parse_docker_prune_script(text: str) -> dict:
    """Facts about docker-prune.sh taken from its code (comment lines ignored; line continuations joined)."""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")).replace("\\\n", " ")
    m = re.search(r"^RETENTION=(\d+)h\s*$", code, re.M)
    c = re.search(r"^BUILD_CACHE_MAX=(\d+(?:\.\d+)?)\s*([KMGT]?B)\s*$", code, re.M)
    return {
        "retention_h": int(m.group(1)) if m else None,
        "cache_max_bytes": int(float(c.group(1)) * _SIZE_UNITS[c.group(2)]) if c else None,
        "container_prune": bool(re.search(r"docker container prune\b[^\n]*until=\$RETENTION", code)),
        "image_prune_all": bool(re.search(r"docker image prune --all\b[^\n]*until=\$RETENTION", code)),
        "buildx_prune": bool(re.search(r"docker buildx prune\b[^\n]*--all\b[^\n]*until=\$RETENTION[^\n]*--max-used-space", code)),
        "volume_prune": bool(re.search(r"docker (?:volume|system) prune", code)),
    }


def _docker_ts(s: str) -> float | None:
    """Docker prints RFC 3339 with nanoseconds; the zero time 0001-01-01 means 'never'."""
    try:
        d = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return None if d.year < 1971 else d.timestamp()


def _containers_inventory() -> list[dict] | None:
    """Every container (running or not): id, name, status, created, finished, image, compose project, image id. None when
    unsure. Only these fields are requested: `docker inspect` output also holds Env/Cmd, which can carry secrets."""
    ps = sh(["docker", "ps", "-a", "-q", "--no-trunc"], timeout=30)
    if ps.returncode != 0:
        return None
    ids, rows = ps.stdout.split(), []
    fmt = ('{{.ID}}|{{.Name}}|{{.State.Status}}|{{.Created}}|{{.State.FinishedAt}}|{{.Config.Image}}|'
           '{{index .Config.Labels "com.docker.compose.project"}}|{{.Image}}')
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = sh(["docker", "container", "inspect", "--format", fmt, *chunk], timeout=60)
        lines = r.stdout.splitlines()
        if r.returncode != 0 or len(lines) != len(chunk):
            return None
        for ln in lines:
            f = ln.split("|")
            if len(f) != 8 or not re.fullmatch(r"[0-9a-f]{12,64}", f[0]):     # the id ends up as an argv word of `docker rm`
                return None
            rows.append({"id": f[0], "name": f[1].lstrip("/"), "status": f[2], "created": _docker_ts(f[3]),
                         "finished": _docker_ts(f[4]), "image": f[5], "project": f[6], "image_id": f[7]})
    return rows


def legacy_container_prune_set(rows: list[dict], now: float, hours: float) -> list[str]:
    """What `docker container prune --filter until=<hours>h` removes: every non-running container CREATED before the
    cutoff (the creation time, not the time it stopped)."""
    return sorted(r["name"] for r in rows if r["status"] in _STOPPED and r["created"] is not None
                  and now - r["created"] >= hours * 3600)


def stopped_candidates(rows: list[dict], now: float, days: float, keep: set[str],
                       is_protected: Callable[..., bool]) -> list[dict]:
    """Native selection: stopped (exited/created/dead) for at least `days` since it FINISHED (created, if it never
    ran), oldest first, each row marked with why_kept ('' = removable): kept names or protected name/image/project."""
    out = []
    for r in rows:
        ref = r["finished"] or r["created"]
        if r["status"] not in _STOPPED or ref is None or now - ref < days * 86400 or not _NAME.fullmatch(r["name"]):
            continue
        why = "kept by config" if r["name"] in keep else ("protected" if is_protected(r["name"], r["image"], r["project"]) else "")
        out.append({**r, "ref": ref, "why_kept": why})
    return sorted(out, key=lambda c: (c["ref"], c["name"]))


def _expected_stopped(ctx: Ctx) -> set[str]:
    """Containers that are stopped on purpose: failed_units.expected_stopped_containers + [tasks.docker_containers_prune].keep.
    `keep` is read from the PRUNE task's table whoever asks (the parity/exposure tasks must see the very selection the prune
    task will make: reading their own `keep` made a container kept by the prune task look exposed)."""
    tasks = ctx.cfg.get("tasks", {})
    fu = (tasks.get("failed_units") or {}).get("expected_stopped_containers", [])
    mine = (tasks.get("docker_containers_prune") or {}).get("keep", [])
    return {x for x in [*(fu if isinstance(fu, list) else []), *(mine if isinstance(mine, list) else [])] if isinstance(x, str)}


def _native_protected(ctx: Ctx) -> Callable[..., bool]:
    """is_protected as docker_containers_prune evaluates it (its own `unprotect` list), whichever task asks."""
    return ctx.is_protected if ctx.name == "docker_containers_prune" else Ctx(ctx.cfg, "docker_containers_prune", False, ctx.now).is_protected


@task("docker_containers_prune", klass="C1", tier="weekly", title="Stopped Docker containers", timeout=600)
def docker_containers_prune(ctx: Ctx) -> Result:
    """`docker rm` (no -f, no -v) for containers stopped >= stopped_days (see PARITY: docker-prune.sh)."""
    days = _num(ctx.opt("stopped_days", 7), 1, 3650)
    if days is None:
        return _skipped("bad stopped_days config: nothing done")
    rows = _containers_inventory()
    if rows is None:
        return _skipped("docker unavailable or unparsable: nothing selected")
    if ctx.opt("max_items_per_run") is None:
        ctx.cap_items = min(ctx.cap_items, 25)             # removing containers is irreversible: small default batch
    cands = stopped_candidates(rows, ctx.now, days, _expected_stopped(ctx), ctx.is_protected)
    acts, kept = _Acts(ctx), [c for c in cands if c["why_kept"]]
    for c in cands:
        if c["why_kept"]:
            continue
        acts.run("docker-rm", c["name"], 0, lambda cid=c["id"]: _run_ok(["docker", "rm", cid], 120),
                 protect=(c["image"], c["project"]), label=f"{c['name']} stopped {(ctx.now - c['ref']) / 86400:.0f} d")
    res = acts.result("containers")
    n = acts.n["done"] if ctx.apply else acts.n["would"]
    res.summary = _ascii(f"{'removed' if ctx.apply else 'report: would remove'} {n} container(s) stopped >= {days:g} d; "
                         f"{len(kept)} kept (protected/expected), {len(rows)} total"
                         + (f"; {acts.n['capped']} deferred by cap" if acts.n["capped"] else "")
                         + (f"; {acts.n['failed']} failed" if acts.n["failed"] else ""))
    res.items = [{"name": c["name"][:40], "state": c["why_kept"] or "removable",
                  "stopped_d": round((ctx.now - c["ref"]) / 86400, 1)} for c in cands][:12]
    res.metrics.update(stopped_days=days, containers=len(rows), kept=len(kept))
    return res


def _find_legacy(paths: list[str]) -> str | None:
    for p in paths:
        try:
            return Path(p).read_text()
        except OSError:
            continue
    return None


def _buildx_names(running_only: bool = False) -> list[str] | None:
    """Builder names from `docker buildx ls --format json` (JSON lines or one list). running_only: only builders with a running
    node, which is what cleaners.docker_cache prunes (its _builders()): a stopped builder is listed but never touched."""
    r = sh(["docker", "buildx", "ls", "--format", "json"], timeout=30)
    if r.returncode != 0:
        return None
    out, dec, i, text = [], json.JSONDecoder(), 0, r.stdout.strip()
    while i < len(text):
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            return None
        out.extend(obj if isinstance(obj, list) else [obj])
        while i < len(text) and text[i].isspace():
            i += 1

    def up(o: dict) -> bool:
        return any(isinstance(n, dict) and n.get("Status") == "running" for n in o.get("Nodes") or [])

    return sorted(o["Name"] for o in out if isinstance(o, dict) and isinstance(o.get("Name"), str)
                  and (not running_only or (_NAME.fullmatch(o["Name"]) and up(o))))


def _owner_builders(docker_config: str) -> list[str] | None:
    """Builders defined in the config dir the legacy job used (names of buildx/instances/*; contents never read). Stopped ones
    are included, so a stopped builder the runner cannot see is reported too: a gap that errs on the side of caution."""
    try:
        return sorted(os.listdir(os.path.join(docker_config, "buildx", "instances")))
    except OSError:
        return None


CACHE_AGE_OPTION = "max_age_hours"


def _reads_option(source: str, key: str) -> bool:
    """True when the code calls `<something>.opt("<key>", ...)`: the option is really read, not merely named in a comment."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "opt" and n.args
               and isinstance(n.args[0], ast.Constant) and n.args[0].value == key for n in ast.walk(tree))


def _cache_age_supported() -> bool:
    """Does cleaners.docker_cache REALLY implement max_age_hours? A capability table exported by cleaners wins
    (CAPABILITIES = {"docker_cache": {"max_age_hours", ...}}); otherwise the function's own code is inspected. Unknown = no.
    A config key alone proves nothing: an option the task ignores must not turn the retirement gate green."""
    cap = getattr(_cleaners, "CAPABILITIES", None)
    if isinstance(cap, dict) and isinstance(cap.get("docker_cache"), (set, frozenset, list, tuple)):
        return CACHE_AGE_OPTION in cap["docker_cache"]
    try:
        return _reads_option(inspect.getsource(_cleaners.docker_cache), CACHE_AGE_OPTION)
    except (OSError, TypeError):
        return False


# ---- what the LEGACY script would delete (the harm the native tasks were written to avoid)
_IMG_ID = re.compile(r"sha256:[0-9a-f]{64}")
_TIMER_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,80}\.timer")
_TIMER_LIVE = {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "indirect", "generated", "transient"}
_TIMER_DEAD = {"disabled", "masked", "masked-runtime", "not-found", "static", "bad"}
LEGACY_TIMER = "docker-prune.timer"


def _images_inventory() -> list[dict] | None:
    """Every local image, intermediate and dangling ones included (what `image prune --all` looks at): id, created, size,
    parent, tags. None when unsure. Only these fields are requested (no Config/Env)."""
    ls = sh(["docker", "image", "ls", "--all", "--quiet", "--no-trunc"], timeout=30)
    if ls.returncode != 0:
        return None
    ids = sorted(set(ls.stdout.split()))
    if not all(_IMG_ID.fullmatch(i) for i in ids):             # an id ends up as an argv word
        return None
    fmt = "{{.Id}}|{{.Created}}|{{.Size}}|{{.Parent}}|{{json .RepoTags}}"
    out = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = sh(["docker", "image", "inspect", "--format", fmt, *chunk], timeout=60)
        lines = r.stdout.splitlines()
        if r.returncode != 0 or len(lines) != len(chunk):
            return None
        for ln in lines:
            f = ln.split("|", 4)
            if len(f) != 5 or not _IMG_ID.fullmatch(f[0]):
                return None
            try:
                tags = json.loads(f[4]) or []
            except ValueError:
                return None
            if not isinstance(tags, list):
                return None
            out.append({"id": f[0], "created": _docker_ts(f[1]), "size": int(f[2]) if f[2].isdigit() else 0,
                        "parent": f[3] if _IMG_ID.fullmatch(f[3]) else "", "tags": [t for t in tags if isinstance(t, str)]})
    return out


def _img_name(i: dict) -> str:
    return i["tags"][0] if i["tags"] else "<none>:" + i["id"].removeprefix("sha256:")[:12]


def legacy_image_prune_set(images: list[dict], used_ids: set[str], now: float, hours: float) -> list[dict]:
    """What `docker image prune --all --filter until=<hours>h` removes when the containers hold exactly `used_ids`: images no
    container references, created before the cutoff, that are not the parent of an image which stays (a young unused image
    keeps its old parent alive). An image of unknown age stays (fail closed). Layer sharing beyond Parent is not modelled."""
    stays = set(used_ids) | {i["id"] for i in images
                             if i["id"] not in used_ids and (i["created"] is None or now - i["created"] < hours * 3600)}
    by_id, protected, stack = {i["id"]: i for i in images}, set(), list(stays)
    while stack:
        x = stack.pop()
        if x not in protected:
            protected.add(x)
            if by_id.get(x, {}).get("parent"):
                stack.append(by_id[x]["parent"])
    return sorted((i for i in images if i["id"] not in protected), key=lambda i: (_img_name(i), i["id"]))


def legacy_image_exposure(containers: list[dict], images: list[dict], only_legacy: list[str], now: float,
                          hours: float) -> tuple[list[dict], list[dict]]:
    """(exposed, other): the images the legacy image prune frees AFTER its container prune has removed the legacy-removable
    containers. `exposed` = images a container the native tasks keep uses today (so they only become unused through the legacy
    container prune: the harm); `other` = images that are unused anyway (docker_images removes those after unused_days)."""
    gone = set(legacy_container_prune_set(containers, now, hours))
    after = legacy_image_prune_set(images, {c["image_id"] for c in containers if c["name"] not in gone}, now, hours)
    held = {c["image_id"] for c in containers if c["name"] in only_legacy}
    return [i for i in after if i["id"] in held], [i for i in after if i["id"] not in held]


def _legacy_timer(unit: str) -> tuple[bool | None, str]:
    """(will it still fire?, state word) for the legacy timer: True = enabled or running, False = disabled/masked/absent,
    None = systemd could not say (callers treat that as True: fail closed)."""
    e = (sh(["systemctl", "is-enabled", unit], timeout=15).stdout or "").strip().lower()
    a = (sh(["systemctl", "is-active", unit], timeout=15).stdout or "").strip().lower()
    if e in _TIMER_LIVE or a in ("active", "activating", "reloading"):
        return True, e if e in _TIMER_LIVE else a
    if e in _TIMER_DEAD and a in ("inactive", "failed", "deactivating", "dead", ""):
        return False, e
    return None, "unknown"


def _names(names: list[str], n: int = 3) -> str:
    return ", ".join(names[:n]) + (f" +{len(names) - n}" if len(names) > n else "")


def _legacy_params(ctx: Ctx) -> tuple[int, int, dict, str]:
    """(retention hours, build-cache cap bytes, parsed script facts, "script" | "baked") of docker-prune.sh."""
    text = _find_legacy(list(ctx.opt("legacy_script", LEGACY_DOCKER_PRUNE_PATHS)))
    leg = parse_docker_prune_script(text) if text else {}
    return (leg.get("retention_h") or LEGACY_DOCKER_PRUNE["retention_h"],
            leg.get("cache_max_bytes") or LEGACY_DOCKER_PRUNE["cache_max_bytes"], leg, "script" if text else "baked")


@dataclass
class Exposure:
    """What the legacy docker-prune.timer would still delete that the native tasks keep (shared by docker_prune_parity, which
    reports it as information, and docker_prune_exposure, which pages it)."""
    unit: str
    t_live: bool | None                                  # will the timer still fire? None = systemd could not say (treated as yes)
    t_word: str
    t_next: float | None
    rows: list[dict] | None = None                       # container inventory; None = docker said nothing usable
    lset: list[str] = field(default_factory=list)        # containers the script removes
    nset: list[str] = field(default_factory=list)        # containers docker_containers_prune removes
    only_legacy: list[str] = field(default_factory=list)
    images: list[dict] | None = field(default_factory=list)   # [] = not needed (nothing to expose); None = needed but unavailable
    exp_img: list[dict] = field(default_factory=list)    # images that only become unused through the legacy container prune
    other_img: list[dict] = field(default_factory=list)  # images unused anyway (docker_images removes those after unused_days)

    @property
    def exposed(self) -> bool:
        return bool(self.only_legacy) and self.t_live is not False

    @property
    def exp_bytes(self) -> int:
        return sum(i["size"] for i in self.exp_img)

    def when(self, now: float) -> str:
        return f"next in {_ago(self.t_next - now)}" if self.t_next and self.t_next > now else self.t_word

    def head(self, now: float) -> str:
        return (f"{self.unit} ON ({self.when(now)}): deletes container {_names(self.only_legacy)}"
                + (f" + image {_names([_img_name(i) for i in self.exp_img])} ({human(self.exp_bytes)})" if self.exp_img else "")
                + "; natives keep")


def _timer_state(ctx: Ctx) -> Exposure:
    """The legacy timer: will it fire, and when. Two cheap systemctl reads; the inventories are filled in by _measure_exposure."""
    unit = ctx.opt("legacy_timer", LEGACY_TIMER)
    unit = unit if isinstance(unit, str) and _TIMER_UNIT.fullmatch(unit) else LEGACY_TIMER
    t_live, t_word = _legacy_timer(unit)
    return Exposure(unit, t_live, t_word, ((_list_timers() or {}).get(unit) or (None, None))[1])


def _measure_exposure(ctx: Ctx, ex: Exposure, rows: list[dict] | None, ret_h: float, days: float) -> Exposure:
    """Fill ex from the container inventory: what the script removes vs what docker_containers_prune would (its own `keep`,
    `unprotect` and expected-stopped list), then the images that become unused only through that difference."""
    ex.rows = rows
    if rows is None:
        return ex
    ex.lset = legacy_container_prune_set(rows, ctx.now, ret_h)
    ex.nset = [c["name"] for c in stopped_candidates(rows, ctx.now, days, _expected_stopped(ctx), _native_protected(ctx))
               if not c["why_kept"]]
    ex.only_legacy = sorted(set(ex.lset) - set(ex.nset))
    if ex.only_legacy:
        ex.images = _images_inventory()
        if ex.images is not None:
            ex.exp_img, ex.other_img = legacy_image_exposure(rows, ex.images, ex.only_legacy, ctx.now, ret_h)
    return ex


@task("docker_prune_parity", klass="C0", tier="weekly", title="docker-prune.sh parity", timeout=120)
def docker_prune_parity(ctx: Ctx) -> Result:
    """Does the native stack cover docker-prune.sh, and what does the script still delete that the natives keep? Verdicts per
    behaviour: same | differs (documented) | gap | exposed. Status reflects GAPS ONLY, because this task is the retirement gate
    of the docker-prune cutover and the cutover is what ends an exposure:
      ok    no gap, nothing exposed;
      info  no gap, but docker-prune.timer is still on and would delete something the natives keep (alert=False; paged by
            docker_prune_exposure, not here);
      warn  a gap (alert=False: dashboard only; NOT green for the retirement gate, which accepts ok/info only)."""
    ret_h, cap_b, leg, src = _legacy_params(ctx)
    tcfg = ctx.cfg.get("tasks", {})
    cache, imgs, cont = tcfg.get("docker_cache", {}), tcfg.get("docker_images", {}), tcfg.get("docker_containers_prune", {})
    mode = lambda c, n: "missing" if c.get("enabled") is False else str(c.get("mode", "report"))      # noqa: E731
    items: list[dict] = []

    def add(what: str, legacy: str, native: str, verdict: str, note: str = "") -> None:
        items.append({"behaviour": what, "legacy": legacy[:70], "native": native[:70], "verdict": verdict, "note": note[:90]})

    # 1. containers: the clock differs (created vs finished) and the native task has exclusions
    days = _num(cont.get("stopped_days", 7), 1) or 7
    ex = _measure_exposure(ctx, _timer_state(ctx), _containers_inventory(), ret_h, days)
    only_legacy, exposed, unit = ex.only_legacy, ex.exposed, ex.unit
    live = (f"legacy would remove {len(ex.lset)}, native {len(ex.nset)}; only legacy: {', '.join(only_legacy[:4]) or '-'}"
            if ex.rows is not None else "")
    add("stopped containers", f"container prune until={ret_h}h (created time)",
        f"docker_containers_prune stopped_days={days:g} (finished time), keeps protected/expected [{mode(cont, 'c')}]",
        "differs" if leg.get("container_prune", True) else "unknown", live or "docker unavailable")
    # 1b. the harm: is the legacy timer still going to do it? (information here; docker_prune_exposure pages it)
    when = ex.when(ctx.now)
    if only_legacy:
        add("legacy DELETES containers", _names(only_legacy), "keeps them (protected / intentionally stopped)",
            "exposed" if exposed else "differs", f"{when}; stays safe once {unit} is disabled")
        if ex.exp_img:
            add("legacy DELETES images", _names([_img_name(i) for i in ex.exp_img]), "keeps them (a kept container uses them)",
                "exposed" if exposed else "differs", f"{human(ex.exp_bytes)}; unused once the container is gone: keep a copy first")
        elif ex.images is None:
            add("legacy DELETES images", "unknown", "unknown", "exposed" if exposed else "differs", "docker image inventory unavailable")
    # 2. images
    ud = _num(imgs.get("unused_days", 14), 1) or 14
    add("unused images", f"image prune --all until={ret_h}h (build date)",
        f"docker_images unused_days={ud:g} via ledger [{mode(imgs, 'i')}]", "differs" if ud * 24 >= ret_h else "gap",
        "later and safer" if ud * 24 >= ret_h else "native is more aggressive than the script")
    # 3. build cache: cap and age filter
    hi = _num(cache.get("high_gib", 15), 0.001) or 15
    add("build cache cap", f"--max-used-space {cap_b / 10 ** 9:g}GB per builder",
        f"docker_cache high={hi:g} GiB -> low {cache.get('low_gib', 8)} GiB [{mode(cache, 'k')}]",
        "differs" if hi * GIB <= cap_b else "gap", "stricter cap" if hi * GIB <= cap_b else "native allows a larger cache")
    age = _num(cache.get(CACHE_AGE_OPTION), 0.001)
    supported = _cache_age_supported()
    if age is not None and supported:
        add("build cache age filter", f"--filter until={ret_h}h", f"docker_cache {CACHE_AGE_OPTION}={age:g}",
            "same" if age <= ret_h else "differs", "" if age <= ret_h else "native keeps entries longer than the script")
    elif age is not None:
        add("build cache age filter", f"--filter until={ret_h}h", f"{CACHE_AGE_OPTION}={age:g} set, but docker_cache ignores it", "gap",
            "the option is not implemented by cleaners.docker_cache yet: setting it changes nothing")
    else:
        add("build cache age filter", f"--filter until={ret_h}h", "none", "gap",
            "entries unused > 7 d below the cap are kept; " + (f"add {CACHE_AGE_OPTION}" if supported else "docker_cache has no age option yet"))
    # 4. builder visibility (the script ran with DOCKER_CONFIG=<owner>/.docker; as root docker_cache sees only root's).
    #    docker_cache only prunes RUNNING builders, so that is the set compared.
    owner = _owner_builders(str(ctx.opt("legacy_docker_config", LEGACY_DOCKER_PRUNE["docker_config"])))
    seen = _buildx_names(running_only=True)
    if owner is None or seen is None:
        add("builder discovery", "DOCKER_CONFIG=owner dir", "unknown", "gap", "cannot read the owner's buildx store or docker")
    else:
        missing = sorted(set(owner) - set(seen))
        add("builder discovery", f"owner builders {', '.join(owner) or '-'}", f"runner prunes running: {', '.join(seen) or '-'}",
            "gap" if missing else "same", f"not pruned by docker_cache: {', '.join(missing)}" if missing else "")
    # 5. volumes and schedule
    vp = leg.get("volume_prune")
    add("volumes", "never pruned" if vp is False else ("PRUNES VOLUMES" if vp else "unknown"), "never pruned",
        "same" if vp is False else ("gap" if vp is None else "differs"))
    add("schedule", "Sun 04:00 weekly", "daily 07:30 (cache, images), weekly Wed (containers)", "differs")
    gaps = [i for i in items if i["verdict"] == "gap"]
    ready = not gaps and all(mode(c, "") == "apply" for c in (cache, imgs, cont))
    metrics = {"source": src, "retention_h": ret_h, "gaps": len(gaps), "differs": sum(i["verdict"] == "differs" for i in items),
               "same": sum(i["verdict"] == "same" for i in items), "cutover_ready": ready,
               "cutover": "ready" if ready else ("blocked: gaps" if gaps else "blocked: natives not in apply mode"),
               "legacy_timer": ex.t_word, "legacy_next_h": round((ex.t_next - ctx.now) / 3600, 1) if ex.t_next else -1,
               "exposed_containers": len(only_legacy) if exposed else 0, "exposed_images": len(ex.exp_img) if exposed else 0,
               "exposed_gib": round(ex.exp_bytes / GIB, 1) if exposed else 0, "legacy_other_images": len(ex.other_img)}
    order = {"exposed": 0, "gap": 1}
    ranked = sorted(items, key=lambda i: order.get(i["verdict"], 2))[:12]
    # EXPOSURE NEVER DECIDES THE STATUS: this task is the retirement gate (legacy.GREEN = ok/info) and the cutover is what
    # ends an exposure, so a status of warn while it exists made the gate unreachable. docker_prune_exposure pages it.
    seen_exposed = f"{unit} still ON ({when}) and deletes {_names(only_legacy, 2)}" if exposed else ""
    if gaps:
        return Result("warn", _ascii(f"{len(gaps)} gap(s) vs docker-prune.sh: " + "; ".join(g["behaviour"] for g in gaps)
                                     + (f"; {seen_exposed}" if exposed else "")), metrics, ranked, alert=False)
    if exposed:
        return Result("info", _ascii(f"docker-prune.sh covered natively; {seen_exposed}: the cutover ends that"),
                      metrics, ranked, alert=False)
    return Result("ok", _ascii(f"docker-prune.sh covered natively ({metrics['same']} same, {metrics['differs']} documented differences)"),
                  metrics, ranked, alert=False)


# ---- docker_prune_exposure: the page. A separate, tiny, CHECK-tier task: a deliberate `docker stop` of a container that the
# legacy weekly prune would delete (by CREATION time, ignoring every protection list) must be noticed within one tick.
def _exposure_event(ctx: Ctx, ex: Exposure, sev: str, sig: str, seq: int, now: float) -> dict:
    nxt = (datetime.fromtimestamp(ex.t_next).astimezone().strftime("%a %Y-%m-%d %H:%M %Z") if ex.t_next else "an unknown time")
    imgs = [_img_name(i) for i in ex.exp_img]
    lines = [f"  - container {n}: stopped on purpose, but the script counts from its CREATION time" for n in ex.only_legacy[:8]]
    lines += [f"  - image {n} ({human(i['size'])}): unused once that container is gone" for n, i in zip(imgs[:8], ex.exp_img)]
    body = (f"{ex.unit} runs docker-prune.sh ({ex.when(now)}; {nxt}). Its first step, `docker container prune --filter "
            "until=168h`, removes every stopped container CREATED more than a week ago and ignores the protected list and the "
            "intentionally-stopped list; its second step then removes the images only those containers used. It would delete:\n\n"
            + "\n".join(lines) + "\n\nhomelab-maint changed nothing. Pick ONE before then:\n"
            f"  1. start the container (running containers are never pruned): docker start {ex.only_legacy[0]}\n"
            f"  2. stop the legacy timer until homelab-maint takes over: sudo systemctl stop {ex.unit}\n"
            "     (it also does the build-cache and image cleanup, so keep running `docker buildx prune` by hand meanwhile)\n"
            "  3. cut over: homelab-maint migrate cutover docker-prune (its native replacements keep stopped-on-purpose containers)\n"
            "If an image cannot be rebuilt from its Dockerfile, keep a copy first: docker save <image> | gzip > <file>.tar.gz\n")
    key = ctx.name if seq == 0 else f"{ctx.name}:{hashlib.sha1(sig.encode()).hexdigest()[:8]}:{seq}"
    return {"kind": "alert", "severity": sev, "title": f"{ex.unit} will delete a stopped container on {socket.gethostname()}",
            "summary": _ascii(ex.head(now), 130), "details": body, "status": sev, "task": ctx.name, "dedupe_key": key,
            "facts": {"timer": ex.unit, "next": nxt, "containers": ", ".join(ex.only_legacy[:6]), "images": ", ".join(imgs[:6]),
                      "image_gib": round(ex.exp_bytes / GIB, 1)}}


def _exposure_recovery(ctx: Ctx, ex: Exposure) -> dict:
    return {"kind": "recovery", "severity": "ok", "title": "docker-prune.timer exposure", "status": "ok", "task": ctx.name,
            "summary": _ascii(f"{ex.unit} no longer deletes anything the natives keep ({ex.t_word})", 130), "dedupe_key": ctx.name,
            "facts": {"was": "crit"}}


@task("docker_prune_exposure", klass="C0", tier="check", title="docker-prune.timer exposure", timeout=90)
def docker_prune_exposure(ctx: Ctx) -> Result:
    """Would the LEGACY docker-prune.timer delete something the native tasks keep (a container stopped on purpose, and the image
    only it used)? crit when the timer fires within crit_within_h (72) or its next run is unknown, else warn; ok when the timer is
    off or would delete nothing the natives keep. PAGES at first sight (see _announce) and once more when it turns imminent
    (imminent_h = 6). Read-only; the owner's way out is in the page. Docker is not asked while the timer is off."""
    o = _Opts(ctx)
    crit_h, soon_h = o.num("crit_within_h", 72, 0, 24 * 90), o.num("imminent_h", 6, 0, 24 * 90)
    ret_h, _cap, _leg, _src = _legacy_params(ctx)
    days = _num((ctx.cfg.get("tasks", {}).get("docker_containers_prune") or {}).get("stopped_days", 7), 1) or 7
    ex = _timer_state(ctx)
    st, nxt_h = ctx.state, (round((ex.t_next - ctx.now) / 3600, 1) if ex.t_next else -1)
    m: dict[str, Any] = {"legacy_timer": ex.t_word, "legacy_next_h": nxt_h, "exposed_containers": 0, "exposed_images": 0,
                         "exposed_gib": 0, "legacy_other_images": 0, "transition": "none", "sig_page": "", "imminent": False}
    if o.bad:
        m["bad_config"] = ",".join(o.bad)[:60]

    def settle() -> None:
        """Nothing exposed: close any open announcement (the recovery of a blip the Notifier never paged)."""
        m["transition"] = _sig_transition(st.get("signature"), "OK")
        keep, m["sig_page"] = _announce(ctx, "OK", m["transition"], lambda seq: {}, lambda: _exposure_recovery(ctx, ex))
        _remember(st, keep)

    if ex.t_live is False:                                    # the script cannot fire: nothing is exposed, nothing to ask docker
        settle()
        return Result("ok", _ascii(f"{ex.unit} is {ex.t_word}: the legacy prune cannot run"), m)
    rows = _containers_inventory()
    if rows is None:
        return _skipped("docker unavailable: exposure unknown", **{k: m[k] for k in ("legacy_timer", "legacy_next_h")})
    ex = _measure_exposure(ctx, ex, rows, ret_h, days)
    if not ex.exposed:
        settle()
        return Result("ok", _ascii(f"{ex.unit} ON ({ex.when(ctx.now)}) but deletes nothing the natives keep ({len(rows)} containers)"), m)
    imminent = ex.t_next is not None and ex.t_next - ctx.now <= soon_h * 3600
    sev = "crit" if (ex.t_next is None or ex.t_next - ctx.now <= crit_h * 3600) else "warn"
    sig = ",".join(["c:" + n for n in ex.only_legacy] + ["i:" + _img_name(i) for i in ex.exp_img]) + ("!" if imminent else "")
    m["transition"] = _sig_transition(st.get("signature"), sig)
    keep, m["sig_page"] = _announce(ctx, sig, m["transition"], lambda seq: _exposure_event(ctx, ex, sev, sig, seq, ctx.now))
    _remember(st, keep)
    m.update(exposed_containers=len(ex.only_legacy), exposed_images=len(ex.exp_img), exposed_gib=round(ex.exp_bytes / GIB, 1),
             imminent=imminent, legacy_other_images=len(ex.other_img))
    items = [{"what": "container", "name": n[:40], "note": "stopped on purpose; the script counts from creation"} for n in ex.only_legacy]
    items += [{"what": "image", "name": _img_name(i)[:60], "note": f"{human(i['size'])}, unused once its container is gone"} for i in ex.exp_img]
    return Result(sev, _ascii(ex.head(ctx.now)), m, items[:12],
                  issue_key=core.ikey(containers=ex.only_legacy, images=[_img_name(i) for i in ex.exp_img]))      # SPEC5: ALL of them; never the countdown or the GiB


# =========================================================================== os_jobs
# max_age_hours = the real OnCalendar cadence + its RandomizedDelaySec + slack, read from `systemctl show` on this host.
DEFAULT_OS_JOBS: list[dict] = [
    {"name": "apt-daily", "timer": "apt-daily.timer", "service": "apt-daily.service", "max_age_hours": 36},
    {"name": "apt-daily-upgrade", "timer": "apt-daily-upgrade.timer", "service": "apt-daily-upgrade.service", "max_age_hours": 36},
    {"name": "unattended-upgrades", "kind": "daemon", "service": "unattended-upgrades.service",
     "log": "/var/log/unattended-upgrades/unattended-upgrades.log", "log_hours": 36},
    {"name": "logrotate", "timer": "logrotate.timer", "service": "logrotate.service", "max_age_hours": 36},
    {"name": "systemd-tmpfiles-clean", "timer": "systemd-tmpfiles-clean.timer", "service": "systemd-tmpfiles-clean.service", "max_age_hours": 36},
    {"name": "fstrim", "timer": "fstrim.timer", "service": "fstrim.service", "max_age_hours": 200},
    {"name": "e2scrub_all", "timer": "e2scrub_all.timer", "service": "e2scrub_all.service", "max_age_hours": 200},
    {"name": "fwupd-refresh", "timer": "fwupd-refresh.timer", "service": "fwupd-refresh.service", "max_age_hours": 12},
    {"name": "man-db", "timer": "man-db.timer", "service": "man-db.service", "max_age_hours": 48},
    {"name": "sysstat-collect", "timer": "sysstat-collect.timer", "service": "sysstat-collect.service", "max_age_hours": 2},
    {"name": "sysstat-summary", "timer": "sysstat-summary.timer", "service": "sysstat-summary.service", "max_age_hours": 36},
    {"name": "dpkg-db-backup", "timer": "dpkg-db-backup.timer", "service": "dpkg-db-backup.service", "max_age_hours": 36},
    {"name": "snapd-refresh", "kind": "snapd", "max_age_hours": 26},
]
SNAPD_SOCKET = "/run/snapd.socket"
_UA_ERROR = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (?:ERROR|CRITICAL)\b\s*(.*)")
_OK_STATES = {"ok", "running", "waiting", "absent"}


def _list_timers() -> dict[str, tuple[float | None, float | None]] | None:
    """{timer unit: (last trigger epoch|None, next elapse epoch|None)} from `systemctl list-timers --output=json`."""
    r = sh(["systemctl", "list-timers", "--all", "--output=json", "--no-pager"], timeout=20)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    out = {}
    for t in data if isinstance(data, list) else []:
        if isinstance(t, dict) and isinstance(t.get("unit"), str):
            us = lambda v: v / 1e6 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None   # noqa: E731
            out[t["unit"]] = (us(t.get("last")), us(t.get("next")))
    return out


def _show(units: list[str]) -> dict[str, dict[str, str]] | None:
    """{unit: {property: value}} from one `systemctl show` over many units (blocks are separated by blank lines)."""
    if not units:
        return {}
    props = "Id,LoadState,ActiveState,SubState,UnitFileState,Result,ExecMainStatus,ActiveEnterTimestampMonotonic"
    r = sh(["systemctl", "show", "-p", props, *units], timeout=20)
    if r.returncode != 0:
        return None
    out: dict[str, dict[str, str]] = {}
    for block in r.stdout.split("\n\n"):
        kv = dict(ln.split("=", 1) for ln in block.splitlines() if "=" in ln)
        if kv.get("Id"):
            out[kv["Id"]] = kv
    return out


def _ago(sec: float) -> str:
    return f"{sec / 60:.0f} min" if sec < 5400 else (f"{sec / 3600:.1f} h" if sec < 172800 else f"{sec / 86400:.1f} d")


def _ua_errors(path: str, now: float, hours: float) -> tuple[int, str]:
    """(count, last text) of ERROR/CRITICAL lines of the unattended-upgrades log within `hours` (local-time stamps)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0, ""
    n, last = 0, ""
    for ln in lines:
        m = _UA_ERROR.match(ln)
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if 0 <= now - ts <= hours * 3600:
                n, last = n + 1, m.group(2)
    return n, _scrub(last, 80)


def _snapd_refresh() -> tuple[str, float | None, str]:
    """(state, last refresh epoch|None, detail): state ok | absent | unknown. Read from snapd's own API (ISO stamps)."""
    if not os.path.exists(SNAPD_SOCKET):
        return "absent", None, "snapd not installed"
    r = sh(["curl", "-fsS", "-m", "5", "--unix-socket", SNAPD_SOCKET, "http://localhost/v2/system-info"], timeout=10)
    try:
        last = json.loads(r.stdout)["result"]["refresh"].get("last") if r.returncode == 0 else None
        return "ok", (datetime.fromisoformat(last).timestamp() if last else None), ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unknown", None, "cannot read snapd refresh info"


def _eval_job(j: dict, now: float, mono_now: float, timers: dict | None, props: dict) -> dict:
    """One row: name, state (ok|running|waiting|absent|overdue|failed|inactive|unknown), detail, last, next, max_h."""
    row = {"name": j["name"], "state": "ok", "detail": "", "last": None, "next": None, "max_h": j.get("max_age_hours")}
    kind, max_h = j.get("kind", "timer"), j.get("max_age_hours")
    if kind == "snapd":
        st, last, why = _snapd_refresh()
        row.update(state="ok" if st == "ok" else st, last=last, detail=why)
        if st != "ok":
            return row
    elif kind == "daemon":
        sp = props.get(j["service"], {})
        if sp.get("LoadState") != "loaded":
            return {**row, "state": "absent", "detail": "not installed"}
        if sp.get("ActiveState") != "active":
            return {**row, "state": "inactive", "detail": f"{j['service']} is {sp.get('ActiveState', '?')}"}
        n, text = _ua_errors(j.get("log", ""), now, float(j.get("log_hours", 36)))
        if n:
            return {**row, "state": "failed", "detail": f"{n} error line(s) in the upgrade log, last: {text}"}
        return {**row, "detail": "shutdown helper active"}
    else:
        tp, sp = props.get(j["timer"], {}), props.get(j["service"], {})
        if tp.get("LoadState") != "loaded":
            return {**row, "state": "absent", "detail": "not installed"}
        if tp.get("ActiveState") != "active":
            return {**row, "state": "inactive", "detail": f"timer {tp.get('ActiveState', '?')} ({tp.get('UnitFileState', '?')})"}
        last, nxt = (timers or {}).get(j["timer"], (None, None))
        row.update(last=last, next=nxt)
        res = sp.get("Result", "success")
        if res not in ("success", "") or sp.get("ActiveState") == "failed":
            # Result is the verdict: fwupd-refresh exits 2 with Result=success (SuccessExitStatus=2)
            return {**row, "state": "failed", "detail": f"last run failed: {res} (exit status {sp.get('ExecMainStatus', '?')})"}
        if sp.get("ActiveState") in ("active", "activating", "reloading"):
            return {**row, "state": "running", "detail": "running now"}
    if max_h is None:
        return row
    last = row["last"]
    if last is None:                                       # never triggered since the timer started (or since boot)
        mono = props.get(j.get("timer", ""), {}).get("ActiveEnterTimestampMonotonic", "")
        age_active = mono_now - int(mono) / 1e6 if mono.isdigit() else None
        if kind == "snapd":
            age_active = mono_now                          # no refresh recorded: judge by uptime (a fresh install is not late)
        if age_active is not None and age_active < max_h * 3600:
            return {**row, "state": "waiting", "detail": "no run yet (timer started recently)"}
        return {**row, "state": "overdue", "detail": f"never ran (limit {max_h:g} h)"}
    if now - last > max_h * 3600:
        return {**row, "state": "overdue", "detail": f"last ran {_ago(now - last)} ago (limit {max_h:g} h)"}
    return {**row, "detail": f"last ran {_ago(now - last)} ago"}


def os_jobs_table(jobs: list[dict] | None = None, now: float | None = None, mono_now: float | None = None) -> list[dict]:
    """Read-only status of every OS-managed job (for the os_jobs task and for the unified schedule export)."""
    now = time.time() if now is None else now
    mono_now = _mono_now() if mono_now is None else mono_now
    jobs = DEFAULT_OS_JOBS if jobs is None else jobs
    timers = _list_timers()
    props = _show(sorted({u for j in jobs for u in (j.get("timer"), j.get("service")) if u}))
    if timers is None or props is None:
        return [{"name": j["name"], "state": "unknown", "detail": "systemctl unavailable", "last": None, "next": None,
                 "max_h": j.get("max_age_hours")} for j in jobs if j.get("kind") != "snapd"]
    return [_eval_job(j, now, mono_now, timers, props) for j in jobs]


@task("os_jobs", klass="C0", tier="check", title="OS maintenance timers", timeout=60)
def os_jobs(ctx: Ctx) -> Result:
    """Verify the OS-managed timers ran recently and succeeded (observed, never replaced); see the os_jobs section of
    the module docstring. warn = overdue / failed / timer inactive; absent and waiting are fine."""
    jobs = ctx.opt("jobs", DEFAULT_OS_JOBS)
    if not isinstance(jobs, list) or not all(isinstance(j, dict) and isinstance(j.get("name"), str) for j in jobs):
        return _skipped("bad jobs config: nothing verified")
    extra = ctx.opt("extra_jobs", [])
    ignore = {x for x in ctx.opt("ignore", []) if isinstance(x, str)}
    jobs = [j for j in [*jobs, *(extra if isinstance(extra, list) else [])] if isinstance(j, dict) and j.get("name") not in ignore]
    table = os_jobs_table(jobs, ctx.now)
    order = {"failed": 0, "overdue": 1, "inactive": 2, "unknown": 3, "waiting": 4, "running": 5, "ok": 6, "absent": 7}
    bad = sorted((r for r in table if r["state"] not in _OK_STATES), key=lambda r: (order.get(r["state"], 9), r["name"]))   # worst first
    counts = {s: sum(r["state"] == s for r in table) for s in ("ok", "running", "waiting", "absent", "overdue", "failed", "inactive", "unknown")}
    ages = [ctx.now - r["last"] for r in table if r["last"]]
    metrics = {"jobs": len(table), "on_schedule": len(table) - len(bad), **{k: v for k, v in counts.items() if k not in ("ok",)},
               "oldest_run_h": round(max(ages) / 3600, 1) if ages else -1}
    items = [{"name": r["name"], "state": r["state"],
              "last": f"{_ago(ctx.now - r['last'])} ago" if r["last"] else "-",
              "next": f"in {_ago(r['next'] - ctx.now)}" if r["next"] and r["next"] > ctx.now else "-",
              "note": r["detail"][:60]} for r in sorted(table, key=lambda r: (order.get(r["state"], 9), r["name"]))][:12]
    if not bad:
        n_ok = len(table) - counts["absent"]
        return Result("ok", _ascii(f"OS jobs: {n_ok}/{n_ok} on schedule ({counts['absent']} not installed)"), metrics, items)
    txt = "; ".join(f"{r['name']} {r['state']}" + (f" ({r['detail'].split(' (')[0][:40]})" if r["state"] == "failed" else "") for r in bad[:4])
    return Result("warn", _ascii(f"OS jobs: {len(bad)} of {len(table)} need attention: {txt}" + (f" (+{len(bad) - 4})" if len(bad) > 4 else "")),
                  metrics, items, issue_key=core.ikey(jobs=[f"{r['name']}={r['state']}" for r in bad]))             # SPEC5: ALL of them, with their state word
