# SPEC6 (v2, supersedes the earlier "rules page / website edits rules" draft): ONE rules registry on the host, two decoupled sides

Builds on SPEC.md (rules of engagement), SPEC2-5. SAME HARD RULES: do NOT mutate the live system while developing (nothing under /etc,/usr,/var/lib,/var/log; no systemctl; no real sends); tests use tmp dirs; Python 3.12 stdlib only on the runner side; write only owned files; list `glue`. No secrets in any public file or log.

## 1. What the owner wants (final)
- A **maintenance script running locally** on the host (the runner/umbrella) and a **front-end container** that only *collects the updates* the script publishes. The website NEVER influences the rules and NEVER inspects scripts directly.
- The website also has **a health check of its own**: how current the system is doing, and whether the monitoring pipeline itself (runner -> publish -> website) is healthy.
- **One place where all the rules are collected** — everything the script is going to do (checks, thresholds, cleanup/retention rules, protections, alert routes, schedules, probes, jobs, safety limits). So that in future there are **two places to audit** (the registry on the host that DEFINES what the script does, and the website that DISPLAYS what the script reports and which rules it ran under) and rules can be checked, removed or modified **in the registry** (on the host), while the website cross-references the published registry instead of reading the script.
- The earlier ideas of a website Rules page that edits rules, rule proposals, approvals from the web and website-influenced config are CANCELLED. (Acknowledging alerts via email link / logged-in website stays: SPEC5; that is alert state, not rules.)

## 2. Architecture: two sides, one contract
```
HOST (authoritative)                                         WEBSITE CONTAINER (read-only mirror)
/etc/homelab-maint/rules.d/*.toml   <- THE REGISTRY (edit here)
        |  rules sync (validate -> compile -> record change)
        v
generated legacy config files (maint.toml, routine.toml, jobs.toml, probes.toml, classes.toml, notify.toml, ack.toml, protected.toml, ...)
        |  read by the existing runner modules (unchanged)
        v
runner/tick/check/daily/weekly/live/metrics  --publish-->  STATE_DIR/public/*.json  --(bind mount :ro)-->  web app  -->  browser
                                                (manifest.json + rules.json + status/health/actions/...)
```
- The **registry** is the single human-edited source of truth. The existing modules keep reading the legacy config files unchanged: those files become **generated artifacts** (header "GENERATED from rules.d: edit the registry, not this file"), produced by a deterministic compiler, atomically, only from a registry that passed validation. A broken registry never replaces the last good generated files (the runner alerts and keeps running on the last good config).
- The **website contract** = the public JSON files plus `manifest.json` (schema versions of each file, `generated_at`, `runner_version`, `registry_hash`, `registry_synced_at`, `rules_count`). The website refuses nothing but shows a clear compatibility/staleness banner when versions or ages are off. No other coupling: no shared code, no direct file reads outside the public dir, no docker socket, no scripts.

## 3. Registry format (`/etc/homelab-maint/rules.d/NN-<category>.toml`)
```toml
[meta]                                   # once per file, optional
category = "checks"                      # checks|spike|cleanup|protection|alerts|schedule|monitoring|jobs|safety|ack
title    = "Health checks"
blurb    = "What the script verifies every 15 minutes and when it complains."

[[rule]]
id        = "check.disk.root_free"       # stable, unique, ^[a-z][a-z0-9_.-]{2,80}$  (never reuse a retired id)
title     = "Root disk free space"
kind      = "check"                      # check|cleanup|protection|alert|schedule|probe|job|spike|policy|safety
why       = "plain-language reason this rule exists (cite the principle: capacity planning, detect by saturation, never kill by size, ...)"
does      = "plain-language description of exactly what the script does under this rule"
applies_to = ["disk_forecast"]           # task/job/probe names it configures (cross-reference for the website)
target    = "tasks.disk_forecast"        # dotted path of the config table this rule's params feed (legacy file chosen by `file`)
file      = "maint.toml"                 # which generated legacy file receives it
params    = { warn_free_pct = 12, crit_free_pct = 6 }
merge     = "set"                        # set (default: deep-merge keys) | append (list-valued params, e.g. retention rules, protected patterns)
mode      = "report"                     # optional report|apply -> target.mode
enabled   = true                         # false => the rule is compiled out (task disabled / item omitted)
severity  = "warn"                       # informational: warn|crit|none
destructive = false                      # true for anything that deletes/restarts/kills/purges
proof     = "in-use proof required before removal: ..."   # for destructive rules: how safety is established
principle = "capacity-planning"
owner_notes = ""
since     = "2026-10-01"
```
Rules for compilation: deterministic order (by file name, then id); `target` is created if missing; `params` types are preserved; `append` extends lists (dedupe exact duplicates); two rules writing the same scalar key = validation ERROR (no silent override); `enabled=false` on a task rule sets `[tasks.X] enabled=false`; comments/ids/why never reach the generated files except an id comment above each emitted table (`# rule: check.disk.root_free`) so a reader of a generated file can find the source rule.
**Invariants enforced by `rules check`** (a violation blocks the sync and raises an alert): protected patterns are a SUPERSET of the shipped baseline (cannot be silently shrunk; shrinking needs the owner's explicit `[meta] allow_baseline_removal = ["pattern"]` entry in a dedicated `99-owner-overrides.toml` and is logged loudly); per-run caps <= hard limits; no delete-type rule (retention, cache trims, cleanup) whose path escapes `allowed_roots` or touches the never-touch list; destructive rules default to `mode="report"` unless explicitly `apply`; unknown keys per known task are warnings (typo catching) with the task option schema derived from the code (`ctx.opt` keys); duplicate ids; ids referenced by `applies_to` must exist in the task/job/probe registry.

## 4. Change tracking and audit (host side)
- `STATE_DIR/rules/current.json` = `{"hash","synced_at","rules_count","files":[{name,sha}]}`; `STATE_DIR/rules/history.jsonl` = one record per detected change `{"ts","from","to","added":[ids],"removed":[ids],"modified":[{"id","fields":[...],"before":{..},"after":{..}}],"valid":bool,"errors":[...],"applied":bool}`; snapshots of each applied registry under `STATE_DIR/rules/snapshots/<hash>/` (kept 20) enabling `rules rollback`.
- A change triggers: validation, compile, a "rules changed" **maintenance notice** through notify.py (email; what was added/removed/modified, in plain words), an entry in the maintenance journal/changes log, an incident if invalid.
- CLI (`homelab-maint rules ...`): `list [--category C] [--kind K] [--enabled|--disabled] [--destructive]`, `show ID`, `check` (validate the registry and print every error/warning), `diff` (registry vs last applied vs generated files), `sync` (validate+compile+record; idempotent; the tick calls it when the hash changed), `history [N]`, `rollback [HASH]`, `export` (print rules.json), `migrate` (one-time: build rules.d from the current legacy config files and PROVE `compile(rules.d)` parses to the same data as the current files), `explain TASK` (which rules configure this task and the effective values), `where KEY` (which rule sets a config key), `orphans` (config keys no rule owns).

## 5. Public export for the website (read-only; written by publish/sync)
`manifest.json` (0644): `{"schema":1,"generated_at","runner_version","registry_hash","registry_synced_at","rules_count","files":{"overview":{"schema":1,"generated_at"},...}}`.
`rules.json` (0644, < 400 KB): `{"generated_at","registry_hash","categories":[{id,title,blurb,count}],"rules":[{id,category,title,kind,why,does,applies_to,params (current values),mode,enabled,severity,destructive,proof,principle,since,source_file,last_evaluated,last_triggered,triggers_30d,last_result,related:[task names]}],"history":[<=50 recent registry changes, redacted],"stats":{...}}` where last_* are cross-referenced from status.json/history/audit by the publisher (so the website shows "this rule: evaluated 2 min ago, triggered 3 times this month").
The website shows this **read-only** (a "What the script does" reference inside the Maintenance tab: filter/search by category/kind/mode/destructive, each rule's why/does/params and when it last ran/fired, the registry hash + last change time, and a link from every check/cleanup/alert card to its rule(s)). There are NO edit controls, no forms, no write endpoints for rules.

## 6. Self health (pipeline health of the monitoring system itself)
Task `self_health` (C0, check tier) and `self.json` public export: ages and states of the whole pipeline: status.json age, publish age (manifest.generated_at), tick/scheduler last run, live.json age, metrics ring age, check/daily/weekly last runs vs expected cadence, registry valid + in sync (generated files match rules.d), runner error rate (tasks in error last 24 h), state-dir free space and sizes, inbox backlog (acks), website container state and its `/healthz` result (GET only, local), the umbrella's own Kuma heartbeat, and a verdict `{"level":"ok|degraded|down","reasons":[...],"since":t}`. The website shows it as a persistent health strip (top bar: "Monitoring pipeline: healthy / degraded: runner stopped 12 min ago") and `/healthz` returns the web container's own readiness. If the runner dies the website must say so loudly (stale data banner + reason) instead of showing old numbers as current.

## 7. First-run login (acknowledge feature only)
Kept from SPEC5 section 5/8 but restricted to what acknowledging needs: setup mode on first visit protected by a host-side **bootstrap secret** (`homelab-maint web bootstrap` prints it once), passphrase (+ optional TOTP), sessions + CSRF; the login unlocks ONLY the acknowledge/un-acknowledge actions. No rule editing, ever.

## 8. Streams (this workflow = "registry core"; content authoring comes after the other builds finish)
- `registry_core`: `homelab_maint/registry.py`, `tests/test_registry.py`, `etc/rules.d/00-baseline-invariants.toml` (the protection baseline used by the superset check), plus the exact glue (cli subcommand `rules`, tick hook `registry.sync()`, publish call, install.sh/uninstall, systemd path/timer if wanted, docs). Includes the `migrate` converter and the equality proof against the CURRENT shipped etc/*.toml (maint, routine, jobs, probes, classes, notify, ack, protected; playbooks stay content files referenced by id).
- `self_health`: `homelab_maint/tasks/self_health.py`, `tests/test_self_health.py` + glue for publish/manifest/web strip.
- LATER (after all other builds and integration): `rules_content_*` agents write the human content of the registry per category (titles/why/does/proof for EVERY rule, ~all knobs), a coverage test makes it impossible to add a config knob without a rule; then `registry_view` (website read-only reference + pipeline strip).
