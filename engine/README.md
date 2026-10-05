# homelab-maint

One maintenance umbrella for one Ubuntu 24.04 homelab host. It watches disk, memory, backups, SMART, Docker, the apps and the
alert path, manages load spikes, cleans up what is safe to clean, schedules and reports the host's other maintenance jobs, and
publishes what it knows to Homarr widgets and a maintenance website. Python 3.12 standard library only; no pip.

It can act on its own and still not hurt anything:

| Class | Tasks do | Mutates? |
|-------|----------|----------|
| C0 | checks, samplers, reports | never; the runner forces apply off |
| C1 | safe cleaners (caches, old logs, unused images), spike-ladder rungs | only when the task is set to `mode = "apply"` and every action passes `ctx.act` |
| C2 | plans for risky cleanups | only through `homelab-maint approve TASK HASH` for a plan you reviewed |

Every mutation goes through `Ctx.act`, which refuses when the kill switch is present, when a per-run cap would be exceeded, when
the target matches `protected.toml`, or when the selector is empty. Each attempt, allowed or refused, is appended to
`/var/log/homelab-maint/audit.jsonl` and syslog. Every cleaner ships in `report` mode (see "Turning cleanup on").

## One umbrella

Nothing else on the host should schedule maintenance, watch a service or send a maintenance message. The pieces:

| One of | Is | Where |
|--------|----|-------|
| scheduler | the 1-minute tick starts what is due: the probes, the routine and, after a cutover, the legacy jobs | `homelab-maint-tick.timer`, `etc/jobs.toml` |
| runner | the tier runs (`check` every 15 min, `daily`, `weekly`) and the routine's steps | `homelab-maint-{check,daily,weekly}.timer`, `etc/routine.toml` |
| state | `status.json`, `history.jsonl`, the audit log, the change log | `/var/lib/homelab-maint`, `/var/log/homelab-maint` |
| monitoring plane | every service, container, endpoint and status file, debounced | `etc/probes.toml` |
| notification path | alerts, recoveries, digests, reports, through the existing Hermes SMS and Gmail transports | `etc/notify.toml`, `notify.py` |
| dashboard | the OhmzMaintainer beszel hub: read-only view fed by the public export; its only write is a signed acknowledge request | `supplemental/systemd/`, `/var/lib/homelab-maint/public/`, `ack/inbox` |
| rules registry | the one place that defines what the script does (checks, thresholds, cleanup rules, protections, routes, schedules, probes, jobs, limits) | `/etc/homelab-maint/rules.d/`, `homelab-maint rules` |
| acknowledgements | "I know about this exact error, stay quiet for 90 days": an e-mail button or the logged-in website | `acks.py`, `acks_auth.py`, `ack.toml`, `ack/` |

Rules that follow from it:

* There is no separate routine timer. The routine is a job of the tick (`routine-run` in `jobs.toml`), which also opens the
  monthly window and retries steps a busy gate deferred. The check, daily and weekly timers stay as they are for now; the tick
  only shows them (`tier-*` jobs in `observe` mode) and `docs/MIGRATION.md` is where they would be folded in.
* Adding a thing means adding a table to a TOML file (a job, a probe, a notification route, a routine step) or one small Python
  file, never a new timer or cron line (`docs/EXTENDING.md`).
* Critical legacy jobs (the backups) keep their proven scripts and are scheduled, gated, logged, alerted on and reported by the
  umbrella. Small scripts were ported to native tasks with parity tests. Anything superseded is retired in place: the unit is
  disabled and the script moved, never deleted, and each step is reversible (see "Legacy migration").
* The distro's own timers (apt-daily, unattended-upgrades, logrotate, fstrim, snapd refresh) are observed by the `os_jobs` check,
  not replaced, so security updates keep working.

## Layout

Source tree (this repository):

```
homelab-maint            entry script, installed as /usr/local/sbin/homelab-maint
homelab_maint/           python package: core.py (contract), cli.py, scheduler.py, jobs.py, probes.py, notify.py, routine.py,
                         incidents.py, reports.py, publish.py, metrics_ring.py, live.py, legacy.py, server.py, tasks/*.py, data/
etc/*.toml               config shipped as the defaults: maint.toml, protected.toml, classes.toml, routine.toml, jobs.toml,
                         probes.toml, notify.toml, ack.toml, playbooks.toml (overrides template), legacy-retirement.toml
etc/rules.d/             the rules registry's release files (00-baseline-invariants.toml: the safety floor; the rules follow)
systemd/                 units and timers, plus dropins/<unit>.d/*.conf
widgets/                 Homarr custom-widget JSON (ops-overview, ops-disk, ops-jobs, ops-guard, ops-reclaim, ops-thermals, ops-load)
docs/                    EXTENDING.md (add things), MIGRATION.md (retire legacy things)
install.sh uninstall.sh  idempotent installer and remover
tests/                   pytest, one test_<module>.py per module
```

Installed layout:

| Path | Contents | Owner/mode |
|------|----------|------------|
| `/usr/local/lib/homelab-maint/homelab_maint/` | the package (replaced as a whole on upgrade) | root, 0755 |
| `/usr/local/sbin/homelab-maint` | entry script | root, 0755 |
| `/etc/homelab-maint/*.toml` | your config, installed only if absent; never overwritten (a changed default is saved as `NAME.dist`) | root, 0644 |
| `/etc/homelab-maint/legacy-retirement.toml` | release data, replaced on every install; your own items go in `legacy-retirement.d/*.toml` | root, 0644 |
| `/etc/homelab-maint/{plugins.d,probes.d,legacy-retirement.d}/` | your extensions (tasks, probes, legacy items); must stay root-owned and not group/world-writable or they are ignored | root, 0755 |
| `/etc/homelab-maint/rules.d/` | the rules registry (see "The rules registry"): installed once the release ships rules; `00-baseline-invariants.toml` is release data (always replaced), the other files are yours after the first install | root, 0755, files 0644 |
| `/etc/homelab-maint/kuma.toml` | optional Uptime Kuma push tokens (`[push]` table); never shipped, create by hand | root, 0600 |
| `/etc/homelab-maint/PAUSE`, `PAUSE.<task>`, `FREEZE` | kill switch files, freeze file | root |
| `/var/lib/homelab-maint/` | `status.json` (world-readable), `history.jsonl`, `alerts.json`, `gates.json`, `sched.json`, `job-modes.json`, `metrics-ring.json`, `live-history.json`, `changes.jsonl`, `spikes.jsonl`, `incidents.jsonl`, `migration.json`, `tasks/`, `ledger/`, `approvals/`, `jobruns/` | root, 0755 dir |
| `/var/lib/homelab-maint/public/` (+ `reports/`) | the public JSON export the website reads (`overview.json`, `live.json`, `self.json`, `rules.json`, `manifest.json`, `acks.json`, `reports/<id>.json`, ...), files 0644 | root, 0755 |
| `/var/lib/homelab-maint/ack/` | the acknowledge postbox, see "Acknowledging a known issue": `inbox/` (1730 root:10001, a group-10001 writer may only create files in it), `inbox/rejected/` (0700), `web.key` (0640 root:10001, the HMAC key), `bootstrap.secret` (0600 root, first-run login; retired flow), `tokens.json` (0644, hashes only, written by publish), `auth.json` (0640 root:10001, the old site's passphrase hash, written by the runner; retired flow), `web_ready` (empty, the switch of the e-mail button) | root:10001, 0750 |
| `/var/lib/homelab-maint/acks.json`, `acks.jsonl` | the acknowledged issues and their audit trail | root, 0600 |
| `/var/lib/homelab-maint/rules/` | `current.json`, `history.jsonl`, `baseline.json`, `snapshots/<hash>/` (kept 20, for `rules rollback`), `orig/` (the originals of every adopted config file) | root |
| `/var/log/homelab-maint/audit.jsonl`, `jobs/<job>/` | one JSON line per mutation attempt; the output of every job run (secrets scrubbed) | root |
| `/etc/systemd/system/homelab-maint-*` | units from `systemd/` | root, 0644 |
| `/etc/systemd/system/immich-server-recycle.service.d/10-homelab-gate.conf` | the Immich recycle gate drop-in | root, 0644 |

## Install, upgrade, uninstall

```
sudo ./install.sh --dry-run      # preview; needs no root, writes nothing
sudo ./install.sh                # install or upgrade
sudo ./install.sh --first-check  # same, then run the check tier once in the background
sudo ./install.sh --adopt-rules  # same, then adopt the rules registry (see "The rules registry"); never implied
sudo ./uninstall.sh              # remove; keeps config, state and logs
sudo ./uninstall.sh --purge --yes
```

`install.sh` refuses to run unless root (except `--dry-run`), runs `set -euo pipefail`, and prints one line per thing it did
(`installed`, `same`, `kept`, `skipped`, `retired`, `enable`, `start`). Running it twice changes nothing the second time.
Specifically:

* The package is compiled and then imported (every module and every task module, the set the runner, the tick and the daemons
  load) in a temporary copy first. A syntax error or a module-level `NameError`/`ImportError` aborts before anything live is
  touched; otherwise it is swapped in with `rename(2)`. It is only replaced when its content differs.
* Config under `/etc/homelab-maint` is installed only if absent. When you already have one and the shipped default differs, the
  shipped copy is saved next to it as `maint.toml.dist`, and the tables or named entries your copy lacks are listed; merge them by
  hand (`diff /etc/homelab-maint/maint.toml /etc/homelab-maint/maint.toml.dist`). `kuma.toml` is never installed (it holds tokens).
* It creates `/var/lib/homelab-maint/public` and `public/reports` (0755) and never deletes and re-creates them: the dashboard's agent
  bind-mounts that directory read-only, and a bind mount follows the directory's inode.
* It creates the acknowledge postbox `/var/lib/homelab-maint/ack/` (0750, root:**10001**, the same as `homelab-maint ack init --group
  10001`) with `inbox/` (1730, root, group 10001: the dashboard's web gid, given as a number because no such group exists on the
  host; a process in that group may create a request file there but not list or read the others), `inbox/rejected/` (0700), `web.key` (0640,
  root:10001: 64 hex characters, the HMAC key both sides sign requests with) and `bootstrap.secret` (0600, root: the first-run login
  secret, 43 characters). Both secrets are generated **only when absent**, never overwritten and never printed. Their mode and group
  are put back if someone changed them (a 0755 `ack/` of an older install is closed to 0750). The gid is `HM_WEB_GID` if set, else the
  group `web.key` already has, else 10001; a process that cannot `chgrp` to it (a staged run in a user namespace) falls back to group
  root, closed, and warns on a real host.
* It installs the shipped config only if absent (`ack.toml` included). It installs `rules.d/` only when the release ships rules
  (a registry made of the safety baseline alone would compile to a config without protections and be refused): root-owned 0755,
  files 0644, `00-baseline-invariants.toml` always replaced (it mirrors the floor pinned in the code; a stale copy blocks every
  sync), every other file only if absent. It never runs `rules sync` by itself: it prints `sudo homelab-maint rules sync --adopt`,
  and runs it only with `--adopt-rules`.
* It enables and starts `homelab-maint-check.timer`, `-daily.timer`, `-weekly.timer`, `-metrics.timer`, `-tick.timer`,
  `-selfhealth.timer`, `homelab-maint-www.service` and `homelab-maint-live.service`, and nothing else (no legacy unit, no
  `docker-prune.timer`). It never starts a tier, metrics, self-health or tick service itself; their timers do. `--no-start`
  enables without starting. `--first-check` starts the check service once (the same run its timer does). The two daemons are
  restarted when code or a unit changed.
* It never touches a legacy unit. It also never puts back what `homelab-maint migrate cutover` retired: a timer or drop-in listed
  by `homelab-maint migrate retired` is skipped (`retired` in the output).
* It installs the Immich gate drop-in only if `immich-server-recycle.service` exists, and never edits that unit.
* `install.sh` no longer deploys a website. The old `--deploy-web` / `--no-web-ready` flags built and (re)created the
  `maintenance-web` Docker container from `web/`; that container is retired (nothing listens on 8098 and `web/` is gone). The
  dashboard is the OhmzMaintainer beszel hub (`beszel-hub.service` with its data collector `beszel-agent.service`, on
  `127.0.0.1:8088`), deployed separately: see `../supplemental/systemd/README.md`. The agent (running as root) mounts
  `/var/lib/homelab-maint/public/` read-only and writes signed acknowledge requests into `ack/inbox`; `ack/web_ready`, the switch of
  the e-mail Acknowledge button, is created by that deploy once the site answers, or by hand
  (`sudo touch /var/lib/homelab-maint/ack/web_ready`). The Cloudflare tunnel and Access steps stay manual. The whole first install, in
  order, is the runbook in `docs/INTEGRATION.md`.
* At the end it prints which tasks have `mode = "apply"` (the ones that can mutate when a timer fires) and runs
  `homelab-maint doctor`. On a fresh install doctor shows FAIL for the sampler, the live monitor and the tick until their first
  run, under a minute later; run it again.

`uninstall.sh` stops and removes every `homelab-maint-*` unit and the program files. It refuses (unless `--force`):

* while a tier run is in progress (a oneshot unit is `activating` for the whole run, so `ActiveState` is checked, not
  `is-active`; an unreadable state counts as running; `--force` stops the run with SIGTERM, which loses that run's status);
* while a job the scheduler tick started is still alive (a backup runs detached from every unit);
* while a cutover has retired a legacy timer, drop-in or script (removing the umbrella would leave that job running nowhere:
  `homelab-maint migrate rollback ITEM --apply` first).

Removing the drop-in means the Immich recycle timer goes back to restarting `immich_server` without checking whether Immich is
busy. Without `--purge`, config and state stay: the acknowledgements, the `ack/` keys and the rules registry with its history and
the originals of every adopted file are inside them. `--purge` deletes config, state and the audit log and needs `--yes`. The
`maintenance-web` container that used to bind-mount those directories is retired; the dashboard's agent reads `public/` read-only
and keeps nothing in the state directory, so there is no container to stop first (restart or redeploy the dashboard if it holds a
stale mount; see `../supplemental/systemd/README.md`).

For tests and image builds, `HM_ROOT=/some/dir ./install.sh` stages everything under that prefix and skips systemd.

## What runs

| Unit | When | Runs |
|------|------|------|
| `homelab-maint-check.timer` | 3 min after boot, then every 15 min (+ up to 60 s jitter) | `run --tier check --apply`: health checks, sensors, the spike ladder |
| `homelab-maint-daily.timer` | 07:30 daily (+ up to 20 min), catches up after downtime | `run --tier daily --apply` |
| `homelab-maint-weekly.timer` | Wednesday 07:45, catches up after downtime | `run --tier weekly --apply` (waits for the daily run) |
| `homelab-maint-tick.timer` | every minute | `tick`: starts what is due (jobs below), reaps what finished |
| `homelab-maint-metrics.timer` | every minute | a sampler: temperatures, fans, load into the 7-day ring `metrics-ring.json` (sandboxed) |
| `homelab-maint-selfhealth.timer` | every minute | `homelab-maint-selfhealth.service` rewrites `public/self.json`: is the monitoring pipeline itself healthy (sandboxed). Its own timer, NOT a tick job: it is the dead-man of the tick |
| `homelab-maint-live.service` | always on | the 5 s live monitor: writes `public/live.json` (sandboxed, 10 % CPU cap) |
| `homelab-maint-www.service` | always on | read-only JSON on `127.0.0.1:9111` for the Homarr widgets (dynamic user, sandboxed, loopback only) |

`--apply` on the check, daily and weekly services only permits mutation. Whether a task actually mutates is decided per task by its
`mode` in `maint.toml`, and `homelab-maint pause` or a freeze overrides both. On the check tier that means: the checks cannot
mutate, `comfyui_idle_reclaim` and `immich_recycle` are `report`, and the one thing that acts is the spike ladder's reclaim rung.

What the tick runs (`homelab-maint schedule` prints the whole list with next runs; `etc/jobs.toml` is where it is declared):

* `probes-run` every minute: the monitoring plane (`etc/probes.toml`). It pages through `notify.py` once a probe is confirmed down.
* The tick itself, every minute, right after it started the due jobs: syncs the rules registry when `rules.d` changed (before it reads
  `jobs.toml`) and applies what the website queued in `ack/inbox`, ends acknowledgements that ran out and sends the expiry notices.
  An idle minute is one directory listing; none of it restarts or deletes anything, and it is not a job (a job would run it twice).
* `routine-run` every 15 minutes: the routine (`etc/routine.toml`): which maintenance steps may run when, in what order, with
  windows, freeze windows (evenings), canary caps, post-checks and a change log. The monthly window has no timer of its own.
* Every legacy job (backups, Hermes watchdog and canary, docker-prune, ...) is declared with its real command, but ships in
  `observe` mode: the legacy unit or cron line still runs it, the tick only shows it. A job becomes `managed` at its cutover.

## Commands

```
homelab-maint run --tier check|daily|weekly|monthly [--task NAME] [--dry-run | --apply]
homelab-maint status              last result of every task
homelab-maint plan [TASK]         C2 plans, with the hash you must approve
homelab-maint approve TASK HASH   apply a C2 plan (re-plans first; the hash must still match)
homelab-maint pause [TASK]        kill switch
homelab-maint resume [TASK]
homelab-maint gate NAME           exit 0 = idle, proceed; 1 = busy, skip (used by systemd ExecCondition)
homelab-maint doctor              paths, permissions, config, notification path, tick, probes, sampler, live monitor, task registry
homelab-maint tick [--dry-run]    one scheduler tick (the timer does this every minute)
homelab-maint schedule [--json]   every job, task and timer with its next run
homelab-maint job run|mode|status|validate|health|export   external jobs (run one now, switch observe/managed/retired)
homelab-maint routine plan|status|due|explain|check|run|export|note|ack|clear-halt|canary-reset   windows, freeze, change log
homelab-maint probes run|validate|export|forget            the monitoring plane
homelab-maint incidents list|show|playbook|slo|export|update   incident ledger, playbooks, SLOs
homelab-maint report daily|weekly|index [--print]          generate a report into public/reports/
homelab-maint notify send|test|route|render|export|flush|doctor   the notification path
homelab-maint notify-test [KIND] [--dry-run]               one clearly labelled TEST per notification kind
homelab-maint ack list|add|remove|process|issue-token|explain|init|validate|doctor   acknowledged known issues (see below)
homelab-maint web bootstrap   the old site's first-run login secret (retired flow; root terminal only; see "First-run web login")
homelab-maint swap [status] | relieve [--apply]   who holds the swap, whether anything waits on it, and a guarded way to empty it (see "Swap and disk readers")
homelab-maint rules list|show|check|diff|sync|history|rollback|export|migrate|explain|where|orphans   the rules registry
homelab-maint self-health [--json] [--check] [--published]   is the monitoring pipeline itself healthy (runner -> publish -> website)
homelab-maint migrate status|plan|check|cutover|rollback|journal|audit   retire legacy things (docs/MIGRATION.md)
homelab-maint publish             write the public JSON export now
homelab-maint metrics-sample      one sensor sample into the ring, then publish
homelab-maint metrics-export      print the 7-day ring as JSON
homelab-maint live [--once]       the live monitor (--once prints one sample and writes nothing)
homelab-maint serve               the Homarr widget server on 127.0.0.1:9111 (the www unit runs the same server)
homelab-maint new task|job|probe NAME      scaffold a task, job or probe from a template
homelab-maint plugins             load plugins.d as the runner would; report what was loaded or refused
homelab-maint smart-event         smartd -M exec hook (prints nothing; used after the smart-alert cutover)
```

Without `--apply`, `run` is a dry run: C1 tasks list what they would do (audit outcome `dry-run`). `run --task NAME` is a manual
override of the routine's window; a freeze, `PAUSE` and a busy gate still hold it (`--force` also overrides a freeze).


## Swap and disk readers

Both exist because a percentage alone is not a verdict. A spinning disk serving small random reads is "100% busy" at a few MiB/s, and a swap
that is full of idle pages is normal. The Live tab therefore follows Ubuntu's System Monitor: it shows what moves, and judges only by what
the kernel itself calls trouble (I/O stall and swap-in/out, both PSI/vmstat based).

* **Disk I/O tile**: Reading and Writing throughput, the busiest device as plain text, and the **top reader**, for example
  `top reader: bfs 4.8 MiB/s, orphaned 3h40m` (program name from argv[0], the container when it runs in one). It comes from `/proc/PID/io`
  deltas in the live monitor (`io_top` block of `live.json`, every 10 s, about 10 ms). Never a command line: the file is served by the website.
* **Swap tile**: state `idle | cold | active | thrashing` from swap-in/out rates and the memory stall, plus who holds it (`held by open-notebook
  3.7 GiB, Seerr 2.5 GiB`, exact, from cgroup `memory.swap.current`). Only `thrashing`, or no free swap while RAM is scarce, is coloured.
* **`stuck_scans`** (check tier, report only): a find/bfs/du/ncdu/tree/recursive grep that is orphaned, older than 20 minutes and reading
  the disk pages with the exact `kill PID` in the alert; attached scans are only listed after 3 hours. It never signals anything.
* **`swap_audit`** (check tier, report only): the same verdicts as a check, with the holders table and whether relief looks feasible.
* **`homelab-maint swap`**: the holders, the verdict and the relief plan. `relieve` is a dry run; `sudo homelab-maint swap relieve --apply`
  does `swapoff` + `swapon` of the swap area, but only when every precondition holds: enough free RAM, no protected workload working (the
  busy gates), the swap disk quiet, memory stall calm, and every holder's `memory.max` large enough to take its pages back (pages swapped in
  are charged to that cap, so a small cap would turn the swap-in into an OOM kill). It aborts and keeps the swap
  in service if available RAM or the memory stall turns bad, and always runs `swapon` again. A relief you run by hand is never read as pressure by the
  other checks or the pressure ladder: it records itself in a ledger (`swap-relief.json`) and everything that judges swap-in subtracts exactly those pages.
* **`swap_auto_relief`** (check tier, C1, **report-only until you set `mode = "apply"`** on its rule in the host registry): clears the swap by itself when
  it has hung around too long, by the standard used by systemd-oomd, Meta's oomd and proactive-reclaim work, which judge pressure and refaults and never how
  full swap is. Each holder is *cold* (not refaulting its swapped pages: idle, harmless) or *active* (in use). The swap counts as hanging around when it is at least
  80% full, quiet (no swap-in, no stall) and at least 70% cold, continuously for 2 h; the watch ends only below 60% (hysteresis). Then every precondition of the
  relief is checked again (free RAM, all busy gates, swap disk, memory stall, each holder's `memory.max`); blocked is retried and never forced (24 h blocked = a
  warning, not a page). At most one relief per 12 h, and a **circuit breaker**: if swap is back at 80% within 6 h after two reliefs in a row, the workload does not
  fit in RAM or something leaks, so it pauses for 3 days and pages you with the top holders (one page, then a daily reminder) instead of hiding it. It never kills or restarts anything; the relief runs
  as its own unit (`homelab-maint-swap-relief`, 45 min limit, runs `swapon -a` when it stops however it stops) and **only between 02:00 and 06:00 local time** (swapoff can stall a process that maps memory while its pages are read back, which the busy gates cannot see). A launch is never booked as a relief: the task reads the outcome the relief wrote, and a relief that was refused or aborted backs off and, after 3 failures, shows as a warning. `homelab-maint pause` also stops a relief that is already running (the swap stays in service), and so does setting the rule's mode back to `report` (the unit checks every few seconds). A freeze holds it too. Its safety limits are the `relief_*` options under `swap_audit`: one place for every relief, manual or automatic.
  `swap_audit` also warns if `/etc/fstab` lists swap that is not switched on (an interrupted relief: `sudo swapon -a`).

## Turning cleanup on

Everything ships as `mode = "report"` (the owner enables apply, one task at a time; the same comment heads `etc/maint.toml`). To
let a cleaner act, edit `/etc/homelab-maint/maint.toml`:

```toml
[tasks.docker_cache]
mode = "apply"        # was "report"
```

Both things must be true for a task to mutate: the run was started with `--apply` (the check, daily and weekly services and the
routine tick all pass it) and the task's `mode` is `"apply"`. So `homelab-maint run --task docker_cache --apply` by hand also
needs the config change first. Suggested order: leave a task in report mode for a few days, read what it says it would do in
`homelab-maint status` and in `audit.jsonl` (`"outcome":"dry-run"`), then flip it. Per-run limits are `max_gib_per_run` and
`max_items_per_run` (global `[caps]`, overridable per task); hitting one stops the task with a warning. The first apply run of a
cleaner is capped to 10 % by the routine's canary, and a regression in the post-check halts further disruptive steps until
`homelab-maint routine clear-halt`. C2 tasks never apply from a timer: review `homelab-maint plan TASK`, then
`homelab-maint approve TASK HASH`.

What ships on, and what each switch means:

| Setting in `maint.toml` | Ships | Effect when `apply` |
|-------------------------|-------|---------------------|
| `[tasks.pressure_response] mode` + `reclaim` | `apply` | unload idle Ollama models and free idle ComfyUI VRAM at memory or GPU pressure level 2 or more; budgeted, audited, non-destructive |
| `pressure_response` `throttle` / `restart` / `emergency` | `report` | lower cpu-shares of batch containers / restart one proven-stuck container / stop containers from `classes.toml` `emergency_stop` |
| `[tasks.qos_classes]`, `docker_cache`, `docker_images`, `apt_clean`, `snap_revisions`, `retention`, `trash`, `gradle_reaper`, `caps` | `report` | the daily cleaners |
| `openwebui_media_prune`, `docker_containers_prune`, `comfyui_idle_reclaim`, `immich_recycle` | `report` | ports of legacy scripts; their cutovers refuse until the mode is `apply` (`docs/MIGRATION.md`) |
| `[tasks.routine_rotate]` | `report` | archive old audit, change and spike records (monthly) |

Pressure thresholds, class assignments (P0 never touched ... P3 shed first), budgets and the `emergency_stop` list are in
`classes.toml`. `protected.toml` lists what is never touched by any task. A task that must act on a protected name says so
itself with `unprotect = [regex]` for that task only.

### Enabling the newer cleaners, one task at a time

All of these ship `report` and run from the routine (`etc/routine.toml`). Each removes only what an in-use proof allows and does
nothing on any doubt (an unreadable `/proc`, a docker error, a stale ledger, an untrusted clock); a refusal text names the proof that
failed. The ones that need the whole process table refuse to apply without root: the daily and weekly services are root, a manual run
as `ohmz` only reports. To enable one: read what it reports, flip `mode = "apply"` under its `[tasks.NAME]`, and watch the first run
(the routine's canary caps it to 10 %). Suggested order, safest first: `dangling_images`, `crash_dumps`, `log_compress`,
`tool_caches`, `apt_autoremove_unused`, `stale_driver_packages`, `flatpak_unused`, `stale_build_output`, then `app_cache_trim` last.

| Task (tier) | Removes | Read first | Knobs in `maint.toml` |
|-------------|---------|------------|-----------------------|
| `dangling_images` (daily) | untagged images older than 24 h that no container, running or exited, references, one `docker image rm` each | `docker images --filter dangling=true`; needs a fresh image ledger | `min_age_hours`, `local_build_days` (a local build waits 7 days) |
| `crash_dumps` (daily) | `/var/crash/*.crash` older than 2 d, coredump storage older than 7 d | `ls /var/crash` | `crash_max_age_days`, `coredump_max_age_days` |
| `log_compress` (daily) | gzips rotated logs over 50 MiB (gzip -t, length check, never an open file, never while logrotate runs) | `ls -laS /var/log` | `roots`, `min_mib`, `min_age_days` |
| `tool_caches` (daily) | npm, pip, uv, pnpm caches, thumbnails over 30 d, Gradle daemon logs over 14 d, browser caches while the browser is closed | the report's sizes | `min_mib`, `thumbs_days`, switches `npm`, `pip`, `uv`, `pnpm`, `browsers` |
| `apt_autoremove_unused` (daily) | autoremove candidates that are shared libraries no process maps or opens, outside the keep list | the listed packages and their proofs | `keep`, `never`, `allow_purge` |
| `stale_driver_packages` (daily) | apt packages of NVIDIA branches other than the loaded driver, as one set | the set; `nvidia-smi` and `cat /proc/driver/nvidia/version` | `keep_branches` |
| `flatpak_unused` (weekly) | unused runtimes, never an app | the listed refs | `installations` (default: system only) |
| `stale_build_output` (weekly) | git-ignored `target`, `.next`, `.turbo`, caches of projects idle for 90 d, referenced by no unit, script or container | the kept list with reasons | `idle_days`, `output_days`, `apply_generic` (`dist`/`build` stay report-only until true), `unprotect` |
| `app_cache_trim` (daily) | files older than 30 d in the Tunarr subtitle cache, in batches, watching the container's health | the manifest of a first apply run (`manifests/`) | `rules`, `allowed_roots`, `canary_files`, `check_every` |
| `unused_venvs`, `large_cold_files` (weekly, C2) | nothing by themselves: they only plan; `homelab-maint approve TASK HASH` archives to the cold disk, verifies the copy, then removes | `homelab-maint plan TASK` | `idle_days`, `min_gib`, `cold_days`, `allow_manual_check_items` |

`apt_cache` is the alternative to `apt_clean` (the same `apt-get clean`, with a size floor): it ships switched off
(`[tasks.apt_cache] enabled = false`); to use it set `enabled = true` there and `enabled = false` on `apt_clean`, never both. An existing
`/etc/homelab-maint/maint.toml` is not rewritten by an upgrade: its missing tables arrive as `maint.toml.dist` and every missing table
means defaults, i.e. report mode (see "Install"). Merge the ones you want. `retention` no longer carries the Tunarr and crash-dump
rules; `app_cache_trim` and `crash_dumps` own those file sets, so two cleaners never act on the same files.

### Re-enabling docker-prune safely

The legacy `docker-prune.timer` (Sunday 04:00) is left exactly as the owner set it; installing homelab-maint never enables it. It is
dangerous for one reason: its script deletes every container stopped and *created* more than a week ago, whether you stopped it on
purpose or not, then the image only that container used (for `comfyui` that is a local build nobody can rebuild). If you do re-enable
it, do it in this order:

1. `homelab-maint status | grep -E 'docker_prune_exposure|docker_prune_parity'`: `docker_prune_exposure` must be `ok`
   ("deletes nothing the natives keep"). If it names containers, `docker start NAME` them, or accept their loss knowingly. The
   check pages (SMS and e-mail) while the timer is on and something is exposed, with its own playbook.
2. `docker_prune_parity` should be green, or you know which gap it names (builder discovery is the classic one: the build cache once
   reached 46 GB while the old job reported success).
3. `sudo systemctl enable --now docker-prune.timer`. The next check run (15 minutes) re-judges the exposure.

The supported way is not to bring the script back at all: enable `docker_cache`, `docker_images`, `dangling_images` and
`docker_containers_prune` (the natives keep stopped-on-purpose containers listed in `[tasks.failed_units] expected_stopped_containers`)
and run `homelab-maint migrate check docker-prune`, then `homelab-maint migrate cutover docker-prune` (`docs/MIGRATION.md`).

## Kill switch

```
homelab-maint pause            # touch /etc/homelab-maint/PAUSE     : nothing mutates anywhere
homelab-maint pause docker_images   # PAUSE.docker_images           : that task only reports
homelab-maint resume [TASK]
touch /etc/homelab-maint/FREEZE     # no disruptive step starts until the file is removed (the routine's freeze)
```

While paused, checks keep running and the dashboard shows `paused`. The spike ladder puts back any cpu-shares throttle or
emergency-stopped container it applied. The tick starts no pausable managed job except read-only monitors; the backups are marked
`pausable = false` so the kill switch cannot silently stop one. To stop the schedule itself:
`systemctl disable --now homelab-maint-{check,daily,weekly,tick}.timer`.

## Alerting

State changes page through `notify.py`, the one notification path. It hands the message to the existing Hermes transports (SMS
carrier gateway and Gmail) run as the user in `[global] notify_handle`, renders email in the same HTML style as the backup reports,
and keeps the SMS to one ASCII segment. A problem must persist `alert_confirm_runs` runs before it pages, repeats are limited to
one per `alert_reminder_hours`, and `notify.toml` holds the routes by kind and severity, quiet hours, daily budgets and dedupe.
A task result with `alert = false` or status `info` shows on the dashboard but never pages. `homelab-maint notify-test` sends one
labelled TEST per kind. If `/etc/homelab-maint/kuma.toml` exists, each tier run also pushes a heartbeat to Uptime Kuma; Kuma
itself stays running (Homarr's uptime widget reads it). The `alert_path_health` check watches the delivery path itself.

## Immich recycle gate

`immich-server-recycle.timer` restarts `immich_server` every 2 hours. The drop-in
`systemd/dropins/immich-server-recycle.service.d/10-homelab-gate.conf` adds
`ExecCondition=/usr/local/sbin/homelab-maint gate immich-recycle`. When the gate says busy, systemd skips that run (the unit shows
as inactive, not failed) and the next tick asks again. After `max_defer_hours["immich-recycle"]` (12) of deferral it lets one
restart through. A gate error exits non-zero too, so a broken probe defers the restart instead of forcing it.

systemd reads exit codes 1 to 254 from an `ExecCondition` as "skip quietly", so a gate that crashes (exit 1) looks the same as a
busy Immich: the unit stays inactive, nothing is marked failed and no alert fires. `install.sh` refuses to ship a package that does
not import, but to check a running system, run `sudo homelab-maint gate immich-recycle; echo rc=$?` by hand. The timer and the
drop-in are retired by the `immich-server-recycle` migration item once the native `immich_recycle` task has proven itself.

## Homarr wiring

```
check tier ------> status.json -------------> homelab-maint-www on 127.0.0.1:9111 ---> Homarr custom widgets
metrics timer ---> metrics-ring.json ----------^   GET /overview /disk /jobs /guard /reclaim /status
                                                   GET /thermal /load /metrics /heartbeat
```

1. Check that the server answers: `curl -s http://127.0.0.1:9111/overview`.
2. In Homarr: Manage, Custom widgets, import each `widgets/ops-*.json`, or let `widgets/install_homarr_widgets.py` do it (always
   rehearse on a copy of the Homarr database first; its docstring has the commands).
3. Put them on a board and set the refresh interval (30 to 60 s; the status changes every 15 min, the sensor ring every minute).

The fetch happens from the Homarr container on the host network, so loopback works for both local and remote boards. Endpoints
always answer HTTP 200 (Homarr shows a red triangle otherwise); on trouble the body is `{"error": ..., "stale": true}`, and a
status older than 45 minutes is marked stale. Each endpoint stays under 4 KB (the thermal and load ones under 14 KB) because the
whole body goes to the browser on every poll. Widget templates follow `widgets/CONVENTIONS.md`. `/heartbeat` is a small
dead-man's-switch body (`"ok":true`) an Uptime Kuma keyword monitor can poll.

## The maintenance website

The public site is the OhmzMaintainer dashboard (this fork of Beszel), run on the host as `beszel-hub.service` with its data
collector `beszel-agent.service`, listening on `127.0.0.1:8088` and published at `https://maintainer.ohmzhomelab.ca` behind
Cloudflare Access.

```
runner (root) --publish--> /var/lib/homelab-maint/public/ --bind mount, read-only--> beszel-agent (root)
live monitor --live.json-->                                                                |
                       browser --HTTPS--> Cloudflare Access --tunnel--> cloudflared --> 127.0.0.1:8088
```

The runner writes the public export (`homelab-maint publish`, run after every tier run) into `public/`; the live monitor rewrites
`public/live.json` every 5 s. The hub reads that directory (through the agent, read-only) and shows live monitoring, health,
incidents, what was done, the routine, reports and capacity; it also serves the `/ack` acknowledge page (see "Acknowledging a known
issue"). It holds no docker socket, never edits a rule and writes nothing except acknowledge requests, which the agent signs into
`/var/lib/homelab-maint/ack/inbox`. Deploy and run docs for the hub and agent are in `../supplemental/systemd/README.md`; then add the
public hostname and an Access policy in the Cloudflare dashboard. Until the sampler has run, the sensor section shows "No sensor
history yet". Manual maintenance the runner cannot see can be added as JSON lines `{"ts": ..., "title": ..., "detail": ...}` to
`/var/lib/homelab-maint/maintenance-journal.jsonl`; the site lists the newest 100. The site's health is the `self_health` "website"
row (`GET 127.0.0.1:8088/api/health` plus `systemctl is-active beszel-hub.service`). The old `maintenance-web` Docker container
(`127.0.0.1:8098`, `GET /healthz`) and the `install.sh --deploy-web` flag are retired.

## Two sides: the registry and the website

The script that runs on the host and the website are decoupled on purpose. There are exactly two places to audit.

```
HOST (authoritative)                                                  DASHBOARD (read-only mirror)
/etc/homelab-maint/rules.d/*.toml   <- the registry: every rule the script follows, edited only here
        | rules sync   validate -> compile -> record the change -> "rules changed" notice
        v
generated config files (maint, routine, jobs, probes, classes, notify, ack, protected .toml)
        | read, unchanged, by the runner modules
        v
runner / tick / check / daily / weekly / live  --publish-->  /var/lib/homelab-maint/public/*.json  --bind mount :ro-->  hub  -->  browser
                                                               manifest.json  rules.json  self.json  acks.json  ...
```

* The **registry** defines what the script does: checks and thresholds, cleanup and retention rules, protections, alert routes,
  schedules, probes, jobs and safety limits. Only the host edits it. A rule that would shrink the protected list, lift a per-run cap
  above its hard limit or point a cleanup outside its allowed roots is refused, and a broken registry never replaces the last good
  config.
* The **website** displays what the script reports and which rules it ran under (`rules.json`, with when each rule last ran and fired).
  It never reads a script, never edits a rule, has no docker socket and writes nothing but acknowledge requests into `ack/inbox`.
  It also shows the health of the pipeline itself (`self.json`: runner, publish, tick, live monitor, ring, registry, ack inbox,
  alert delivery, the site). If the runner dies the page says so instead of showing old numbers as current.
* Acknowledging an alert is the one write the website can request. It is alert state, not a rule: it cannot change what the script does.

## Acknowledging a known issue

"I understand this error and I am fine with it": the exact error stops paging and stops e-mail for 90 days (1 to 365 per
acknowledgement), and stays listed as an acknowledged issue instead of a red alert.

* **The exact error** is a fingerprint (`id` in the e-mail, 16 hex characters): the task plus a stable key of its message, with
  sizes, times and counters ignored where they only move around. Per-task rules for the key are in `etc/ack.toml` (a task without a
  rule or `issue_key`, `smart_event` and `job:*` cannot be acknowledged; one policy, `ack.toml [ack]`, decides for the pager, the
  dashboard, the inbox and the CLI). `failed_units`, `disk_forecast`, `probes`, `backup_freshness`, `smart_trend`, `os_jobs` and
  `docker_prune_exposure` name the error from the FULL failing set, not from the clipped summary. A worse severity (warn to crit), a different failing set or the next
  magnitude decade is a different error and alerts again. Expiry sends one notice. Recovery e-mails of an acknowledged issue stay
  quiet. The SLO is not changed (honesty), the incident stays open as `acknowledged` and the hero colour ignores it.
* **E-mail button.** Every alert mail carries "Acknowledge for 90 days" and `https://maintainer.ohmzhomelab.ca/ack?id=ID&t=TOKEN&d=90&s=warn`.
  The link only opens a review page (a mail scanner following it changes nothing); the button on that page POSTs, the page shows
  "Received" and flips to "Applied" when the runner has processed it. The token is single-use, valid 30 days and bound to that
  issue and severity; only its sha256 is stored. The button follows the site (`notify.toml [ack] button = "auto"`): mails carry only the
  `Issue ID` line until `ack/web_ready` exists. The dashboard deploy creates it once the site answers; the manual way is
  `sudo touch /var/lib/homelab-maint/ack/web_ready` (or `button = true` in `notify.toml [ack]`). Wait for that until
  `https://maintainer.ohmzhomelab.ca/ack` really opens (tunnel and Access done), or the button would be a dead link. A worsening of the same error (a longer failing set, another decade) alerts at once
  even while the old one is acknowledged.
* **Logged in on the website** every warn card gets an Acknowledge control (days 7/30/90/365, optional note) and an
  Acknowledged-issues panel with Un-acknowledge. A crit issue gets no control at all: it must keep alerting. See "First-run web login".
* **How a request reaches the host.** The container writes a small signed JSON file into `ack/inbox` (it can create files there and
  nothing else); the tick reads it within a minute, checks the size, schema, time (+-10 min), the HMAC with `ack/web.key`,
  the token, a rate limit (50 a day) and that the issue is real and currently failing, then applies it. A refused request moves to
  `ack/inbox/rejected/` with a reason and the page reverts the card. Nothing the container sends can change a rule.
* **CLI** (root): `homelab-maint ack list`, `ack add ID|TASK [--days N] [--note TEXT] [--severity warn|crit]`, `ack remove ID`,
  `ack issue-token ID` (prints the one-time link once), `ack explain TASK` (its current fingerprint and key rule), `ack doctor`.
  Never acknowledge `self_health`: it would hide a blind monitor.

## The rules registry

`/etc/homelab-maint/rules.d/NN-category.toml` holds `[[rule]]` tables: an `id`, plain-language `why` and `does`, the config table the
rule feeds (`file`, `target`, `params`, `merge`), an optional `mode`, and flags (`destructive`, `enabled`, `severity`). The old config
files (`maint.toml`, `routine.toml`, `jobs.toml`, `probes.toml`, `classes.toml`, `notify.toml`, `ack.toml`, `protected.toml`) become
generated artifacts, with a header saying so and a `# rule: ID` comment above every table. `playbooks.toml` stays a content file.

```
homelab-maint rules check        validate: every error blocks a sync, warnings advise
homelab-maint rules diff         the registry vs the last applied one vs the generated files
sudo homelab-maint rules sync    validate, compile, apply, record (the tick does this by itself within a minute of a change)
homelab-maint rules history 5    who changed what, when (each change also sends a "Rules changed" notice)
sudo homelab-maint rules rollback [HASH]
homelab-maint rules explain TASK   which rules configure it, with the effective values
```

Day to day: edit a file in `rules.d`, run `rules check`, and the tick applies it. A change that does not validate is recorded once
and announced once; the runner keeps the last good config. The safety baseline (`00-baseline-invariants.toml`, replaced by every
install) can only be added to: the release floor is also pinned in the code, so a stale or weakened copy blocks the sync.

**First adoption** (once, when the release ships rules). The generated files replace the hand-maintained ones, so it is never silent:

1. `install.sh` installs `rules.d` and prints what to do; it does not run the sync (unless you pass `--adopt-rules`).
2. The tick runs `rules sync`. A config file whose data already equals what the registry compiles is replaced with the generated
   copy; one that differs is reported as **blocked** and left alone, never clobbered.
3. Review with `homelab-maint rules diff`, then adopt: `sudo homelab-maint rules sync --adopt`. The originals are kept in
   `/var/lib/homelab-maint/rules/orig/`, and a significant notice is sent.

Do 2 and 3 in the same sitting as the install, before the first `run --tier check`: the timers run from the moment `install.sh` ends,
and until the registry is adopted the check tier warns `rules registry not adopted yet` (an e-mail, then a text once it survives a
reminder; a config file that predates the release floor is shown as the reason). `./install.sh --dry-run` prints the same notice, and
the "Next:" lines of a real run put adoption before the first check.

`homelab-maint rules migrate [--dry-run]` builds `rules.d` from the config files you have now and proves that compiling it gives the
same data (use it after hand-editing `/etc/homelab-maint/*.toml`).

## First-run web login

The old maintenance website had its own owner login in front of the acknowledge buttons: a first-run setup protected by the
**bootstrap secret** that `install.sh` generated on the host. `sudo homelab-maint web bootstrap` printed it once, on a root terminal
(the file is `ack/bootstrap.secret`, 0600 root; the secret itself is never logged, printed by the installer or overwritten), you
chose a passphrase (an authenticator app and recovery codes optional) and the runner wrote `ack/auth.json`. **That flow is retired
with the `maintenance-web` container**: the hub that serves the site now (`beszel-hub.service`) has its own accounts (see
`../supplemental/systemd/README.md`), and no site reads `bootstrap.secret` or `auth.json` any more. The e-mail acknowledge link itself
still needs no login — its one-time token is the capability — and the signed request path through `ack/inbox` is unchanged (see
"Acknowledging a known issue"). Put Cloudflare Access in front of the site as well.

## Adding a task

1. Create `homelab_maint/tasks/<module>.py` (any file in that package is auto-imported) and register it:

   ```python
   from ..core import task, Result, Ctx

   @task("my_check", klass="C0", tier="check", title="My check", timeout=60)
   def run(ctx: Ctx) -> Result:
       return Result("ok", "all fine", metrics={"things": 3})
   ```

2. Pick the class honestly. C0 never mutates. C1 must route every change through `ctx.act(what, target, size, fn, protect_names=...)`,
   be a no-op unless `ctx.apply`, and use `ctx.act` in dry runs too so the report lists exactly what apply would do. C2 returns
   `Result(plan=...)` and applies only after approval.
3. Fail closed: an error, timeout, unparsable probe or empty selector means do nothing.
4. Keep `Result.summary` to 140 ASCII characters (it can be an SMS), metrics to small scalars.
5. Add `[tasks.my_check]` to `etc/maint.toml` (cleaners start with `mode = "report"`), a step in `etc/routine.toml` if it is a
   cleaner or plan, a `[playbook.my_check]` entry in `homelab_maint/data/playbooks.toml` (the shipped baseline; an owner-written
   task puts its playbook in `/etc/homelab-maint/playbooks.toml`), and a `tests/test_<module>.py` that sets the `HOMELAB_MAINT_*`
   env dirs to a temporary directory before importing the package (see `tests/conftest.py`).
6. Run `python3 -m pytest tests -q`, then `sudo ./install.sh`. Your edited `/etc/homelab-maint/maint.toml` is not replaced; copy the
   new section from `maint.toml.dist`.

A job (an existing command on a schedule), a probe, a notification route or a routine step is a table in `jobs.toml`, `probes.toml`,
`notify.toml` or `routine.toml`; an owner-written task can live in `/etc/homelab-maint/plugins.d/` without touching the package.
`homelab-maint new task|job|probe NAME` scaffolds each one. `docs/EXTENDING.md` has worked examples and the safety checklist.

## Legacy migration

The old timers, cron lines, scripts and notifiers are retired one at a time, each gated on proof that its replacement works and
each reversible with one command; nothing is deleted. `homelab-maint migrate status` shows every legacy item, its replacement and
its parity state; `migrate plan` prints the exact steps; `migrate audit` lists any timer or cron line no item accounts for. The
order, soak times and rollback commands are in `docs/MIGRATION.md`. Until an item is cut over its legacy unit keeps running,
untouched, and `install.sh` leaves it alone.

## Development

```
cd /home/ohmz/homelab-maint
python3 -m pytest tests -q
```

Tests use temporary directories and mocked command output; they never touch `/etc`, `/var` or systemd. `tests/test_packaging.py`
checks the installer and uninstaller (a staged install under a temporary `HM_ROOT`, a dry run against a stub `systemctl`), the unit
files (with `systemd-analyze verify` when available), the shipped config for consistency (jobs, routine steps and probes name real
things; every registered cleaner is a routine step with a `[tasks.X]` table and a playbook; only the spike ladder's reclaim rung
ships on), the acknowledge directories and secrets (modes, generated once, never overwritten), the rules.d install rules and this
README.

## Troubleshooting

* `systemctl list-timers 'homelab-maint-*'` shows the next runs; `journalctl -u homelab-maint-daily` shows the last.
  `homelab-maint schedule` shows every job and timer in one list.
* `homelab-maint doctor` checks config, the state directory, the notification path, the tick, the probes, the sampler, the live
  monitor and the kill switch.
* A tier that is still running when its timer fires skips itself (per-tier lock in `/run/homelab-maint`); a second tick that finds
  the tick lock taken exits at once.
* `status.json` missing or stale: run `homelab-maint run --tier check` by hand and read stderr.
* The sensor ring (`metrics-ring.json`, with `metrics.lock`; a corrupt one is kept as `.bad`) is filled by
  `python3 -m homelab_maint.metrics_ring sample` once a minute; `... export` prints it. A failing sampler is a failed
  `homelab-maint-metrics.service`, which the `failed_units` check reports. It stays a bare sampler and does not publish
  (`homelab-maint metrics-sample` does both, by hand): a publish costs about 0.4 s of CPU and several `systemctl` calls, which would
  run inside its 256 MB / 20 s sandbox and could cost a sample. `metrics.json` and the rest of `public/` are refreshed by each tier
  run (at most 15 minutes old, the same cadence as the health model); the Live tab (5 s) and the Homarr `/thermal` and `/load`
  widgets read the ring and `live.json` directly. After an install, check once that the GPU
  columns are filled (`homelab-maint metrics-export | python3 -c "import json,sys; print(json.load(sys.stdin)['current'])"`); if
  `gpu_temp` is null, relax the sampler unit's sandbox lines one at a time (its comments say which).
* The live tab says "Live monitor not running": `systemctl status homelab-maint-live`; its config (`[live]` in `maint.toml`) is read
  once, so restart it after editing.
* The www service sandbox cannot read `/home` or anything outside `/var/lib/homelab-maint` and `/etc/homelab-maint`; if a payload
  needs more, it belongs in the runner, not the server.
* The website's pipeline strip says the refresher stopped, or `self_health` is degraded: `homelab-maint self-health` lists every part
  with its age; `systemctl status homelab-maint-selfhealth.timer` (it rewrites `public/self.json` every minute and is deliberately not
  a tick job).
* An acknowledge click or e-mail button did nothing: `homelab-maint ack doctor` (inbox present, HMAC key usable, requests waiting more
  than 10 minutes means the tick is not running them, so look at `homelab-maint tick` and `systemctl status homelab-maint-tick.timer`); a refused request is in
  `/var/lib/homelab-maint/ack/inbox/rejected/` with its reason.
* The rules registry is `blocked`, `invalid` or has `drift`: `homelab-maint rules check`, then `rules diff`; the runner keeps the last good
  config meanwhile (`rules_registry` and `self_health` say so). A generated file edited by hand is rewritten by the next sync and the
  edited copy is kept in `/var/lib/homelab-maint/rules/orig/`.
* After `uninstall.sh --purge` and a new install the site shows stale or no data: the dashboard still held the deleted directories
  open. Restart or redeploy the hub and agent so they pick up the recreated `public/` (see `../supplemental/systemd/README.md`).
