# INTEGRATION: how the streams were joined

The modules (metrics, publish, widgets, web, routine, spike, incidents, live, reports, notify, scheduler, ports, monitors, retire) were built in
parallel and each listed the glue it needed from the lead. This file records what was decided where two builders disagreed, where each piece of
glue lives now, what was left out on purpose and what is still open. Pass 2 (decisions 23-33) wired SPEC5 (acknowledgements), SPEC6 (rules registry,
pipeline self-health) and the cleaners-v2 / website-v2 glue into the code and landed the packaging side (`install.sh`, `systemd/`, `etc/*.toml`, `homelab_maint/data/playbooks.toml`);
the pass-2 verification fixes are decisions 34-40. Pass 3 (decisions 41-47) closed the runner-side glue the website lane left open (first-run login
handlers, `ack/` modes, the e-mail button switch, one ack policy, explicit issue keys, `web bootstrap`) and ends with the **first install runbook**. The final pass (decisions 48-51) closed what the last verification found: the first-hour registry warning, the un-acknowledge dedupe, a browser-test freeze and a stale probe assertion.
The website (`web/`) is another lane; its open items are listed below.

## Decisions (one line of reason each)

1. **No `homelab-maint-routine.timer`; one per-minute tick drives the routine.** v2 proposed a 15-minute routine timer, v3 a single tick that runs the
   routine as a job. SPEC4 asks for ONE scheduler, so the routine is the `routine-run` job in `etc/jobs.toml` (:07/:22/:37/:52, `routine run --apply`);
   it also serves the monthly window, retries steps a busy gate deferred and catches up read-only steps after downtime.
2. **The check, daily and weekly timers stay; monthly has no timer.** They are the permanent drivers of their tiers and the thing that notices a dead
   tick (SPEC4: legacy and tier units stay until cutover); the monthly tier only ever runs in the routine's 1st-Saturday window, through the tick.
3. **The check tier runs with `--apply`.** `--apply` only permits mutation: C0 checks stay read-only, every C1 task still needs its own `mode = "apply"`.
   Without it the shipped pressure reclaim rung (on by default, SPEC3), `comfyui_idle_reclaim` and `immich_recycle` could never act, and the retire
   stream's `task_applies` parity would stay red. `systemd/homelab-maint-check.service` and the `tier-check` job in `jobs.toml` carry it.
4. **`cmd_run` uses `notify.HermesNotifier(defer=True)`; `core.Notifier._send` stays a thin adapter.** notify first proposed replacing `_send`, then
   (after review) the subclass, which also retries a failed recovery and has the durable outbox. `core.Notifier` keeps the debounce state machine and
   calls `notify.notifier_send` (bridge fallback if notify is broken, so an alert is never lost to a template bug) for the callers that use it
   directly (incident differential tests, `core.Notifier({...})` in tests). Its `_send(name, subject, body, now)` signature is unchanged because
   `tests/test_checks_basic.py` and `tests/test_incidents.py` patch it.
5. **Nothing slow happens under the status lock.** Inside `with _state_lock()` the notifier only queues (a short durable write) and saves; the publish
   step, the Kuma heartbeat and `deliver()` (SMTP/SMS, up to 90 s on a dead transport) run after it, in that order. `tests/test_cli.py` pins the order
   and that the lock is held / released at each point.
6. **Publish order: `incidents.update` then `publish.publish(status)`.** incidents.json must be current when publish reads it. publish builds the
   scrubbed `routine.json` itself; `routine.write_export()` only runs when publish did not write one (no scrubbing, one more systemctl call).
7. **No `incident_open` / `incident_resolved` mail from `cmd_run`** (re-checked in pass 2, decision 31). Optional glue, skipped: the alert email already carries the
   playbook as "What to do", so a second message per problem would be a duplicate page. `incidents.update()`'s result is ignored apart from its own logging.
8. **Probe detection is the tick's `probes-run` job (every minute); the check-tier `probes` task only folds the latest state into status.json.**
   Running the engine as a task each minute would add a history record per minute (the health calendar counts 15 min per record).
   `alert_mode` stays `"task"`; `cmd_run` already skips the task-level page for a task that sets `metrics.self_notifies`, for when it flips to `"events"`.
9. **One `PASS` table hands the raw argv to each module's own parser** (`routine`, `incidents`, `report`, `notify`, `migrate`, `probes`, `job`, `live`,
   `new`, `plugins`) instead of argparse REMAINDER or `add_args` recipes: leading options such as `routine --now ... due` work and a new subcommand is
   one line. `live` had two incompatible recipes (REMAINDER vs `add_args`/`run_args`); `live.main` parses its own flags, so it joined the table.
10. **`smart-event` is dispatched before argparse and before any import but `smart_hook`.** smartd treats any output as a hook failure, so a usage error is
    as bad as a crash; any exception exits 1 silently so the shim falls back to the legacy script.
11. **Tick-launched `run --task` commands carry `--scheduled`** (`jobs.task_entries`). `routine.is_manual` treats a terminal on stdin, `--override` or
    `--force` as the owner's override of the routine window and never `--scheduled`; the tick already runs with stdin = /dev/null, the flag makes it explicit.
12. **Everything that can mutate stays report-only.** Every `[tasks.X] mode` ships `report`; the one shipped `apply` is the pressure ladder's reclaim rung
    (`[tasks.pressure_response] mode = "apply"`, reclaim `apply`, throttle/restart/emergency `report`). A daily/weekly/monthly C1/C2 task that no routine names
    (or any task while `routine.toml` is missing or invalid) is forced report-only by `RunGuard`: it runs, it applies nothing.
13. **`tier_runs` and the Kuma `tier-*` heartbeat are unchanged: a `run --task X` still stamps its tier.** Considered making them tier-run-only, dropped:
    dashboards and tests read them as they are, and the monthly tier has no other writer.
14. **`homelab-maint serve [--port N --bind A --status F]` is a `PASS` entry** (`server.main`). `cmd_serve` called `server.main()` with no argv, so argparse
    read `sys.argv[1:] == ["serve"]` and exited 2 ("unrecognized arguments: serve"); the www unit never used it, only the CLI did, and `test_cli` mocked `server.main`.
15. **The routine's pressure gate, the Live tab and the reports read `gate_level`, not the host `level`.** `routine._gate_level` (one reader: `scheduler.gate_level_of`) feeds
    `_pressure_gate` and `pressure_level`; `live.PressureSrc` adds `gate_level` and `level_name` to `live.json` (the Live tab already reads them). io-only or gpu-only
    pressure (this host holds io PSI at level 3 for hours every night) no longer defers the routine. Pass 2: `reports.pressure_summary` takes `level_max` from each record's
    `gate_level` (the `level` for a record written before it existed), and a spike whose `gate_peak` is 0 (only io/gpu) no longer makes `spikes.worst_level` 3
    (the list entry keeps the host `level` and gains `gate_level`; the Live and Reports tabs already have the resource word).
16. **A tick that loads no jobs is down, not just late.** `tick.json` carries `jobs` (how many loaded), `scheduler.health()` is `crit` at 0, `homelab-maint tick` exits 1 when
    jobs.toml has errors and nothing loaded (the unit then shows in `failed_units`), and the `umbrella-tick` probe requires `jobs >= 1`. A jobs.toml typo used to leave
    the beat fresh while the routine, the probes and every cut-over job silently stopped. Some jobs bad, others fine: still `warn`.
17. **`core.audit` forks `logger` only for real attempts (not `dry-run`), and since pass 2 one run's attempts go out in ONE `logger` call** (decision 30 for the rows).
    One report-mode daily run audited about 230 "would" rows (retention, qos_classes) and spawned 233 `logger` processes; nobody reads the syslog copies of a non-event.
18. **`docker_prune_exposure` and `legacy_audit` have playbooks** in `homelab_maint/data/playbooks.toml` (the packaged baseline, not `etc/`). The first check run after an install
    pages about docker-prune.timer, and that alert carries the playbook as "What to do": it would have said the generic "Check failed".
19. **The Homarr installer now places `ops-thermals` and `ops-load`** (`widgets/install_homarr_widgets.py` ORDER/BIG/MOBILE, tests updated together: 80 passed). Nothing was run against Homarr.
20. **Kept as they are, on purpose.** The weekly timer stays at Wed 07:45 (the routine's weekly steps wait for the daily routine and the window is 07:45-10:00; moving it means seven
    files that quote the time, jobs.toml's `tier-weekly` parity among them). `homelab-maint-metrics.service` stays on `metrics_ring sample` (public/metrics.json is at most one publish old,
    15 min; the Live tab and the Homarr `/thermal` `/load` read the ring directly; `metrics-sample` would need the hardened sampler to write `public/` and read the audit log).
    `install.sh` prints a heads-up when docker-prune.timer is enabled and containers are stopped (read-only; the owner's choices are in the alert and the playbook).
21. **No test writes to the host's journal: `HOMELAB_MAINT_NO_SYSLOG=1` makes `core.sh` skip a `logger` argv** (`tests/conftest.py` sets it; subprocesses inherit it, `tests/test_native.py`'s
    `hook_env` passes it on). `tests/test_publish.py` (real ack tokens through `core.audit`) and the smart-hook subprocess tests left `acks ...` / `notify send failed ...` lines in syslog on every
    run. A test that stubs `core.sh` sees its own stub as before. Scratch runs against the real host can set it too (the units never do).
22. **The "fork's own import schema" test skips when the schema file is gone** (`tests/test_payloads.py`). The Homarr checkout moved to v2 on 2026-10-02 (`feat!: release v2`):
    `packages/validation/src/custom-widget.ts` no longer exists and v2 custom widgets are JSX with `sources`/`requests`, which the `ops-*.json` files are not. The host still runs
    `homarr:develop` from before that, so the widgets are right for what is deployed; see Open items.

23. **Acknowledgements (SPEC5) are wired in `cli.py`, with `core.Result.issue_key`.** `ack` is a `PASS` entry (`acks.main`); `cmd_run` marks each entry (`_ack_mark`:
    `fp`, `issue_key`, `acked`), puts `acked: true` on the history record, and calls `_ack_apply` (`acks.apply_to_status`: also the entries this tier did not run, and
    `status["acked_n"]`) INSIDE the state lock, after the tasks are merged and before `overall()`, which skips an acknowledged entry. Everything fails closed: no acks module,
    an unreadable store or any exception means the entry is simply not acknowledged (the alert goes out, the colour stays) and one warning is printed.
24. **The notifier hold stays where ack_core put it, inside `notify.send`, not in `core.Notifier.evaluate`.** The debounce / reminder / flap state in `alerts.json` is therefore
    computed from the TRUE status: a held alert counts as sent (reminders keep their cadence silently), a held recovery resets the episode, an escalation past the ceiling is not
    held and pages once, an un-ack or expiry re-opens the episode (`HermesNotifier._reopen_if_released`). `core.Notifier._send_bridge` (reached only when notify.py is broken) has
    the same guard (`_acked_hold`: any doubt = send). `scheduler._overall` and `payloads._lvl` follow `acks.overall` / `acks.flag_live`, so merge_status cannot turn the hero
    yellow a minute after the 15-minute run made it green and the Homarr widgets mute an acknowledged entry exactly as long as the acknowledgement lasts.
25. **`cmd_tick` runs `acks.run_once()` (inbox, expiry, expiry notices) after `_flush_notify()`, and jobs.toml may also ship the `ack-process` job.** Both are idempotent (a second
    run in the same minute finds an empty inbox). The in-tick call is the one that keeps working when jobs.toml has a typo (a tick that loads no jobs starts no `ack-process`),
    the job is the supervised one (own log, 90 s timeout). An idle minute is one directory listing and one read; nothing is imported or created until `acks.json` or `ack/` exists.
26. **The rules registry (SPEC6 v2) is wired as: `rules` `PASS`, `registry.tick()` FIRST in `cmd_tick`, `registry.register_tasks()` in `load_tasks` (the C0 check `rules_registry`;
    a registry that cannot import becomes the error task `module_registry`), two `doctor` rows (`rules registry valid and in sync`, which also fails on an unsafe generated file,
    and `monitoring pipeline healthy, self.json fresh`).** `core.load_config` is NOT changed: the runner modules keep reading the generated legacy files, which the sync rewrites
    before the scheduler looks at jobs.toml (a test edits a rule in rules.d and sees the value through `load_config` after one tick). Nothing is imported until `rules.d` exists
    (`rules migrate` creates it); a sync costs about a millisecond when nothing changed. A line is printed (journal) only when something happened or a sync keeps failing. The "Rules changed" notice is sent from inside that call, so a dead
    transport (up to 90 s) delays the jobs of THAT tick once; the registry never sends anything when nothing changed.
27. **publish writes `self.json`, `rules.json`, `rules-history.json` and `manifest.json`.** `self.json` is one more optional source (`tasks.self_health.export`, builder order after
    migration.json, so a crash there is announced in overview.export_errors like the others). The three registry files come from `_publish_registry`, LAST (the manifest describes the
    files already there): `registry.write_public` (rules.json at most every 5 min unless the registry changed, its own 380 KB cap, so exempt from MAX_FILE_BYTES), then
    `rules-history.json` = `{schema, generated_at, registry_hash, history}` derived from the `history` of the rules.json just written (the website's `rules-history` route works whether it
    reads the file or derives it; it is NOT run through publish's scrubber, which would take a 64-hex registry hash for a secret) and the manifest again with a `schema` for every
    file (most SPEC2/3 documents carry none; this module's SCHEMA stands for them, so the website never sees null). `homelab_maint.__version__` ("2.0.0") is the manifest's
    `runner_version`. The public dir is never recreated (a test pins the inode). `acks.json` + `ack/tokens.json` were already written by publish (verified).
28. **The pipeline's refresher is NOT in the tick.** `self.json` carries a freshness `ttl` (degraded after 180 s, down after 600 s) that the website applies to the FILE, so something must
    refresh it every minute: that is `systemd/homelab-maint-selfhealth.timer` (packaging lane; `python3 -m homelab_maint.tasks.self_health --refresh`). Putting it in the tick would add
    docker and an HTTP GET to every minute and stall the next tick behind a hung website (a oneshot unit does not start again while it runs). `core.kuma_push` now tells
    `self_health.note_kuma` whether the heartbeat landed (`curl exit N`), which the Kuma row needs.
29. **`self_health` is registered by the `tasks/` loader as before and gets its `self-health` `PASS` entry.** It needs a `[tasks.self_health]` table and a playbook from the packaging lane.
30. **Report-mode audit noise is fixed at the source (`core.Ctx.act`) and in the syslog path (`core.run_task`).** The first `[global] audit_dry_sample` (default 20) "dry-run" rows of one
    action are audited one by one; the rest of that run are counted and written as ONE aggregate row (`"n": 181`, target "(+181 more, not listed)", the summed bytes) when the task ends.
    Refusals and real attempts are never aggregated. `reports` counts `n` in `actions.dry_run`; the registry cross-reference only needs timestamps. Attempts made while a task runs wait
    in a buffer and leave in ONE `logger` call (lines on stdin, control characters replaced so a target cannot forge a second syslog line, at most 200 lines then "N more: see audit.jsonl"),
    also when the task fails or times out; an `audit()` outside a task run (approve, notify, acks) still forks at once. Nothing is lost from audit.jsonl.
31. **`incident_open` / `incident_resolved` are still not sent, and now with the reason.** notify's coverage rule (`dedupe.covered_by`, 30 min, same key and severity) swallows the second of
    alert and incident_open for the same task, and incidents open on the same run as the pager (same debounce, "an incident opens when the page goes out"). So for every task `cmd_run`
    evaluates the incident mail would be covered (no value); for the rest (the tick's `J` rows page through the scheduler's own events under another key, probes with `self_notifies`) it
    would be a second page. There is no case where it is the only mail, so there is nothing to gain and a double page to lose.
32. **`live.json` history gained `swap_pct`, `vram_pct` and `load1`** (the Live tab prefers them and otherwise falls back to a trail since the page opened). Whole percents and the 1-minute
    load, null for no swap / a stale or missing GPU reading. The file budget went from 40 KB to 52 KB (44 KB with a full history; a smaller cap trimmed it to 650 points). A
    `live-history.json` from the previous release still restores (the three new series start empty).
33. **Cleaners v2 and the pass-1 leftovers have report titles and change kinds.** `reports.ACTION_TITLES` names every registered C1/C2 task (a test fails for a new one without a title) and
    `routine.CHANGE_KIND` marks the three package cleaners as `maintenance`. The `maint.toml` tables, routine steps and playbooks of the 12 cleaners are the packaging lane's.

34. **The shipped baseline mirror (`etc/rules.d/00-baseline-invariants.toml`) is regenerated from `etc/protected.toml`** (`rules migrate --from etc --baseline-only etc/rules.d/00-baseline-invariants.toml`, date kept).
    Moving the Tunarr unprotect from `tasks.retention` to `tasks.app_cache_trim` left the mirror weaker than the floor in `registry.py` (UNPROTECT_ALLOW): `rules migrate` failed its proof ("baseline is weaker than the release
    baseline") and, once rules.d ships content, `install.sh` (which replaces the installed mirror every time) would have blocked every `rules sync`. `tests/test_registry.py` is the guard; whoever changes `protected.toml`
    or the floor regenerates the mirror in the same change.
35. **`RULES_JSON_MAX` is 700 KB, not 380 KB.** rules.json is 332 KB with only TODO-CONTENT text (89 % of the old cap); real why/does/proof prose for ~430 rules is about 650 KB, and `registry._fit` would have started cutting it
    to 200/100/60 characters. The website's `MAX_JSON` is 1 MB and publish's read-back of rules.json (for `rules-history.json`) reads up to 790 KB; `tests/test_publish.py` pins cap <= read-back.
36. **All 13 `routine_*` tasks have playbooks** (`routine_verify_daily/weekly/monthly` are one f-string registration). `tests/test_incidents.py::registered_tasks()` used to see only `@task`/`core.task`, so the "every task has a
    playbook" test was green without them; it now also sees `@register_task`, `register_task(` and the per-cadence f-string. Playbook `checks` may use `homelab-maint routine status|plan|due|check` (all read-only; run, ack,
    note, clear-halt and canary-reset write, and the lint test rejects them).
37. **`retention` refuses a match inside a never-touch tree at run time** (`cleaners.NEVER_TOUCH`, equal to `registry.NEVER_TOUCH`, test-pinned): `registry.analyze` cannot prove a `**` glob under an allowed root against the
    unanchored never-touch regexes (ai-stack, .cursor ...), so each selected path is tested (audit `refused-never-touch`, counted as protected). `app_cache_trim` already had its own list.
38. **The ack policy lived in two files for the pager (superseded by decision 43).** `notify.py` read `notify.toml [ack] require_rule/allow_tasks/deny_*`, the dashboard, inbox and CLI read `ack.toml [ack]`, so a list edited in one file only made the site
    say "acknowledged" while the alert paged. Shipped values stay equal (a test compares them, `tests/test_acks.py::TestPolicy`) and `doctor` still has the row ("ack policy equal in ack.toml and notify.toml"), now as a check for a stale deprecated copy.
39. **Dead knobs and stale markers removed:** `[tasks.stuck_detector] actions = "alert"` (nothing reads it; `rules check` warned), the 8 `NOTIFIER_UNFIXED` and 2 glue `xfail` markers (the fixes landed, the tests XPASSed: a regression
    would have stayed invisible). `tests/test_incidents.py::TestClockSkew` was a time bomb: `incidents list` prints its clock note for 24 h of the REAL clock, the fixtures sit on 2026-10-02, so it failed for good from
    2026-10-03 00:30; the clock is frozen in that test now. A +8 day shift of `time.time` over the whole suite fails 35 tests, all in file-mtime or subprocess tests (new files keep real mtimes); the ones inspected are artifacts of the shift, the rest were not analysed.
40. **`backup_freshness` counts the late backups it does not list** ("; +2 more", kept whole by truncating the body instead of the marker): a 4th late backup used to be invisible to the page and to an acknowledgement's key.

41. **The website's first-run login is answered by the production runner (`acks_auth.py`, registered in `acks.process_inbox`).** `acks_auth.py` is a verbatim copy of the tested reference `web/tools/ack_auth_handlers.py` (`auth_setup`: proof =
    HMAC of the request with a key derived from `ack/bootstrap.secret`, refused when `auth.json` exists, written 0640 root:<web gid> atomically and verified readable, outcome in `ack/auth_result.json`; `auth_change` op `burn`: a recovery code is removed durably) plus `register()`, which also syncs the `ack/` DIRECTORY after every handled request (`durable`, `fsync_dir`: the
    reference fsyncs the file, but a rename only survives a power cut once the directory entry is on disk, so a burned recovery code could otherwise come back after a crash; the handlers themselves stay verbatim the tested reference);
    `tests/test_acks_auth.py` keeps the two copies equal and compares them with `web/app.py`'s formats and `parse_auth`. `process_inbox` calls `_auth_handlers()` after it has seen a request and before it reads one (an idle minute registers
    nothing), and passes ITS OWN module: under `python3 -m homelab_maint.acks process` that is `__main__`, and registering into the package module would have left the kinds unanswered there (tested with a real subprocess). A handler someone else registered for a kind stays. `acks._quarantine` keeps
    `acks_auth.scrub(data)` of a refused request and redacts any base32 run of 26+ characters (an authenticator secret) on top of the 64-hex cut. Proven end to end against the real `web/app.py` (setup through the API -> `cli._ack_tick` -> login, with an authenticator and a burned recovery code) and, opt-in
    (`HM_DOCKER_TESTS=1`), as real root against uid 10001 in a throw-away container (`tests/test_acks_auth_container.py`).
42. **`ack/` is 0750 root:<web gid> in both installers; the e-mail button follows the site.** `install.sh` and `acks.init_dirs` agree: `ack/` 0750, `ack/inbox` 1730, `web.key` 0640, all root:<gid>; an older 0755 `ack/` is closed once it belongs to the group. The gid is `HM_WEB_GID`, else the group `web.key` already has (what the runner's
    `web_gid()` trusts, so a host that deployed another gid keeps it), else 10001. `install.sh --deploy-web` creates `ack/web_ready` (empty, 0644) ONLY after `docker inspect` says `maintenance-web` is healthy (up to `HM_WEB_WAIT_S`, 120 s); an unhealthy or slow container leaves it absent with a warning that names the manual command,
    `--no-web-ready` leaves it absent on purpose (the link in the mail points at the public hostname: create the file after the Cloudflare step), and a dry run only prints `would-wait`. Manual: `sudo touch /var/lib/homelab-maint/ack/web_ready`. `notify.toml [ack] button = "auto"` is what reads it.
    The wait function was also run unchanged against REAL throw-away containers of `maintenance-web:dev` (hardened like the compose file, loopback port, default bridge): a fresh `public/overview.json` gave `healthy` and return 0, one 2 hours old gave `unhealthy` and return 1 within seconds, so the container is healthy
    only while the runner has published recently (`STALE_MINUTES`, 45): run step 3 of the runbook (a check run publishes) shortly before the deploy, or the installer warns and leaves `web_ready` for you.
43. **One ack policy for the pager and the dashboard; the token gets the fingerprint's mode; a worsening re-alerts at once.** `notify._ack_allowed` delegates to `acks.ackable(task, fp)` (the notify.toml keys are a deprecated fallback for a module without it) and the deny check of `_event_fp` asks the same module, so
    `allow_tasks` in `ack.toml` alone now offers the button AND holds the alert. `_ack_offer` passes `mode=` to `issue_token` (HermesNotifier carries it as the reserved fact `ack_mode`), so an explicit-key task keeps its button. `_hold_released(key, nc, now, cur_fp)` treats a changed fingerprint at the SAME severity as released: a longer or
    different failing set, or the next magnitude decade, alerts in the very run that sees it instead of at the 24 h reminder.
44. **Seven tasks name their error from the FULL failing set (`Result.issue_key`, built with `core.ikey`).** `failed_units` (units, exited, unhealthy, restarting containers), `disk_forecast` (mounts), `probes` (down / degraded / flapping names + bad definitions), `docker_prune_exposure` (containers + images), `backup_freshness` (every bad backup; a failed one by its result word, a late one by the DECADE of its age in
    hours), `smart_trend` (per disk by model + serial tail: attribute + decade of growth, hot, stale, unreadable) and `os_jobs` (job + state word). Volatile numbers never enter (ages, percentages, restart counts, GiB, countdowns); a swap among the entries the summary hides changes the key (tests per task: a changed set
    changes the fingerprint, a moved number does not). `ikey` sorts and escapes `%`, `;` and `,`, so two different sets can never read alike. The `[key.<task>]` regex rules of `ack.toml` stay as the fallback for an entry without an `issue_key` (an old status.json, a summary-only event) and for tasks with no explicit key. What remains number-blind by design (SPEC5 S2): disk mount names, unit names, probe
    names, job names. A MONITORING BLIND probes result (`error`) keeps its text key (an `|error` suffix: never covered by an acknowledgement of a condition). The id of these seven tasks changed with this decision (explicit key instead of the rule's capture): an acknowledgement made on an earlier build would not match, and none exists on the live host.
45. **`homelab-maint web bootstrap` and two doctor rows.** The command (`acks.web_main`) prints `ack/bootstrap.secret` once, to a root terminal: it refuses when not root, when setup is already complete (`auth.json` exists, even an unusable one), when stdout is not a terminal (a pipe or file would keep it) and when the secret file is missing, empty,
    a symlink or owned by someone else; it audits THAT it was shown, never what. `doctor` gains "website /healthz (when deployed) has no warnings" (one loopback GET of `/healthz` on the `self_health` port, 3 s; nothing listening is fine) and "website login set up and readable by the site (ack/auth.json)" (`acks.auth_state`: absent is only a problem once the site answers; an `auth.json` the site's
    group cannot read or the site would refuse is a FAIL with the fix). The stale doctor hint about an "ack-process job" now names the tick.
46. **The runtime-registered `rules_registry` task is known to the registry (`registry.RUNTIME_TASKS`).** `known_names` (applies_to) and the `[tasks.X]` reference check take it from that table (a test keeps it equal to what `register_tasks` registers; the registration keeps its literal name so the playbook test's static scan still sees it), so a rule MAY name it; it stays out of `task_catalog` ("every task the code declares", and every catalogued task must have a `task.X` rule). The shipped content phase is closed and
    has no rule for it, so `tests/test_rules_shipped.py::ORPHAN_OK` keeps it as the one explicit, reasoned exemption (a content change, not an integration defect).
47. **The notify side is checked against the real site, not only against a reference written from SPEC5.** `tests/test_notify.py` keeps `website_parse/lookup` (written from the spec on purpose) and adds the same chain against `web/app.py` itself (`tests/website_helper.py` starts it on a loopback port): every link notify can build is served with a 200 and no redirect, a mutated one is not, and
    the POST confirm -> signed inbox request -> `process_inbox` -> status poll -> held alert chain runs through the site's own handlers.

48. **Before the first adoption an unsafe old config is a warning with the way out, not a page (final pass).** On the live host `sudo ./install.sh` leaves the Oct-1 `maint.toml` next to the new `rules.d`; its `[tasks.retention] unprotect` (the tunarr cache) is no longer on the
    baseline allow-list (the floor moved it to `app_cache_trim`), so the tick's disk watch found an "unsafe" file, `rules_registry` went CRIT and, after `alert_confirm_runs = 2`, paged by SMS and e-mail, and the same finding went out as a significant notice (a text) on the first tick:
    a predictable false alarm in the first hour of every install. `registry.status()` now says `adopted` (the registry has applied something at least once); while it is false `check_task` returns `warn` "rules registry not adopted yet (...): rules diff, then sudo homelab-maint rules sync --adopt"
    (the problems stay in its details) and the `disk_unsafe` notice is an e-mail (`unadopted`, not significant). After the first adoption nothing changes: an unsafe file is CRIT and a text (tested both ways, `tests/test_registry.py`, and the live-shaped case in `tests/test_rules_shipped.py`). The runbook
    now adopts in step 2, before the first check run, and `install.sh` prints the adoption notice in `--dry-run` too (it only appeared once `rules.d` existed) and puts adoption before the check in its "Next:" lines.
49. **An un-acknowledge pages at once even inside the 6 h alert dedupe window.** `_reopen_if_released` clears core's "already alerted" state but the page that went out shortly BEFORE the acknowledgement (a short `alert_reminder_hours`, a flap) still sat in the dedupe window, so the re-opened alert was logged as
    `deduped` ("same alert/crit ... 0 min ago"). The release now also drops that one `alert|<key>` dedupe slot (`_forget_alert_dedupe`); budgets, quiet hours and the hard cap still apply, and the next alert of the key is deduped normally again.
50. **`web/tests/test_ack_ui.mjs` could freeze for ever after test 10, and every browser test could outlive a stuck Chrome.** Cause, reproduced with a python3 shim that delays the dev server's start by 0.3 s: node:test starts a registered test while the module is still awaiting, the file started the dev server and Chrome AFTER registering its tests, and the `watch` test (10)
    replaces `setTimeout`, `Date` and `fetch` for a few milliseconds; a dev server or Chrome still starting at that moment lost its `await sleep(100)` to the fake clock and never woke. The start-up now comes before the first `test()`. `web/tools/browser.mjs` also gives every DevTools call a 60 s deadline, the page socket 10 s, the target list 5 s, and
    kills the Chrome and removes its profile dir when `launch()` fails (stuck runs used to leave headless Chromes behind); the dev server probe has a 2 s deadline per try.
51. **`web/tests/container_auth_probe.py` expects what each layout makes**: `ack/` root:10001 for `install` (what `install.sh` creates), root:root for `legacy`. The opt-in docker tests are green (4 passed).

## Where each glue item went

| Stream | Glue | Done in |
|---|---|---|
| routine | `TIERS` + monthly, `RunGuard` loop, `routine.write_export`, `record_approved`, `routine` subcommand, `--override/--force/--scheduled`, `is_manual`, doctor tick check | `cli.py` (`cmd_run`, `cmd_approve`, `main`, `cmd_doctor`) |
| routine, reports | `load_tasks` imports tasks outside `tasks/` (`report_daily/weekly`, `routine_*`) | `cli.EXTRA_TASK_MODULES` |
| reports | weekly digest after the report task; daily only when notable or Monday; send on `monitoring_gap` even without a score; once per report id | `cli._send_digests` (state in `digest-sent.json`) |
| spike, incidents, reports | history record carries `"alert"` | `cli.cmd_run` |
| incidents | `incidents.update(status, None, now)` after the lock; `incidents` subcommand | `cli._publish`, `PASS` |
| publish | `publish.publish(status)` after the lock; `publish` subcommand; sampler refreshes metrics.json | `cli._publish`, `cmd_publish`, `cmd_metrics_sample` |
| metrics | `metrics-sample`, `metrics-export` | `cli.py` |
| widgets | `/thermal`, `/load` (frozen by `--now`), `/metrics` | `server.py` (`EXTRA_ROUTES`, `render_extra`) |
| monitors | `/heartbeat` (HTTP 200 whatever `ok` says), skip task page when `self_notifies`, `probes` subcommand | `server.py`, `cli.cmd_run`, `PASS` |
| notify | delivery adapter, deferred sends, `deliver()` after the lock, `notify.flush_pending()` once per tick, `notify`/`notify-test`, doctor rows | `core.Notifier`, `cli._notifier`, `cmd_run`, `cmd_tick`, `cmd_doctor` |
| scheduler | `tick`, `schedule`, `job`; `cmd_run` keeps klass `J` rows; `--scheduled` in task jobs | `cli.py`, `jobs.task_entries` |
| ports | `smart-event` before argparse | `cli.main` |
| retire | `migrate`, `new`, `plugins`, `scaffold.load_plugins` (with `disabled_plugins`) in `load_tasks` | `cli.py` |
| publish/web | `disk_forecast` rows carry `size` (exact, no statvfs) | `tasks/checks_basic.py` |
| routine/publish | `TIER_STALE_S`/`TIER_ORDER` know `monthly` | `payloads.py` |
| widgets | the two `ops-*` checkers count every `ops-*.json` | `tests/test_payloads.py` (`ALL_OPS`) |
| widgets | `ops-thermals` / `ops-load` in the Homarr installer (ORDER/BIG/MOBILE with the test hunks) | `widgets/install_homarr_widgets.py`, `tests/test_payloads.py` (`N = len(inst.ORDER)`) |
| acks (SPEC5) | `ack` subcommand, `apply_to_status` in the lock, `Result.issue_key`, notifier fallback hold, `run_once` in the tick, `overall`/`_lvl` muting | `cli.py`, `core.py`, `scheduler.py`, `payloads.py` |
| registry (SPEC6) | `rules` subcommand, `registry.tick()` in the tick, `register_tasks`, doctor row, `write_public` + `rules-history.json` + manifest | `cli.py`, `publish.py`, `homelab_maint/__init__.py` (`__version__`) |
| self_health (SPEC6) | `self-health` subcommand, `self.json` in publish, doctor row, Kuma result | `cli.py`, `publish.py`, `core.kuma_push` |
| spike (pass 2) | `reports` level_max / worst_level from `gate_level` / `gate_peak` | `reports.py` |
| cleaners v2 | `ACTION_TITLES`, `CHANGE_KIND` | `reports.py`, `routine.py` |
| audit noise | aggregate dry-run rows, one `logger` per task run | `core.py`, `reports.py` (`n`) |
| website v2 | `live.history` `swap_pct` / `vram_pct` / `load1` | `live.py` |
| pass-2 fixes | mirror regenerated, 700 KB rules.json cap, routine playbooks, retention never-touch guard | `etc/rules.d/`, `registry.py`, `data/playbooks.toml`, `tasks/cleaners.py` |
| website login (SPEC6 s7) | `auth_setup` / `auth_change` handlers in the production inbox path, quarantine scrub + base32 redaction, `web bootstrap`, doctor rows | `acks_auth.py`, `acks.py` (`_auth_handlers`, `_kept`, `auth_state`, `web_main`), `cli.py` (`PASS["web"]`, `cmd_doctor`) |
| acknowledge postbox | `ack/` 0750 root:<web gid> in both installers, `ack/web_ready` after the container is healthy, `--no-web-ready` | `install.sh`, `acks.init_dirs` |
| ack policy, link and release | `_ack_allowed` -> `acks.ackable`, `issue_token(mode=)`, `_hold_released(cur_fp)` | `notify.py`, `notify_templates.py` (`ack_mode` is a reserved fact), `etc/notify.toml`, `etc/ack.toml` (comments) |
| explicit issue keys | the FULL failing set of seven tasks | `core.ikey`, `tasks/checks_basic.py`, `checks_health.py`, `monitors.py`, `native.py` |
| registry | `RUNTIME_TASKS` in `known_names` and the reference check | `registry.py`, `tests/test_rules_shipped.py` (`ORPHAN_OK` reason) |

Import cost: `import homelab_maint.cli` is about 30 ms and pulls in nothing but `core` (the per-minute tick imports it); `load_tasks()` is about 60 ms.
No module-level import cycle exists (`tests/test_cli.py` imports every module alone). The registry has no duplicate task name and no `module_*`
placeholder; a module that fails to import becomes ONE `error` task and `doctor` lists it.

## Left out on purpose (other lanes wire these; where the hook goes)

* **Weekly timer move to 08:00** (routine's optional note): the weekly window opens 07:45, the daily run can finish up to 20 min late; left to packaging.
* **`incident_open` / `incident_resolved`** (decision 31) and **a `/monitors` server route** (nothing reads it).
* (Done in pass 3, decision 44: the explicit `Result.issue_key` from the FULL failing set in `failed_units`, `disk_forecast`, `probes`, `docker_prune_exposure`, `backup_freshness`, `smart_trend` and `os_jobs`.)

## Open items (not done here, and why)

* **Number-blind ack rules** (disk mount names, unit names, probe names, job names) keep the same fingerprint while the size worsens (4 % free to 1 %): the accepted trade-off of SPEC5 S2. Backups and SMART growth do carry their magnitude decade (decision 44);
  a coarse bucket for the disk percentage would be an `acks.py` + `disk_forecast` change.
* **Not exercised on the live host** (nothing may mutate it): the first real install tick (jobs.toml pins the installed lib, so tick-started children act on live state), notify delivery over the real transport, `install.sh --deploy-web` against the real compose project (its health wait ran against throw-away containers, see decision 42; `web_ready` and the rest are tested with a stub `docker` and
  a dry run), and the Cloudflare hostname and Access policy (a dashboard task). Everything else ran in scratch `HOMELAB_MAINT_*` dirs, the real `web/app.py` on loopback, and (opt-in) a throw-away container as root against uid 10001.
* **A bigger `live.json` (52 KB)** is served as is; `web/` has no size assumption on it (MAX_JSON 1 MB), the Live tab reads the three new series when present.
* **jobs.toml: no last-known-good copy** (a typo is now loud, decision 16, but the tick still schedules nothing until it is fixed), and `admit()` ignores `PAUSE.<job>` for
  `pausable = false` jobs (a single backup cannot be paused on purpose). Scheduler lane.
* **`homelab-maint job --help` prints `usage: homelab-maint scheduler`; `incidents` and `notify` have no `-h` (usage text, exit 2).** Cosmetic, other modules' parsers.
* **A first install is noisy once.** The first `routine run` after an install catches up all 30 daily/weekly/monthly steps in one run (about 30 s; three of them re-run `memory_health`,
  5 s each) and the first check run on a cold state dir takes about 22 s (Immich gate sample 10 s, memory sample 5 s, first probe pass 5 s). Both are far inside their timeouts and
  nothing needs the sleeps to overlap; steady state is 6.5 s for the check tier.
* **The `ops-*.json` widgets are the Homarr v1 custom-widget format.** The deployed `homarr:develop` image still takes it. The checkout under `~/StudioProjects/homarr` is now v2: its
  `customWidgetImportSchema` rejects all seven files (`sources` and `requests` required). Nothing breaks until the owner upgrades Homarr; then `widgets/` needs a port (widgets lane). The three tests
  that parse templates with the fork's `react-jsx-parser` also skip now (the package left `node_modules`), so the template checks do not run on this machine.
* **The website's size budget and fixtures** (`web/tests/test_frontend.mjs`: 332 KB of JS+CSS against the 120 KB in `UI_CONTRACT.md`, and `web/fixtures` regenerated differently)
  belong to the website v2 team. Not an integration bug.

## Live-system reminders (nothing here was changed by this lane)

* **docker-prune.timer was disabled by the owner on the live host** (it would have removed the stopped `comfyui` container and then its 14.9 GiB image). `docker_prune_exposure` now says "docker-prune.timer is disabled:
  the legacy prune cannot run" and does not page; nothing in `install.sh` or `uninstall.sh` enables it, and nothing here may. If the owner re-enables it while a container is stopped the check pages (with its playbook).
* The check-tier `--apply` and `pressure_response` reclaim only take effect after `install.sh` runs; until then the installed units are the old ones.

## First install runbook

Run on the host, in this checkout, in this order. Nothing here turns a cleaner on (every `mode` ships `report`; the one shipped `apply` is the pressure reclaim rung) and nothing enables or disables `docker-prune.timer`. Each step is safe to repeat.

1. **Preview.** No root, writes nothing.
   ```
   ./install.sh --dry-run
   ```
   Read the `would-*` lines, the list of cleaners with `mode = "apply"`, and the warning about `docker-prune.timer` if it is enabled while containers are stopped.
2. **Install and adopt the rules registry, in one sitting.** Package, config (only what is absent; a changed shipped default is saved as `NAME.dist`), units, the `ack/` postbox (0750 root:10001, secrets created once and never printed); enables the six timers, `homelab-maint-www.service` and the live monitor. The timers run from the moment it finishes, so adopt the registry at once (the originals are kept in `/var/lib/homelab-maint/rules/orig`): until then the tick replaces nothing ("blocked") and the check tier warns `rules registry not adopted yet` (an e-mail; it texts only if it is still there after a reminder). Your Oct-1 `maint.toml` predates the release floor (the tunarr cache exemption moved from `retention` to `app_cache_trim`), so `rules diff` will show it.
   ```
   sudo ./install.sh
   homelab-maint rules check
   homelab-maint rules diff                   # a hand-maintained config file that differs is reported "blocked" and left alone
   sudo homelab-maint rules sync --adopt      # or do the install and this in one go: sudo ./install.sh --adopt-rules
   ```
   `./install.sh --dry-run` (step 1) now prints the same adoption notice, so the preview shows it too. Adopting replaces the Oct-1 `maint.toml` with the shipped one (every cleaner `report`, only `pressure_response` `apply`).
3. **First check run.**
   ```
   homelab-maint doctor                       # on a fresh host the sampler, live monitor and tick show FAIL for up to a minute: run it again
   homelab-maint run --tier check && homelab-maint status        # the first check run takes about 22 s; `rules_registry` reads "in sync"
   ```
4. **Deploy the website** (docker compose with BOTH `-f` files; the container listens on 127.0.0.1:8098 only). `--no-web-ready` keeps the e-mail Acknowledge button off until step 7, because the link in a mail points at the public hostname, which does not exist yet.
   ```
   sudo ./install.sh --deploy-web --no-web-ready
   docker ps --filter name=maintenance-web    # STATUS ... (healthy) after about 30 s; it is only healthy while the runner has published in the last 45 min (step 3's check run did)
   curl -s http://127.0.0.1:8098/healthz      # "warnings" lists what is still to do (setup not complete, nothing in front of the login)
   ```
5. **Bootstrap the website login** while the site is reachable only from this host. The command prints the first-run secret once, on this terminal, and never logs it.
   ```
   sudo homelab-maint web bootstrap
   ```
   Open `http://127.0.0.1:8098/` in a browser on this host (from another machine: `ssh -L 8098:127.0.0.1:8098 ohmz@<host>`, then the same URL), choose a passphrase (an authenticator app and recovery codes are optional) and paste the secret. The runner's one-minute tick answers; the screen says when it is done.
   If the browser refuses the site's cookies on plain http, or the runner does not answer within 4 minutes: `sudo python3 web/tools/set-ack-passphrase.py` sets the passphrase from this terminal without a browser. Check: `homelab-maint doctor` shows "website login set up and readable by the site (ack/auth.json)" ok.
6. **Cloudflare: hostname and Access policy** (dashboard steps with every field in `web/README.md`, "Cloudflare: public hostname and Access policy"). Zero Trust > Networks > Tunnels > your tunnel > Public Hostname: `maintenance.ohmzhomelab.ca`, type HTTP, URL `localhost:8098`, Host header empty. Access > Applications > Self-hosted for the same hostname with an Allow policy for your address(es) and no Bypass.
   Then tell the app Access is in front (it only silences the `/healthz` hint) and recreate the container:
   ```
   grep -qs '^OUTER_AUTH=' web/.env || echo 'OUTER_AUTH=access' >> web/.env
   sudo ./install.sh --deploy-web --no-web-ready
   ```
   In a private browser window `https://maintenance.ohmzhomelab.ca` must show the Access login first, then the dashboard (the owner login of step 5 is a second lock for acknowledge actions only).
7. **Turn the e-mail Acknowledge button on.** `install.sh --deploy-web` without `--no-web-ready` does this by itself once docker says the container is healthy; after step 6 do it by hand:
   ```
   sudo touch /var/lib/homelab-maint/ack/web_ready
   ```
8. **Verify.**
   ```
   curl -s http://127.0.0.1:8098/healthz                    # "warnings": []
   homelab-maint doctor                                      # the two website rows ok; read any remaining FAIL
   homelab-maint self-health                                 # the pipeline: runner -> publish -> website
   sudo homelab-maint notify-test --dry-run alert.warn       # renders and routes a TEST mail, sends nothing
   ```
   Optional drill, only if something is failing right now: `homelab-maint ack explain TASK` prints its id, then `homelab-maint ack add ID --days 1 --note drill`, `homelab-maint ack list`, `homelab-maint ack remove ID`.
   Roll back with `sudo ./uninstall.sh` (keeps config, state and logs; `--purge --yes` deletes them and removes the container).

## Verification

```
python3 -m pytest -q tests/test_cli.py        # the glue: subcommands, dispatch, run loop, notifier deferral, digests, server routes, contracts
python3 -m pytest -q tests/test_acks_auth.py tests/test_acks.py tests/test_notify.py tests/test_packaging.py   # the login handlers, acks, notify, installer
HM_DOCKER_TESTS=1 python3 -m pytest -q tests/test_acks_auth_container.py    # opt-in: the production runner as root against uid 10001 (throw-away container)
python3 -m pytest -q tests                    # everything (about 5 min)
python3 -B -c "import homelab_maint.cli"      # ~30 ms; no big module is imported
homelab-maint --help ; homelab-maint doctor   # doctor lists the registry, import errors, duplicate names, tick, probes, sampler, live
```
