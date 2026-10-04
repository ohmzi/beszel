# Extending Beszel: adding a custom metric and alert end-to-end

Research notes against the Beszel source tree at commit `ec96c13` (release `0.21.0`). Every claim
below cites the file and line where it lives. Line numbers are from this checkout and will drift.

> Read-only survey. Nothing in the tree was modified except this document.

## TL;DR

Beszel is three moving parts wired by one shared Go struct:

- **Agent** (`agent/`) collects a `system.CombinedData` payload and sends it over a
  CBOR-framed WebSocket (or SSH stdin/stdout) in response to a hub request.
- **Hub** (`internal/hub/`) decodes that payload into `system.CombinedData`, writes a
  `system_stats` record whose `stats` column is a **schemaless JSON blob** of the `system.Stats`
  struct, and updates the `systems.info` JSON blob used by the dashboard.
- **Site** (`internal/site/`, React) reads `system_stats` / `realtime` and renders charts whose
  `dataKey` closures read fields off `stats`.

The single most important fact for extension work: **`system_stats.stats` is a JSON field with no
per-metric schema** (`internal/migrations/0_collections_snapshot_0_20_0.go` around the
`system_stats` block, field `"name": "stats"`, `"type": "json"`, `maxSize: 2000000`). Adding a new
agent metric field therefore needs **no migration** — the struct's `json`/`cbor` tags are the
schema. Migrations are only needed for new **collections** (tables), new **alert name enum values**,
or new relation/fields on non-JSON collections. Adding a new **alert type** touches a select enum
plus the server evaluator plus the client `alertInfo` map.

There is **no OpenAPI/Swagger spec** in the repo (searched all `.go/.json/.yaml/.yml`). i18n is
Lingui `.po` files; extracting is part of the frontend build.

---

## 1. Agent metric collection

### Entry point and collection flow

- `agent/agent.go` defines `type Agent` (line 25) holding all managers and caches, and
  `func (a *Agent) gatherStats(options common.DataRequestOptions) *system.CombinedData`
  (line 182). It takes the agent lock, checks the per-cache-interval `systemDataCache`,
  fills `system.CombinedData{Stats, Info}` (lines 193-196), then adds containers (line 201),
  network monitors (line 209), systemd services (line 215), package updates (line 232), extra
  filesystems (lines 236-258), and finally attaches static details (line 263).
- `agent/system.go:144` `getSystemStats(cacheTimeMs uint16) system.Stats` is where the core
  metrics are collected: battery (148), CPU (159), load average (183), memory/swap/ZFS ARC (193),
  disk usage (223), disk I/O (226), storage pools (229), network (233), temperatures (237), fans
  (240), GPU (243), Wi-Fi (278). Each branch writes a field on the local `systemStats`.
- Fast realtime requests use `CacheTimeMs: 1000` and are served from the same function with a
  short cache; the default live interval is `defaultDataCacheTimeMs = 60_000` (`agent/agent.go:23`).

### The stats struct / wire schema

The canonical schema is **`internal/entities/system/system.go`**:

- `type Stats struct` (line 22) with fields (JSON key, CBOR key):
  `Cpu`(cpu/0), `Mem`(m/2), `MemUsed`(mu/3), `MemPct`(mp/4), `MemBuffCache`(mb/5),
  `MemZfsArc`(mz/6), `Swap`(s/7), `SwapUsed`(su/8), `DiskTotal`(d/9), `DiskUsed`(du/10),
  `DiskPct`(dp/11), `DiskReadPs`(dr/12), `DiskWritePs`(dw/13), `NetworkSent`(ns/16),
  `NetworkRecv`(nr/17), `Temperatures`(t/20), `ExtraFs`(efs/21), `GPUData`(g/22),
  `Bandwidth`(b/26), `LoadAvg`(la/28), `Battery`(bat/29), `NetworkInterfaces`(ni/31),
  `DiskIO`(dio/32), `CpuBreakdown`(cpub/33), `CpuCoresUsage`(cpus/34), `DiskIoStats`(dios/35),
  `Fans`(f/36), `Batteries`(bats/37), `DiskIOTotal`(diot/38), `ZfsPools`(z/39), `WiFi`(wf/40).
  `Max*` fields are computed only during rollups (cbor tag `-`).
  Note **CBOR keys are `keyasint`**; new numeric keys must be unique and appended, never reused.
- `type Info struct` (line 167): the small always-needed summary persisted to `systems.info`. Includes
  `Cpu`, `MemPct`, `DiskPct`, `GpuPct`(g/12), `DashboardTemp`(dt/13), `LoadAvg`, `ExtraFsPct`,
  `Services []uint16`(sv/22, `[total, failed]`), `Battery`, `RootDiskName`, `PackageUpdates
  []uint16`(pu/25), `WiFi`, `SystemdLogs`(jl/27).
- `type Details struct` (line 201): static once-per-connection host info (hostname, kernel, CPU model,
  OS, memory total, plus `SmartInterval`/`ZfsInterval` refresh hints).
- `type CombinedData struct` (line 217): the envelope actually serialized —
  `Stats`(0), `Info`(1), `Containers`(2), `SystemdServices`(3), `Details`(4),
  `SystemdServicesUpdated`(5), `Monitors`(6).
- Supporting entities: `FsStats` (line 121), `ZfsPool` (line 71), `GPUData` (line 109),
  `WiFi` (line 17), `Uint8Slice` (line 86, custom JSON marshal).

### Which files collect what

| Metric | Agent file(s) |
| --- | --- |
| CPU / per-core / breakdown | `agent/cpu.go`, `agent/cpu_linux.go`, `agent/cpu_unsupported.go` |
| Memory, disk usage, disk I/O, load, temps orchestration | `agent/system.go` `getSystemStats` |
| Disk/filesystem | `agent/disk.go` (`updateDiskUsage`, `initializeDiskInfo`) |
| Disk I/O counters | `agent/disk.go`, `agent/network_counters_linux.go` style delta trackers |
| Network | `agent/network.go` |
| Docker / Podman containers | `agent/docker.go`, `agent/docker_registry.go`, `agent/docker_image_updates.go` |
| SMART | `agent/smart.go` (+ `smart_windows.go`, `smart_nonwindows.go`) |
| GPU (NVIDIA/AMD/Intel) | `agent/gpu.go`, `gpu_nvml*.go`, `gpu_amd_linux.go`, `gpu_intel*.go`, `gpu_nvtop.go` |
| systemd services | `agent/systemd.go` (+ `systemd_nonlinux.go`) |
| Temperatures / fans / battery / Wi-Fi | `agent/sensors.go`, `agent/fans*.go`, `agent/battery/`, `agent/wifi/` |
| ZFS / btrfs storage pools | `agent/storage_pool.go`, `agent/zfs/`, `agent/btrfs/`, `agent/mdraid_linux.go`, `agent/emmc*.go` |
| Pending package updates | `agent/package_updates.go` |
| Network monitors (ping/tcp/http/dns) | `agent/network_monitor*.go` |

Collectors that shell out and can be slow (`zpool list`, `smartctl`, package managers) use a
**background-refresh-into-cache** pattern so they never block a stats response:
`agent/package_updates.go` (manager at line 42, `get` at 93, `refresh` at 118) and
`agent/storage_pool.go` (`poolStats` at 191, background goroutine at 202) are the clearest
templates.

### Serialization and transport

- Response envelope: `common.AgentResponse` (`internal/common/common-ws.go:44`) — CBOR map keys
  `Id`(0), legacy typed fields `SystemData`(1)/`Fingerprint`(2)/…, plus generic `Data`(7) and
  `SmartComplete`(8). Request envelope: `common.HubRequest[T]` (line 37) with `Action`(0),
  `Data`(1), `Id`(2). Action enum `GetData, CheckFingerprint, GetContainerLogs, GetContainerInfo,
  GetSmartData, GetSystemdInfo, GetZfsData, SyncNetworkMonitors, GetPackageUpdates,
  GetSystemdLogs` (lines 12-34) — comment says "Add new actions here…".
- Agent → hub encode: `WebSocketClient.sendMessage` / `sendMessageOnConn`
  (`agent/client.go:375-394`) does `cbor.Marshal` then `conn.WriteMessage(gws.OpcodeBinary, …)`.
  `sendResponseOnConn` (line 403) wraps the payload in `AgentResponse` when a request ID is present.
  Handshake/version negotiation lives in `handleAuthChallenge` (line 280) and headers
  `X-Token`/`X-Beszel` (lines 189-192).
- Hub → agent request/response plumbing: `internal/hub/ws/ws.go` (`WsConn`, `handleAgentRequest`
  at 139, `SendRequest` at 180) and `internal/hub/ws/request_manager.go`.
- SSH fallback: the hub can drive the same request over SSH (`internal/hub/transport/ssh.go`,
  `internal/common/common-ssh.go`); `hub/systems/system.go:893 fetchDataViaSSH` encodes a
  `HubRequest` to stdin and decodes `AgentResponse` from stdout.
- Agent request handlers: `agent/handlers.go` — `HandlerRegistry` (line 43), registration
  (lines 53-62), `GetDataHandler` (line 100) calls `gatherStats` and returns `*CombinedData`.

---

## 2. Hub ingest and storage

### Receive → validate → write

- Connection: `internal/hub/agent_connect.go` `agentConnect` (line 59) validates `X-Token` and
  `X-Beszel` headers (`validateAgentHeaders`, 140), upgrades to WebSocket (`verifyWsConn`, 105),
  authenticates by fingerprint, then `sm.AddWebSocketSystem` (line 136).
- Per-system updater: `internal/hub/systems/system.go` `StartUpdater` (line 106) ticks every
  `interval` ms; `update` (line 162) issues `DataRequestOptions{CacheTimeMs: interval}`,
  `fetchDataFromAgent` (line 774) prefers WebSocket (`fetchDataViaWebSocket`, 803, 30 s timeout)
  and falls back to SSH (`fetchDataViaSSH`, 893).
- Validation is essentially **type/size validation from the CBOR/JSON decode plus PocketBase field
  constraints**; `migrateDeprecatedFields` (line 1077) normalises old agent payloads. There is no
  per-metric allowlist — unknown struct fields are simply not decoded into the struct.
- Write: `createRecords` (line 251) opens one transaction (`hub.RunInTransaction`, 261) and:
  - inserts a `system_stats` record with `system`, `stats = data.Stats`, `type = "1m"`
    (lines 263-272);
  - inserts `container_stats` (+ upserts `containers`) when containers are present (276-293);
  - writes `systemd_services` via `createSystemdStatsRecords` (298-301, 393);
  - upserts `system_details` (305-313, 368);
  - updates `network_monitors` / `network_monitor_stats` (315-319, 441);
  - syncs `zfs_pools` health (321);
  - **last** updates the `systems` record: `status="up"`, `info` = `system.Info` plus a `*GpuPct`
    (325-338). The comment notes the order exists because the `systems` save triggers alerts.
- `createContainerRecords` (576) and `createSystemdStatsRecords` (393) are precedents for
  **upsert-into-a-side-table** (`ON CONFLICT(id) DO UPDATE`), useful for a maintenance-check table.

### Collections and granularity

Collections are created by `internal/migrations/0_collections_snapshot_0_20_0.go` (a PocketBase
collection snapshot applied at startup). Relevant collections:
`systems`, `system_details`, `system_stats`, `container_stats`, `containers`, `systemd_services`,
`smart_devices`, `zfs_pools`, `network_monitors`, `network_monitor_stats`, `alerts`,
`alerts_history`, `fingerprints`, `universal_tokens`, `users`, `user_settings`, `quiet_hours`.

- **Time series** live in `system_stats` (field `stats` = JSON, `type` = select
  `1m/10m/20m/120m/480m`; index on `system,type,created`) and `container_stats` (same shape).
  Network monitor history goes in `network_monitor_stats` (one row per monitor; `created` is a
  unix-ms number, not a date).
- One agent sample per `interval` is stored as `type="1m"` (`createRecords`, line 270).
- Realtime (1-second) data is **not stored**; it is broadcast only (section 3).

### Rollups

- Computed by `internal/records/records.go` `CreateLongerRecords` (line 42), scheduled by a cron
  in `internal/hub/hub.go:147` (`"*/10 * * * *"`). Deletion runs `"8 * * * *"`
  (`hub.go:145` → `rm.DeleteOldRecords`).
- Tiers (lines 44-70): `1m → 10m` (needs ≥9 records), `10m → 20m` (≥2), `20m → 120m` (≥6),
  `120m → 480m` (≥4). Each system's shorter records are averaged into one longer record.
- Averaging: `AverageSystemStats` (244) → `AverageSystemStatsSlice` (263). **Adding a numeric
  metric requires adding its accumulation and division here**, or it rolls up as zero. This apply
  to `system_stats`/`container_stats`; `network_monitor_stats` has its own aggregator (697).
- Retention: `internal/records/records_deletion.go` deletes by tier (1h / 12h / 24h / 7d / 30d)
  across `system_stats`, `container_stats`, `network_monitor_stats`.

### Where a new field/schema change goes

- **New metric field on the JSON blob** (`system.Stats` / `system.Info`): no migration. Add the
  field with `json` and `cbor` tags to `internal/entities/system/system.go`, populate it in the
  agent, and (if it must survive rollups) teach `AverageSystemStatsSlice` about it.
- **New collection/table** (e.g. a maintenance-check snapshot table like `systemd_services`): add a
  new migration file following `internal/migrations/1790193183_network_monitor_cert.go`
  (`app.FindCollectionByNameOrId`, mutate `Fields`, `app.Save`) — this is the template for
  editing an existing collection. For a brand-new collection, follow the snapshot style and call
  `app.ImportCollectionsByMarshaledJSON`.
- Migrations are `init()`-registered with `m.Register(...)` and imported for side effects in
  `internal/cmd/hub/hub.go:12` (`_ "…/internal/migrations"`). `migratecmd` is registered at
  `hub.go:60`; `Automigrate` is on only when `ENV=dev`. PocketBase runs migrations at boot.

---

## 3. UI

### Data fetch

- REST/collection reads: `internal/site/src/components/routes/system/chart-data.ts`
  `getStats<T>` (line 54) queries `system_stats`/`container_stats` with
  `filter: system && created > … && type`, `fields: "created,stats"`, sorted by `created`.
- The System page hook: `internal/site/src/components/routes/system/use-system-data.ts`
  `useSystemData` (line 34). It loads cached stats, fetches per `chartTime` (lines 203-258), and
  for the `1m` view subscribes to PocketBase realtime topic **`rt_metrics`** (lines 144-184) whose
  payload is `{ container, info, stats }`.
- Realtime is produced server-side in `internal/hub/systems/system_realtime.go`: worker ticks every
  second (`startRealtimeWorker`, 141), fetches with `CacheTimeMs: 1000`
  (`fetchRealtimeDataAndNotify`, 158), marshals `CombinedData` via `marshalRealtimeData` (203) and
  broadcasts through `notify` (220). Any field added to the struct is automatically included.
- Chart-time → record-type mapping: `chartTimeData` in `internal/site/src/lib/utils.ts:128`
  (`1m`/`1h` → record type `1m`, `12h` → `10m`, `24h` → `20m`, `1w` → `120m`, `30d` → `480m`).

### Rendering: charts and tiles

- The page: `internal/site/src/components/routes/system.tsx` imports and lays out each chart
  component (lines 9-28, default grid at 92-176, tabs at 178-319). Charts receive shared props
  `{ chartData, grid, dataEmpty, showMax, isLongerChart, maxValues }` (`coreProps`, line 90).
- Chart components: `internal/site/src/components/routes/system/charts/*.tsx`. Each defines
  **`dataPoints`** as `{ label, dataKey: (record) => record.stats?.<field>, color }` and passes
  them to `AreaChartDefault`/`LineChartDefault`
  (`internal/site/src/components/charts/area-chart.tsx`, `line-chart.tsx`).
  `charts/sensor-charts.tsx` `TemperatureChart` (line 100) is the best template for a dynamic
  keyed map; `BatteryChart` (line 12) for a simple scalar.
- The card wrapper is `internal/site/src/components/routes/system/chart-card.tsx` (`ChartCard`,
  line 87; `FilterBar`, line 16).
- Adding a chart = write a `charts/<name>-charts.tsx` component reading a `stats` field, then
  mount it in `system.tsx` (and, for tabs mode, both `defaultLayout` and the right `TabsContent`).

### Dashboard tiles / systems table

- `internal/site/src/components/systems-table/systems-table-columns.tsx` defines one column per
  tile, each with `accessorFn: ({ info }) => info.<key>`, an `Icon`, and a `cell`. Examples that
  map 1:1 to an `Info` field: `cpu`→`info.cpu` (183), `memory`→`info.mp` (192), `disk`→`info.dp`
  (200), `gpu`→`info.g` (209), `net`→`info.bb` (261), `temp`→`info.dt` (283), `battery`
  →`info.bat` (305), `wifi` (351), `services`→`info.sv` (402), `updates`→`info.pu` (459).
  A new dashboard tile = a new column entry (plus optional visibility handling in the table).
- Client types are hand-written in `internal/site/src/types.d.ts`: `SystemStats` (line 96),
  `SystemInfo` (41), `AlertInfo`/`AlertUnit` (442/466). A new metric should be added to
  `SystemStats`/`SystemInfo` here for TypeScript safety (the `dataKey` closures are typed against it).

---

## 4. Alerts

### Rule definition and storage

- Rules are rows in the `alerts` collection: `user`, `system` (relations), `name`
  (select enum), `value`, `min`, `triggered`, hidden `state` (JSON) and `pending_since`, plus
  timestamps. See the `alerts` block in `internal/migrations/0_collections_snapshot_0_20_0.go`
  (starts line 13; the `name` enum values are lines ~72-90):
  `Status, CPU, Memory, Disk, Temperature, Bandwidth, GPU, LoadAvg1, LoadAvg5, LoadAvg15,
  Battery, ContainerHealth, SystemdFailed, CPUIOWait, CPUSteal, NetworkMonitorLoss`.
- Create/update/delete API: `internal/alerts/alerts_api.go` `UpsertUserAlerts` (line 17, POST
  `/api/beszel/user-alerts`) and `DeleteUserAlerts` (90). These save with **`SaveNoValidate`**
  (line 74), so the select enum is not enforced on that path — but a new name should still be
  added to the enum for correctness/readability.
- Routes registered in `internal/hub/api.go:198-199` under `apiAuth := se.Router.Group("/api/beszel")`.

### Evaluation

- Numeric threshold alerts: `internal/alerts/alerts_system.go` `HandleSystemAlerts` (line 39).
  - It excludes the non-numeric alerts (line 50): `Status`, `SystemdFailed`, `ContainerHealth`,
    `NetworkMonitorLoss`.
  - **Current-value switch** at lines 64-116 maps `name → val` (CPU/Memory/Disk/Temperature/
    Bandwidth/LoadAvg*/GPU/Battery, default = CPU-breakdown states `CPUIOWait`/`CPUSteal`).
  - It re-reads `system_stats` rows for the duration window (169-186) and accumulates per-name at
    lines 232-305 (the historical switch), then averages and compares against `value`/`min`
    (310-368). A new numeric metric needs an entry in **both** switches.
- Binary/state alerts (the right model for a maintenance pass/warn/crit check):
  `internal/alerts/alerts_systemd.go` `HandleSystemdAlerts` (line 26) reads the latest snapshot from
  `systemd_services` (`queryServiceStates`, 78), fires only on state change (line 60), and sends
  via `sendSystemdAlert` (104). `internal/alerts/alerts_container.go` and `alerts_zfs.go` are
  similar.
- Alert cache: `internal/alerts/alerts_cache.go` `CachedAlertData` (line 13), `GetAlertsByName` (177),
  `GetAlertsExcludingNames` (189); kept in sync by record hooks in `bindEvents` (64).
- History: `internal/alerts/alerts_history.go` writes `alerts_history` around triggered transitions
  (hooks bound in `internal/alerts/alerts.go:122-123`).
- Trigger point: `internal/hub/systems/system_manager.go:254` calls `HandleSystemAlerts` from the
  `OnRecordAfterUpdateSuccess("systems")` hook (bound at line 140) when a system becomes `up`.
  `HandleNetworkMonitorAlerts` is called from `createRecords` (`hub/systems/system.go:361`).
- Notification delivery: `internal/alerts/alerts.go` `SendAlert` (line 214) → `sendShoutrrrAlert`
  (289) and email; quiet-hours check `IsNotificationSilenced` (147).
  The `alerts` package is wired through the `hubLike` interface (line 17).

### Client

- `internal/site/src/lib/alerts.ts` `alertInfo` (line 11) is the per-type UI metadata
  (label, unit, icon, desc, min/max/step, `noDuration`/`noThreshold`/`triggeredDesc`, `units`).
- The form enumerates it: `internal/site/src/components/alerts/alerts-sheet.tsx` line 29
  (`alertKeys = Object.keys(alertInfo)`), rendering each type; the sheet POSTs to
  `/api/beszel/user-alerts` (line 59). `active-alerts.tsx`, `alerts-history-columns.tsx`,
  `settings/alerts-history-data-table.tsx` also key off `alertInfo`.

### What adding a NEW alert type touches

1. `internal/migrations/…` new migration to append the name to the `alerts.name` select enum
   (optional if only ever created via `SaveNoValidate`, but do it).
2. Server evaluation: a case in `HandleSystemAlerts` (numeric) **or** a dedicated
   `Handle*Alerts` function plus its call site (binary) — `alerts_systemd.go` is the model.
3. If binary/state, a source of truth (a snapshot collection or an `Info` field) and a
   stale-state resolver at startup (e.g. `resolveSystemdAlerts`, `alerts_systemd.go:150`, bound at
   `alerts.go:136`).
4. `internal/site/src/lib/alerts.ts` `alertInfo` entry (and any of the history/active components
   that need special-casing).
5. New UI strings get wrapped in `t`/`Trans`; run Lingui extract/compile (part of the frontend
   build) so `src/locales/**` regenerate.
6. Tests: the `_test.go` files beside each `alerts_*.go`.

---

## 5. Extension seam — ordered checklist for a custom agent-reported metric

**A. Add the metric field**
1. `internal/entities/system/system.go` — add the field to `Stats` (time-series) and/or `Info`
   (dashboard tile) with unique `json` and `cbor` tags. If it is a map keyed by name, follow
   `Temperatures`/`Fans`/`ZfsPools`.
2. `internal/records/records.go` `AverageSystemStatsSlice` (line 263) — accumulate/divide the new
   field so rollups keep it (skip if the metric is only in `Info`).
3. `internal/site/src/types.d.ts` — add it to `SystemStats` (line 96) / `SystemInfo` (line 41).

**B. Collect it in the agent**
4. New collector file under `agent/` (e.g. `agent/mycheck.go`), modelled on
   `agent/package_updates.go` (background refresh + cached result) for anything that shells out.
5. Wire the manager into `Agent` (`agent/agent.go:25` fields, init near lines 126-169) and call it
   from `getSystemStats` (`agent/system.go:144`) writing the new `systemStats` field.
6. If it is expensive, gate it on the default cache interval like systemd/ZFS do
   (`agent/system.go:215`, 278) and/or an env-configured interval (`SMART_INTERVAL`,
   `ZFS_INTERVAL`, `PACKAGE_UPDATES_INTERVAL` precedents in `agent/agent.go:116-145`).
7. For out-of-band detail (like SMART/ZFS), optionally add a `WebSocketAction`
   (`internal/common/common-ws.go:12`), a handler (`agent/handlers.go`), a hub fetch method
   (`hub/systems/system.go` `Fetch*FromAgent`), and a persistence path (see `system_smart.go`,
   `system_zfs.go`).

**C. Persist / roll up (no change needed for JSON fields)**
8. Nothing for a `Stats`/`Info` field — `createRecords` (`hub/systems/system.go:251`) writes the
   whole struct into `system_stats.stats` and `systems.info` automatically.
9. If you need a **side table** (per-check rows, history, list UI), add a migration
   (`internal/migrations/…`, model on `1790193183_network_monitor_cert.go`) and an upsert writer
   (model on `createSystemdStatsRecords`, `hub/systems/system.go:393`), plus collection rules in
   `internal/hub/collections.go` (81-99 list the system-scoped read collections).

**D. Surface it in the UI**
10. Chart: new `internal/site/src/components/routes/system/charts/<name>-charts.tsx` with
    `dataKey: ({ stats }) => stats?.<field>`; mount it in
    `internal/site/src/components/routes/system.tsx` (grid + tabs).
11. Tile: new column in `internal/site/src/components/systems-table/systems-table-columns.tsx`
    with `accessorFn: ({ info }) => info.<field>` (follow the `services`/`updates` columns).
12. i18n: wrap labels in `t`/`Trans`; the frontend build extracts locales.

**E. (Optional) Alert for it**
13. Numeric: add the name to the `alerts.name` enum (new migration), a `case` in both switches of
    `internal/alerts/alerts_system.go` (lines 64 and 232), and `alertInfo` in
    `internal/site/src/lib/alerts.ts`. Binary state: mirror `alerts_systemd.go` and add its startup
    resolver + call site.

### Things that make this harder

- **`AverageSystemStatsSlice` is exhaustive.** Any new numeric field silently rolls up as zero
  unless added there. Forgetting this is the most likely subtle bug.
- **Two parallel encodings.** Go structs carry both `json` (storage/REST) and `cbor` (wire) tags;
  missing one breaks the metric on one path only. CBOR `keyasint` numbers must not collide.
- **Stat alerts need two switch edits** (current value and historical accumulation) or the alert
  works for `min=1` but misbehaves for longer windows.
- **Schema/validation.** The `alerts.name` select enum is the only place a new alert name is
  schema-constrained; JSON metric fields are schema-free. New collections require migrations and
  collection rules in `collections.go` (auth is otherwise default-deny: `system_scoped_read_rule`).
- **Migrations are append-only.** The 0.20.0 snapshot already ran on existing DBs; change schema
  by adding a new `m.Register` migration, not by editing the snapshot.
- **Generated/embedded frontend.** `internal/site/dist` is `go:embed`-ed
  (`internal/site/embed.go`), so Go-only builds serve a blank page unless the UI is built first
  (`build-hub` depends on `build-web-ui`, `Makefile`).
- **i18n.** UI strings live in Lingui catalogs under `internal/site/src/locales/`; the build runs
  `lingui extract --overwrite && lingui compile`.
- **No OpenAPI spec**, so there is no generated client/schema to update — only the hand-written
  `types.d.ts`.

---

## 6. Build / run

### Tool versions
- Go: `go 1.27.1` (`go.mod` line 3). Module path is `github.com/henrygd/beszel`.
- Frontend: `internal/site/package.json` — React 19, Vite 7, TypeScript 5.9, Lingui 5, Recharts 2,
  PocketBase JS SDK 0.26, Biome. There is **no `packageManager` field**; the Makefile prefers
  **bun** and falls back to **npm** (`build-web-ui`, `dev-server`). A `pnpm-lock`/prompt mention in
  the task does not match this tree — it uses `bun.lock` and `package-lock.json`.

### Makefile targets (`Makefile`)
- `build` = `build-agent` + `build-hub`.
- `build-agent`: `tidy` + Windows-only .NET/smartctl steps, then
  `go build $(AGENT_GO_TAGS) -o ./build/beszel-agent_$(OS)_$(ARCH) ./internal/cmd/agent`.
  `AGENT_GO_TAGS` can be `-tags glibc` for NVML (`NVML=auto|true|false`).
- `build-hub`: depends on `build-web-ui` unless `SKIP_WEB=true`; builds
  `./internal/cmd/hub` to `./build/beszel_$(OS)_$(ARCH)`.
- `build-web-ui`: `bun install --cwd ./internal/site && bun run build` (or npm fallback).
- `build-hub-dev`: builds with `-tags development` against a stub `internal/site/dist/index.html`
  (the dev server proxies to Vite instead of embedding).
- `dev-server` / `dev-hub` / `dev-agent`: `dev-hub` runs `go run -tags development . serve
  --http 0.0.0.0:8090` with `ENV=dev`; `dev-server` runs Vite (`bun run dev --host`); `dev-agent`
  runs the agent. `make dev` runs all three (proxy target is `localhost:5173`,
  `internal/hub/server_development.go:44`).
- `test`: `go test -tags='testing no_ui' ./...`. `lint`: golangci-lint.

### Docker (multi-stage)
- Hub: `internal/dockerfile_hub` — `golang:alpine` builder → `FROM scratch`, copies the binary and
  CA certs, `VOLUME ["/beszel_data"]`, `ENTRYPOINT ["/beszel"]`, default `CMD ["serve",
  "--http=0.0.0.0:8090"]`. **Note:** the hub Dockerfile builds only the Go binary; it does **not**
  run the frontend build. `internal/site/dist` is not committed (`.gitignore` ignores `dist` and
  `internal/site/src/locales/**/*.ts`), so the frontend must be built before the Go compile that
  `go:embed`s it. CI does this explicitly: `.github/workflows/docker-images.yml` runs
  `bun run --cwd ./internal/site build` before `docker/build-push-action`; locally `make build-hub`
  does it via `build-web-ui`.
- Agent: `internal/dockerfile_agent` (scratch), `internal/dockerfile_agent_alpine`
  (`alpine:3.24` + `smartmontools zfs`), plus `_nvidia`, `_nvidia_slim`, `_intel` variants. All
  are multi-stage `golang:alpine` builders with `ARG TARGETOS TARGETARCH` and
  `CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH`.
- CI: `.github/workflows/docker-images.yml` builds these Dockerfiles on `v*` tags. For a fork,
  change the `image:` names/registries there (and the Dockerfiles stay the same); the build context
  is the repo root with `COPY ../go.mod ../go.sum`/`COPY . ./` (build context is set to the repo
  root for `.github`-driven builds, or `-f internal/dockerfile_* .`).

### Local dev workflow
1. `make dev` (or in three terminals: `make dev-server`, `make dev-hub`, `make dev-agent`).
2. Hub dev server proxies unknown routes to Vite on `:5173` and serves the API on `:8090`
   (`server_development.go`).
3. `dev-hub` sets `ENV=dev`, which enables PocketBase `Automigrate` (collection edits in the admin
   UI generate migration files under `internal/migrations`, `cmd/hub/hub.go:60-63`).
4. The agent needs `KEY` (hub public key) and either `HUB_URL`+`TOKEN` (WebSocket) or a listen
   address (SSH). Flags/env are parsed in `internal/cmd/agent/agent.go` (`-k/-l/-u/-t`,
   `KEY`/`TOKEN`/`HUB_URL` env in `agent/client.go`).
5. Tests use PocketBase in-process (see `internal/tests/` and `internal/hub/hub_test_helpers.go`).

---

## 7. Where a host maintenance-check system would hook

Goal: run external maintenance shell scripts/checks on the host and surface pass/warn/crit as a
native metric/tile + alert.

### Best insertion points

1. **Agent-side collector (primary).** Add a manager modelled on `packageUpdatesManager`
   (`agent/package_updates.go`) or `StoragePoolManager` (`agent/storage_pool.go`): run the scripts
   in a background goroutine on a configurable interval, cache the result, and never block
   `gatherStats`. Results should be a small struct (overall status + per-check entries) with both
   `json` and `cbor` tags.
2. **Surface as an `Info` field for the tile.** A compact `Info` field (e.g. worst-of status and
   counts) mirrors `Info.Services []uint16` (`sv`) and `Info.PackageUpdates` (`pu`), which already
   drive the `services` and `updates` columns in
   `internal/site/src/components/systems-table/systems-table-columns.tsx` (402/459). This is the
   lowest-friction "native tile".
3. **Surface as a `Stats` field for history/charts.** Add the per-check map to `Stats` (like
   `Temperatures`/`Fans`) so `system_stats.stats` stores it automatically and a
   `charts/<name>-charts.tsx` component can plot it. Remember `AverageSystemStatsSlice` (records.go)
   so rollups don't zero it.
4. **Alert.** For a binary pass/fail (any check crit), follow `alerts_systemd.go` exactly: a
   binary-state `Handle*Alerts`, state-change notification, and a startup stale-state resolver.
   For numeric thresholds (e.g. "N checks failed"), add a name + a case in both switches of
   `alerts_system.go`.
5. **Optional detail table.** If you need a per-check list view (like the SMART or systemd tables),
   add a collection + upsert writer (model `createSystemdStatsRecords`,
   `hub/systems/system.go:393`, and the `smart_devices`/`systemd_services` tables) and a
   `lazy-tables.tsx`/`*-table.tsx` UI.

### Existing precedent to copy

- **SMART** (`agent/smart.go`; hub `system_smart.go`; `smart_devices`), **ZFS/btrfs**
  (`agent/storage_pool.go`; hub `system_zfs.go`; `zfs_pools`), **systemd**
  (`agent/systemd.go`; hub `systemd_services` + `alerts_systemd.go`), **package updates**
  (`agent/package_updates.go` → `Info.PackageUpdates`), and **network monitors**
  (`agent/network_monitor*.go` + `SyncNetworkMonitors` action + `network_monitors` tables) all
  demonstrate external-command collection with a cached/snapshot model and a matching alert/UI.
  The systemd and package-update paths are the closest to "run a host check and expose its result".

### Risks / cautions

- **Security surface.** The agent often runs as root (systemd unit `CAP_*`/root; the Docker agent
  runs as root). Executing arbitrary shell scripts supplies remote command execution through the
  config channel. Prefer an allowlisted script directory + fixed arguments (no shell interpolation),
  a hard timeout (`package_updates.go` `packageUpdatesTimeout = 5m` and `cmd.WaitDelay` are the
  pattern), a bounded upstream buffer (`systemd.go` `limitedBuffer`), and no script-supplied paths
  from the hub.
- **Container agents.** `runningInContainer()` (`agent/package_updates.go:133`) already excludes
  container agents from host-package checks; a maintenance runner must decide whether it targets the
  container or (via mounted host paths) the host, and document it.
- **Blocking collection.** Never run scripts inline in `getSystemStats` without caching; a slow
  script would stall the per-system updater and realtime loop (the reasons SMART/ZFS/package
  updates are backgrounded). `wsDataRequestTimeout` is 30 s (`hub/systems/system.go:801`);
  `sshOperationTimeout` is 20 s (line 1010).
- **Rollup blind spot.** A new numeric `Stats` field not added to `AverageSystemStatsSlice` will
  read zero on 10m+ charts and in alert history windows.
- **Alert enum + rules.** New alert names need the select enum migration; a new collection needs
  `collections.go` rules or it is default-deny for non-superusers.
- **UI build/embed.** Any site change requires rebuilding the frontend into `internal/site/dist`
  before `build-hub` (or `SKIP_WEB=true` + a separately served UI), or the hub serves nothing.
- **i18n.** New UI strings must be extracted (`lingui extract`) and compiled; otherwise they show
  as raw message IDs or English fallback only.

---

## Ordered checklist: add a custom metric

1. `internal/entities/system/system.go` — add field(s) to `Stats` and/or `Info` with `json` +
   `cbor` tags (unique `keyasint`).
2. `internal/records/records.go` — add accumulation/averaging in `AverageSystemStatsSlice` if the
   field is in `Stats`.
3. `internal/site/src/types.d.ts` — extend `SystemStats`/`SystemInfo`.
4. `agent/<feature>.go` — new collector/manager (background + cached for shell-outs).
5. `agent/agent.go` + `agent/system.go` — initialise the manager and populate the new field in
   `getSystemStats`; gate interval/env as needed.
6. (Only for out-of-band detail) add a `WebSocketAction`, agent handler, hub `Fetch*FromAgent`, and
   a migration + upsert writer if a side table is needed.
7. `internal/hub/collections.go` — add read rules if you added a collection.
8. UI chart: `internal/site/src/components/routes/system/charts/<name>-charts.tsx`, mounted in
   `internal/site/src/components/routes/system.tsx`.
9. UI tile: new column in `systems-table-columns.tsx` (for an `Info` field).
10. Wrap new UI strings with `t`/`Trans`; build the site (Lingui extract/compile).
11. (Optional) New alert type: enum migration + `alerts_system.go`/`alerts_systemd.go` evaluator +
    `alertInfo` entry; add a stale-state resolver if binary.
12. Tests: Go `_test.go` beside each package (`agent/*_test.go`, `internal/records/*_test.go`,
    `internal/alerts/*_test.go`), plus frontend tests under `internal/site/tests` where relevant.
