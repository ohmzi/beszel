# Ohmz redesign: fork Beszel into the homelab dashboard

Status: design (approved in outline by the owner: fork + native custom check, replace the
whole stack, build from source, Ohmz skin). This document is the map; `docs/EXTENDING-BESZEL.md`
holds the file-by-file Beszel extension detail that this builds on.

## Goal

One product for the homelab: **this fork of Beszel**, wearing the **Ohmz skin**, with the
homelab-maint maintenance engine surfaced **natively** — its checks, health and (later)
acknowledgements / incidents / reports appearing as first-class Beszel metrics, tiles and alerts.
The existing `maintenance-web` site is retired once this is proven, not before.

## Why a fork (not a plugin)

Beszel has **no custom-metric or plugin API**: the hub's `/api/beszel/…` surface is read-mostly and
metrics arrive only from its own Go agent over CBOR/WebSocket with a fixed struct. So a custom check
must be added in Go. The good news (see `EXTENDING-BESZEL.md`): `system_stats.stats` and
`systems.info` are **schemaless PocketBase JSON fields** — a new metric needs **no DB migration**;
the Go struct tags are the schema. Migrations are only needed for new collections, new alert-name
enum values, or non-JSON fields.

## Architecture

- **Engine stays, UI moves.** The Python daemon (`homelab-maint`) keeps doing the real maintenance —
  checks, cleaners, routine, incidents, acknowledgements, reports. It already publishes everything to
  `STATE_DIR/public/*.json` (world-readable). Beszel becomes the presentation + observability layer.
- **A native collector, read-only.** A new agent-side background manager (Go), modelled on
  `agent/package_updates.go` / `agent/storage_pool.go`, reads homelab-maint's published state
  (`self.json`, `checks.json`, `incidents.json`, `acks.json`) on a bounded interval and caches a
  compact verdict. It **reads files, never runs the scripts** — no new RCE surface, and no duplicate
  of what systemd already schedules. (Running scripts from the agent is explicitly out of scope.)
- **Surfaced natively.** A `Maintenance` object on `Info` (system tile, like Services/Package
  updates) now; a numeric `MaintenanceLevel` on `Stats` (history + chart) and a binary alert later.
- **Ohmz skin.** Token remap in `internal/site/src/index.css` (Tailwind v4 `:root` / `.dark`): the
  Ohmz warm ramp + the single amber accent, plus the chart series and the brand mark. Same approach
  as the Open WebUI Ohmz `custom.css`.

## Phases

1. **Skin + maintenance tile** (this build). Ohmz tokens in `index.css`; agent collector reading the
   published state; a `Maintenance` column/panel. No migrations, no rollup change, additive only —
   the live site is untouched.
2. **History + alerts.** `Stats.MaintenanceLevel` (must be added to
   `records.AverageSystemStatsSlice` or it rolls up as zero), a chart on the system page, and a
   binary alert handler (enum migration + `alertInfo` entry) so a crit maintenance verdict pages.
3. **Ack + incidents + reports.** Port the homelab-maint surfaces into Beszel pages backed by the
   same published JSON, then retire `maintenance-web`.

## Deployment (target)

Beszel hub + agent in Docker on this host (compose from this fork's Dockerfiles), behind the existing
Cloudflare Access. The agent runs **on the host** (bind-mount `STATE_DIR/public` read-only) so the
collector can read the published state. `maintenance-web` is retired only at the end of phase 3.

## Out of scope / non-goals

- No reimplementation of the check/cleaner engine in Go; the Python daemon stays the source of truth.
- No running of arbitrary scripts from the agent.
- No SQLite/protocol changes beyond what the fork already uses; no multi-host ambitions.
