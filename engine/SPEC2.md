# SPEC2: public status export, 7-day metrics ring, maintenance web UI, Homarr history widgets

Builds on SPEC.md (read it first: rules, core.py contract, status.json). Same hard rules: Python 3.12 stdlib only for the runner side;
NEVER mutate the live system while developing (no installs, no systemctl changes, no writes under /etc,/usr,/var/lib,/var/log,
no touching existing containers). Allowed extras for the web part: `docker build` of OUR image and running a throw-away test container
named `maintenance-web-test-<n>` on a loopback port, removed afterwards. Do not edit files you do not own; list required glue edits to
`cli.py`, `server.py`, `install.sh`, `etc/maint.toml` in your final report under `glue` (the lead applies them).

> **Retired: the `maintenance-web` container described in this SPEC no longer exists.** It was replaced by the OhmzMaintainer
> beszel hub (`beszel-hub.service` with its data collector `beszel-agent.service`), which listens on `127.0.0.1:8088` and is
> published at `https://maintainer.ohmzhomelab.ca`. Nothing listens on 8098 and the `web/` directory is gone; `install.sh` no
> longer deploys a website. The old hostname `maintenance.ohmzhomelab.ca` no longer resolves, and the container, port, hostname
> and `web/` references below are kept as a historical design record.

## 1. Facts about this host
- Sensors: `127.0.0.1:9110/` (sensor-exporter, JSON: cpu_temp, gpu_temp, nvme_temp, systin, cpu_fan, case_fan, gpu_fan, gpu_fan_rpm,
  fans{fan1..7 rpm}, fan_duty_cpu, fan_duty_case) is root-owned: DO NOT modify it; READ it (GET) and fall back to lm-sensors/nvidia-smi.
  DIMM temps: `sensors` shows four `spd5118-i2c-3-5x` chips (temp1 each; /sys/class/hwmon/*/temp1_input, name == spd5118).
  CPU package: coretemp `Package id 0`. GPU: `nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,fan.speed,power.draw --format=csv,noheader,nounits`
  (fan.speed is a percent; the 3090 fans can read 0 at idle). NVMe: composite from hwmon `nvme`. CPU util from /proc/stat deltas,
  RAM util from /proc/meminfo (1 - MemAvailable/MemTotal).
- An older `thermal_log.py` exists in ~/sensor-exporter/tuning (read it for ideas; do not modify).
- Docker: the host is out of default address pools. The web app MUST use an explicit subnet via a second compose file
  (`docker compose -f docker-compose.yml -f docker-compose.override.yml up -d`); use `10.88.0.0/24` (verified free). Free host port: `127.0.0.1:8098`.
  Cloudflared is token-managed (remote config), runs on the host, so the public hostname `maintenance.ohmzhomelab.ca` -> `http://localhost:8098`
  is added in the Cloudflare Zero Trust dashboard by the owner; the app must work behind it (X-Forwarded-* ignored, no absolute URLs, relative fetches).
- Chrome is installed (`google-chrome --headless=new --screenshot=... --window-size=...`) for UI screenshots; `node` exists.

## 2. Public export (written by `homelab_maint/publish.py`, owner: publish agent)
`publish.publish(status: dict | None = None, now: float | None = None) -> list[str]` writes ATOMICALLY (tmp + os.replace), mode 0644, into
`STATE_DIR/public/` (dir 0755). The web container mounts ONLY this directory, read-only. It must never contain secrets, tokens, alert bodies,
raw command output, or full traceback text. All times are epoch seconds (floats); the UI formats them.
`python3 -m homelab_maint.publish` runs it once from the CLI (reads STATE_DIR/status.json etc.).

Files (all JSON, UTF-8, compact):
1. `overview.json`: `{"schema":1,"generated_at":t,"host":str,"overall":"ok|warn|crit","paused":bool,"counts":{"ok":n,"warn":n,"crit":n,"info":n,"error":n},
   "headline":str (<=80 chars, e.g. "2 warnings: Disk space, Services"), "tiers":{"check":{"last_run":t|null,"next_run":t|null,"mode":"check"},
   "daily":{...,"mode":"report|apply"},"weekly":{...}},"cleanup_mode":"report|apply|mixed","uptime_s":int,"kernel":str}`
2. `checks.json`: `{"generated_at":t,"checks":[{"name","title","klass":"C0|C1|C2","tier","status","summary","last_run":t,"duration_s":float,
   "mode":"check|report|apply|dry-run","metrics":{scalars only, <=20 keys},"items":[{...<=8 rows, scalar values}]}]}` sorted: crit, warn, error, info, ok.
3. `actions.json`: what maintenance was DONE. `{"generated_at":t,"recent":[{"ts":t,"task","action","target","bytes":int,"outcome"}...<=300 newest first],
   "by_task":{"<task>":{"last_run":t,"last_action":t|null,"last_outcome":str,"freed_24h":int,"freed_7d":int,"freed_30d":int,"freed_total":int,"runs_30d":int}},
   "totals":{"freed_24h":int,"freed_7d":int,"freed_30d":int,"actions_7d":int}}`. Source: `LOG_DIR/audit.jsonl` (records have ts "%Y-%m-%dT%H:%M:%S%z", task,
   action, target, bytes, outcome in {done,dry-run,refused-*,failed:...,sent,approved}) + status.json `reclaimed_log` + each task's last_run. Exclude `notify` entries'
   free text; show them only as {"task":"notify","action":"alert","outcome":"sent|failed"} with target blanked. `target` for file paths is kept but any path segment
   after the 3rd component under /home/<user>/ is truncated to `...`; never include tokens/URLs with query strings.
   Also include manual maintenance the owner did that the runner cannot see, from `STATE_DIR/maintenance-journal.jsonl` if present (`{"ts","title","detail"}`): list it under
   `"journal":[...<=100]`.
4. `storage.json`: `{"generated_at":t,"mounts":[{"mount","free_b","size_b","used_pct","free_h","days":int|null,"level","info":bool}],
   "series":{"<mount>":{"t":[hour epochs],"free_gib":[floats]}}` (<=30 days hourly, from history.jsonl `kind:"disk"` records, downsampled by hour mean, only watch mounts),
   `"freed_by_day":[{"day":"YYYY-MM-DD","bytes":int}]` (<=30 days)}`.
5. `metrics.json`: the thermal/load ring export (section 3).
6. `schedule.json`: `{"generated_at":t,"timers":[{"unit","title","last":t|null,"next":t|null,"schedule":str}]}` for `homelab-maint-*.timer` plus the notable system ones
   (backup-system, backup-immich, docker-prune, fstrim, logrotate, apt-daily-upgrade, sysstat-collect) via `systemctl list-timers --all --output=json` (epoch microseconds in the
   JSON; convert). Degrade to an empty list when systemctl is unavailable.
7. `health-history.json`: `{"generated_at":t,"days":[{"day":"YYYY-MM-DD","worst":"ok|warn|crit","warn_minutes":int,"crit_minutes":int}]}` for the last 30 days from
   history.jsonl task records (15-min cadence: each record's status counts toward that day's worst and minutes estimate = 15 per record with that status; if no data for a
   day use `"worst":"unknown"`).

Caps: each file < 200 KB; trim oldest entries to fit.

## 3. 7-day metrics ring (owner: metrics agent), module `homelab_maint/metrics_ring.py`
Goal: keep the last 7 days of hourly averages for temperatures, fan speeds and load, as a ring buffer that starts overwriting the oldest hour once full ("always see 7 days").
- Sampler `sample_once(now=None) -> dict` (every 60 s from a systemd timer; must finish in < 3 s; never raises; missing sensors => None): returns and records
  `cpu_temp` (C, coretemp package), `gpu_temp`, `ram_temp` (mean of DIMM spd5118 temps) and `ram_temp_max`, `nvme_temp`, `cpu_fan_rpm`, `case_fan_rpm` (mean of case fans > 0),
  `gpu_fan_pct`, `gpu_fan_rpm` (if the exporter has it), `cpu_pct`, `gpu_pct`, `ram_pct`, `gpu_mem_pct`, `gpu_power_w`, `load1`.
- Ring: 168 slots in `STATE_DIR/metrics-ring.json` (mode 0644): `{"v":1,"slots":168,"hours":[{"h":hour_epoch_int,"n":count,"sum":{metric:float},"cnt":{metric:int},"max":{metric:float},"min":{...}} x168]}`.
  Slot index = `(epoch_hour % 168)`. When a sample arrives for hour H: if slot.h != H the slot is RESET to hour H (this is the overwrite: the hour 7 days ago disappears), then the sample is
  folded in (sum/cnt per metric so None values do not skew the mean). Clock jumps backwards must not corrupt anything (ignore samples older than the newest slot's hour minus 1 unless the
  ring is empty). Writes are atomic (tmp+os.replace) under an flock on `STATE_DIR/metrics.lock`; a corrupt/missing file is replaced by a fresh ring (keep the corrupt one as `.bad`).
  Memory/IO trivial: the whole file is < 60 KB.
- Export `export(now=None) -> dict` (read-only, no sensors): 
  `{"generated_at":t,"interval_s":3600,"slots":168,"current":{metric:value|None,"sampled_at":t},"hour_avg":{metric:avg of the hour in progress|None},
    "prev_hour_avg":{...},"avg_24h":{...},"avg_7d":{...},"max_7d":{...},"loop":{"pos":int (0..167 index of the in-progress hour in `series`),"overwrites_next_at":t (start of next hour),
    "oldest_hour":t,"complete":bool (true once 168 distinct hours are present)},
    "series":{"t":[168 hour-start epochs, oldest first, ENDING at the in-progress hour],"<metric>":[168 hourly means, 1 decimal, null where no data] for every metric above}}`.
  Stale ("sampler stopped") detection: `"stale": true` when the newest sample is > 5 min old.
- CLI entry: `python3 -m homelab_maint.metrics_ring sample|export` (export prints JSON). Systemd units to ship in `systemd/`: `homelab-maint-metrics.service` (Type=oneshot,
  User=root, Nice=15, IOSchedulingClass=idle, ExecStart=/usr/bin/python3 -m homelab_maint.metrics_ring sample with PYTHONPATH=/usr/local/lib/homelab-maint, TimeoutStartSec=20, hardening:
  ProtectSystem=strict, ReadWritePaths=/var/lib/homelab-maint, ProtectHome, PrivateTmp, NoNewPrivileges; it needs access to /dev/nvidia*, so do NOT set PrivateDevices) and
  `homelab-maint-metrics.timer` (OnBootSec=1min, OnUnitActiveSec=1min, AccuracySec=5s).

## 4. Homarr widgets (owner: widgets agent)
Two customJsx definitions (see widgets/CONVENTIONS.md and the existing build tooling in widgets/build_widgets.py, check_templates.mjs, install_homarr_widgets.py):
`ops-thermals` ("Thermals & fans, 7 days": CPU/GPU/RAM temperature + CPU/case/GPU fan speed) and `ops-load` ("Processing, 7 days": CPU, GPU, RAM utilisation).
Each shows: current values (big), "1 h avg" values, 24 h and 7 d averages, and a 7-day history chart of the 168 hourly points (Mantine LineChart/AreaChart/Sparkline from the
fork whitelist; verify in packages/widgets/src/custom-api/*) with the newest hour at the right edge and a visible marker/label of where the ring loops ("overwrites next at ...").
Payload functions in `homelab_maint/payloads_metrics.py`: `thermal(export) -> dict`, `load(export) -> dict`, each < 14 KB JSON, with precomputed strings, colour words, pre-built chart
`data` rows (`[{"x":"Thu 21h","cpu":47.1,...}]` x168 where x is a short label computed in Python from the hour epoch in the host TZ) and `series` colour specs, plus `stale`.
Server routes `/thermal` and `/load` (glue for server.py; you write the handler functions only) serve them. Templates must pass `node widgets/check_templates.mjs` (the
fork's JsxParser render) with the fixtures: full ring, partial ring (first day), stale sampler, all-None metrics (e.g. no GPU).

## 5. Maintenance web UI (owners: web-backend agent and web-frontend agent), directory `web/`
A NEW read-only dashboard container `maintenance-web` for `https://maintenance.ohmzhomelab.ca` (via Cloudflare tunnel -> http://localhost:8098). It answers: "is the server healthy,
what maintenance has been done, when was the last time each thing ran, and what runs next".
- Backend (`web/app.py`, single file, Python 3.12 stdlib, no pip): serves static files from `web/static/` and a tiny read-only JSON API that returns the files of the mounted
  public dir (`/data/public`, default env `PUBLIC_DIR`): `GET /api/overview|checks|actions|storage|metrics|schedule|health-history`, `GET /healthz` (200 if the public dir is
  readable and overview.json is < 45 min old else 503 with a reason; used by the Docker HEALTHCHECK), `GET /` + static. ONLY GET/HEAD (405 otherwise), no directory listings, path
  traversal safe, `Cache-Control: no-store` on /api, strong security headers on everything (CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, X-Content-Type-Options, Referrer-Policy no-referrer, Permissions-Policy empty, COOP/CORP same-origin),
  gzip for JSON when accepted, ETag/304, request size/time limits, ThreadingHTTPServer with a connection cap, structured one-line access log without query strings or client IP
  (it sits behind Cloudflare), graceful SIGTERM. Optional HTTP Basic auth enabled when env `BASIC_AUTH_FILE` points to a file containing `user:password` (constant-time compare,
  `WWW-Authenticate`), documented as a fallback to Cloudflare Access (the README must recommend Cloudflare Access with an email allow-list because the page reveals infrastructure details).
  If any public file is missing/invalid the API returns `{"error":"unavailable","stale":true}` with HTTP 200 for /api/* (so the UI can show a banner) except /healthz.
- Container: `web/Dockerfile` (python:3.12-slim pinned by digest if resolvable else tag, non-root uid 10001, no shell needed at runtime, `PYTHONDONTWRITEBYTECODE=1`),
  `web/docker-compose.yml` (service `maintenance-web`, container_name `maintenance-web`, `restart: unless-stopped`, `read_only: true`, `tmpfs: /tmp`, `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]`, `mem_limit: 128m`, `pids_limit: 64`, `cpus: 0.5`, ports `127.0.0.1:8098:8080`, volume `/var/lib/homelab-maint/public:/data/public:ro`,
  healthcheck, log rotation json-file 5m x3, no docker.sock, no host network) and `web/docker-compose.override.yml` (explicit network with subnet 10.88.0.0/24; name the network
  `maintenance-web_default`). `web/README.md`: build/run commands with the two `-f` files, the Cloudflare dashboard steps (Networks > Tunnels > <tunnel> > Public Hostname:
  maintenance.ohmzhomelab.ca -> HTTP localhost:8098) and an Access policy recommendation.
- Frontend (`web/static/index.html`, `app.js` as an ES module, `style.css`, no external requests, no frameworks, no build step, <= 60 KB total): single page, mobile-first and good on a
  big screen, dark/light via `prefers-color-scheme` + manual toggle (persist in localStorage guarded by try/catch), auto-refresh every 30 s (pause when the tab is hidden), a clear
  "data is stale" banner when `generated_at` is old, `aria-live` status updates, keyboard accessible, `prefers-reduced-motion` respected. Sections in this order:
  1. Hero: overall health (big status word + colour + headline), "updated N min ago", host + uptime, cleanup mode chip.
  2. "Right now": the checks grid (card per check: status dot, title, one-line summary, "checked N min ago"), problems sorted first, collapsible "all healthy" group.
  3. "What was done": a timeline of maintenance actions (grouped by day, human wording: "Docker build cache pruned: 21.2 GiB freed", "Snap old revisions removed", "Kavita logs trimmed")
     with relative + absolute time, plus per-task "last ran / last changed something / freed" table, plus the manual `journal` entries, plus totals (freed 24 h / 7 d / 30 d).
  4. "Schedule": next/last run of each timer in plain words ("Daily cleanup: next tomorrow 07:30, last ran today 07:31").
  5. "Storage": usage bars per watched mount with free space and days-until-full, plus a 30-day free-space line chart and "freed per day" bars (inline SVG you generate).
  6. "Temperatures, fans & load, last 7 days": hourly 7-day charts from metrics.json (temps; fans; CPU/GPU/RAM load) with current + 1 h avg + 7 d avg tiles and a marker at the loop point.
  7. "30-day health calendar": a GitHub-style strip from health-history.json.
  Footer: "Read-only view. Generated by homelab-maint" + schema version. Use formatting helpers (Intl.RelativeTimeFormat/DateTimeFormat in the viewer's timezone). Charts are
  hand-written SVG helpers with accessible titles/desc and tooltips on focus/hover. Never use innerHTML with data (use textContent / createElementNS) to be XSS-safe even though the
  data is trusted. Build fixtures under `web/fixtures/` (ok, warn+crit, stale, empty, partial-ring) via a script `web/fixtures/make_fixtures.py` and a `?fixture=<name>` dev mode that is
  only honoured when the server runs with env `DEV_FIXTURES=1` (backend serves /api from web/fixtures/<name>/ then). Provide `web/tools/screenshot.sh` that renders each fixture at
  390x844 and 1440x900 with headless Chrome into web/shots/ so the result can be inspected.
- Tests: `web/tests/test_app.py` (pytest: methods, traversal, headers, healthz logic, auth, staleness, gzip/etag), `web/tests/test_frontend.mjs` (node, no deps: parse app.js, check no innerHTML/eval,
  run the pure formatting/aggregation helpers exported from `web/static/lib.js`).

## 6. Integration glue (done by the lead after the build)
cli.py: call `publish.publish(status)` at the end of `cmd_run` (and subcommands `metrics-sample`, `metrics-export`, `publish`); server.py: routes `/thermal`, `/load`, `/metrics`; install.sh:
install the metrics units, create STATE_DIR/public (0755), enable `homelab-maint-metrics.timer`; deploy the container; add widgets to Homarr.
