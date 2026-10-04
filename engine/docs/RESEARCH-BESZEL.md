# RESEARCH: Beszel vs homelab-maint

Read-only research note. Date 2026-10-04. No code, config or state was changed.

**Question.** What can homelab-maint learn from [Beszel](https://github.com/henrygd/beszel), an
open-source homelab monitoring server, given that homelab-maint is a Python 3.12, stdlib-only,
single-host, mostly-read-only maintenance daemon that must never act on its own?

**Method.** Beszel facts are from its own repo and docs (repo `main`, docs version 0.21.0) and the
Go/JS source, cited by URL. homelab-maint facts are from this repo's README, SPEC*.md, docs/ and the
code, cited by absolute path and line. Beszel's hub, agent, migrations, records/retention, alerts,
auth, frontend package and Dockerfiles were read; homelab-maint's `homelab_maint/` core, `web/` and
`widgets/` were read.

**Naming/location.** This file sits beside the other design docs (`docs/EXTENDING.md`,
`docs/HOMARR_V2_WIDGETS.md`, `docs/INTEGRATION.md`, `docs/MIGRATION.md`), following the repo's
uppercase-doc convention.

---

## 1. What Beszel is

Beszel is "lightweight server monitoring with historical data, docker stats, and alerts"
([README](https://github.com/henrygd/beszel), [docs](https://beszel.dev/guide/what-is-beszel)). It is
two components: a **hub** (web app, dashboard, database) and an **agent** that runs on each monitored
host and reports metrics to the hub.

- **Stack.** Everything is Go — hub, agent, and the frontend's server
  ([api.github.com/repos/henrygd/beszel](https://api.github.com/repos/henrygd/beszel)). The hub is
  built directly on **PocketBase** `v0.40.4` (`github.com/pocketbase/pocketbase`) over its bundled
  **SQLite** (`modernc.org/sqlite v1.57.0`, `pocketbase/dbx v1.12.0`)
  ([go.mod](https://raw.githubusercontent.com/henrygd/beszel/main/go.mod)). The web UI is **React 19 +
  Vite 7 + TypeScript**, Tailwind v4, Radix UI, `nanostores`, charts with **`recharts` ^2.15.4**
  ([internal/site/package.json](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/package.json)),
  embedded into the Go binary with `//go:embed all:dist`
  ([internal/site/embed.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/embed.go)).
- **Deployment.** Docker images `henrygd/beszel` (hub) and `henrygd/beszel-agent`, both two-stage
  builds ending in `scratch`
  ([internal/dockerfile_hub](https://raw.githubusercontent.com/henrygd/beszel/main/internal/dockerfile_hub),
  [internal/dockerfile_agent](https://raw.githubusercontent.com/henrygd/beszel/main/internal/dockerfile_agent));
  single static binaries via `get.beszel.dev` install scripts that create a `beszel` user and a
  systemd unit ([hub install](https://beszel.dev/guide/hub-installation),
  [agent install](https://beszel.dev/guide/agent-installation)). Agent ports: SSH `45876`, hub `8090`
  ([security](https://beszel.dev/guide/security)).
- **License.** MIT ([LICENSE](https://github.com/henrygd/beszel/blob/main/LICENSE)).
- **Team/popularity/cadence.** ~25.9k stars, 1,061 forks, repo created 2024-07-07, 77 tags, latest
  `v0.21.0` (2026-10-02); 2026 releases land roughly every 2–3 weeks with frequent patches
  ([repo API](https://api.github.com/repos/henrygd/beszel),
  [tags](https://api.github.com/repos/henrygd/beszel/tags?per_page=100),
  [releases](https://github.com/henrygd/beszel/releases)).

### 1.1 Monitoring model (Beszel)

- **Pull, not push.** The hub runs one updater per system: an immediate update, then
  `time.NewTicker(interval)` where the package-level `interval int = 60_000` ms — one poll per host
  **per minute** ([internal/hub/systems/system.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/hub/systems/system.go),
  [system_manager.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/hub/systems/system_manager.go)).
  The agent caches its own data for 60 s (`defaultDataCacheTimeMs = 60_000`,
  [agent/agent.go](https://raw.githubusercontent.com/henrygd/beszel/main/agent/agent.go)).
- **Transport.** WebSocket preferred (agent dials out, retries on a fixed 10 s ticker), SSH fallback
  (hub dials in with an ED25519 key) ([agent/connection_manager.go](https://raw.githubusercontent.com/henrygd/beszel/main/agent/connection_manager.go)).
- **What the agent collects** ([README](https://github.com/henrygd/beszel)): CPU (host + per
  container) incl. iowait/steal; memory + swap/ZFS ARC; disk usage and I/O (utilization/await/queue);
  network (host + containers); load 1/5/15; temperatures and fan RPM via `/sys/class/hwmon`; GPU
  (Nvidia/AMD/Intel/Jetson/Apple); battery; Docker/Podman containers (health, ports, image-update
  flag); S.M.A.R.T.; ZFS/btrfs pools; systemd services (+ last 200 journal lines); WiFi signal and
  pending package updates (0.21). There is **no general process list** feature
  ([agent/](https://api.github.com/repos/henrygd/beszel/contents/agent),
  [components](https://api.github.com/repos/henrygd/beszel/contents/internal/site/src/components)).

### 1.2 Metrics storage, granularity, retention (Beszel)

- Metrics are **JSON blobs** in PocketBase/SQLite collections `system_stats`, `container_stats`,
  `network_monitor_stats`; each row's `type` is one of `1m`, `10m`, `20m`, `120m`, `480m`, indexed on
  `(system, type, created)`
  ([migrations/0_collections_snapshot_0_20_0.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/migrations/0_collections_snapshot_0_20_0.go)).
- **Automatic rollups** (`RecordManager.CreateLongerRecords`, averages sums then divides, preserves
  peaks via `max`): `1m→10m` (needs ≥9 sources), `10m→20m` (≥2), `20m→120m` (≥6), `120m→480m` (≥4)
  ([internal/records/records.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/records/records.go)).
- **Retention** (hourly deletion job): `1m` → 1 h, `10m` → 12 h, `20m` → 24 h, `120m` → 7 d,
  `480m` → 30 d. Alert history keeps 200 rows/user; `systemd_services` >20 min and `containers`
  >10 min are pruned ([internal/records/records_deletion.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/records/records_deletion.go)).
- **Jobs:** `"delete old records"` at `"8 * * * *"`, `"create longer records"` at `"*/10 * * * *"`
  ([internal/hub/hub.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/hub/hub.go)).
- **No user-facing retention config** was found — it is a fixed built-in policy.

### 1.3 Charts (Beszel)

`recharts` on React; a **time-range selector** (`chart-time-select.tsx`) maps each range to a record
type: `1m`→`1m` (realtime, ~2 s), `1h`→`1m`, `12h`→`10m`, `24h`→`20m`, `1w`→`120m`, `30d`→`480m`;
default `1h` ([chart-time-select.tsx](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/src/components/charts/chart-time-select.tsx),
[utils.ts](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/src/lib/utils.ts)). Per-system pages chart
host CPU/mem/disk/network plus per-container and per-filesystem series
([what-is-beszel](https://beszel.dev/guide/what-is-beszel)).

### 1.4 Alerts (Beszel)

- Rules are **PocketBase `alerts` rows**, configured per user, per system, per metric via the UI
  ([notifications](https://beszel.dev/guide/notifications)). Schema `name` select — the authoritative
  list: `Status, CPU, Memory, Disk, Temperature, Bandwidth, GPU, LoadAvg1/5/15, Battery,
  ContainerHealth, SystemdFailed, CPUIOWait, CPUSteal, NetworkMonitorLoss`
  ([migration](https://raw.githubusercontent.com/henrygd/beszel/main/internal/migrations/0_collections_snapshot_0_20_0.go),
  [lib/alerts.ts](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/src/lib/alerts.ts)).
  SMART failures notify automatically and are not configurable
  ([smart-data](https://beszel.dev/guide/smart-data)).
- **Threshold + duration:** `min` is 1–60 minutes the condition must hold; with `min>1` the `1m` rows
  are averaged over the window requiring ~83% of expected samples (`minCount := float32(alert.min) / 1.2`),
  and `min==1` uses the instantaneous value. Battery is inverted (fires below threshold)
  ([internal/alerts/alerts_system.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/alerts_system.go)).
  UI duration defaults to 10 min, threshold to 80.
- **Dedupe:** notify only on a **state transition** (a persisted `triggered` flag); auto-resolves when
  the condition clears. **No manual acknowledge** action was found
  ([alerts.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/alerts.go)).
- **Channels:** a maintained shoutrrr fork (`github.com/nicholas-fedor/shoutrrr v0.21.1`) with ~24
  services (Discord, Gotify, Ntfy, Slack, Telegram, Pushover, Twilio, MS Teams, Signal, MQTT, generic
  webhook, …) plus hub SMTP email and a per-user webhook
  ([notifications](https://beszel.dev/guide/notifications),
  [notification_client.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/notification_client.go)).
  Non-admin notifications are **SSRF-guarded** (private/loopback/link-local/CGNAT destinations
  refused, resolved-address check in the dialer `Control` hook; 10 s dial / 15 s HTTP / 10 s TLS
  timeouts); the client does **no retries** ([same file](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/notification_client.go)).
- **Quiet hours:** one-time and recurring, via a `quiet_hours` collection
  ([alerts.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/alerts.go)).

### 1.5 Multi-host, auth, footprint (Beszel)

- **Multi-host by design:** a `systems` collection with per-system updaters, staggered activation;
  systems are added explicitly (UI prints the agent command) or via universal registration tokens;
  sharing via the system's `users` relation or `SHARE_ALL_SYSTEMS=true`. No auto-discovery
  ([getting-started](https://beszel.dev/guide/getting-started),
  [user-accounts](https://beszel.dev/guide/user-accounts)).
- **Auth:** PocketBase `users` with roles `user | admin | readonly`; OAuth2/OIDC (GitHub, Google,
  Microsoft, custom OIDC such as Authelia/Keycloak/Pocket ID); email OTP MFA (`MFA_OTP`);
  `TRUSTED_AUTH_HEADER` behind `TRUSTED_PROXY_IPS`; separate PocketBase superusers
  ([user-accounts](https://beszel.dev/guide/user-accounts), [oauth](https://beszel.dev/guide/oauth),
  [environment-variables](https://beszel.dev/guide/environment-variables)). Agent↔hub: ED25519 hub
  keypair for SSH (hub does **not** verify the agent host key) or WebSocket token + hub signature +
  agent machine fingerprint ([security](https://beszel.dev/guide/security)). Agents verify HTTPS
  certs since 0.19.0.
- **Footprint:** `scratch` images, compressed amd64 ≈ **13.3 MB** hub and **≈4.6 MB** agent
  ([Docker Hub tags](https://hub.docker.com/v2/repositories/henrygd/beszel/tags)). No published RAM
  figure was found.
- **Embeddability:** no official embeddable widget or status badge; third-party Homepage and Homarr
  card widgets exist ([Homepage](https://gethomepage.dev/widgets/services/beszel/),
  [Homarr](https://homarr.dev/docs/widgets/beszel-system-grid/)).

### 1.6 What homelab-maint is, for grounding

Single Ubuntu 24.04 host (94 GiB RAM, ~72 Docker containers), one Python 3.12 **stdlib-only**
package installed to root (`README.md:47-64`, `SPEC.md:1-6`). It is a maintenance umbrella first —
disk/memory/backup/SMART/Docker checks, safe cleaners, a routine engine, a notification path,
reports — and a monitoring plane second (`probes.py`, `live.py`). State is **plain JSON/JSONL under
`/var/lib/homelab-maint`**, no database (`README.md:74-86`). It can act only through `Ctx.act` under
C0/C1/C2 classes, kill switches and an audit log (`README.md:7-17`, `homelab_maint/core.py`).

---

## 2. Side-by-side

| Dimension | Beszel | homelab-maint |
|---|---|---|
| **Monitoring model** | Pull hub, one poll per host per 60 s; agent dials out on WebSocket with SSH fallback; agent caches 60 s | Daemonless pull checks driven by systemd timers: check tier every 15 min, probes every 1 min, live daemon every 5 s (`README.md:163-180`; `homelab_maint/live.py:99-101`) |
| **Metrics granularity** | 1 min base sample; 5 rollup buckets `1m/10m/20m/120m/480m` (`records.go`) | Sensor ring 1 sample/min → 168 **hourly** slots (7 d, 15 metrics, ~185 KB) (`metrics_ring.py:57-69`); live 720 pts @ **5 s** (60 min, <52 KB) (`live.py:99-101`); check history 15-min records (`SPEC2.md:50-51`) |
| **Retention / downsampling** | Automatic averaging rollups, **preserving `max`**; retention 1 h→30 d; hourly prune cron | 7 d hourly ring (slot overwrite); 60 min live history persisted once/min; `history.jsonl` trimmed by **size** (40 MiB) not age (`core.py:526`) |
| **Storage** | SQLite (PocketBase) JSON-blob collections | JSON/JSONL files, atomic `os.replace` + `flock`; no DB, no pip (`README.md:60-64`) |
| **Charts** | React + **recharts** ^2.15.4 | **Hand-rolled inline SVG** (`web/static/charts.js`): sparkline/line/bar/gauge/ring, ResizeObserver redraw, crosshair+tooltip, keyboard nav (`charts.js:1-14,49-131`) |
| **Chart time ranges** | Selector maps `1m/1h/12h/24h/1w/30d` to record types | Fixed window per tab (Capacity: 7 d + 30 d storage; Reports: period) (`web/static/tabs/capacity.js:1-12`) |
| **Alert rules** | UI data rows, 16 metric types, password-auth threshold + 1–60 min duration, transition dedupe, auto-resolve | Thresholds in task code; confirm 2 runs ≈30 min, per-task overrides; rules/registry shown read-only (`README.md:354-362`; `homelab_maint/core.py:406-499`) |
| **Notification channels** | ~24 shoutrrr services + SMTP email + webhook, SSRF-guarded, no retries | **SMS (Hermes gateway) + Gmail SMTP only** (`notify.py:148,66-84`) |
| **Dedupe / ack / resolution** | Transition-based, auto-resolve, quiet hours; **no manual acknowledge** | Dedupe windows + covered_by, quiet hours, budgets, durable outbox, **acknowledgement** (90 d, warn-only, HMAC-signed, single-use token, rate-limited) (`README.md:439-468`) |
| **Incidents / SLO / reports** | Alert history only | Incident ledger, sev1-3, correlation, MTTD/MTTR, SLO error budgets, postmortem stubs, playbooks, daily/weekly reports with health score (`SPEC3.md:70-97`) |
| **Dashboard / widgets** | System grid + per-system page; REST API; no official embed widget | 6-tab read-only site + **7 Homarr v2 custom widgets** over loopback `127.0.0.1:9111` (`web/README.md:24-26`; `widgets/CONVENTIONS.md`) |
| **Multi-host** | Core: many systems, staggered updaters, sharing | **Single host by design** (`SPEC.md:1-2`) |
| **Auth / security** | PocketBase users/roles, OAuth2/OIDC, OTP MFA; agent ED25519/WS fingerprint | Cloudflare Access + owner passphrase (PBKDF2-SHA256 600k, TOTP, recovery codes); container uid 10001, read-only rootfs, loopback-only, strict CSP; the one write path HMAC-signed (`web/README.md:34-120`) |
| **Dependency / ops cost** | Go/PocketBase/SQLite + embedded React build; scratch images ~13.3 MB / ~4.6 MB | Python 3.12 stdlib only, no pip, no DB, no Node at runtime; live daemon ~26 MiB RSS, <1% core (`live.py:10-13`) |
| **License / cadence** | MIT; Go; ~2–3-week releases, v0.21.0 | No LICENSE file in repo; private; rolling commits |

### 2.1 Where each is ahead

**Beszel leads on:** multi-host aggregation; per-container and per-filesystem **history** (not just a
snapshot); the tiered **retention + automatic rollup** model with peak preservation; **selectable
chart time ranges**; breadth of alert channels; OAuth/OIDC/MFA; and a tiny `scratch`-image deployment.

**homelab-maint leads on:** acknowledgement (Beszel has none); incident ledger with SLO/MTTR and
postmortems; reports and a health score; **maintenance actions** (cleaners, routine, gates) — Beszel
only observes; a single reviewable rules registry; a hardened read-only website with one signed,
rate-limited write path; delivery resilience (budgets, durable outbox, retries, circuit breaker); and
a self-monitoring dead-man's switch (`self.json`, `/heartbeat`).

---

## 3. Ranked, concrete ideas to adopt

Ordered by value × fit ÷ effort for a Python, single-host, read-only daemon. Each idea names the
constraint that makes it fit and an effort/risk tag. None of these changes the safety model.

### 1. TLS-certificate-expiry (and TCP-latency) probe types — **effort S, risk low**

Beszel 0.21 added TLS cert-expiry checks and 0.20 added network monitors (ICMP/TCP/HTTP/DNS with
latency and loss) ([network-monitors](https://beszel.dev/guide/network-monitors),
[releases](https://github.com/henrygd/beszel/releases)). homelab-maint's probe plane has
`http/tcp/docker/systemd/command/file_age/json` types but no TLS or latency dimension
(`homelab_maint/probes.py:99`, `etc/probes.toml`). Add a `tls` probe type (stdlib `ssl` +
`socket.getpeercert()` → days-to-expiry) and a latency field to the existing `tcp`/`http` probes,
surfaced through the existing probe state machine, debounce, Kuma push and `monitors.json`.
*Why it fits:* pure stdlib read-only socket work, sits exactly on the existing extension point
(`docs/EXTENDING.md` §7), no new daemon. High value here because the public site is fronted by
Cloudflare and the host terminates several internal TLS endpoints.

### 2. A generic webhook notification channel with an SSRF guard — **effort M, risk medium**

The single biggest capability gap: homelab-maint can only page by SMS and Gmail (`notify.py:148,66-84`),
where Beszel reaches ~24 services through shoutrrr ([notifications](https://beszel.dev/guide/notifications)).
Add one `webhook` transport that POSTs a small JSON body via stdlib `urllib.request`, behind the
existing route → dedupe → budget → quiet-hours → claim pipeline, so ntfy/Gotify/Discord/Slack all
work from configuration. **Borrow Beszel's SSRF guard verbatim in spirit:** refuse private/loopback/
link-local/CGNAT destinations and check the resolved address in the dialer
([notification_client.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/notification_client.go)).
*Why it fits:* stdlib-only, no pip; the pipeline already has budgets, dedupe, retries, a circuit
breaker and a durable outbox that Beszel lacks (`notify.py:1399-1709`), so a new leg inherits them.

### 3. A coarse retention tier + rollup with peak preservation — **effort M, risk low/medium**

homelab-maint keeps 7 d of hourly sensor data (`metrics_ring.py:57`) and a 60-min live window
(`live.py:99`); `history.jsonl` is trimmed by **size**, not by an age/rollup policy (`core.py:526`).
Beszel's model is the template: average short records into longer ones and keep `max` so spikes
survive (`records.go`), with a 30-day top tier (`records_deletion.go`). Add a `metrics-daily.json`
(30–90 d of per-day mean/max/min for the 15 ring metrics) written by the existing per-minute sampler,
and an explicit age-based rollup+prune step for `history.jsonl` (raw 15-min 30 d → daily 1 y).
*Why it fits:* a new JSON file plus a pure function, no DB; the ring already stores `sum/cnt/max/min`
(`metrics_ring.py:118`), so the peak-preserving rollup is a small extension.

### 4. Deploy Beszel as a **complement**, not a rewrite — **effort S, risk low**

homelab-maint already probes external monitors (Glances, Uptime Kuma) and mirrors Kuma monitors into
its own probe plane (`SPEC4.md:81`, `etc/probes.toml:34,146,186`). Beszel is the one tool that
already does per-container history and multi-host without new local code. Register it the same way:
a `probes.toml` entry for the hub, optionally a Kuma/Beszel heartbeat, and a Homarr card via the
existing third-party widget ([Homarr](https://homarr.dev/docs/widgets/beszel-system-grid/)), using
`payloads.py` if a native tile is wanted.
*Why it fits:* zero new daemon, zero mutation, reuses the existing integration path, and buys the
per-container/multi-host history (#6 below) without reimplementing it.

### 5. Selectable chart time ranges — **effort M, risk low**

Beszel's `chart-time-select` maps ranges to granularities, default `1h`
([chart-time-select.tsx](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/src/components/charts/chart-time-select.tsx)).
homelab-maint charts show one fixed window per tab (`web/static/tabs/capacity.js:1`). Add a range
switcher (e.g. 1 h / 24 h / 7 d, later 30 d with idea #3) to the Capacity and Health charts, reusing
`charts.js` and the pure helpers in `lib.js` (safe `niceAxis`, `linePath`).
*Why it fits:* presentation-only, no new data source for 1 h/24 h/7 d (live history + ring already
carry them), no external requests (the site's size/CSP budget holds).

### 6. A small per-container resource history ring — **effort M, risk medium**

Beszel stores per-container CPU/mem/network history (`container_stats`); homelab-maint keeps only the
current `top_cpu`/`top_mem` snapshots in `live.json` (`live.py:43`). Extend the live sampler to fold a
bounded top-N set into a small ring (e.g. last 24 h of hourly means, or a 60-min 5 s window for the
Live tab), and show one container trend.
*Why it fits:* cgroup v2 reads already exist (`live.py` docker/cgroup readers), read-only, one more
bounded file. **Risk is the file growth** — bound it and prune like the ring (`metrics_ring.py:197`).

### 7. Surface alert rules as reviewable cards (threshold + duration + enabled) — **effort S/M, risk low**

Beszel's alerts are editable data rows with explicit thresholds, durations and an enabled flag
([alerts.ts](https://raw.githubusercontent.com/henrygd/beszel/main/internal/site/src/lib/alerts.ts)).
homelab-maint's thresholds live in task code and its registry is already published read-only
(`web/static/rules-*.js`, the Maintenance tab's "What the script does"). Add per-check cards to the
Health tab that render the effective threshold, confirm-runs/duration and alert/off state from
`rules.json`, so the owner can audit "what will page me" without the CLI.
*Why it fits:* the registry is already the source of truth and `rules.json` already reaches the
browser; this is a read-only view, no rule editing.

### 8. Pending-package-update check and tile — **effort S, risk low**

Beszel 0.21 added pending package updates ([releases](https://github.com/henrygd/beszel/releases)).
homelab-maint observes `os_jobs`/`apt-daily` and `config_drift` but has no "N updates pending" signal
(`README.md:33`, `homelab_maint/tasks/checks_health.py:799`). Add a C0 check reading
`apt-get -s upgrade` / `unattended-upgrades` state and a widget tile.
*Why it fits:* a read-only check in the existing check tier and the existing widget pipeline
(`docs/EXTENDING.md` §3, §12).

### 9. Age-based retention/prune for `history.jsonl` and the audit log — **effort S/M, risk low-medium**

Related to #3: make retention explicit (raw 15-min records kept 30 d, daily rollups a year) instead
of the current "40 MiB, then oldest half" trim (`core.py:526`), and document it. Beszel's hourly
delete job with per-bucket ages is the reference design.
*Why it fits:* a routine step and a pure trimming function; improves the capacity tab's long-range
accuracy without a database.

### 10. Adopt Beszel's duration semantics for check-tier alerts — **effort S, risk low**

Beszel requires a threshold to hold N minutes (1–60) and averages the samples in that window
([alerts_system.go](https://raw.githubusercontent.com/henrygd/beszel/main/internal/alerts/alerts_system.go)).
homelab-maint's equivalent is `alert_confirm_runs = 2` on a fixed 15-min tier ≈ 30 min, with
per-task overrides (`README.md:359`, `core.py`). Expose an explicit `for_minutes`/duration value in
the rules registry that maps onto runs (and, on the 1-min probe tier, onto real minutes), so a rule
reads as "CPU > 90% for 10 min" the way Beszel's does.
*Why it fits:* the confirm-runs machinery already exists; this is a clearer, reviewable expression of
it in the registry, not a behavioural rewrite.

### 11. (Optional) Express the heartbeat as a structured JSON body — **effort S, risk low**

Beszel's hub heartbeat POSTs a JSON body with system/alert counts to an external URL
([heartbeat](https://beszel.dev/guide/heartbeat)); homelab-maint's Kuma heartbeat and `/heartbeat`
are minimal (`server.py`, `core.py:502`). Enriching the `/heartbeat` body (counts, worst level,
pipeline state) would help external watchers without changing behaviour.
*Why it fits:* one function, no new state.

---

## 4. Non-goals / anti-ideas

These Beszel properties should **not** be copied here; they conflict with homelab-maint's stated
constraints.

- **No SQLite/PocketBase or any database.** homelab-maint is stdlib-only and file-based by design
  (`README.md:2`, `SPEC.md:1-6`). Adopt Beszel's *rollup idea*, not its storage engine.
- **No Node/React/recharts toolchain at runtime.** The site is dependency-free by contract
  ("no external requests", `UI_CONTRACT.md:78-86`); the hand-rolled SVG charts already meet the need.
- **No multi-host rewrite.** homelab-maint is explicitly single-host (`SPEC.md:1`); the multi-host
  case is better served by running Beszel alongside (idea #4).
- **No pull-agent with outbound credentials.** It would widen the attack surface against the
  project's read-only, loopback-only posture (`web/README.md:109-120`).
- **Do not drop acknowledgement to match Beszel's auto-resolve-only model.** The ack system
  (`README.md:439-468`) is a genuine homelab-maint advantage; instead, keep Beszel's useful
  *duration* semantics (idea #10) without adopting its lack of ack.
- **No notification retries deficiency.** Beszel's notification client has no retries; the local
  outbox/retry/circuit-breaker (`notify.py:1399-1709`) is better and must be kept.

---

## 5. Source index

**Beszel primary sources**
- Repo/README: https://github.com/henrygd/beszel
- Docs: https://beszel.dev/guide/what-is-beszel, /hub-installation, /agent-installation, /security,
  /notifications, /network-monitors, /heartbeat, /smart-data, /systemd, /gpu, /oauth, /user-accounts,
  /environment-variables, /rest-api, /getting-started, /guide/rest-api
- Releases/tags: https://github.com/henrygd/beszel/releases , https://api.github.com/repos/henrygd/beszel/tags?per_page=100
- go.mod: https://raw.githubusercontent.com/henrygd/beszel/main/go.mod
- Hub polling: internal/hub/systems/system.go, internal/hub/systems/system_manager.go
- Agent: agent/agent.go, agent/connection_manager.go
- Records: internal/records/records.go, internal/records/records_deletion.go, internal/hub/hub.go
- Alerts: internal/alerts/alerts_system.go, alerts_status.go, alerts.go, notification_client.go,
  internal/site/src/lib/alerts.ts
- Schema/migrations: internal/migrations/0_collections_snapshot_0_20_0.go
- Frontend: internal/site/package.json, internal/site/embed.go,
  internal/site/src/components/charts/chart-time-select.tsx, internal/site/src/lib/utils.ts
- Auth: internal/hub/hub.go, internal/hub/collections.go
- Images: internal/dockerfile_hub, internal/dockerfile_agent, https://hub.docker.com/v2/repositories/henrygd/beszel/tags

**homelab-maint primary sources**
- `README.md`, `SPEC.md`, `SPEC2.md`, `SPEC3.md`, `SPEC4.md`, `SPEC5.md`, `SPEC6.md`
- `docs/EXTENDING.md`, `docs/INTEGRATION.md`, `docs/HOMARR_V2_WIDGETS.md`, `web/README.md`, `web/UI_CONTRACT.md`, `widgets/CONVENTIONS.md`
- `homelab_maint/metrics_ring.py`, `live.py`, `notify.py`, `incidents.py`, `acks.py`, `acks_auth.py`,
  `reports.py`, `registry.py`, `probes.py`, `core.py`, `payloads.py`, `payloads_metrics.py`,
  `publish.py`, `server.py`, `tasks/checks_basic.py`, `tasks/checks_health.py`, `tasks/monitors.py`
- `web/app.py`, `web/static/index.html`, `app.js`, `lib.js`, `dom.js`, `charts.js`,
  `web/static/tabs/{live,health,incidents,maintenance,reports,capacity}.js`
- `widgets/ops-*.jsx`, `widgets/ops-*.json`, `widgets/build_v2.py`, `widgets/install_homarr_widgets.py`
- `etc/probes.toml`, `etc/rules.d/*.toml`
