# SPEC3: enterprise-style routine, spike management, incidents, live monitoring and reports

Builds on SPEC.md (runner contract, rules) and SPEC2.md (public export, 7-day ring, web UI). SAME HARD RULES: Python 3.12 stdlib only on the runner side;
NEVER mutate the live system while developing (no installs, no systemctl changes, nothing under /etc,/usr,/var/lib,/var/log, no touching existing containers);
write only your owned files; list glue edits (cli.py, server.py, install.sh, etc/*.toml, publish.py) under `glue`. No secrets in any public file.

## 0. Principles (from the research on how operators manage load without hurting users) and how each lands on this ONE host
1. **Detect by saturation and stall, never by size.** A big process is not a problem; a stalled one is. Signals: memory/io/cpu PSI (some/full), swap-in rate, MemAvailable,
   queue depth of the app, no-progress (cpu/io/network/GPU flat) AND growth. (Google SRE golden signals/USE, Meta oomd/PSI.)
2. **Classes of service and a fixed order of sacrifice.** P0 platform (dockerd, containerd, networking, tailscale, cloudflared, sshd, systemd) never touched. P1 interactive/serving
   (Plex playback, Immich API, Open WebUI, Homarr, Seerr, Nextcloud, Uptime Kuma, Glances, this maintenance site). P2 batch/background (Plex analysis/scans, ComfyUI jobs, Ollama
   batch, embeddings, Sonarr/Radarr scans, Kavita/BookLore scans, Kometa, goodreads pipeline, backups). P3 best-effort (builds, caches, Gradle, emulator, cleanup itself).
   Under real pressure shed P3 first, then slow P2, protect P1, never P0. (Borg priority bands, Netflix prioritised load shedding, K8s QoS.)
3. **Throttle compressible resources, only kill non-compressible ones, and only in order.** CPU/IO: weights and queues, never kills. Memory: soft limit (memory.high/reservation) first,
   hard ceiling as backstop (`docker update --memory`), kill/restart only a stuck, non-protected candidate after reclaim failed. (Borg compressible/non-compressible.)
4. **Graduated response ladder (runbook), each rung gated, budgeted and audited:** L0 normal -> L1 annotate+alert -> L2 reclaim idle (unload idle Ollama models, ComfyUI /free, drop
   nothing else) -> L3 slow P2/P3 (lower cpu-shares/blkio-weight of batch containers via `docker update`; pause nothing) -> L4 restart a proven-stuck P2/P3 candidate with exponential
   backoff and a retry budget (max 2 per 6 h) -> L5 emergency (only if host-level stall persists > N min: stop best-effort P3 containers from a configured list). Rungs L3-L5 default to
   report-only; L1-L2 are non-destructive.
5. **Admission control / bulkheads.** Per-app concurrency limits are the real spike shaper (ComfyUI single worker, Ollama NUM_PARALLEL/MAX_QUEUE/KEEP_ALIVE, Plex transcode count,
   Immich job concurrency, Open Notebook worker max tasks). A `bulkhead_check` task reports drift from the agreed values (read-only).
6. **Liveness is not progress.** Watchdog on forward progress + deadline, restart with backoff and jitter, retry budget so recovery never becomes the next overload.
7. **Error budgets / SLOs.** Define SLOs per service class (e.g. Plex up 99.5% / 30 d), compute availability and budget burn from check history, show it.
8. **Incident management.** detect -> triage -> mitigate -> resolve -> postmortem: an incident ledger with severity, timeline (links to the audit actions taken), MTTD/MTTR, playbook text
   (what to do), and an auto-generated postmortem stub for crit incidents.
9. **Change management.** Maintenance windows and freeze windows, canary caps on first apply, post-change verification, a change log, kill switch. Cleanup never runs while a gate is busy.
10. **Capacity planning.** Days-to-full per mount, growth by consumer, weekly memory baseline drift, recommendations.
11. **Reports.** Daily digest + weekly report (health, incidents, what was done, space reclaimed, spikes seen and how they were handled, capacity outlook, recommendations, what is next),
    published to the website and archived.
12. **Dry-run before enforce.** Every new enforcing rung ships report-only until reviewed; the audit trail records "would have" decisions so they can be reviewed.

## 1. Facts (in addition to SPEC2 section 1)
- Existing runner pieces to reuse, not rewrite: `core.py` (Task/Result/Ctx/act/audit/Notifier/read_history/append_history), `tasks/gates.py` (busy probes, cli_gate),
  `tasks/guard.py` (spike_sampler writes `kind:"sample"` history records with per-container anon/file/swap/cpu_us/io_b/oom/pressure, stuck_detector, image_ledger),
  `tasks/cleaners.py` (docker_cache, docker_images, apt_clean, snap_revisions, retention, trash, gradle_reaper, caps, c2_candidates), `tasks/checks_*.py`, `payloads.py`, `server.py`.
  Read them first. status.json shape is in cli.py `cmd_run`; alerts state in STATE_DIR/alerts.json (Notifier); audit in LOG_DIR/audit.jsonl; history in STATE_DIR/history.jsonl.
- Protected/class config: `etc/protected.toml` (patterns). New `etc/classes.toml` (written by the spike agent) maps container/unit name regexes to P0..P3 plus per-class defaults.
- Host apps and their control surfaces (read-only to inspect): ComfyUI `http://127.0.0.1:8188` (`/queue`, `/free` POST, `/system_stats`), Ollama `http://127.0.0.1:11434` (`/api/ps`,
  `/api/generate` with keep_alive 0 to unload, env OLLAMA_* in its systemd unit), Plex snap (`/identity`, scanner/transcoder processes), Immich (container cpu), docker CLI
  (`docker update --cpu-shares/--blkio-weight/--memory-reservation/--memory/--memory-swap`).
- Timers today: homelab-maint-check (15 min), -daily (07:30), -weekly (Wed 07:45), -metrics (1 min, SPEC2). Backups: backup-system Sat 01:00, backup-immich Sun 01:00, docker-prune Sun 04:00,
  stack-backup 03:30 daily, Plex butler ~02:00-05:00, fstrim Mon 01:26. Peak human use is evenings (18:00-23:30): freeze window for anything disruptive.

## 2. Streams, owners, files and contracts

### S1 routine engine (owner `routine`): `homelab_maint/routine.py`, `etc/routine.toml`, `tests/test_routine.py`
Declarative maintenance routine + change management. `etc/routine.toml`:
`[windows] daily="07:30-09:30", weekly="Wed 07:45-10:00", monthly="1st Sat 04:30-07:00"`, `[freeze] evenings="18:00-23:30"` (applies to disruptive steps only), `[[routine]]` entries
(name, cadence daily|weekly|monthly, window, steps = ordered list of task names or built-in steps like `verify`, `report`, `restore_check`, depends_on, canary = true/false, post_check = [check names]).
Routine content to ship: **daily** (spike_sampler review, cleanup tasks in report/apply per config, verify, daily digest), **weekly** (capacity report, SMART short self-test reminder via smartd status, C2 plan refresh,
docker image update review (read Diun state if present), apt/snap pending updates summary, backup verification, weekly report), **monthly** (restore drill checklist, certificate/expiry review, long-term trend review, config drift review, journal/audit rotation).
API: `plan(now) -> list[Step]` (what will run when, with window/freeze decisions), `due(now, state) -> list[str]`, `record_change(task, kind, detail, before, after)` appending to `STATE_DIR/changes.jsonl`
(`{"ts","task","kind":"maintenance|config|restart|cleanup","detail","bytes","outcome","verified":bool}`), `post_check(names) -> (ok, detail)` re-running named C0 tasks via registry and comparing to the pre-state,
`canary_caps(task, state)` returning reduced caps on a task's first N apply runs (default 1 run at 10% caps), `export() -> dict` for `routine.json` (below). CLI glue: `homelab-maint routine plan|status|export`.
`routine.json` (public): `{"generated_at","windows":{...},"freeze":{...},"routine":[{"name","cadence","window","steps":[{"task","class","next_due":t,"last_run":t|null,"last_outcome":str,"mode"}],"next_run":t}],
"changes":[{ts,task,kind,detail,bytes,outcome,verified}...<=100 newest first],"calendar":[{"date":"YYYY-MM-DD","items":[{"time":"07:30","title":str,"kind":"daily|weekly|monthly|backup|system"}]} x14 days]}`.
The calendar merges routine windows with the known system timers (SPEC2 schedule.json logic: you may call `publish`'s helper if present, else read `systemctl list-timers --output=json` yourself).

### S2 spike manager (owner `spike`): `homelab_maint/tasks/pressure.py`, `etc/classes.toml`, `tests/test_pressure.py`
Tasks (all `@task`):
- `pressure_state` (C0, check): computes the current pressure LEVEL 0-5 from PSI (mem some/full, io some/full, cpu some), MemAvailable, swap-in rate, load vs cores, GPU memory, using hysteresis (enter at X for 2 consecutive runs, leave at Y for 3) and
  stores the level history in ctx.state; also records `spike` events (start, peak level, duration, top contributors by class from the latest guard sample) to `STATE_DIR/spikes.jsonl` for reports ("a spike happened at 14:10, memory PSI 12%, caused by tunarr, P2, resolved by itself in 9 min, nothing was killed").
  Metrics: level, why, psi values, top_contributors[{name,class,anon_gib,cpu_pct}]. Status warn at level >= 2 sustained, crit at >= 4.
- `pressure_response` (C1, check tier, 15 min; default modes: `reclaim="apply"`, `throttle="report"`, `restart="report"`, `emergency="report"`): walks the ladder in section 0.4 for the CURRENT level using classes.toml. L2 reclaim: Ollama loaded models idle > `idle_s` (from /api/ps `expires_at`/no recent activity: probe twice) get unloaded via `POST /api/generate {"model":..,"keep_alive":0}`; ComfyUI `/free` ONLY when queue_running+queue_pending == 0. L3: `docker update --cpu-shares` / `--blkio-weight` for P2/P3 containers while pressure persists, restore to defaults when level returns to 0 (record originals in ctx.state, idempotent, revert on pause/kill switch). L4: restart ONE proven-stuck non-protected P2/P3 candidate (from stuck_detector's candidates + no busy gate) with exponential backoff and a retry budget in ctx.state. L5: stop containers from the explicit `emergency_stop` list in classes.toml (empty by default) in P3 -> P2 order, one per run. Every rung writes `ctx.act` audit records (also in report mode as "would"), respects PAUSE, protected.toml + classes (P0/P1 never), and `max_per_day`.
- `qos_classes` (C1, daily, mode report): idempotently applies baseline `cpu-shares`/`blkio-weight`/`memory-reservation` per class to running containers via `docker update` (P1 high weight, P3 low), skipping those already at target; refuses to lower anything below safe floors.
- `bulkhead_check` (C0, weekly, alert=False info): compares agreed concurrency settings with reality: ComfyUI queue/worker settings, Ollama env (`systemctl show ollama -p Environment` for NUM_PARALLEL, MAX_QUEUE, KEEP_ALIVE, MAX_LOADED_MODELS), Open Notebook `OPEN_NOTEBOOK_WORKER_MAX_TASKS`, Plex transcode limits (Preferences TranscodeCountLimit), Immich job concurrency if discoverable; emits drift items with the recommended value and why (cite the principle). Never changes anything.
`etc/classes.toml`: `[classes] P0=[regex..] P1=[..] P2=[..] P3=[..]` for the real container/unit names on this host (read `docker ps` and `systemctl` to assign; unknown => P2), `[defaults.P1] cpu_shares=2048 blkio_weight=800 ...`, `[ladder]` thresholds, `emergency_stop=[]`, `max_restarts_per_6h=2`.
Metrics contract for the web: `pressure_state.metrics` as above; `pressure_response.items` rows `{"ts","level","rung","action","target","class","outcome"}` (also appended to `STATE_DIR/pressure-log.jsonl`, last 200 exported in `pressure.json`: `{"generated_at","level","since","history":[{"t","level"}]x(last 24h 15-min), "spikes":[...last 30 events],"actions":[...last 50],"classes":[{"class","members":[names],"policy":str}]}` via `export() -> dict`).

### S3 incidents, playbooks and SLOs (owner `incidents`): `homelab_maint/incidents.py`, `etc/playbooks.toml`, `tests/test_incidents.py`
- Ledger `STATE_DIR/incidents.jsonl` (append-only events) + `incidents.json` snapshot. `update(status, history, now)` is called once per runner tick (glue in cli.cmd_run after the state lock, read-only on tasks): opens an incident when a check has been confirmed warn/crit for >= `confirm_runs` (same debounce as Notifier; read `alerts.json`), keyed by task name; escalates severity (sev3 warn, sev2 crit, sev1 crit + multiple related); resolves after confirmed recovery; records `detected_at`, `acknowledged_at` (when an alert was sent), `mitigated_at` (when an audit action touched the same task/target), `resolved_at`, MTTD/MTTR, timeline entries (state changes + related audit actions within the window), `cause_hint` and `playbook` text from `etc/playbooks.toml` (per task: what it means, first checks, safe fixes, what NOT to do, who/what to ask). Auto-generate a postmortem stub for sev1/sev2 (summary, impact, timeline, detection gap, what went well/badly, follow-ups) as Markdown text inside the record.
  Also group correlated incidents (several checks failing within 10 min) under one parent.
- `incidents.json` (public): `{"generated_at","open":[{id,task,title,severity,since,duration_s,summary,playbook,timeline:[<=20],related_actions:[<=10]}],"recent":[<=50 resolved with mttr_s,postmortem_md (truncated 3 KB)],"stats":{"mttd_s_30d","mttr_s_30d","incidents_30d","open_count"}}`.
- SLOs `slo.json`: `{"generated_at","window_days":30,"objectives":[{"name","target_pct","class","checks":[task names],"availability_pct","budget_remaining_pct","burn_rate_1d","status":"ok|at_risk|breached"}]}` computed from history.jsonl task records (15-min samples) with configurable objectives in `etc/playbooks.toml` `[[slo]]` (host health, storage, backups, services (failed_units), platform (plex_media_mount_check), pressure).
- Everything redacted like SPEC2 section 2 (no secrets, no raw outputs). Provide `export_incidents()`, `export_slo()`.

### S4 live monitor (owner `live`): `homelab_maint/live.py`, `systemd/homelab-maint-live.service`, `tests/test_live.py`
A lightweight daemon (`python3 -m homelab_maint.live`) sampling every 5 s and writing `STATE_DIR/public/live.json` atomically (0644) every 5 s, plus a 60-minute history at 5-s resolution in memory (720 points; persisted every minute to `STATE_DIR/live-history.json` so a restart keeps it). Budget: < 1.5% of one core, < 40 MB RSS, never blocks > 2 s (run nvidia-smi and the 9110 fetch with short timeouts in a worker thread; keep last good value; mark stale). Content:
`{"generated_at","interval_s":5,"host":{"uptime_s","load":[1,5,15],"cores","cpu_pct","cpu_user","cpu_sys","cpu_iowait","mem":{"total","used","avail","cache","swap_used","swap_total"},"psi":{"mem_some60","mem_full60","io_some60","io_full60","cpu_some60"},
"disk":[{"mount","free_b","size_b","used_pct"} watch mounts only],"io":[{"dev","read_bps","write_bps","util_pct"}],"net":{"rx_bps","tx_bps"} (physical interface only)},
"gpu":{"util","mem_used","mem_total","temp","power_w","fan_pct"},"sensors":{"cpu_temp","gpu_temp","ram_temp","nvme_temp","cpu_fan_rpm","case_fan_rpm"},
"containers":{"running":n,"unhealthy":[names],"top_cpu":[{"name","class","cpu_pct","mem_gib"} x8],"top_mem":[... x8]},
"services":[{"name","state":"up|down|degraded","detail"}] (a configurable short list probing docker health/state and, for a few, local HTTP: Plex /identity, Homarr, Immich, Kavita, Seerr, Open WebUI, Uptime Kuma - 5 s timeout, cached 30 s),
"activity":{"maintenance_running":["daily"...] (from RUN_DIR/*.lock), "last_action":{ts,task,action}},"pressure":{"level","why"} (from pressure_state if present),
"history":{"t0","step_s":5,"cpu":[720],"mem_pct":[720],"psi_mem":[720],"psi_io":[720],"gpu":[720],"net_rx":[720],"net_tx":[720],"disk_r":[720],"disk_w":[720]}}` (history arrays as ints/1-decimals, null for gaps; total file < 40 KB).
Unit: `homelab-maint-live.service` (Type=simple, Restart=always, RestartSec=5, User=root, Nice=10, IOSchedulingClass=idle, MemoryMax=64M, CPUQuota=10%, ProtectSystem=strict, ReadWritePaths=/var/lib/homelab-maint, ProtectHome=true, PrivateTmp, NoNewPrivileges; no PrivateDevices because of nvidia). Graceful SIGTERM. Tests: sampling math (cpu%, rates), history ring, atomic write, stale marking, degraded probes, overhead measurement script `tests/bench_live.py` run read-only against this host (report CPU% and RSS).

### S5 reports (owner `reports`): `homelab_maint/reports.py`, `tests/test_reports.py`
`reports.generate(kind: "daily"|"weekly", now)` writes `STATE_DIR/public/reports/<id>.json` (id `2026-10-02` / `2026-W40`) and refreshes `reports/index.json` (`[{id,kind,period_start,period_end,generated_at,headline,health}]` newest first, <= 60 kept). Tasks: `report_daily` (C0-like "report" class C1 in `daily` tier, mutates only its own public files; use klass "C0" because it never touches the host) and `report_weekly` (`weekly` tier). Report body:
`{"id","kind","period":{"start","end"},"headline":str,"health":{"score":0-100,"grade":"A..F","worst_status","time_ok_pct","checks_ok_pct_by_task":{..}},
"highlights":[str x<=8] (plain-language bullets: what mattered),"actions":{"freed_bytes","count","by_task":[{task,count,freed}],"notable":[{ts,title,detail}] (audit + maintenance-journal + changes)},
"incidents":{"opened","resolved","open_now","mttr_s","list":[{id,title,severity,duration_s,resolved}]},"spikes":{"count","worst_level","list":[{t,level,duration_s,contributors:[..],outcome}],"handled_without_harm":bool},
"capacity":{"mounts":[{mount,free_h,days_to_full,trend_gib_per_day}],"memory_baseline_gib":{"start","end","drift"},"recommendations":[str]},
"temperature":{"cpu_avg","cpu_max","gpu_avg","gpu_max","ram_avg","fan_note"} (from the SPEC2 ring),"slo":[{name,availability_pct,status}],
"upcoming":[{when,what}],"notes":[str]}`. Health score formula documented in the module docstring (deductions for crit/warn minutes, open incidents, failed backups, stale checks) and unit-tested. The weekly report also produces `digest_text` (<= 600 chars ASCII) that the lead may send via the Notifier once a week (do not send anything yourself).
Pure functions over inputs (status.json, history.jsonl, audit.jsonl, incidents.jsonl, spikes.jsonl, changes.jsonl, maintenance-journal.jsonl, metrics ring, pressure logs) with graceful degradation when files are missing (new install: say "collecting data").

### S6 website v2 (owner set `web_v2`, runs AFTER S1-S5 and the SPEC2 web build): see section 3.

## 3. Website v2 additions (extends SPEC2 section 5; `web/`)
Navigation: top bar with tabs (hash routes, no framework): **Live**, **Health**, **Incidents**, **Maintenance**, **Reports**, **Capacity**. Keep every page read-only.
- Backend routes (add to `web/app.py`): `/api/live` (no-store; served from public/live.json; 5 s cache hint), `/api/incidents`, `/api/slo`, `/api/pressure`, `/api/routine`, `/api/reports` (index), `/api/reports/<id>` (strict `[0-9A-Za-z-]{1,16}` id validation), plus existing. `/healthz` also requires live.json < 60 s old as a WARNING field (not failure). A server-sent-events or long-poll is NOT needed: the UI polls every 5 s on the Live tab (pause when hidden), 30 s elsewhere.
- **Live tab** (the "current monitoring" the owner asked for): top strip of big gauges (CPU, Memory, Swap, GPU, GPU VRAM, Load, Disk IO, Network) each with value, a tiny 60-min sparkline from `history`, and colour by threshold; pressure indicator (PSI memory/IO + current LEVEL 0-5 with a one-line "what the system is doing about it": nothing/reclaiming idle models/throttling batch/...); temperatures and fans (live); services grid (up/down with detail); top containers by CPU and by memory with class badges (P0-P3); "maintenance running now" + last action; active alerts/incidents ribbon linking to the Incidents tab; a "spike log" for the last 24 h (time, level, contributors, outcome "handled, nothing killed"). Auto-refresh 5 s with a visible "live" heartbeat dot and a stale warning when live.json is older than 30 s.
- **Health tab**: SPEC2 sections 1-2 (hero, checks grid) + SLO cards (availability, budget remaining, burn) + the 30-day health calendar.
- **Incidents tab**: open incidents (severity, duration, summary, playbook "what to do", timeline), recent resolved with MTTR and postmortem (render the Markdown subset safely: headings, lists, bold, code; no HTML), MTTD/MTTR stats.
- **Maintenance tab**: "What was done" timeline (SPEC2 section 3), the change log (verified yes/no), the routine (daily/weekly/monthly steps with last/next run and mode report/apply, windows and freeze windows), a 14-day calendar grid, per-task table, totals freed, spike-response ladder explanation and the service classes table (P0-P3 members and policy).
- **Reports tab**: list of daily/weekly reports (newest first), click to render a report page (health score ring, highlights, actions, incidents, spikes, capacity with recommendations, temperatures, SLOs, upcoming) with a print stylesheet; deep link `#/reports/<id>`.
- **Capacity tab**: SPEC2 storage section + days-to-full table, memory baseline drift, temperature/fan/load 7-day charts (SPEC2 metrics).
Frontend rules as SPEC2 (vanilla ES modules, no innerHTML with data, strict CSP, <= 120 KB total now, accessible, mobile-first, dark/light, no external requests). Fixtures: extend `web/fixtures/` with `incident-open`, `spike-recent`, `report-weekly`, `live-degraded`, `new-install` and a `?fixture=` dev mode as before. Screenshots at 390x844 and 1440x900 for every tab/fixture, LOOKED at and iterated.

## 4. Glue (lead)
cli.py: after the state lock call `incidents.update`, `routine.export`, `pressure.export`, `publish.publish` (which adds routine/incidents/slo/pressure/reports to the public dir); new subcommands `routine`, `report`, `live`; install.sh: install live unit, create public/reports; server.py routes; etc/maint.toml entries for the new tasks; classes/playbooks/routine toml install-if-absent.
