# Network Watch: decisions made during the build

Every ruling the build controller made on the owner's behalf, in order, with what it costs if wrong. Source: the build ledger.

| T6 config edits etc/maint.toml | etc/*.toml generated from rules.d | CONFLICT -> Ruling R1 |
| T7, T11 edit etc/jobs.toml, notify.toml, maint.toml | same | CONFLICT -> Ruling R1 |
| T6 step 5, T16 steps 1,4,5 | host install / firewall / deploy | stop-class -> Ruling R2 |
| T12 api.go allowlist, T15 Go | no Go toolchain on host | Ruling R3 |
| T13 site build | worktree lacks node_modules | Ruling R4 |
## Rulings
- Ruling R1: netwatch config goes in a new engine/etc/rules.d/65-netwatch.toml as [[rule]]s, with matching generated etc/*.toml tables kept in step; playbooks.toml edited directly — the registry is the source of truth (README "The rules registry") — if wrong: a rules sync reports drift, fix by regenerating.
- Ruling R2: implementers never touch the live host (sudo/install/systemctl/nft/Cloudflare); host install, firewall table and deploy happen after the final review, by the controller, only with the owner's explicit OK — security-sensitive and outside the worktree — cost if wrong: later live verification.
- Ruling R3: Go code is tested with the official golang:1.27.1 Docker image (`docker run --rm -v $PWD:/src -w /src golang:1.27.1 go test ./agent/ ./internal/hub/...`) since no Go toolchain is installed — cost if wrong: an image pull.
- Ruling R4: the site build uses a symlink to the main checkout's node_modules (`ln -s ../../../beszel/internal/site/node_modules internal/site/node_modules`, not committed) — avoids a network install — cost if wrong: a version mismatch caught by the build.
- private example addresses in tests (192.168.40.x, 100.101.x) are allowed — they are the plan's default ranges, not public identifiers — cost if wrong: swap literals in two test files.
- HARD_RANGES gains optional cfg so configured lan/tailscale CIDRs are hard-protected — owner's never-block intent covers the configured LAN — cost if wrong: an extra protected range.
- sample() returns (flows, "ok"|"stale:<why>") instead of list[Flow] — fail-closed constraint needs a staleness signal for Task 6 — cost if wrong: one call site.
- skip all loopback sockets in every namespace — loopback is intra-process, not network traffic; tunnel ports subsumed — cost if wrong: local-only service chatter is invisible.
- LAN direction by listening port: inbound (lan:<ip> -> entity, local port) vs outbound (entity -> lan:<ip>, remote port) — reviewer found inversion — cost if wrong: LAN rows mislabelled.
- private LAN addresses and the public handle in tests are allowed — consistent with Task 2 ruling — cost if wrong: swap literals.
- read_journal returns (lines, "ok"|"stale:journalctl") — fail-closed constraint — cost if wrong: one call site in T6.
- plex_log_lines(margin=) + CODEC_WINDOW; T6 must read codec lines with margin=CODEC_WINDOW but exploit lines without (no double events) — cost if wrong: duplicate or missed codec checks.
- Plex _STAMP accepts space-padded days (promoted from minor) — a missed format is a missed exploit detection — cost if wrong: none.
- R1 revised — netwatch rule goes into existing etc/rules.d/70-monitoring.toml (after target "live"), no new registry category, registry.py untouched — a new file needed registry code changes — cost if wrong: move one rule.
- load_topology must not start socket-activated dockerd: same guard as live monitor (dockerd process + systemctl is-active docker) else stale — reviewer of T6 concerns — cost if wrong: none (fail-closed).
- signins table writer belongs to Task 8 (record_signins from edge rows, auth_ok events, Plex sessions) — plan already assigns it — cost if wrong: none.
- threat lists are spamhaus_drop_v4/v6 (NDJSON) + firehol_level1 + feodo, replacing the plan's DROP+EDROP — Spamhaus merged EDROP into DROP in 2024 and moved to JSON feeds — cost if wrong: one URL/parser change.
- threat-refresh job rule in 80-jobs.toml with mode "managed" is correct — jobs belong in the jobs registry; managed matches the daily-job intent; first host run is owner-gated anyway — cost if wrong: flip mode.
- normalise IPv4-mapped IPv6 (::ffff:a.b.c.d) in threat.lookup AND sockets.py (Task 3 code) — dual-stack listeners like Plex report v4 peers mapped; otherwise LAN peers classify as internet and threat hits are missed — cost if wrong: none.
- path classes are generic (auth_login, auth_api) with no hostnames; [netwatch.signin] hosts ships empty and is filled on the host — repo is public — cost if wrong: owner must list sign-in hosts before sign-in protection works for tunnel apps.
- protection() gains cfg param (passes to HARD_RANGES); record_signins runs in the collector every 300 s as source "signins" — plan named no caller — cost if wrong: one call site.
- is_global filter for Plex session IPs stays; tests monkeypatch protect._internet because documentation ranges are not global — cost if wrong: none.
- protection() returns ("hard","invalid-address") for unparseable input — the block path must fail closed — cost if wrong: none.
- flows/edge are hourly buckets, so judge windows use overlapping buckets for them and exact ts only for events — the plan's 10-min/5-s windows are not resolvable — cost if wrong: coarser thresholds.
- plex_exploit source IP is inferred only when exactly one internet peer had an in-flow on 32400 that hour, else "unknown" with candidates listed and no block — avoids a wrong-IP block suggestion — cost if wrong: fewer one-click blocks for Plex attempts.
- stale topology suppresses new_peer/line_not_meant and all block targets — stale rule from Task 2 — cost if wrong: findings without block buttons during docker outages.
- host-sourced out flows stay in new_peer (finding rejected) — Plex runs in the host network namespace, so a Plex2Shell-style miner calls out as "host"; this rule must see it — cost if wrong: extra new_peer warnings for host CDN rotation during week 2+ (tunable by widening the host net prefix).
- TDD-order deviation accepted (tests assert real behaviour) — cost if wrong: none for the code.
- container->host svc pairs not baselined (spec's "host port it has no history with" covered by pinholes + line_not_meant) — cost if wrong: an allowed-but-new host port from a container goes unflagged.
- unlabelled containers ("" project) are judged, not treated as same project; edge auth_burst normalises client_ip (promoted from minor: fail-open / duplicate findings) — cost if wrong: none.
- site_url gains one strict path form (^/[A-Za-z0-9/_-]{1,40}(\?[A-Za-z_]{1,16}=[A-Za-z0-9]{1,32})?$) so Result.link "/network?finding=<id>" renders; existing "#/..." links unchanged — the dashboard is a path router, the old fragment regex forbids ?= — cost if wrong: link falls back to the site root.
- net_watch staleness = status starts "stale" OR source epoch older than 1800 s (collector stopped); tracked via meta stale_since:<name>; threat stale lists count as stale sources — plan's "stale 30 min" needs a since-marker — cost if wrong: a warn that comes too early/late.
- unreadable store -> Result("error", ...) not raise — fail closed — cost if wrong: none.
- net_watch playbook lives only in the git-ignored engine/homelab_maint/data/playbooks.toml (owner's .gitignore: 'kept local, never published'); at integration copy that block into the main checkout's data file by hand — cost if wrong: alert emails lack the playbook section ([todo] lines still give next steps).
- missing source rows count as stale after the same 1800 s grace (known names sockets,kernel,auth,plex,cloudflare,tunnel,signins) — fail-closed without paging on a fresh install — cost if wrong: a 30-min delay before "collector not running" warns.
- implementers run only focused tests; controller runs the full suite per milestone — owner flagged slow progress; full suite costs ~5 min per task — cost if wrong: a cross-file regression is caught at the milestone, not the task.
- block ids are 16 hex (plan T14/T15), under the 24-char redaction threshold in publish clean() — cost if wrong: ids redacted in network.json.
- payload net() shows "--" for a missing flags metric (promoted: fail-open) — cost if wrong: none.
- site build is run for verification but regenerated locale .po files are reverted, not committed — keeps the diff to the page; untranslated strings fall back to source text and the deploy build regenerates catalogs — cost if wrong: translators miss new msgids until next extract.
- node_modules symlinked from main checkout (R4 applied) — cost if wrong: none.
- dialog mode computed from the whole network.json (block null / hard_ranges / non-IP -> hard; stolen_account / protected.signins -> signin), as a pure module lib/netwatch-mode.ts — server side (T14 apply_block) remains the authoritative guard — cost if wrong: none.
- block_id = sha1(ip|target|zone)[:16] so repeat blocks are idempotent; nft drop rules in both input and forward hooks (forward covers Docker-published ports) — cost if wrong: a re-block after unblock reuses the id (fine, removed rows are history).
- blocks are refused for "hard" always and for "signin" without a reason, server side (authoritative guard) — cost if wrong: none.
- test_acks_auth handler-set assertion relaxed to >= {auth_setup, auth_change} is accepted — netwatch kinds now register in process_inbox; a separate test pins net_block/net_unblock registration — cost if wrong: an unexpected extra kind would not fail that one test.
- host blocks require zone "" and unblock closes all open host rows for the IP; restore re-checks protection and drops now-hard rows; scoped IPv6 and control chars in reasons rejected; override audited only for signin (M1-M3 promoted: safety/audit integrity) — cost if wrong: none.
- no explicit stale-topology refusal (HARD_RANGES fallback covers it); inbox processing is serial so concurrent duplicate CF rules are not a concern — cost if wrong: a duplicate Cloudflare rule, removable by sweep/unblock.
- R3 verified: go build ./internal/hub/... ./agent/... ok and go test ./agent -run Maintenance ok in golang:1.27.1 (T12 api.go entry compiles); module cache at scratchpad/gocache
- MaintenanceNetBlock WebSocket action is appended at the END of the iota const block — inserting mid-block would renumber existing actions between hub and agent — cost if wrong: none.
- Go refuses raw control chars, any non-ASCII byte, and raw length > 200 in override_reason, then only trims — matches block.py and keeps Go/Python canonical bytes identical (ASCII-only) — cost if wrong: owner must retype a reason with an accented character.
- I3 (signin hosts) and M3 (Plex log CPU) are install gates handled with the owner, not code — cost if wrong: web sign-ins unprotected until hosts are set.
- I5 — new_peer findings share one issue_key (rule only) so they debounce as one alert with a 24 h reminder; spec's "service talking somewhere new emails" kept — cost if wrong: later new peers in the same day don't re-alert (page shows them).
- I6 — plex_exploit matches only "unsupported protocol '" rejections; the /player/timeline protocol= rule is dropped (0 matches in 5 days of live logs; legit Companion subscribes carry protocol=) — cost if wrong: a timeline-based variant that Plex does not reject would not be flagged by this rule (codec rule still covers payload drops).
- export marks an aged 'skipped' source stale — the collector refreshes even skipped sources every tick, so an old epoch means the collector stopped — cost if wrong: none.
- inherent to write-ahead ordering; exceptions roll back; sweep/unblock on the page clears it — cost if wrong: one stray block until unblocked.
- install starts the collector (which creates the table) before the first publish; a raised handler exception is caught by process_inbox as handler_error — cost if wrong: one failed publish cycle.
