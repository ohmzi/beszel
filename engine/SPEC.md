# homelab-maint: implementation spec (read this fully before writing code)

Single-host maintenance runner for an Ubuntu 24.04 homelab (94 GiB RAM, ~72 Docker containers, Plex snap,
Ollama, ComfyUI, Immich, *arr stack, Homarr dashboard). Source lives in /home/ohmz/homelab-maint and is
INSTALLED (copied, root-owned) to /usr/local/lib/homelab-maint by `install.sh`. Python 3.12 stdlib only.

## Hard rules for everyone building this
1. NEVER mutate the live system while developing: no deleting, pruning, restarting, killing, `docker update`,
   `systemctl` changes, writes under /etc, /usr, /var/lib (except tests under a tmp dir), no installing, no `sudo`
   writes. Reading is fine (`sudo` only for reading, e.g. `sudo cat`, `sudo du`, `sudo smartctl -H`).
2. Do NOT edit files you do not own (see ownership below). `core.py`, `cli.py`, `etc/*.toml` are owned by the lead;
   if you need a change there, say so in your final report instead of editing.
3. C0 tasks are read-only by construction. C1 tasks MUST route every mutation through `ctx.act(what, target, size, fn,
   protect_names=...)` and must be a pure no-op unless `ctx.apply` is true; their dry-run output must list exactly what
   `act` would do (use `ctx.act` in dry-run too: it audits "dry-run" and returns False). C2 tasks return `Result(plan=...)`
   and never mutate unless `ctx.apply` AND an approval file exists for the plan hash (`STATE_DIR/approvals/<task>.<hash>`).
4. Fail closed: an error, a timeout, an unparsable probe, an empty selector, or a missing tool means "do nothing" (and for
   gates: "busy").
5. Never print or store secrets (alert bridge credentials, Kuma tokens, X-Fan-Token).
6. Tests: pytest, in tests/test_<module>.py, using tmp dirs. Set env (HOMELAB_MAINT_STATE/LOG/RUN/CONF) to tmp dirs BEFORE
   importing homelab_maint.core (module constants read env at import; see tests/conftest.py which you may import from).
   Mock subprocess output by monkeypatching `homelab_maint.core.sh` or the module-local `sh` import. Run
   `cd /home/ohmz/homelab-maint && python3 -m pytest tests -q`.
7. Keep every task's `Result.summary` <= 140 chars, ASCII only, human readable (it can be an SMS).

## Contract (see core.py)
```python
from ..core import task, Result, Ctx, sh, human, GIB, read_history, read_json, STATE_DIR

@task("disk_forecast", klass="C0", tier="check", title="Disk space", timeout=120)
def run(ctx: Ctx) -> Result: ...
```
`Ctx`: `ctx.opt(key, default)` reads `[tasks.<name>]` from /etc/homelab-maint/maint.toml; `ctx.state` is a persistent dict for
the task (saved automatically); `ctx.now` epoch seconds; `ctx.apply`; `ctx.is_protected(*names)`; `ctx.act(...)`;
`ctx.freed` bytes. `core.read_history(since_s, kind)` reads STATE_DIR/history.jsonl; `core.append_history({"t":ts,"kind":...})`.
`Result(status in ok|info|warn|crit|skipped|error, summary, metrics{small scalars}, items[{...}] (<=12 rows),
reclaimed_bytes, plan, alert)`. Use `alert=False` for informational dashboard-only findings. Status "info" never pages.

Metrics must be small JSON scalars/lists (they are shipped to the browser every poll by Homarr customJsx widgets).
Pre-compute everything a widget shows: human sizes ("123.4 GiB"), colour words ("ok"|"warn"|"crit"|"info"), percentages,
ages in minutes. Homarr's customJsx has NO Date, no icons, 10,000-char template cap.

## Modules, owners and task names

| Module (file you own)                        | Tasks / contents |
|----------------------------------------------|------------------|
| tasks/checks_basic.py + tests/test_checks_basic.py | C0/check: `disk_forecast`, `failed_units`, `backup_freshness`, `docker_df`, `memory_health`, `plex_media_mount_check` |
| tasks/checks_health.py + tests/test_checks_health.py | C0/check: `smart_trend`, `alert_path_health`, `growth_watch`, `config_drift` (config_drift is tier weekly) |
| tasks/guard.py + tasks/gates.py + tests/test_guard.py | C0/check: `spike_sampler`, `stuck_detector`, `orphan_report`, `image_ledger`; gates API |
| tasks/cleaners.py + tests/test_cleaners.py   | C1/daily: `docker_cache`, `docker_images`, `apt_clean`, `snap_revisions`, `retention`, `trash`, `gradle_reaper`, `caps`; C2/weekly: `c2_candidates` |
| homelab_maint/payloads.py, server.py, widgets/*, tests/test_payloads.py | JSON served on 127.0.0.1:9111 and the Homarr widget definitions |
| install.sh, systemd/*.service|*.timer, README.md, tests/conftest.py (create it) | packaging |

### disk_forecast (check)
Per mount in `watch` (alerting) and `info_only` (dashboard only, never pages): used/free/percent from `os.statvfs` or `df -B1`
(skip mounts that are not mounted; ext4 reserved blocks: report free as `f_bavail` the way `df` does). Every run append a
history record `{"t":..,"kind":"disk","mount":..,"free":bytes}` for watch mounts (done inside the task via core.append_history).
Trend = least-squares slope of free bytes over `trend_window_hours` (need >= 6 samples spanning >= 6 h; else no forecast) =>
`days_until_full`. warn/crit on percent free or days. metrics: `{"mounts":[{"mount","free","free_h","used_pct","days","level"}...],
"root_free_h","root_days"}`; items: worst 12 mounts.

### failed_units (check)
`systemctl --failed --no-legend --plain` (system) and `systemctl --user` is NOT available to root: skip user units. Units
failed > 30 min => warn (known `nginx.service`, `plexmediaserver.service` ones are reported with a note that the apt Plex unit
can steal port 32400 when the snap is down). `docker ps -a --filter status=exited` containers not in
`expected_stopped_containers` => warn; containers `unhealthy` => warn; containers restarting (RestartCount grew since last run,
store in ctx.state) => warn. metrics: counts.

### backup_freshness (check)
Read `/var/log/backup/*-status.json` (keys: `result`, `finished` or similar; inspect real files first to learn the schema; do
NOT rely on the FAILED-*.txt marker files which persist forever) and the stack-backup LAST_OK file mtime. Age beyond config =>
warn, last result not ok => crit. metrics per backup: name, age_h, result.

### docker_df (check)
`docker system df --format json` (and `docker buildx du` per builder if cheap) => images/containers/volumes/build cache sizes and
reclaimable. warn on config thresholds. NEVER suggests pruning volumes. metrics: sizes in GiB floats + human strings.

### memory_health (check)
/proc/pressure/{memory,io,cpu} avg10/60/300 (some+full), /proc/meminfo (MemAvailable, Cached, SReclaimable, SwapTotal/Free),
vmstat swap-in/out pages/s (two samples of /proc/vmstat pswpin/pswpout 5 s apart, or ctx.state delta), oom_kill from
/proc/vmstat. Classify: crit if memory PSI full avg60 >= crit OR MemAvailable < crit; warn on warn thresholds or sustained
swap-in. Big page cache and "swap used" ALONE are NOT problems (say so in the summary: "cache is reclaimable"). metrics: `mem_available_h,
swap_used_h, psi_mem_full60, psi_io_some60, swap_in_pps, oom_kills_total, oom_kills_delta`.

### plex_media_mount_check (check)
The Plex Media dir `mount_point` must be a mountpoint whose source is under `expected_source_prefix` (use /proc/self/mountinfo).
If not mounted while the SSD path exists => crit ("Plex would regenerate Media on the root disk"). If Plex is not yet migrated (no
bind mount and no `/media/SandiskSSD/plex`) => info. metrics: mounted(bool), source, media_size_root_gib (only `stat`, no du).

### smart_trend (check)
smartd leaves world-readable attribute logs in `attrlog_dir` (`attrlog.*.csv`, semicolon-separated "timestamp;id;norm;raw;..."
pairs) and `smartd.*.state`. Compare the latest values with the value 7 days ago: any increase in attrs 5/197/198 (raw) => warn,
and a CRC (199) increase => warn. NVMe: read `smartctl`-free data only if available without root (else use
`/sys/class/hwmon` temps); the stock smartd 55 C temperature trigger is noise, use `nvme_temp_warn_c`. Do not call smartctl as
non-root. metrics: per-device {dev, temp_c, realloc, pending, crc, power_on_h}.

### alert_path_health (check)
Meta-monitor: parse `/var/log/smart-alert.log`; any "ALERT SEND FAILED" in the last 24 h => warn (include the error text that
follows `rc=`); check `/var/log/homelab-maint/audit.jsonl` for notify failures; check that the alert bridge file exists and is
executable. This task must not itself send anything.

### growth_watch (check)
For each configured path, record `{"t","kind":"size","path","bytes"}` using a cheap size measure (for directories of files:
sum of `st_size` over an `os.scandir` walk with a 20 s budget; cache the last value in ctx.state if the walk is cut off).
Rate = (latest - value ~24 h ago) per day from history; > `warn_gib_per_day` => warn with the path. (This would have caught the
Kavita 1 GiB/day debug-log runaway.)

### config_drift (weekly, C0, alert=False => info)
Read-only comparison against agreed policy: /etc/systemd/journald.conf.d/10-homelab.conf with SystemMaxUse; root crontab lines
still containing `journalctl --vacuum` / `find /tmp` / `logrotate` (use `crontab -l -u root` only if running as root, else skip);
`snap get system refresh.retain`; `/etc/docker/daemon.json` has log-opts; tmpfiles /tmp age; reserved blocks via `tune2fs -l` only
if root. Output items describing each drift; status "info".

### spike_sampler (check)
Every run append ONE compact history record `{"t","kind":"sample","host":{...},"c":{<container>:{"anon","file","swap","cur","peak",
"cpu_us","io_b","oom","pressure_full60"}},"p":[top 8 non-container processes by anon: [name,pid,anon_kb]]}` using cgroup v2 files under
`/sys/fs/cgroup/system.slice/docker-<fullid>.scope/` (memory.current, memory.peak, memory.stat anon/file, memory.swap.current,
memory.events oom_kill, cpu.stat usage_usec, io.stat rbytes+wbytes, memory.pressure) for every running container (names via
`docker ps --no-trunc --format '{{.ID}} {{.Names}}'`), plus host MemAvailable/PSI and GPU util/vram from `nvidia-smi
--query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits`. Keep `keep_days` of history. Writing the sample is NOT a
mutation of the host (it is the tool's own state). metrics: container count, total anon GiB, largest 3.

### stuck_detector (check)
Using the last `min_samples` samples: a container is a CANDIDATE only if ALL hold: anon+swap >= `min_anon_gib`; not protected-by-
name is NOT required for reporting (report protected ones too, flagged `protected: true`), but protected ones are never actions;
no progress over the window: cpu_us delta < 1 % of one core AND io delta < 1 MiB/min AND (if known via gates) its app queue idle;
AND either memory is growing (> `growth_gib_per_hour` of anon+swap) = "leak/runaway" or anon is large and flat = "idle but holding".
Also flag "real pressure" from memory_health numbers. Output a candidates table with reason. `enforce` is false: NEVER act in this
build; implement the restart path (docker restart with exponential backoff recorded in ctx.state, at most 2 per 6 h, only when
real pressure AND candidate AND not protected AND not busy) behind `ctx.opt("enforce") is True and ctx.apply` for later, and test it
with mocks. status warn only for candidates that are not protected; protected/legit-busy ones are `info`.

### orphan_report (check)
Report (alert=False unless huge): idle Gradle/Kotlin daemons (no CPU for `idle_hours`, from samples of /proc/<pid>/stat), headless
emulator `qemu-system-x86_64 -avd` whose parent chain is gone, stray test servers (`python3 slow.py`), zombies, orphan
`crashpad_handler`. Report only; never kill (the `gradle_reaper` cleaner owns that).

### image_ledger (check)
Every run: record each image ID referenced by any container (running or exited) with last-seen timestamp in
STATE_DIR/ledger/images.json (this is the tool's own state). Prune of images by `docker_images` consults it.

### gates (tasks/gates.py)
`busy(name) -> tuple[bool, str]` for names: comfyui, ollama, plex, immich, docker_build, backup, apt, gradle, any. Every probe error
=> busy. Probes use the URLs/patterns from protected.toml `[busy]`. `cli_gate(name) -> int` implements `homelab-maint gate NAME`
(exit 0 = proceed, 1 = skip): if busy, increment a deferral record in STATE_DIR/gates.json and exit 1 unless deferred for longer than
`max_defer_hours[name]`, then exit 0 (log it). Names usable by systemd: `immich-recycle` (Immich recycle every 2 h must not interrupt
active Immich jobs: busy if immich_server/immich_machine_learning cgroup cpu.stat shows > immich_cpu_busy_pct of a core over 10 s, or
ML/Postgres show active jobs, or backup units are active), `comfyui`, `ollama`, `plex`.

### Cleaners (C1, daily). All obey the contract. Default config is mode=report.
* `docker_cache`: if build cache (default builder + others from `docker buildx ls`) > `high_gib`, prune LRU down to `low_gib` with
  `docker builder prune -f --max-used-space <low>` / `docker buildx prune --builder X -f --max-used-space`; skip when `gates.busy("docker_build")`.
  Measure before/after for `reclaimed_bytes`. Never touches volumes.
* `docker_images`: candidates = images NOT used by any container (running or exited), not in the ledger within `unused_days`, not
  tagged with a protected name, dangling or not; remove with `docker image rm <id>` (no `-f`), max items/bytes via caps. Never `docker system prune`.
* `apt_clean`: `apt-get clean` only when no apt/dpkg lock; report package-cache size. Never autoremove (that is C2 info).
* `snap_revisions`: `snap set system refresh.retain=<retain>` if different (idempotent) and remove disabled revisions
  (`snap list --all`), skipping while `snap changes` shows an in-progress change; never removes the active revision.
* `retention`: rules from config: `{name,path,glob,max_age_days|keep_newest,files_only}`; every path must resolve (realpath) inside
  `allowed_roots` and must not be protected; symlinks are never followed; mtime-based; keep the newest `keep_newest`; a rule with a
  missing/empty selector selects nothing; skip files modified in the last 10 minutes; report matched count/bytes per rule.
* `trash`: `~/.local/share/Trash/{files,info}` entries older than `max_age_days` by deletion date for configured users.
* `gradle_reaper`: Gradle/Kotlin compile daemons (cmdline contains `GradleDaemon` or `kotlin-compiler-embeddable`/`KotlinCompileDaemon`)
  idle (no CPU growth over `idle_minutes` using successive samples kept in ctx.state) AND no `Gradle Test Executor`/`Gradle Worker`
  children AND no attached `gradle`/`gradlew` client process => SIGTERM (then no SIGKILL unless still alive after 30 s). Never touch the
  daemon of a build that is running.
* `caps`: idempotent memory ceilings via `docker update --memory <ceiling>g --memory-swap <ceiling*(1+ratio)>g <name>` for containers
  in `ceilings` that are running and whose current HostConfig.Memory differs; skip containers that already have a stricter limit.
  The ceiling must be >= 1.25x the container's observed memory.peak anon; if not, refuse and report. Not applied to protected DBs.
* `c2_candidates` (weekly, C2): for each configured candidate that exists: size (use `du -sxb` with a 120 s budget, else
  `stat`), last-modified, in-use check (any running container mount or open file via `fuser`/`lsof +D` skipped if slow), `why`,
  `needs_manual_check`, the exact manual command that would archive/remove it. `Result.plan = {"items":[...sorted by name...],
  "total_bytes":N}` (stable ordering => stable hash). Apply (only with approval file for the hash): rsync to `archive_to`
  when given then remove; otherwise remove; refuse `needs_manual_check` items unless `ctx.opt("allow_manual_check_items")`.

## payloads.py / server.py / widgets
`payloads.py`: pure functions of the status dict (STATE_DIR/status.json) plus `now`: `overview()`, `disk()`, `jobs()`, `guard()`,
`reclaim()`; each returns a SMALL dict (< 4 KB) with precomputed strings and colour words, plus `age_min` and `stale` (true if
`generated_at` older than 45 min). `server.py`: `http.server.ThreadingHTTPServer` on 127.0.0.1:`www_port` (9111), GET only,
routes `/overview /disk /jobs /guard /reclaim /status` (the last returns the whole status.json minus plans), always HTTP 200 with
`{"error":...,"stale":true}` on trouble (Homarr widgets show a red triangle on non-200), no directory serving, 405 for other verbs.
`widgets/`: Homarr custom-widget definitions as customJsx templates (see the Thermals template conventions in
widgets/CONVENTIONS.md written by the lead): `ops-overview`, `ops-disk`, `ops-jobs`, `ops-guard`, `ops-reclaim`; each as a JSON file
importable via Homarr (Manage > Custom widgets > import): `{name,description,url,authType:"none",method:"GET",displayType:"customJsx",
displayConfig:{type:"customJsx",template:...}}` (inspect `custom_widget_definition` columns in a COPY of the Homarr DB at
/tmp/claude-1000/-home-ohmz-StudioProjects/a892927f-f870-4100-8cb1-f2b3f8ad4df8/scratchpad/homarr-copy.sqlite and the existing Thermals
definition for exact shape). Templates must be <= 9,500 chars, avoid the forbidden words (constructor, __proto__, prototype, eval, Function,
import (, require, globalThis, window, document, fetch, XMLHttpRequest), use only whitelisted components (Stack, Group, Text, Badge,
ColorSwatch, Progress, Table/Thead/Tbody/Tr/Th/Td, Divider, Tooltip, SimpleGrid, Card, Paper, RingProgress, ScrollArea, Sparkline),
and render sensibly when fields are missing. Validate templates with the same JsxParser configuration the fork uses (the lead has a
Node harness description in widgets/CONVENTIONS.md) or at minimum a static check.

## Packaging
`install.sh` (idempotent, run by the lead as root): copies `homelab_maint/` to /usr/local/lib/homelab-maint (root:root 0755), the
entry script to /usr/local/sbin/homelab-maint, config files into /etc/homelab-maint ONLY IF ABSENT, creates /var/lib/homelab-maint and
/var/log/homelab-maint, installs systemd units, `daemon-reload`, and enables only: `homelab-maint-check.timer` (every 15 min,
RandomizedDelaySec=60, Nice=10, IOSchedulingClass=idle), `homelab-maint-daily.timer` (daily 07:30 +RandomizedDelay 20 min, Persistent),
`homelab-maint-weekly.timer` (Wed 07:45, Persistent), `homelab-maint-www.service` (hardened: User=nobody-style DynamicUser=yes,
ProtectSystem=strict, ReadOnlyPaths=/var/lib/homelab-maint, PrivateTmp, NoNewPrivileges, IPAddressAllow=localhost). The services run
`homelab-maint run --tier X` with `--apply` only on daily/weekly (config decides per task; default report). Provide an
`uninstall.sh`. README.md: layout, commands, how to enable apply per task, how to add a task, kill switch, how Homarr wiring works.
