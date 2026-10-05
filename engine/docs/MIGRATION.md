# Migrating everything under homelab-maint

The goal: nothing on this host schedules maintenance, watches a service or sends a maintenance message except homelab-maint.
The old timers, cron lines, scripts and notifiers are retired one at a time, each one gated on proof that its replacement works, and each
one reversible with one command. Nothing is ever deleted. This page is the runbook. The per-item part at the bottom is generated from
`etc/legacy-retirement.toml`, which is the inventory of every legacy scheduled thing and hook found on this host (60 items on 2026-10-02). Items you add yourself go in `/etc/homelab-maint/legacy-retirement.d/*.toml`.

## 1. The short version

1. Install the umbrella, prove it can reach your phone and inbox, let it watch the host for two days.
2. Work through the waves in order (section 6). One item at a time: `check`, dry run, `--apply`, look, soak (the loop is section 5).
3. Backups go last, one per fortnight, and the notification wrapper after both. Nothing that depends on a job counts until the tick has actually run it.
4. If anything looks wrong, `homelab-maint migrate rollback ITEM --apply` puts the unit, the script or the cron line back exactly as it was.
5. The whole thing takes about seven weeks if nothing goes wrong, because the soaks add up (calendar in section 6), plus the wait for the first real run of each backup. Nothing forces that pace.

`sudo homelab-maint migrate status` is the progress report at any time. `sudo homelab-maint migrate plan` prints what every remaining
cutover would do.

## 2. The five modes

| Mode | Meaning | Example on this host | What the cutover does |
|------|---------|----------------------|-----------------------|
| `adapter` | The proven script stays. The umbrella schedules it, gates it, times it out, logs it, alerts on it and reports it. | `backup-system`, `stack-backup`, `search-canary`, the Bazarr cron line | disables the old timer or comments the cron line, then hands the job to the scheduler tick |
| `port` | The logic was rewritten as a native task and tested against the old script's decisions. | `notebook-db-alert` -> `surrealdb_health`, `docker-prune` -> `docker_cache` + `docker_images` + `docker_containers_prune`, `smart-alert.sh` -> `smart_event` | disables the timer, moves the script to `/usr/local/lib/homelab-maint/legacy/NAME/` |
| `retire` | Superseded, nothing replaces it (or a platform setting does). | `mem-guard` (it only ever logged), the three root cron lines retired on 2026-10-01 | same mechanics as `port` |
| `observe` | OS-managed. Security updates must keep working. | `apt-daily-upgrade`, `logrotate`, `fstrim`, `systemd-tmpfiles-clean`, snapd refresh | nothing is touched; the `os_jobs` check verifies each ran on time and succeeded |
| `keep` | Daemon or app plumbing. | docker, cloudflared, tailscale, glances, **sensor-exporter** (fan control), Hermes, Uptime Kuma, Plex, Ollama | nothing is touched; the monitoring plane probes it. `cutover` only records that it is accounted for |

Uptime Kuma stays: Homarr's uptime widget reads it. The probe plane in `probes.toml` covers the same targets and is the source of truth.

## 3. Ground rules

* **Dry run is the default.** `cutover` and `rollback` print the exact commands and change nothing unless you add `--apply`. Anyone can
  dry-run; `--apply` needs root (`sudo`).
* **Parity gate.** `cutover` refuses until the replacement has proven itself: native tasks need 8 consecutive green runs over at least 48 hours with
  the newest run less than 2 hours old (daily and weekly tasks set their own limits), a probe must also be `up` right now with a fresh last run, the scheduler must
  list the job, status files must be fresh, and the notification path must have delivered a test in the last 30 days. Two checks stand in for the proof run that cannot
  exist before an adapter's cutover (the legacy timer's interlock forbids it): `unit_equiv` (the effective `jobs.toml` job launches exactly what `systemctl show` says the legacy
  service launches: command, user, nice, ionice, timeout, `RequiresMountsFor`, environment variable names) and `scheduler_validate` (`homelab-maint scheduler validate` is clean
  for the job; one `jobs.toml` typo makes the tick schedule nothing at all). `homelab-maint migrate check ITEM` shows each check as `[ok]`, `[NO]` or `[??]`
  (could not be verified, counts as not green). A status file written by the legacy driver is a baseline of the old script, not proof about the umbrella.
* **Dependencies, proof and soak.** An item waits until everything it depends on is retired *and* has soaked for that item's `soak_days`. When the dependency handed a job to
  the tick, two more conditions apply: the tick must have recorded `verified_runs` green runs of that job since the cutover (2 for the backups, 1 elsewhere; `[NO] depends X is retired
  but unproven`), and the soak counts from the FIRST of them. Days on the calendar with a backup the tick never launched prove nothing.
  Right after such a cutover start the first run yourself, attended: `homelab-maint job run NAME`.
* **Never under a running job.** The cutover AND the rollback are refused while a unit of the item is running (`require_idle`), while the tick is running the item's job
  (a cut-over backup is never `backup-system.service` again, so the unit alone would always say idle), and for the backup items while a `/run/lock/backup-*.lock` is held.
  `--force` cannot override this. While a job is handed over or taken back the tick's own lock is held, so the tick cannot launch it between the check and the switch.
* **`--force --reason "at least eight characters"`** overrides parity and soak only. The reason goes into the maintenance journal, the change log and
  the notification. Use it for a thing you can see is fine and the check cannot (a keep item that has no probe yet), not to hurry.
* **PAUSE is honoured.** While `/etc/homelab-maint/PAUSE` or `PAUSE.migrate` exists, `--apply` cutovers are refused (`homelab-maint pause migrate`,
  `homelab-maint resume migrate`). A rollback while paused needs `--force --reason`.
* **One at a time, locked.** A lock stops two migrations from running at once.
* **PAUSE also stops the tick.** While `PAUSE` exists the tick starts no *pausable* managed job except read-only monitors, so a backup you already moved to the tick would not run during a
  pause, and nothing but the eventual expiry alert would say so (before the migration the legacy backup timers ignored PAUSE). So the backup items require `pausable = false` on their
  jobs (`job_attr` parity), and `migrate status` prints a `!!` banner and a `PAUSED` note on every item whose job is switched off whenever `PAUSE` or `PAUSE.<name>` exists.
* **Write-ahead state.** Before each action the prior state is recorded in `/var/lib/homelab-maint/migration.json` (enablement, active state, file mode,
  owner and sha256, the original cron line). A crash leaves a record that `rollback` can use.
* **Fail closed.** If a unit's state cannot be read, a script is unreadable, two cron lines match, a file changed underneath, or something unexpected
  sits at a path, the step is refused and nothing in that step is touched. A cutover that fails half way undoes its own earlier steps. A cutover that is killed half way, or whose
  undo could not finish (`attention`), keeps its records: run it again (it finishes what is left) or roll it back, and either way the rollback undoes what the first run did.
* **The platform cannot be retired by a typo.** Validation refuses to disable `docker`, `containerd`, `ssh`, `cron`, `cloudflared`, `tailscaled`,
  `NetworkManager`, `fail2ban`, `smartd`, `snapd`, `systemd-*`, or the umbrella's own `www`, `live`, `tick` and **`check`** units. The check timer is the one runner that does not depend on
  the tick: it is what notices (probe `umbrella-tick`, paged through `core.Notifier`) when the tick dies, so it stays on its own systemd timer for good.

## 4. Before the first cutover

```sh
cd /home/ohmz/homelab-maint
python3 -m pytest tests -q                       # everything green
sudo ./install.sh --dry-run && sudo ./install.sh # code, config (only if absent), units; the legacy units stay exactly as they are
sudo homelab-maint doctor
systemctl list-timers 'homelab-maint-*'          # check, daily, weekly, metrics and the one-minute tick
python3 -m homelab_maint.scheduler health        # "ok: tick ran Ns ago"
sudo homelab-maint migrate validate              # "inventory ok: 60 items, 25 retirable"
sudo homelab-maint migrate status
```

Then, in this order:

1. **Config is complete.** `ls /etc/homelab-maint` shows `jobs.toml`, `probes.toml`, `notify.toml`, `routine.toml`, `classes.toml`, `playbooks.toml`,
   `legacy-retirement.toml` next to `maint.toml` and `protected.toml`. Every job in `jobs.toml` ships `mode = "observe"`: the tick only *shows* it and the legacy
   driver still runs it. Installing the umbrella changes nothing about how the host runs today.
2. **Look at the one gap the audit found.** `purge_public_guests.py` is load-bearing and nothing scheduled it. `jobs.toml` ships it as the job `purge-public-guests` in `observe` mode
   (it deletes accounts, so it is adopted deliberately). Run the script by hand without `--yes` (a dry run that only lists accounts), read the list, and adopt it in wave 3.
3. **Prove the notification path.** `sudo homelab-maint notify-test` sends one clearly labelled TEST of each kind to your phone and inbox. Check that
   they arrived, then `sudo homelab-maint migrate check notify-route`. Nothing that notifies is cut over before this is green.
4. **Let the checks run for two days.** The native tasks (`surrealdb_health`, `comfyui_idle_reclaim`, `immich_recycle`, `openwebui_media_prune`,
   `docker_prune_parity`, `os_jobs`, `probes`, ...) run in report mode. Parity needs their history.
5. **Check-tier ports need `--apply`, and they have it.** `comfyui_idle_reclaim` and `immich_recycle` are C1 tasks in the check tier. A C1 task only changes
   anything when its mode is `apply` **and** the run carries `--apply`, so `homelab-maint-check.service` and the `tier-check` job in `jobs.toml` both run
   `homelab-maint run --tier check --apply` (check-tier tasks that are C0 can never change anything, whatever the flag; keep the two commands in step).
   What is still missing at install time is the task's own `mode = "apply"` in `maint.toml`: the `task_applies` parity check stays red, and the cutover refuses,
   until you set it. (Alternative: give the task its own cron `schedule` in `maint.toml`, which the tick runs with `--apply`.)
6. **Backups ignore the global kill switch.** `etc/jobs.toml` ships `pausable = false` for `backup-system`, `backup-immich` and `stack-backup`: the parity
   checks read the effective file and refuse otherwise. (`PAUSE.<job>` stays the way to stop one deliberately; the scheduler must honour it for a non-pausable job too.)
7. **Upgrades keep your config.** `/etc/homelab-maint/*.toml` is never overwritten (`install.sh` saves the shipped copy as `NAME.dist` and lists the `[tasks.X]`
   tables and jobs your copy lacks), except `legacy-retirement.toml`, which is release data: replace it with every install; your own items go in
   `/etc/homelab-maint/legacy-retirement.d/*.toml`. `install.sh` also asks `homelab-maint migrate retired` and never re-enables a timer, or re-installs a
   drop-in, that a cutover retired.

## 5. The loop for one item

```sh
sudo homelab-maint migrate check notebook-db-alert              # 1. is the replacement proven? (read only)
sudo homelab-maint migrate cutover notebook-db-alert            # 2. dry run: the exact commands, nothing changes
sudo homelab-maint migrate cutover notebook-db-alert --apply    # 3. do it
sudo homelab-maint migrate status | grep notebook-db-alert      # 4. state "retired"; then watch it for the soak
sudo homelab-maint migrate rollback notebook-db-alert --apply   #    only if needed
```

What step 2 prints for a port whose replacement has not proven itself yet (real output on this host, from a read-only run):

```
DRY RUN (nothing will change; add --apply) - cutover notebook-db-alert: notebook-db-alert (SurrealDB WAL / disk leading indicators) [port, wave 3]
  todo      disable notebook-db-alert.timer
      $ systemctl disable --now notebook-db-alert.timer
  todo      move /usr/local/sbin/notebook-db-alert.sh -> /usr/local/lib/homelab-maint/legacy/notebook-db-alert/notebook-db-alert.sh
      $ mkdir -p /usr/local/lib/homelab-maint/legacy/notebook-db-alert
      $ write /usr/local/lib/homelab-maint/legacy/notebook-db-alert/README.md
      $ mv /usr/local/sbin/notebook-db-alert.sh /usr/local/lib/homelab-maint/legacy/notebook-db-alert/notebook-db-alert.sh
  todo      scheduler: job notebook-db-alert -> retired
      $ homelab-maint job mode notebook-db-alert retired   # writes job-modes.json
  [NO] depends notify-route: notify-route is pending; cut it over first
  [NO] task surrealdb_health: no runs recorded yet
  [NO] notify test: 0 delivered in the last 30 days (run: homelab-maint notify-test)
REFUSED: parity is not green (see the checks above); fix that, or override with --force --reason '...'
```

For a user timer the same step reads `runuser -u ohmz -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user disable --now stack-backup.timer`, and for a
cron line `crontab -u ohmz -   # comments 1 line: '15 4 * * 1 /home/ohmz/tunarr-sync/run.sh # tunarr-weekly-cha'`, preceded by a saved copy of the whole crontab.

What an applied cutover does, per kind of action, always in the inventory's order:

| Action | Forward | Rollback restores |
|--------|---------|-------------------|
| `disable` | `systemctl disable --now UNIT` (never `mask`, never deletes the unit file). A unit that is already disabled or gone is left alone. | the recorded `UnitFileState` (`enable`, `enable --runtime` if it was runtime-enabled) and `start` if it was active. A oneshot service is never started: that would run the job. A linked unit is refused: its state cannot be restored exactly |
| `move` | the file goes to `/usr/local/lib/homelab-maint/legacy/ITEM/` with a `README.md` that says what it was and how to put it back (`rename(2)`; across filesystems copy, verify the sha256, then remove). Never overwrites, never moves directories | the file back to its exact path with its recorded mode and owner, refusing if something new sits there or the legacy copy changed |
| `move` + `stub` on the same path | where something else still calls the old path (`smartd` names `smart-alert.sh` and does not retry; `jobs.toml` names `backup-failed.sh`) the file is **copied** (verified) and stays in place; the stub then replaces it with ONE `rename(2)`, and only if the copy still equals the original. The path is never absent, not even for an instant | one `rename(2)` of the verified copy over the stub (exact mode and owner), refusing if the stub was edited or the copy changed. A crash between the two steps leaves the real script in place |
| `cron_comment` | prefixes the one matching line with `#HM-RETIRED[ITEM] `. Every other line is proven byte-identical before and after, the crontab is read back, and a copy is kept in `/var/lib/homelab-maint/migration/ITEM/`. Refuses unless exactly one active line matches | strips our marker from that one line; refuses if you have since removed the marker line |
| `job_mode` | `homelab-maint job mode JOB managed` (adapters) or `retired` (ports): the tick starts running the job. Always the **last** forward action | the recorded override (or none) first, **before** the legacy driver comes back, so the tick stops launching the job before the old timer can |
| `manual` | printed as a reminder; the tool never does these (recreating a container, editing a file with a password in it) | nothing |

Each applied step is written to `/var/log/homelab-maint/audit.jsonl` (task `migrate`: `dry-run`, `done`, `refused-paused` or `failed: ...`), the
cutover as a whole to the maintenance journal (the website's "what was done" list), the change log, and one `maintenance` message through `notify.send`.
A rollback is marked significant: text plus email. `homelab-maint migrate journal` lists the migration entries.

**Idempotent.** Running the same cutover again finds every action already in effect and changes nothing. If you retired part of an item by hand, the cutover
finishes only what is left, and rollback leaves alone what it did not do. If a cutover was killed half way (`partial`), or its undo could not finish (`attention`),
running it again keeps the records of the steps the earlier run already did, so a later rollback still undoes them (the timer comes back as well as the script). What a failed run fully undid
is marked `undone` and never counts as something the tool did.

## 6. Order, soak and the "do not retire X until Y" rules

The waves are the safe order. Within a wave, go top to bottom through the generated runbook below; the tool already refuses an item whose dependencies are not
retired and soaked. The calendar at the top of the generated runbook is the earliest day each item can go, assuming day 0 is a healthy install.

| Wave | What | Why this position |
|-----:|------|-------------------|
| 0 | Observe: the OS timers, cron shims, user package timers | no host change; the `os_jobs` check must show them green |
| 1 | Keep: monitors and always-on daemons (Kuma, docker, cloudflared, tailscale, glances, sensor-exporter, Hermes, the umbrella's own services, boot units, stray daemons) | no host change; each is probed. Adopt them so the status page is complete |
| 2 | Notification hooks: `notify-route` (the proof), `smartd-alert-hook`, `diun-mail` (a manual step), `stack-alert-hook` (kept) | everything after this sends through one path, so this path must be proven first |
| 3 | Small jobs and cron lines: `notebook-db-alert`, `comfyui-idle-vram`, `prune-openwebui-media`, `mem-guard`, `immich-server-recycle` and its gate drop-in, `docker-prune`, the Bazarr and Tunarr cron lines, `purge-public-guests`, the retired root cron lines | low blast radius, easy to see a regression |
| 4 | The tick takes over: `tier-check` is *adopted* (its timer stays), then `search-canary`, `stack-watchdog`, `stack-backup`, then the daily, weekly and metrics runner timers (`tier-daily`, `tier-weekly`, `metrics-sample`) | prove the tick on a harmless job first; after this wave everything that runs starts from one process, so the independent check tier must be proven first and stays |
| 5 | System backups: `backup-system`, then `backup-immich` | the crown jewels, one per fortnight so a regression is isolated to one disk |
| 6 | Backup wrappers: `backup-notify-wrapper`, then `backup-failure-hook` | only after both backups run under the umbrella |

Rules that are enforced (parity or dependency) and the reason behind each:

* **Do not retire anything that notifies (smartd, the backups, the Hermes jobs) until `notify-route` is adopted.** It needs a delivered `notify-test` and 8 green
  `alert_path_health` runs over 48 hours. Otherwise the thing you retire could fail and the replacement's alert go nowhere.
* **`tier-check` is adopted, never retired, and everything the tick takes over waits for it.** After wave 4 the stack watchdog, both backups and the daily/weekly runners all start from one
  process, the tick, and so does `probes-run`. Retiring `homelab-maint-check.timer` as well would leave no runner that is independent of it: a `jobs.toml` typo makes the tick still beat
  (so a heartbeat probe stays green) while it schedules nothing, and Kuma's dead-man push pages nobody (Kuma has no notification channel). So the check tier keeps its own timer; its `probes`
  task judges `umbrella-tick` (tick.json older than 5 minutes is crit) and `backup_freshness`, `os_jobs` and `alert_path_health` run there. Adopting the item
  (`migrate cutover tier-check --apply`) changes nothing on the host; it requires the tick beating, `scheduler validate` clean, the timer enabled and the `umbrella-tick` probe up,
  and `search-canary` waits for it. `tier-daily` and `metrics-sample` wait 7 days for `stack-watchdog`, and `tier-weekly` for `tier-daily`.
  If `status.json` stops refreshing, roll the last item back at once: `sudo homelab-maint migrate rollback ITEM --apply` (it works even if `jobs.toml` no longer loads).
* **Do not cut over `stack-watchdog` until `search-canary` has soaked 3 days, and `stack-backup` until `stack-watchdog` has soaked 7.** The watchdog is the alarm
  that covers the Hermes alarm. It should be the first important thing the tick runs, so a misbehaving tick shows up there before a backup depends on it.
* **Do not cut over `backup-system` until `stack-backup` and `tier-daily` have each soaked 7 days, and `backup-immich` until `backup-system` has soaked 14 days AND the tick has recorded 2 green runs of it.**
  Two weekly cycles per backup, counted from the first run the tick really made. Before the cutover `unit_equiv` proves the job is the unit (same command, user, nice, ionice, no timeout, same mounts); after
  it the first run is yours: `sudo homelab-maint job run backup-system`, attended, then compare its `/var/log/backup/*-status.json` with the previous one. Cut over Monday to Thursday, not on the day before
  the first run, and never while a backup runs (the unit, the tick's own run, or a held lock: refused, also for the rollback).
* **Do not move `backup-notify.sh` until both backup jobs have `self_notifies = false` in `/etc/homelab-maint/jobs.toml`.** While a job has `self_notifies = true`
  the umbrella stays silent about ordinary failures because the script alerts for itself. Moving the script without flipping that flag would make a failed backup
  silent. The parity checks read the effective jobs.toml and refuse otherwise, and so does a `jobs.toml` that no longer validates (edit it, then run `homelab-maint scheduler validate`). After the cutover `backup-common.sh` finds no executable `backup-notify.sh` and skips
  it (`[ -x /usr/local/sbin/backup-notify.sh ]`), and the umbrella tells you instead: an email after each backup, a text only for a failure. You stop getting the
  "backup OK" text.
* **Never move `backup-notify-hermes.py` or `backup_report_html.py`.** `core.Notifier` and `notify.py` import them (the bridge fallback and the email palette). They
  are libraries of the umbrella now, not legacy.
* **Never retire `sensor-exporter`.** It supervises the fans (hardware control). `fancontrol.service` is enabled but never starts, because `/etc/fancontrol` does not exist;
  do not create it, or two programs would drive the same PWM outputs.
* **Do not drop the OS timers.** `apt-daily-upgrade` carries the unattended security updates. They are observed, never replaced.
* **A port that can only report is not a replacement.** `comfyui-idle-vram`, `prune-openwebui-media`, `immich-server-recycle` and `docker-prune` need their native task in
  `mode = "apply"` and something that really passes `--apply` (section 4, step 5). Flip `mode = "apply"` in `maint.toml` and cut over in the same sitting: until the legacy
  timer is disabled, both sides act (idempotent for the cleaners, a possible second restart for ComfyUI or Immich).
* **`docker-prune` waits for `docker_prune_parity` to be green.** The classic regression: the build cache once reached 46 GB while the old job reported success, because
  root's docker only sees its own empty `default` builder. The natives need `Environment=DOCKER_CONFIG=/home/ohmz/.docker` in `homelab-maint-daily.service` and
  `-weekly.service`; `docker_prune_parity` reports "builder discovery" as a gap until they have it. Volumes are never pruned by either side.

Soak, in short (each counted from the first green run the tick recorded after that item's cutover): `search-canary` 3 days; `stack-watchdog`, `stack-backup` and `tier-daily` 7 days each; `backup-system` and `backup-immich` 14 days each;
`immich-server-recycle` 1 day before its drop-in goes; `smartd-alert-hook` 14 days of watching (nothing later waits for it). Soak is time with the new arrangement in place and
no alert you did not expect, not time on the clock.

## 7. After each wave: what to look at

* `homelab-maint status` and the website's Health tab: no new warn or crit, no task in `error`.
* `homelab-maint schedule`: every managed job has a next run, nothing runs twice.
* `homelab-maint migrate status`: the item says `retired` (or `adopted` for keep items), no `DRIFT`.
* The job's own log: `/var/log/homelab-maint/jobs/NAME/<run>.log`, and for natives the audit trail `grep NAME /var/log/homelab-maint/audit.jsonl | tail`.
* `sudo homelab-maint migrate audit`: read only. It lists any timer (system or ohmz user manager), active crontab line (root, ohmz) or file in `/etc/cron.*` that no inventory item accounts for.
  On 2026-10-02 it printed `audit: everything on the host is in the inventory`. Run it after installing anything new; if it names something, add an `[[item]]` or retire the thing.
* The next real run of the thing you moved: the Bazarr rules at 02:30, the Tunarr sync on Monday 04:15, the stack backup at 03:30, the weekly backups on Saturday and Sunday 01:00.
  Read the email it sends.

Done means: `migrate status` ends with all retirable items retired, `migrate audit` is clean, the website's Migration card has disappeared, and `homelab-maint schedule` is the only list of
what runs on this host.

## 8. Rolling back

```sh
sudo homelab-maint migrate rollback ITEM                 # dry run: prints the inverse commands, in reverse order
sudo homelab-maint migrate rollback ITEM --apply
```

Rollback replays the inverse from the state recorded before the cutover, in reverse order, and checks that the host now matches that record. An action that was already
satisfied before the cutover is not touched. It is refused while the item's job is running under the tick or a backup lock is held (otherwise re-enabling a `Persistent` timer under a
running backup would start a second one that dies on the lock and sends a false BACKUP FAILURE); `--force` cannot override that, and the dry run says so without refusing. Taking a job back
from the tick only touches `job-modes.json`, so it works even when `jobs.toml` no longer loads. If something cannot be restored exactly (the legacy copy was edited, a new file sits at the old path) it stops with a precise message and the
item's state becomes `attention`; fix the cause and run it again. Rolling back an item that others depend on is refused until those are rolled back first (or `--force --reason`).

When a mutating native replacement exists (`comfyui_idle_reclaim`, `immich_recycle`, `openwebui_media_prune`, `docker_cache`, `docker_images`, `docker_containers_prune`),
rollback also creates `PAUSE.<task>` so the restored legacy timer and the native task never both act. The tool tells you; `homelab-maint resume TASK` removes it, and a later cutover
removes the ones it made.

Rollback by hand, if the tool itself is unavailable (every legacy directory has a `README.md` with the same commands):

```sh
sudo systemctl enable --now notebook-db-alert.timer                                         # a system timer
sudo -u ohmz XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now stack-backup.timer   # a user timer
sudo mv /usr/local/lib/homelab-maint/legacy/notebook-db-alert/notebook-db-alert.sh /usr/local/sbin/   # a moved script (check the README for the exact path)
sudo crontab -u ohmz -e                                                                     # delete the "#HM-RETIRED[ITEM] " prefix from the one line
sudo homelab-maint job mode notebook-db-alert reset                                         # take the job back from the tick, FIRST
```

Order matters in that last case: take the job back from the tick before the old timer returns, or both may run.

Most of these timers are `Persistent=yes` (the backups, the tier timers, `stack-backup`). When one is enabled and started again, systemd may run an occurrence it missed while it was
off, immediately. The backup scripts hold their own lock, so a second backup waits or exits, but expect one extra run right after a rollback and pick a quiet moment for it.
The rollback dry run says which timers do this (`note: X.timer is Persistent=yes`).

## 9. Things the tool does not do (do them yourself, once)

* **Diun's own email.** Diun mails with its own SMTP credentials from `/home/ohmz/diun/diun.env`, the one notifier outside `notify.py`. To retire it, comment out the `DIUN_NOTIF_MAIL_*`
  lines with a text editor (never print the file) and recreate only that container: `docker compose -f /home/ohmz/homelab-capture/diun.yml up -d diun`. Diun keeps checking registries; the weekly
  report lists the findings. Until then you get both.
* **`install.sh` re-runs.** `install.sh` enables the tier, metrics, self-health and tick timers and the two daemons, and installs the Immich gate drop-in, every time it runs, but it first asks what a cutover
  retired: `homelab-maint migrate retired --kind unit` and `--kind path` print one reference per line, and `install.sh` leaves those timers disabled and that drop-in absent (it prints `retired`
  instead of `enable`). A host that predates this check still shows `DRIFT` in `migrate status` after a re-run, and the cutover (idempotent) fixes it. `uninstall.sh` goes the other way: it
  refuses while a cutover has retired a timer, drop-in or script (nothing would run that job any more), and while a job the tick started is still alive; roll the items back first.
* **`routine.toml` `[[system]]` entries.** The 14-day calendar lists `backup-system`, `backup-immich`, `stack-backup`, `docker-prune` and `prune-openwebui-media` with `managed = "legacy"`. After their cutover
  change them to `managed = "app"` or delete them, since the unified schedule shows the jobs themselves.
* **`jobs.toml` `self_notifies` and `pausable`.** Set `self_notifies = false` for `backup-system` and `backup-immich` before the wrapper item (section 6), and `pausable = false` for the three backup jobs
  before their cutovers (section 4). Nothing else flips them. Edit the file with care and run `homelab-maint scheduler validate` straight after: one typo makes the tick schedule nothing.
* **`OnFailure=` lines** in `backup-system.service`, `backup-immich.service` and `stack-backup.service` stay. They are inert once the jobs run under the tick and they cost nothing.
* **The legacy directory.** `/usr/local/lib/homelab-maint/legacy/` keeps everything that was moved. Remove it yourself, later, when you are sure; no tool deletes it.
* **`launchpadlib-cache-clean`, `firmware-notifier`, the anacron shims and the Ubuntu convenience timers** are listed and observed only. `/etc/cron.daily/apport` deletes crash reports older than 7 days; the
  `crash-dumps` retention rule does the same for files, so either may act first and both are safe.

## 10. Status words

| State | Meaning |
|-------|---------|
| `pending` | not cut over yet |
| `retired` | cut over; the old timer, script or cron line is out of the way |
| `adopted` | recorded as accounted for (keep, observe, or an adapter with nothing to retire) |
| `retired-by-hand` | the host already matches the cutover although the tool did not do it; run the cutover to record it |
| `partial` | some but not all actions are in effect (a cutover that stopped, or half done by hand) |
| `failed` | a cutover failed and was fully undone |
| `attention` | a cutover or rollback stopped half way and could not restore everything, or a re-run failed while an earlier run's steps are still in effect: read the message, fix the cause, run it again (the earlier run's records are kept, so a rollback still undoes everything) |
| `rolled_back` | restored |
| `DRIFT` (note) | recorded as retired but the host says otherwise, for example `install.sh` re-enabled a timer |

## 11. Refusals you will see

| Message | Meaning and fix |
|---------|-----------------|
| `parity is not green` | read the `[NO]` lines. Wait, fix the cause, or `--force --reason` if you can see what the check cannot |
| `X is pending; cut it over first` / `X is still soaking: N more day(s)` | a dependency is not retired, or has not soaked |
| `busy: X.service is running right now` / `job X is running under the tick` / `a backup is running: ...lock is held` | wait for the job to finish (cutover and rollback alike). Not overridable |
| `X is retired but unproven: job X has 0 of 2 green run(s) recorded` | the tick has not run the dependency's job yet: `homelab-maint job run X` attended, then wait for the soak, which counts from its first green run |
| `the scheduler tick holds its lock right now` | the tick is mid-run (it normally takes tens of milliseconds); try again in a minute |
| `no longer matches its legacy copy` / `was modified since it was made` | a script changed between the verified copy and the stub (or the copy was edited): nothing was replaced; look at both files |
| `PAUSE (or PAUSE.migrate) is present` | `homelab-maint resume migrate` (rollback: `--force --reason`) |
| `N active crontab lines match` | the tag or text no longer identifies exactly one line; look at the crontab |
| `the crontab changed while the cutover was running` | somebody edited it; nothing was written, run again |
| `already exists` / `something now exists at` | a legacy copy or an original path is occupied; the tool never overwrites |
| `job X is not defined in jobs.toml` | add the job to `/etc/homelab-maint/jobs.toml` first |
| `cannot read ...: permission denied` | run it as root |
| `another migrate run is in progress` | the lock is held; wait |
| `inventory error` | `/etc/homelab-maint/legacy-retirement.toml` or a file in `legacy-retirement.d/` is missing, not root-owned, group/world writable, or invalid (every problem is listed) |

## 12. Commands

```sh
homelab-maint migrate status [--json] [--fast]         # every item: mode, wave, state, replacement, blockers (--fast reads no host state); shouts when PAUSE is on
homelab-maint migrate plan [ITEM]                      # what each remaining cutover would do, exact commands
homelab-maint migrate check ITEM                       # parity and dependencies, read only
homelab-maint migrate cutover ITEM [--apply] [--force --reason "..."]
homelab-maint migrate rollback ITEM [--apply] [--force --reason "..."]
homelab-maint migrate journal [-n 20]                  # the migration entries of the maintenance journal
homelab-maint migrate retired [--kind unit|path]       # what an installer must not put back
homelab-maint migrate audit [--json]                   # timers, cron lines and cron files nobody accounted for (read only, exit 1 if any)
homelab-maint migrate validate                         # the inventory parses and is safe
homelab-maint migrate export                           # migration.json for the website (no paths, no commands)
homelab-maint migrate runbook [--write docs/MIGRATION.md]   # regenerate the section below
```

## 13. Per-item runbook

<!-- BEGIN GENERATED RUNBOOK -->

Generated by `python3 -m homelab_maint.legacy runbook` from etc/legacy-retirement.toml. Do not edit by hand.

### Earliest calendar

Day 0 is a healthy install with `notify-test` delivered. A day is the soonest the parity window and the soak of everything the item depends on allow; the tool enforces the same numbers, and a cutover you postpone only pushes later items back. The soak of a job the tick took over counts from that job's FIRST green run, so add the wait for it (a daily job about a day, a weekly backup up to a week): the days below are a lower bound.

| Day | Item | Mode | Waits for |
|----:|------|------|-----------|
| 2 | `notify-route` | keep | - |
| 2 | `smartd-alert-hook` | port | `notify-route` |
| 2 | `diun-mail` | adapter | `notify-route` |
| 2 | `notebook-db-alert` | port | `notify-route` |
| 2 | `comfyui-idle-vram` | port | `notify-route` |
| 2 | `prune-openwebui-media` | port | `notify-route` |
| 2 | `mem-guard` | retire | - |
| 2 | `immich-server-recycle` | port | `notify-route` |
| 2 | `docker-prune` | port | `notify-route` |
| 2 | `cron-bazarr-subtitle-rules` | adapter | `notify-route` |
| 2 | `cron-tunarr-weekly-sync` | adapter | `notify-route` |
| 2 | `purge-public-guests` | adapter | `notify-route` |
| 2 | `search-canary` | adapter | `notify-route`, `tier-check` |
| 2 | `tier-check` | keep | - |
| 3 | `immich-recycle-gate-dropin` | retire | `immich-server-recycle` +1 d |
| 5 | `stack-watchdog` | adapter | `notify-route`, `search-canary` +3 d |
| 12 | `stack-backup` | adapter | `stack-watchdog` +7 d |
| 12 | `tier-daily` | adapter | `stack-watchdog` +7 d |
| 12 | `metrics-sample` | adapter | `stack-watchdog` +7 d |
| 19 | `tier-weekly` | adapter | `tier-daily` +7 d |
| 19 | `backup-system` | adapter | `stack-backup` +7 d, `tier-daily` +7 d, `notify-route` |
| 33 | `backup-immich` | adapter | `backup-system` +14 d |
| 47 | `backup-notify-wrapper` | retire | `backup-system` +14 d, `backup-immich` +14 d |
| 47 | `backup-failure-hook` | retire | `backup-notify-wrapper`, `backup-system` +14 d, `backup-immich` +14 d |

### Wave 0: Observe: OS-managed jobs (no host change)

#### `os-apt-daily` (observe): apt-daily (package index refresh)

- Legacy: timer `apt-daily.timer`, 06:00 and 18:00, up to 12 h random delay
- Replaced by: `apt-daily` (os_job)
- Parity (all must be green): os_jobs covers `apt-daily` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-apt-daily`, then `homelab-maint migrate cutover os-apt-daily` (dry run), then `... --apply`.

#### `os-apt-daily-upgrade` (observe): apt-daily-upgrade (unattended security upgrades)

- Legacy: timer `apt-daily-upgrade.timer`, 06:00, up to 1 h random delay
- Replaced by: `apt-daily-upgrade` (os_job)
- Parity (all must be green): os_jobs covers `apt-daily-upgrade` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-apt-daily-upgrade`, then `homelab-maint migrate cutover os-apt-daily-upgrade` (dry run), then `... --apply`.
- Notes: Must keep running: /etc/apt/apt.conf.d/50unattended-upgrades allows only the -security and ESM origins (linux-* is not blacklisted).

#### `os-unattended-upgrades` (observe): unattended-upgrades shutdown helper

- Legacy: unit `unattended-upgrades.service`, always on (the upgrades themselves run from apt-daily-upgrade.timer)
- Replaced by: `unattended-upgrades` (os_job)
- Parity (all must be green): os_jobs covers `unattended-upgrades` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-unattended-upgrades`, then `homelab-maint migrate cutover os-unattended-upgrades` (dry run), then `... --apply`.
- Notes: os_jobs also reads /var/log/unattended-upgrades/unattended-upgrades.log for ERROR lines.

#### `os-logrotate` (observe): logrotate

- Legacy: timer `logrotate.timer`, daily 00:00
- Replaced by: `logrotate` (os_job)
- Parity (all must be green): os_jobs covers `logrotate` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-logrotate`, then `homelab-maint migrate cutover os-logrotate` (dry run), then `... --apply`.
- Notes: Replaced the root cron `logrotate /etc/logrotate.conf` line retired 2026-10-01 (see cron-root-logrotate).

#### `os-tmpfiles-clean` (observe): systemd-tmpfiles-clean

- Legacy: timer `systemd-tmpfiles-clean.timer`, 15 min after boot, then daily
- Replaced by: `systemd-tmpfiles-clean` (os_job)
- Parity (all must be green): os_jobs covers `systemd-tmpfiles-clean` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-tmpfiles-clean`, then `homelab-maint migrate cutover os-tmpfiles-clean` (dry run), then `... --apply`.
- Notes: Replaced the root cron `find /tmp -atime +7 -delete` line retired 2026-10-01; config_drift checks the 30 d age policy.

#### `os-fstrim` (observe): fstrim

- Legacy: timer `fstrim.timer`, Mon 00:00, up to 1 h 40 min random delay
- Replaced by: `fstrim` (os_job)
- Parity (all must be green): os_jobs covers `fstrim` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-fstrim`, then `homelab-maint migrate cutover os-fstrim` (dry run), then `... --apply`.

#### `os-e2scrub` (observe): e2scrub_all (ext4 online scrub)

- Legacy: timer `e2scrub_all.timer`, Sun 03:10
- Replaced by: `e2scrub_all` (os_job)
- Parity (all must be green): os_jobs covers `e2scrub_all` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-e2scrub`, then `homelab-maint migrate cutover os-e2scrub` (dry run), then `... --apply`.
- Notes: e2scrub@.service carries OnFailure=e2scrub_fail@%i.service (a package hook; left alone).

#### `os-fwupd-refresh` (observe): fwupd-refresh (firmware metadata)

- Legacy: timer `fwupd-refresh.timer`, hourly, up to 1 h random delay
- Replaced by: `fwupd-refresh` (os_job)
- Parity (all must be green): os_jobs covers `fwupd-refresh` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-fwupd-refresh`, then `homelab-maint migrate cutover os-fwupd-refresh` (dry run), then `... --apply`.

#### `os-man-db` (observe): man-db index update

- Legacy: timer `man-db.timer`, daily 00:00, up to 12 h random delay
- Replaced by: `man-db` (os_job)
- Parity (all must be green): os_jobs covers `man-db` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-man-db`, then `homelab-maint migrate cutover os-man-db` (dry run), then `... --apply`.

#### `os-sysstat-collect` (observe): sysstat-collect (sar samples)

- Legacy: timer `sysstat-collect.timer`, every 10 minutes
- Replaced by: `sysstat-collect` (os_job)
- Parity (all must be green): os_jobs covers `sysstat-collect` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-sysstat-collect`, then `homelab-maint migrate cutover os-sysstat-collect` (dry run), then `... --apply`.

#### `os-sysstat-summary` (observe): sysstat-summary

- Legacy: timer `sysstat-summary.timer`, daily 00:07
- Replaced by: `sysstat-summary` (os_job)
- Parity (all must be green): os_jobs covers `sysstat-summary` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-sysstat-summary`, then `homelab-maint migrate cutover os-sysstat-summary` (dry run), then `... --apply`.

#### `os-dpkg-db-backup` (observe): dpkg database backup

- Legacy: timer `dpkg-db-backup.timer`, daily 00:00
- Replaced by: `dpkg-db-backup` (os_job)
- Parity (all must be green): os_jobs covers `dpkg-db-backup` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-dpkg-db-backup`, then `homelab-maint migrate cutover os-dpkg-db-backup` (dry run), then `... --apply`.

#### `os-snapd-refresh` (observe): snapd automatic refresh (refresh.retain=2)

- Legacy: hook `snapd.service (internal refresh timer)`, snapd decides (about 4 times a day)
- Replaced by: `snapd-refresh` (os_job)
- Parity (all must be green): os_jobs covers `snapd-refresh` and is green
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-snapd-refresh`, then `homelab-maint migrate cutover os-snapd-refresh` (dry run), then `... --apply`.
- Notes: Old revisions are trimmed by the snap_revisions cleaner (retain=2), not by snapd. snapd.failure.service is snapd's own OnFailure hook.

#### `os-certbot` (observe): certbot renewal timer

- Legacy: timer `certbot.timer`, 00:00 and 12:00, up to 12 h random delay
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (no certificates are issued (/etc/letsencrypt/live does not exist), so the timer is a no-op)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-certbot`, then `homelab-maint migrate cutover os-certbot` (dry run), then `... --apply`.

#### `os-cron-shims` (observe): anacron and /etc/cron.{daily,weekly,monthly} package shims

- Legacy: script `/etc/cron.daily/{0anacron,apport,apt-compat,dpkg,google-chrome,logrotate,man-db,sysstat}, /etc/cron.weekly/{0anacron,man-db}, /etc/cron.monthly/0anacron, anacron.timer`, anacron.timer 07:30-23:30 hourly runs run-parts; anacrontab: daily +5 min, weekly +10 min, monthly +15 min
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (package shims: apt-compat, dpkg, logrotate, man-db and sysstat exit at once under systemd;)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-cron-shims`, then `homelab-maint migrate cutover os-cron-shims` (dry run), then `... --apply`.
- Notes: /etc/cron.daily/apport deletes crash reports older than 7 days from /var/crash. The `retention` rule `crash-dumps` in maint.toml does the same for files, so either may act first; both are safe. Leave apport's script alone (package-owned) and keep the umbrella rule in report mode unless you want the umbrella to be the only janitor.

#### `os-cron-d` (observe): /etc/cron.d package entries

- Legacy: cron `/etc/cron.d/{anacron,certbot,e2scrub_all,sysstat}`, inert under systemd (each line is guarded by `test -e /run/systemd/system ||` or an equivalent)
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (inert while systemd is the init system; nothing to verify)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-cron-d`, then `homelab-maint migrate cutover os-cron-d` (dry run), then `... --apply`.

#### `os-ubuntu-misc-timers` (observe): Ubuntu convenience timers (motd-news, ua-timer, apport-autoreport, snapd.snap-repair, update-notifier-*, systemd-sysupdate*, apt-news, esm-cache)

- Legacy: timer `motd-news.timer, ua-timer.timer, apport-autoreport.timer, snapd.snap-repair.timer, update-notifier-download.timer, update-notifier-motd.timer, systemd-sysupdate.timer and systemd-sysupdate-reboot.timer (both disabled)`, various; apport-autoreport and the update-notifier ones never fire here (their conditions are false)
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (cosmetic or condition-gated Ubuntu timers; an alert about them would be noise. Listed so n)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check os-ubuntu-misc-timers`, then `homelab-maint migrate cutover os-ubuntu-misc-timers` (dry run), then `... --apply`.

#### `user-launchpadlib-cache-clean` (observe): launchpadlib-cache-clean (user timer)

- Legacy: timer `launchpadlib-cache-clean.timer (ohmz user manager, /usr/lib/systemd/user)`, 5 min after login, then daily (user `ohmz`)
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (package timer: `find ~/.launchpadlib/api.launchpad.net/cache -type f -mtime +30 -delete`. )
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check user-launchpadlib-cache-clean`, then `homelab-maint migrate cutover user-launchpadlib-cache-clean` (dry run), then `... --apply`.

#### `user-firmware-notifier` (observe): snap firmware-updater notifier (user timer)

- Legacy: timer `snap.firmware-updater.firmware-notifier.timer (ohmz user manager)`, daily 03:00 (user `ohmz`)
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (snap-generated desktop notifier; no maintenance effect)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check user-firmware-notifier`, then `homelab-maint migrate cutover user-firmware-notifier` (dry run), then `... --apply`.

### Wave 1: Monitors and always-on daemons (no host change)

#### `kuma-monitors` (keep): Uptime Kuma monitors (13 plus the Media Stack group, feeding the Homarr uptime widget)

- Legacy: hook `container uptime-kuma :3011 (v1); monitors: Internet, Plex (paused), Radarr, Sonarr, Prowlarr, SABnzbd, Deluge, Transmission, Bazarr, Tautulli, Seerr, Kometa, Immich`, every 60 s (Internet every 900 s)
- Replaced by: `internet`, `radarr`, `sonarr`, `prowlarr`, `sabnzbd`, `deluge`, `transmission`, `bazarr`, `tautulli`, `seerr`, `ct-kometa`, `immich` (probe)
- Parity (all must be green): probe(s) `internet`, `radarr`, `sonarr`, `prowlarr`, `sabnzbd`, `deluge`, `transmission`, `bazarr`, `tautulli`, `seerr`, `ct-kometa`, `immich` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check kuma-monitors`, then `homelab-maint migrate cutover kuma-monitors` (dry run), then `... --apply`.
- Notes: Kuma stays: Homarr's uptimeStatus widget reads it. The umbrella becomes the source of truth: the probes above cover the same targets (verify with `python3 -m homelab_maint.probes kuma-diff <COPY of the Kuma sqlite>`; never point it at the live DB). Kuma has no notification channels today, so it pages nobody; the umbrella pages through notify. The Plex monitor is paused in Kuma, so the plex probe is left out of the parity set. kuma.toml push tokens (optional) let the umbrella heartbeat Kuma push monitors.

#### `platform-daemons` (keep): Platform daemons: docker, cloudflared, tailscaled, fail2ban, smartd, ollama, Plex snap

- Legacy: unit `docker.service, cloudflared.service, tailscaled.service, fail2ban.service, smartmontools.service (alias smartd.service), ollama.service, snap.plexmediaserver.plexmediaserver.service`, always on
- Replaced by: `docker-daemon`, `svc-cloudflared`, `tailscale`, `fail2ban`, `svc-smartd`, `ollama`, `svc-plex` (probe)
- Parity (all must be green): probe(s) `docker-daemon`, `svc-cloudflared`, `tailscale`, `fail2ban`, `svc-smartd`, `ollama`, `svc-plex` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check platform-daemons`, then `homelab-maint migrate cutover platform-daemons` (dry run), then `... --apply`.
- Notes: plexmediaserver.service (the apt unit) is masked (-> /dev/null); the snap unit is the live one. cloudflared-update.timer is disabled on purpose (the service runs with --no-autoupdate).

#### `umbrella-services` (keep): The umbrella's own services: homelab-maint-www (:9111), homelab-maint-live, the tick timer, the self-health timer

- Legacy: unit `homelab-maint-www.service (127.0.0.1:9111, installed), homelab-maint-live.service, homelab-maint-tick.timer and homelab-maint-selfhealth.timer (shipped by install.sh)`, always on; the tick every minute
- Replaced by: `umbrella-www`, `svc-hm-www`, `umbrella-status` (probe)
- Parity (all must be green): probe(s) `umbrella-www`, `svc-hm-www`, `umbrella-status` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check umbrella-services`, then `homelab-maint migrate cutover umbrella-services` (dry run), then `... --apply`.
- Notes: The platform itself: this tool refuses to disable any of these (a validation rule, not a convention). They are probed so a dead umbrella is seen by something other than itself: svc-hm-tick / umbrella-probes (Kuma push dead-man's switch) when the tick is installed.

#### `dashboards-and-sensors` (keep): Always-on helpers: glances, sensor-exporter, smart-bridge, thermal-log

- Legacy: unit `glances.service (127.0.0.1:61208), sensor-exporter.service (127.0.0.1:9110, fan supervisor), smart-bridge.service (127.0.0.1:7634), thermal-log.service (CSV every 30 s)`, always on
- Replaced by: `glances`, `sensor-exporter`, `svc-smart-bridge`, `svc-thermal-log` (probe)
- Parity (all must be green): probe(s) `glances`, `sensor-exporter`, `svc-smart-bridge`, `svc-thermal-log` 8 consecutive green runs over 48 h; `/home/ohmz/StudioProjects/sensor-exporter/tuning/logs/thermal.csv` newer than 0.0833333 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check dashboards-and-sensors`, then `homelab-maint migrate cutover dashboards-and-sensors` (dry run), then `... --apply`.
- Notes: sensor-exporter drives fans (hardware control): it is NEVER retired by this tool. thermal-log.service keeps a 30 s CSV with package and GPU watts that the 1-minute metrics ring does not carry; retire it only after the ring does.

#### `hermes-stack` (keep): Hermes pieces: gateway, delivery timer, flightclaw, cancel-service, internal cron ticker

- Legacy: unit `ohmz user manager: hermes-gateway.service, hermes-delivery.timer (1 min), flightclaw.service (127.0.0.1:8765), cancel-service.service (loopback, behind cloudflared); ~/.hermes/cron (one fare-watch job)`, always on; hermes-delivery every minute (user `ohmz`)
- Replaced by: `hermes-api`, `hermes-gateway`, `hermes-ticker`, `hermes-ticker-ok`, `hermes-flightclaw`, `cancel-service`, `hermes-wd-delivery` (probe)
- Parity (all must be green): probe(s) `hermes-api`, `hermes-gateway`, `hermes-ticker`, `hermes-ticker-ok`, `hermes-flightclaw`, `cancel-service`, `hermes-wd-delivery` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check hermes-stack`, then `homelab-maint migrate cutover hermes-stack` (dry run), then `... --apply`.
- Notes: Application plumbing, not maintenance: probed through its state files (~/.hermes/cron/ticker_heartbeat, ticker_last_success), never imported.

#### `app-schedulers` (keep): Application-internal schedulers: Kometa (23:00), nextcloud_cron, Plex butler and library scans, Immich/arr jobs

- Legacy: hook `containers kometa and nextcloud_cron (cron.sh); Plex (ScheduledLibraryUpdateInterval=3600, butler default window); Immich and *arr internal job queues`, owned by each application
- Replaced by: `ct-kometa`, `nextcloud` (probe)
- Parity (all must be green): probe(s) `ct-kometa`, `nextcloud` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check app-schedulers`, then `homelab-maint migrate cutover app-schedulers` (dry run), then `... --apply`.
- Notes: Out of scope on purpose: they are application logic, not host maintenance. The routine calendar shows the Plex butler window so heavy jobs avoid it.

#### `other-services` (keep): Other services found on the host: fancontrol, coolercontrold, teamviewerd, kerneloops, libvirt, nginx; user: x11vnc, gnome-remote-desktop, lucebox, qwen36-vllm

- Legacy: unit `fancontrol.service (enabled, inactive), coolercontrold.service (disabled), teamviewerd.service (active), kerneloops.service, lm-sensors.service, rsyslog.service, libvirtd.service (+ sockets), nginx.service (disabled); user manager: x11vnc.service (active), gnome-remote-desktop.service, lucebox-dflash(.proxy).service (disabled), qwen36-vllm.service (broken symlink)`, none of them is a scheduled job
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (daemons and desktop helpers that run no maintenance. Listed so the inventory is complete; )
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check other-services`, then `homelab-maint migrate cutover other-services` (dry run), then `... --apply`.

#### `boot-nvidia-tdp` (keep): nvidia-tdp (GPU power limit at boot)

- Legacy: timer `nvidia-tdp.timer -> nvidia-tdp.service (nvidia-smi -pm 1; nvidia-smi -pl 385)`, 5 s after boot
- Replaced by: nothing
- Parity (all must be green): `nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits` exits 0
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check boot-nvidia-tdp`, then `homelab-maint migrate cutover boot-nvidia-tdp` (dry run), then `... --apply`.
- Notes: Hardware setting, not maintenance. The parity check reads the live power limit (385 W).

#### `boot-nvidia-cdi-refresh` (keep): nvidia-cdi-refresh (CDI spec regenerated each boot)

- Legacy: unit `nvidia-cdi-refresh.service -> /usr/local/sbin/nvidia-cdi-refresh (writes /etc/cdi/nvidia.yaml)`, boot, before docker.service
- Replaced by: nothing
- Parity (all must be green): `systemctl is-active nvidia-cdi-refresh.service` exits 0
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check boot-nvidia-cdi-refresh`, then `homelab-maint migrate cutover boot-nvidia-cdi-refresh` (dry run), then `... --apply`.
- Notes: Boot dependency of every GPU container. Do not move the script.

#### `boot-tunarr-autostart` (keep): tunarr-autostart (docker start tunarr-host-net on boot)

- Legacy: unit `tunarr-autostart.service (oneshot, RemainAfterExit)`, boot
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (tunarr-host-net already has RestartPolicy=always, so this unit is redundant; harmless and )
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check boot-tunarr-autostart`, then `homelab-maint migrate cutover boot-tunarr-autostart` (dry run), then `... --apply`.

#### `lmstudio` (keep): lmstudio.service (headless LM Studio, port 1234)

- Legacy: unit `lmstudio.service -> /usr/local/bin/lmstudio-start.sh`, none: disabled, started by hand
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (disabled unit; not part of the routine. Listed because it was easy to miss)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check lmstudio`, then `homelab-maint migrate cutover lmstudio` (dry run), then `... --apply`.

#### `cloudflared-update` (keep): cloudflared-update.timer (disabled)

- Legacy: timer `cloudflared-update.timer`, daily, DISABLED
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (disabled on purpose; cloudflared runs with --no-autoupdate and is updated by hand)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check cloudflared-update`, then `homelab-maint migrate cutover cloudflared-update` (dry run), then `... --apply`.

### Wave 2: Notification hooks

#### `notify-route` (keep): The one notification path (notify.py over the Hermes SMS + Gmail transports)

- Legacy: hook `~ohmz/.hermes/alert_transports.env via /usr/local/sbin/backup-notify-hermes.py and backup_report_html.py (kept as the umbrella's transport and email palette)`, event driven
- Replaced by: `notify` (route)
- Parity (all must be green): a delivered test notification in the last 30 days; task `alert_path_health` 8 consecutive green runs over 48 h
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check notify-route`, then `homelab-maint migrate cutover notify-route` (dry run), then `... --apply`.
- Notes: Prove it before anything depends on it: `homelab-maint notify-test` (sends one clearly labelled TEST of each kind to your phone and inbox), then `homelab-maint migrate check notify-route`. backup-notify-hermes.py and backup_report_html.py are libraries of the umbrella now: NEVER move them (core.Notifier's bridge and notify.py's palette import them).

#### `smartd-alert-hook` (port): smartd -M exec hook (smart-alert.sh)

- Legacy: hook `/etc/smartd.conf: DEVICESCAN -a -n standby,q -W 4,45,55 -m root -M exec /usr/local/sbin/smart-alert.sh`, event driven (smartd)
- Replaced by: `smart_event` (task)
- Do not do this until: `notify-route` is cut over
- Soak after cutover: 14 days of watching it (nothing later waits for it).
- Parity (all must be green): a delivered test notification in the last 30 days; task `alert_path_health` 8 consecutive green runs over 48 h; `python3 -c ...` runs the new smart_hook entry for real on a smartd TEST event (sender replaced, log at /dev/null: nothing is sent or written) and gets exit 0; the installed cli.py knows the `smart-event` subcommand the stub calls
- Cutover does, in order:
  1. `mkdir -p /usr/local/lib/homelab-maint/legacy/smartd-alert-hook`
  2. `cp -p /usr/local/sbin/smart-alert.sh /usr/local/lib/homelab-maint/legacy/smartd-alert-hook/smart-alert.sh   # verified copy; the original stays until the stub replaces it`
  3. `write stub /usr/local/sbin/smart-alert.sh   # one atomic rename over the original, after the copy is verified`
- Rollback does, in order (from the recorded prior state):
  1. `mv /usr/local/lib/homelab-maint/legacy/smartd-alert-hook/smart-alert.sh /usr/local/sbin/smart-alert.sh   # one atomic rename over our stub`
- Commands: `homelab-maint migrate check smartd-alert-hook`, then `homelab-maint migrate cutover smartd-alert-hook` (dry run), then `... --apply`.
- Notes: smartd.conf and smartd are not touched (no reload needed): the old path becomes a stub that calls `homelab-maint smart-event`, which logs to /var/log/smart-alert.log first (alert_path_health reads it) and then goes through notify.send. The swap is atomic: the script is copied (verified) into the legacy dir and the stub replaces it in ONE rename, so smartd, which calls the hook once and does not retry, never finds the path empty; rollback is one rename the other way. After cutover test it end to end: sudo env SMARTD_DEVICE=/dev/TEST SMARTD_FAILTYPE=EmailTest SMARTD_MESSAGE='TEST ONLY' /usr/local/sbin/smart-alert.sh and expect a line in /var/log/smart-alert.log plus a labelled test email. /etc/smartmontools/run.d/10mail is the unused package default.

#### `diun-mail` (adapter): Diun image-update notifier (own SMTP mail, daily 09:00)

- Legacy: hook `container diun (crazymax/diun:4.29), DIUN_WATCH_SCHEDULE=0 9 * * *; its own SMTP settings DIUN_NOTIF_MAIL_* live in /home/ohmz/docker-container-data/diun/diun.env (env_file of /home/ohmz/docker-container-data/homelab-capture/diun.yml); database /home/ohmz/docker-container-data/diun/data`, daily 09:00 (Toronto)
- Replaced by: `routine_image_updates` (task)
- Do not do this until: `notify-route` is cut over
- Parity (all must be green): task `routine_image_updates` 1 consecutive green run; a delivered test notification in the last 30 days
- Cutover does, in order:
  1. MANUAL: comment out the DIUN_NOTIF_MAIL_* lines in /home/ohmz/docker-container-data/diun/diun.env (never print that file: it holds an SMTP password; edit it with a text editor) and recreate only that container: docker compose -f /home/ohmz/docker-container-data/homelab-capture/diun.yml up -d diun. Diun keeps checking registries and writing its database; routine_image_updates reads it and the weekly report carries the findings
- Commands: `homelab-maint migrate check diun-mail`, then `homelab-maint migrate cutover diun-mail` (dry run), then `... --apply`.
- Notes: The tool never recreates a container. Diun is the one notifier left that does NOT go through notify.py (it mails with its own SMTP credentials), so this is the last place a message can leave the host outside the one path. Until you do the manual step you get Diun's own emails AND the umbrella's weekly list: harmless duplication.

#### `stack-alert-hook` (keep): stack-alert@.service OnFailure hook (Hermes failure notifier)

- Legacy: hook `~/.config/systemd/user/stack-alert@.service; OnFailure=stack-alert@%n.service on hermes-gateway, hermes-delivery, flightclaw, cancel-service (drop-ins) and stack-backup.service`, event driven (any listed user unit fails) (user `ohmz`)
- Replaced by: nothing
- Parity (all must be green): nothing the tool can verify (the umbrella's failed_units check cannot see USER units (the runner is root), so this Herm)
- Cutover only records the adoption (no host change); the replacement keeps watching it.
- Commands: `homelab-maint migrate check stack-alert-hook`, then `homelab-maint migrate cutover stack-alert-hook` (dry run), then `... --apply`.
- Notes: Delivery is the same Hermes transport the umbrella uses (alert_transports.send_report); batches simultaneous failures into one message.

### Wave 3: Small jobs and cron lines

#### `notebook-db-alert` (port): notebook-db-alert (SurrealDB WAL / disk leading indicators)

- Legacy: timer `notebook-db-alert.timer -> /usr/local/sbin/notebook-db-alert.sh (state /var/lib/notebook-db-alert/state, log /var/log/notebook-db-alert.log)`, 10 min after boot, then every 15 min
- Replaced by: `surrealdb_health` (task)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: `notebook-db-alert.service`, job `notebook-db-alert` under the tick
- Parity (all must be green): task `surrealdb_health` 8 consecutive green runs over 48 h; config `tasks.surrealdb_health.first_sight_page` = True; the installed runner pages through notify.py (HermesNotifier); with plain core.Notifier every incident would page twice; a delivered test notification in the last 30 days
- Cutover does, in order:
  1. `systemctl disable --now notebook-db-alert.timer`
  2. `mkdir -p /usr/local/lib/homelab-maint/legacy/notebook-db-alert`
  3. `mv /usr/local/sbin/notebook-db-alert.sh /usr/local/lib/homelab-maint/legacy/notebook-db-alert/notebook-db-alert.sh`
  4. `homelab-maint job mode notebook-db-alert retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode notebook-db-alert reset   # or the previous override`
  2. `mv /usr/local/lib/homelab-maint/legacy/notebook-db-alert/notebook-db-alert.sh /usr/local/sbin/notebook-db-alert.sh`
  3. `systemctl enable --now notebook-db-alert.timer`
- Commands: `homelab-maint migrate check notebook-db-alert`, then `homelab-maint migrate cutover notebook-db-alert` (dry run), then `... --apply`.
- Notes: The port fixes the script's broken RESOLVED path (the signature never contained |BAD, so recovery was never announced); see the PARITY.md section in tasks/native.py. The old log and state file stay where they are.

#### `comfyui-idle-vram` (port): comfyui-idle-vram (restart ComfyUI when idle and holding VRAM)

- Legacy: timer `comfyui-idle-vram.timer -> /usr/local/bin/comfyui-idle-vram.sh (strike file /run/comfyui-idle-vram.strike)`, 5 min after boot, then every 5 min
- Replaced by: `comfyui_idle_reclaim` (task)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: `comfyui-idle-vram.service`, job `comfyui-idle-vram` under the tick
- Parity (all must be green): task `comfyui_idle_reclaim` 8 consecutive green runs over 48 h; `comfyui_idle_reclaim` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-check`
- Cutover does, in order:
  1. `systemctl disable --now comfyui-idle-vram.timer`
  2. `mkdir -p /usr/local/lib/homelab-maint/legacy/comfyui-idle-vram`
  3. `mv /usr/local/bin/comfyui-idle-vram.sh /usr/local/lib/homelab-maint/legacy/comfyui-idle-vram/comfyui-idle-vram.sh`
  4. `homelab-maint job mode comfyui-idle-vram retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode comfyui-idle-vram reset   # or the previous override`
  2. `mv /usr/local/lib/homelab-maint/legacy/comfyui-idle-vram/comfyui-idle-vram.sh /usr/local/bin/comfyui-idle-vram.sh`
  3. `systemctl enable --now comfyui-idle-vram.timer`
- Commands: `homelab-maint migrate check comfyui-idle-vram`, then `homelab-maint migrate cutover comfyui-idle-vram` (dry run), then `... --apply`.
- Notes: A port that can only REPORT is not a replacement, hence the task_applies parity check: it needs mode = "apply" AND something that passes --apply. `homelab-maint-check.service` and the tier-check job now carry --apply (C0 tasks stay read-only whatever the flag), so this check goes green once [tasks.comfyui_idle_reclaim] mode = "apply" is set in maint.toml. It stays red until then on purpose: the cutover would otherwise retire the 5-minute legacy timer and leave nothing that ever restarts ComfyUI. The check tier runs every 15 min, so the two-strike rule takes 30 min instead of 10; give the task a `schedule = "*/5 * * * *"` if that matters.

#### `prune-openwebui-media` (port): prune-openwebui-media (generated media older than 7 days)

- Legacy: timer `prune-openwebui-media.timer -> /usr/local/bin/prune-openwebui-media.sh (RETENTION_DAYS=7)`, daily 04:00, up to 30 min random delay
- Replaced by: `openwebui_media_prune` (task)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: `prune-openwebui-media.service`, job `prune-openwebui-media` under the tick
- Parity (all must be green): task `openwebui_media_prune` 3 consecutive green runs over 48 h; `openwebui_media_prune` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-daily`
- Cutover does, in order:
  1. `systemctl disable --now prune-openwebui-media.timer`
  2. `mkdir -p /usr/local/lib/homelab-maint/legacy/prune-openwebui-media`
  3. `mv /usr/local/bin/prune-openwebui-media.sh /usr/local/lib/homelab-maint/legacy/prune-openwebui-media/prune-openwebui-media.sh`
  4. `homelab-maint job mode prune-openwebui-media retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode prune-openwebui-media reset   # or the previous override`
  2. `mv /usr/local/lib/homelab-maint/legacy/prune-openwebui-media/prune-openwebui-media.sh /usr/local/bin/prune-openwebui-media.sh`
  3. `systemctl enable --now prune-openwebui-media.timer`
- Commands: `homelab-maint migrate check prune-openwebui-media`, then `homelab-maint migrate cutover prune-openwebui-media` (dry run), then `... --apply`.
- Notes: The allow-list name patterns (owui_*.png, owui_vid_*.webm, *_owui_vid.*, *_generated-image.png, *_generated_image*) are identical in the port.

#### `mem-guard` (retire): mem-guard (dry-run only, superseded by pressure/stuck logic)

- Legacy: timer `mem-guard.timer -> /usr/local/sbin/mem-guard.py --threshold-gib 5 --dry-run (log /var/log/mem-guard.log)`, 3 h after boot, then every 3 h
- Replaced by: `pressure_state`, `stuck_detector` (task)
- Refuses (cutover AND rollback, not overridable) while running: `mem-guard.service`, job `mem-guard` under the tick
- Parity (all must be green): task `pressure_state` 8 consecutive green runs over 48 h; task `stuck_detector` 8 consecutive green runs over 48 h
- Cutover does, in order:
  1. `systemctl disable --now mem-guard.timer`
  2. `mkdir -p /usr/local/lib/homelab-maint/legacy/mem-guard`
  3. `mv /usr/local/sbin/mem-guard.py /usr/local/lib/homelab-maint/legacy/mem-guard/mem-guard.py`
  4. `homelab-maint job mode mem-guard retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode mem-guard reset   # or the previous override`
  2. `mv /usr/local/lib/homelab-maint/legacy/mem-guard/mem-guard.py /usr/local/sbin/mem-guard.py`
  3. `systemctl enable --now mem-guard.timer`
- Commands: `homelab-maint migrate check mem-guard`, then `homelab-maint migrate cutover mem-guard` (dry run), then `... --apply`.
- Notes: It only ever logged (--dry-run); sizing alone is a bad signal, which is why pressure_state looks at PSI and stuck_detector at progress. Not ported.

#### `immich-server-recycle` (port): immich-server-recycle (docker restart immich_server every 2 h)

- Legacy: timer `immich-server-recycle.timer -> immich-server-recycle.service (ExecStart=/usr/bin/docker restart immich_server)`, 15 min after boot, then every 2 h
- Replaced by: `immich_recycle` (task)
- Do not do this until: `notify-route` is cut over
- Soak after cutover: 1 days before `immich-recycle-gate-dropin` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `immich-server-recycle.service`, job `immich-server-recycle` under the tick
- Parity (all must be green): task `immich_recycle` 8 consecutive green runs over 48 h; `immich_recycle` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-check`
- Cutover does, in order:
  1. `systemctl disable --now immich-server-recycle.timer`
  2. `homelab-maint job mode immich-server-recycle retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode immich-server-recycle reset   # or the previous override`
  2. `systemctl enable --now immich-server-recycle.timer`
- Commands: `homelab-maint migrate check immich-server-recycle`, then `homelab-maint migrate cutover immich-server-recycle` (dry run), then `... --apply`.
- Notes: No script to move: the unit runs docker directly. Same gate semantics natively (busy => defer, 12 h max). immich_recycle is a check-tier C1 task: like comfyui-idle-vram it needs --apply on the check tier (see that item) before the task_applies check goes green.

#### `immich-recycle-gate-dropin` (retire): immich-server-recycle gate drop-in (ExecCondition=homelab-maint gate immich-recycle)

- Legacy: unit `/etc/systemd/system/immich-server-recycle.service.d/10-homelab-gate.conf (installed by install.sh)`, n/a (applies whenever the service starts)
- Replaced by: `immich_recycle` (task)
- Do not do this until: `immich-server-recycle` is cut over and it has soaked 1 days
- Parity (all must be green): task `immich_recycle` 8 consecutive green runs over 48 h
- Cutover does, in order:
  1. `mkdir -p /usr/local/lib/homelab-maint/legacy/immich-recycle-gate-dropin`
  2. `mv /etc/systemd/system/immich-server-recycle.service.d/10-homelab-gate.conf /usr/local/lib/homelab-maint/legacy/immich-recycle-gate-dropin/10-homelab-gate.conf`
  3. `systemctl daemon-reload`
- Rollback does, in order (from the recorded prior state):
  1. `mv /usr/local/lib/homelab-maint/legacy/immich-recycle-gate-dropin/10-homelab-gate.conf /etc/systemd/system/immich-server-recycle.service.d/10-homelab-gate.conf`
  2. `systemctl daemon-reload`
- Commands: `homelab-maint migrate check immich-recycle-gate-dropin`, then `homelab-maint migrate cutover immich-recycle-gate-dropin` (dry run), then `... --apply`.
- Notes: install.sh must stop re-installing this drop-in once the timer is retired (see glue), otherwise the next upgrade quietly puts it back.

#### `docker-prune` (port): docker-prune (weekly image / stopped-container / build-cache prune)

- Legacy: timer `docker-prune.timer -> /usr/local/sbin/docker-prune.sh (Environment=DOCKER_CONFIG=/home/ohmz/.docker, log /var/log/docker-prune.log)`, Sun 04:00, up to 15 min random delay, Persistent
- Replaced by: `docker_cache`, `docker_images`, `docker_containers_prune` (task)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: `docker-prune.service`, job `docker-prune` under the tick
- Parity (all must be green): task `docker_prune_parity` 1 consecutive green run; `docker_cache` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-daily`; `docker_images` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-daily`; `docker_containers_prune` really applies: mode = apply and `--apply` is passed by its own schedule or by `tier-weekly`
- Cutover does, in order:
  1. `systemctl disable --now docker-prune.timer`
  2. `mkdir -p /usr/local/lib/homelab-maint/legacy/docker-prune`
  3. `mv /usr/local/sbin/docker-prune.sh /usr/local/lib/homelab-maint/legacy/docker-prune/docker-prune.sh`
  4. `homelab-maint job mode docker-prune retired`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode docker-prune reset   # or the previous override`
  2. `mv /usr/local/lib/homelab-maint/legacy/docker-prune/docker-prune.sh /usr/local/sbin/docker-prune.sh`
  3. `systemctl enable --now docker-prune.timer`
- Commands: `homelab-maint migrate check docker-prune`, then `homelab-maint migrate cutover docker-prune` (dry run), then `... --apply`.
- Notes: The classic regression: the build cache once reached 46 GB while the old job reported success, because root's docker sees only its own empty `default` builder. The natives need Environment=DOCKER_CONFIG=/home/ohmz/.docker in homelab-maint-daily.service/-weekly.service; docker_prune_parity reports 'builder discovery' as a gap until they have it, which is why it gates this cutover. Volumes are never pruned by either side.

#### `cron-bazarr-subtitle-rules` (adapter): Bazarr nightly subtitle rules (ohmz crontab)

- Legacy: cron `ohmz crontab: 30 2 * * * python3 /home/ohmz/StudioProjects/docker-bazarr/bazarr_subtitle_rules.py >> .../bazarr_subtitle_rules.log # bazarr-nightly-subtitle-rule`, daily 02:30
- Replaced by: `bazarr-rules` (job)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: job `bazarr-rules` under the tick
- Parity (all must be green): scheduler lists job `bazarr-rules`; `/home/ohmz/StudioProjects/docker-bazarr/data/bazarr/config/log/bazarr_subtitle_rules.log` newer than 26 h; a delivered test notification in the last 30 days; `scheduler validate` is clean for `bazarr-rules`
- Cutover does, in order:
  1. `crontab -u ohmz -   # the line tagged/matching 'bazarr-nightly-subtitle-rule' gets the prefix #HM-RETIRED[cron-bazarr-subtitle-rules] ; all other lines untouched`
  2. `homelab-maint job mode bazarr-rules managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode bazarr-rules reset   # or the previous override`
  2. `crontab -u ohmz -   # strip that prefix again`
- After cutover: start the first run attended, `homelab-maint job run bazarr-rules`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check cron-bazarr-subtitle-rules`, then `homelab-maint migrate cutover cron-bazarr-subtitle-rules` (dry run), then `... --apply`.
- Notes: The script stays in place and becomes the job command (user ohmz). Its own log file stops growing: the job log lives under /var/log/homelab-maint/jobs/bazarr-rules/. The script holds /tmp/bazarr_subtitle_rules.lock itself, so a manual run never overlaps.

#### `cron-tunarr-weekly-sync` (adapter): Tunarr weekly channel sync (ohmz crontab)

- Legacy: cron `ohmz crontab: 15 4 * * 1 /home/ohmz/StudioProjects/tunarr-sync/run.sh # tunarr-weekly-channel-sync`, Mon 04:15
- Replaced by: `tunarr-sync` (job)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: job `tunarr-sync` under the tick
- Parity (all must be green): scheduler lists job `tunarr-sync`; `/home/ohmz/StudioProjects/tunarr-sync/logs` newer than 216 h; a delivered test notification in the last 30 days; `scheduler validate` is clean for `tunarr-sync`
- Cutover does, in order:
  1. `crontab -u ohmz -   # the line tagged/matching 'tunarr-weekly-channel-sync' gets the prefix #HM-RETIRED[cron-tunarr-weekly-sync] ; all other lines untouched`
  2. `homelab-maint job mode tunarr-sync managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode tunarr-sync reset   # or the previous override`
  2. `crontab -u ohmz -   # strip that prefix again`
- After cutover: start the first run attended, `homelab-maint job run tunarr-sync`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check cron-tunarr-weekly-sync`, then `homelab-maint migrate cutover cron-tunarr-weekly-sync` (dry run), then `... --apply`.
- Notes: run.sh keeps writing logs/sync-DATE.log and prunes to the last 12; the job adds exit-code alerting. The 04:15 slot sits after Kometa's nightly refresh and inside the Plex butler window: the scheduler treats it as heavy.

#### `purge-public-guests` (adapter): purge_public_guests.py (reap guest accounts on the public Open WebUI) - currently UNSCHEDULED

- Legacy: script `/home/ohmz/StudioProjects/ai-stack/scripts/purge_public_guests.py (README calls it load-bearing; no timer, no cron line and no Hermes cron job runs it)`, none today: run by hand
- Replaced by: `purge-public-guests` (job)
- Do not do this until: `notify-route` is cut over
- Refuses (cutover AND rollback, not overridable) while running: job `purge-public-guests` under the tick
- Parity (all must be green): scheduler lists job `purge-public-guests`; `runuser -u ohmz -- /usr/bin/python3 /home/ohmz/StudioProjects/ai-stack/scripts/purge_public_guests.py` exits 0; `scheduler validate` is clean for `purge-public-guests`
- Cutover does, in order:
  1. `homelab-maint job mode purge-public-guests managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode purge-public-guests reset   # or the previous override`
- After cutover: start the first run attended, `homelab-maint job run purge-public-guests`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check purge-public-guests`, then `homelab-maint migrate cutover purge-public-guests` (dry run), then `... --apply`.
- Notes: A gap, not a retirement: the public instance mints a user row per visit and nothing else prunes them. There is no legacy driver, so there is nothing to disable: jobs.toml ships the job in observe mode (it deletes accounts, so it is adopted deliberately) with no `retire` list, and this cutover only flips it to managed. The parity command is the script's own DRY RUN (its default: it signs in through the loopback admin door and lists accounts; it deletes nothing). Run that once by hand and read the list before the cutover.

#### `cron-root-journal-vacuum` (retire): root cron: journalctl --vacuum-time=7d (retired 2026-10-01)

- Legacy: cron `root crontab (commented): 30 5 * * 0 /usr/bin/journalctl --vacuum-time=7d`, was Sun 05:30
- Replaced by: `config_drift` (task)
- Already retired by hand on 2026-10-01; recorded for completeness, no command to run.
- Parity (all must be green): task `config_drift` 1 consecutive green run
- Cutover does, in order:
  1. `crontab -u root -   # the line tagged/matching 'journalctl --vacuum-time=7d' gets the prefix #HM-RETIRED[cron-root-journal-vacuum] ; all other lines untouched`
- Rollback does, in order (from the recorded prior state):
  1. `crontab -u root -   # strip that prefix again`
- Commands: `homelab-maint migrate check cron-root-journal-vacuum`, then `homelab-maint migrate cutover cron-root-journal-vacuum` (dry run), then `... --apply`.
- Notes: Replaced by /etc/systemd/system/journald.conf.d/10-homelab.conf (SystemMaxUse=1G, MaxRetentionSec=1month); config_drift watches it.

#### `cron-root-find-tmp` (retire): root cron: find /tmp -atime +7 -delete (retired 2026-10-01)

- Legacy: cron `root crontab (commented): 0 6 * * 0 /usr/bin/find /tmp -type f -atime +7 -delete`, was Sun 06:00
- Replaced by: `os_jobs` (task)
- Already retired by hand on 2026-10-01; recorded for completeness, no command to run.
- Cutover does, in order:
  1. `crontab -u root -   # the line tagged/matching 'find /tmp -type f -atime +7' gets the prefix #HM-RETIRED[cron-root-find-tmp] ; all other lines untouched`
- Rollback does, in order (from the recorded prior state):
  1. `crontab -u root -   # strip that prefix again`
- Commands: `homelab-maint migrate check cron-root-find-tmp`, then `homelab-maint migrate cutover cron-root-find-tmp` (dry run), then `... --apply`.
- Notes: Replaced by systemd-tmpfiles-clean (30 d); see os-tmpfiles-clean.

#### `cron-root-logrotate` (retire): root cron: logrotate /etc/logrotate.conf (retired 2026-10-01)

- Legacy: cron `root crontab (commented): 30 6 * * 0 /usr/sbin/logrotate /etc/logrotate.conf`, was Sun 06:30
- Replaced by: `os_jobs` (task)
- Already retired by hand on 2026-10-01; recorded for completeness, no command to run.
- Cutover does, in order:
  1. `crontab -u root -   # the line tagged/matching '/usr/sbin/logrotate /etc/logrotate.conf' gets the prefix #HM-RETIRED[cron-root-logrotate] ; all other lines untouched`
- Rollback does, in order (from the recorded prior state):
  1. `crontab -u root -   # strip that prefix again`
- Commands: `homelab-maint migrate check cron-root-logrotate`, then `homelab-maint migrate cutover cron-root-logrotate` (dry run), then `... --apply`.
- Notes: Replaced by logrotate.timer; see os-logrotate.

#### `cron-root-old-disabled` (retire): root cron: three older jobs the owner had already commented out

- Legacy: cron `root crontab (commented): run_cursor_agent_nightly.sh (07:00 nightly), run_rotten-tomatoes.sh (Wed 03:00), run_kometa.sh (Wed 03:30)`, disabled before the migration
- Replaced by: `ct-kometa` (probe)
- Already retired by hand on 2026-10-01; recorded for completeness, no command to run.
- Cutover does, in order:
  1. `crontab -u root -   # the line tagged/matching 'run_cursor_agent_nightly.sh' gets the prefix #HM-RETIRED[cron-root-old-disabled] ; all other lines untouched`
  2. `crontab -u root -   # the line tagged/matching 'run_rotten-tomatoes.sh' gets the prefix #HM-RETIRED[cron-root-old-disabled] ; all other lines untouched`
  3. `crontab -u root -   # the line tagged/matching 'run_kometa.sh' gets the prefix #HM-RETIRED[cron-root-old-disabled] ; all other lines untouched`
- Rollback does, in order (from the recorded prior state):
  1. `crontab -u root -   # strip that prefix again`
  2. `crontab -u root -   # strip that prefix again`
  3. `crontab -u root -   # strip that prefix again`
- Commands: `homelab-maint migrate check cron-root-old-disabled`, then `homelab-maint migrate cutover cron-root-old-disabled` (dry run), then `... --apply`.
- Notes: Commented out by the owner (date not recorded; first seen commented 2026-10-01). Kometa now runs inside its own container.

### Wave 4: The tick takes over: Hermes user jobs, the stack backup, the daily/weekly/metrics runner timers (the check timer stays)

#### `tier-check` (keep): homelab-maint check tier: stays on its own systemd timer, deliberately (the tick's watchdog)

- Legacy: timer `homelab-maint-check.timer -> homelab-maint-check.service (homelab-maint run --tier check --apply, every 15 min)`, every 15 min, up to 60 s random delay
- Replaced by: nothing
- Parity (all must be green): the scheduler tick ran in the last 5 minutes; `scheduler validate` is clean for every job; `systemctl is-enabled homelab-maint-check.timer` exits 0; probe(s) `umbrella-tick` 8 consecutive green runs over 48 h; a delivered test notification in the last 30 days
- Cutover only records the adoption (no host change); the replacement keeps watching it. The job `tier-check` stays in observe mode on purpose.
- Commands: `homelab-maint migrate check tier-check`, then `homelab-maint migrate cutover tier-check` (dry run), then `... --apply`.
- Notes: NEVER retired (validation refuses to disable homelab-maint-check.timer). After wave 4 the stack watchdog, the stack backup, the daily/weekly runners and both system backups all start from ONE process, the tick, and its probes-run job lives there too. The check tier is the only runner that does not: its `probes` task folds the engine in every 15 minutes and judges the probe umbrella-tick (tick.json older than 5 minutes = crit, paged through core.Notifier), and backup_freshness, os_jobs and alert_path_health run there. That is what notices a dead tick. Kuma's umbrella-probes push is only a second view: Kuma has ZERO notification channels, so it pages nobody. The jobs.toml job `tier-check` therefore stays in observe mode for good (its legacy driver, this timer, stays the driver); the cutover of this item only RECORDS the adoption once the tick is proven beating and jobs.toml validates, and everything the tick takes over depends on it. A jobs.toml typo is the other way the tick silences itself: the tick still beats, but schedules nothing. `scheduler validate` is therefore a parity check of every adapter that hands a job over and of this item. (Not done by this tool, a request to the scheduler: report crit and make the tick service exit non-zero when it loaded no jobs because of config problems, and keep a last-known-good jobs.toml.)

#### `search-canary` (adapter): search-canary (SearXNG canary, two instances)

- Legacy: timer `ohmz user timer search-canary.timer -> ~/ai-stack/scripts/search_canary.py (state ~/.hermes/search_canary_state.json)`, 10 min after boot, then every 30 min, up to 3 min random delay (user `ohmz`)
- Replaced by: `search-canary` (job)
- Do not do this until: `notify-route` is cut over, `tier-check` is cut over
- Soak after cutover: 3 days before `stack-watchdog` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `search-canary.service`, job `search-canary` under the tick
- Parity (all must be green): scheduler lists job `search-canary`; jobs.toml job `search-canary` launches exactly what `search-canary.service` launches (command, user, nice, ionice, timeout, mounts, environment names); `/home/ohmz/.hermes/search_canary_state.json` newer than 2 h; a delivered test notification in the last 30 days; `scheduler validate` is clean for `search-canary`
- Cutover does, in order:
  1. `systemctl --user disable --now search-canary.timer   # as ohmz`
  2. `homelab-maint job mode search-canary managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode search-canary reset   # or the previous override`
  2. `systemctl --user enable --now search-canary.timer   # as ohmz`
- After cutover: start the first run attended, `homelab-maint job run search-canary`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check search-canary`, then `homelab-maint migrate cutover search-canary` (dry run), then `... --apply`.
- Notes: The script keeps its own health_alert confirm/recover engine and its own Hermes alert (self_notifies = true until you decide otherwise); the umbrella adds scheduling, a timeout (TimeoutStartSec=300 in the old unit), run history and an exit-code alert. Keep the 30 minute cadence: SearXNG suspends engines that are queried harder, so a chattier canary causes the outage it exists to detect.

#### `stack-watchdog` (adapter): stack-watchdog (gateway, API, delivery timer, backup freshness, ticker, ...)

- Legacy: timer `ohmz user timer stack-watchdog.timer -> ~/ai-stack/scripts/stack_watchdog.py + health_alert.py (state ~/.hermes/watchdog_state.json)`, 3 min after boot, then every 5 min (user `ohmz`)
- Replaced by: `stack-watchdog` (job)
- Do not do this until: `notify-route` is cut over, `search-canary` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 3 days (counted from its first green run)
- Soak after cutover: 7 days before `stack-backup`, `tier-daily`, `metrics-sample` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `stack-watchdog.service`, job `stack-watchdog` under the tick
- Parity (all must be green): scheduler lists job `stack-watchdog`; jobs.toml job `stack-watchdog` launches exactly what `stack-watchdog.service` launches (command, user, nice, ionice, timeout, mounts, environment names); `/home/ohmz/.hermes/watchdog_state.json` newer than 0.25 h; a delivered test notification in the last 30 days; `scheduler validate` is clean for `stack-watchdog`
- Cutover does, in order:
  1. `systemctl --user disable --now stack-watchdog.timer   # as ohmz`
  2. `homelab-maint job mode stack-watchdog managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode stack-watchdog reset   # or the previous override`
  2. `systemctl --user enable --now stack-watchdog.timer   # as ohmz`
- After cutover: start the first run attended, `homelab-maint job run stack-watchdog`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check stack-watchdog`, then `homelab-maint migrate cutover stack-watchdog` (dry run), then `... --apply`.
- Notes: This is the alarm covering the alarm (the Hermes pipe). It keeps its own 2-run confirm, reminder and KILLED logic; do not rewrite it. If the umbrella's tick stops, this stops too. What notices that is NOT the tick: the check tier stays on its own systemd timer (homelab-maint-check.timer, item tier-check, never retired), and its `probes` task judges the probe umbrella-tick (tick.json older than 5 minutes = crit) and pages through core.Notifier. Its Kuma push (umbrella-probes) is only a second, external view: Kuma has no notification channel, so it pages nobody.

#### `stack-backup` (adapter): stack-backup (nightly ai-stack backup to the SandiskSSD)

- Legacy: timer `ohmz user timer stack-backup.timer -> ~/ai-stack/scripts/stack_backup.sh (OnFailure=stack-alert@%n; LAST_OK /media/SandiskSSD/ai-stack-backups/LAST_OK)`, daily 03:30, up to 5 min random delay, Persistent (user `ohmz`)
- Replaced by: `stack-backup` (job)
- Do not do this until: `stack-watchdog` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 7 days (counted from its first green run)
- Soak after cutover: 7 days before `backup-system` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `stack-backup.service`, job `stack-backup` under the tick
- Parity (all must be green): scheduler lists job `stack-backup`; jobs.toml job `stack-backup` launches exactly what `stack-backup.service` launches (command, user, nice, ionice, timeout, mounts, environment names); job `stack-backup` has pausable = False in jobs.toml; `/media/SandiskSSD/ai-stack-backups/LAST_OK` newer than 26 h; a delivered test notification in the last 30 days; `scheduler validate` is clean for `stack-backup`
- Cutover does, in order:
  1. `systemctl --user disable --now stack-backup.timer   # as ohmz`
  2. `homelab-maint job mode stack-backup managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode stack-backup reset   # or the previous override`
  2. `systemctl --user enable --now stack-backup.timer   # as ohmz`
- After cutover: start the first run attended, `homelab-maint job run stack-backup`. Whatever depends on this item waits for 2 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check stack-backup`, then `homelab-maint migrate cutover stack-backup` (dry run), then `... --apply`.
- Notes: Cutover only when LAST_OK is fresh (< 26 h) and no backup is running (refused otherwise). The job needs TimeoutStartSec=30min, Nice=10 and idle IO like the old unit. The unit's OnFailure=stack-alert@ line stays (harmless; it only fires for manual `systemctl --user start`). The stack-watchdog 'backup' check keeps reading LAST_OK, so it keeps alarming if the umbrella job ever stops.

#### `tier-daily` (adapter): homelab-maint daily tier (runner timer, replaced by the tick)

- Legacy: timer `homelab-maint-daily.timer -> homelab-maint-daily.service (homelab-maint run --tier daily --apply)`, daily 07:30, up to 20 min random delay, Persistent
- Replaced by: `tier-daily` (job)
- Do not do this until: `stack-watchdog` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 7 days (counted from its first green run)
- Soak after cutover: 7 days before `tier-weekly`, `backup-system` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `homelab-maint-daily.service`, job `tier-daily` under the tick
- Parity (all must be green): the scheduler tick ran in the last 5 minutes; scheduler lists job `tier-daily`; `scheduler validate` is clean for `tier-daily`
- Cutover does, in order:
  1. `systemctl disable --now homelab-maint-daily.timer`
  2. `homelab-maint job mode tier-daily managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode tier-daily reset   # or the previous override`
  2. `systemctl enable --now homelab-maint-daily.timer`
- After cutover: start the first run attended, `homelab-maint job run tier-daily`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check tier-daily`, then `homelab-maint migrate cutover tier-daily` (dry run), then `... --apply`.
- Notes: The routine engine still decides WHAT the daily tier may do inside its window; only WHO starts it changes. Wait for the next 07:30 and read the daily digest.

#### `tier-weekly` (adapter): homelab-maint weekly tier (runner timer, replaced by the tick)

- Legacy: timer `homelab-maint-weekly.timer -> homelab-maint-weekly.service (homelab-maint run --tier weekly --apply)`, Wed 07:45, Persistent
- Replaced by: `tier-weekly` (job)
- Do not do this until: `tier-daily` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 7 days (counted from its first green run)
- Refuses (cutover AND rollback, not overridable) while running: `homelab-maint-weekly.service`, job `tier-weekly` under the tick
- Parity (all must be green): the scheduler tick ran in the last 5 minutes; scheduler lists job `tier-weekly`; `scheduler validate` is clean for `tier-weekly`
- Cutover does, in order:
  1. `systemctl disable --now homelab-maint-weekly.timer`
  2. `homelab-maint job mode tier-weekly managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode tier-weekly reset   # or the previous override`
  2. `systemctl enable --now homelab-maint-weekly.timer`
- After cutover: start the first run attended, `homelab-maint job run tier-weekly`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check tier-weekly`, then `homelab-maint migrate cutover tier-weekly` (dry run), then `... --apply`.
- Notes: The job waits for the daily tier (after = tier-daily), like the old After=homelab-maint-daily.service.

#### `metrics-sample` (adapter): 7-day metrics ring sampler (runner timer, replaced by the tick)

- Legacy: timer `homelab-maint-metrics.timer -> homelab-maint-metrics.service (python3 -m homelab_maint.metrics_ring sample)`, every minute (OnUnitActiveSec=1min)
- Replaced by: `metrics-sample` (job)
- Do not do this until: `stack-watchdog` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 7 days (counted from its first green run)
- Refuses (cutover AND rollback, not overridable) while running: `homelab-maint-metrics.service`, job `metrics-sample` under the tick
- Parity (all must be green): the scheduler tick ran in the last 5 minutes; scheduler lists job `metrics-sample`; `/var/lib/homelab-maint/metrics-ring.json` newer than 0.0833333 h; `scheduler validate` is clean for `metrics-sample`
- Cutover does, in order:
  1. `systemctl disable --now homelab-maint-metrics.timer`
  2. `homelab-maint job mode metrics-sample managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode metrics-sample reset   # or the previous override`
  2. `systemctl enable --now homelab-maint-metrics.timer`
- After cutover: start the first run attended, `homelab-maint job run metrics-sample`. Whatever depends on this item waits for 1 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check metrics-sample`, then `homelab-maint migrate cutover metrics-sample` (dry run), then `... --apply`.
- Notes: A one-minute job: the tick costs tens of ms when nothing is due, and a missed minute is simply skipped. Watch the ring file stay fresh.

### Wave 5: System backups (last: the crown jewels)

#### `backup-system` (adapter): backup-system (weekly system-drive backup to BK-SYSTEM)

- Legacy: timer `backup-system.timer -> /usr/local/sbin/backup-system.sh (sources backup-common.sh; status /var/log/backup/system-status.json; OnFailure=backup-failure@%n)`, Sat 01:00, up to 15 min random delay, Persistent
- Replaced by: `backup-system` (job)
- Do not do this until: `stack-backup` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 7 days (counted from its first green run), `tier-daily` is cut over, the tick has recorded 1 green run(s) of its job and it has soaked 7 days (counted from its first green run), `notify-route` is cut over
- Soak after cutover: 14 days before `backup-immich`, `backup-notify-wrapper`, `backup-failure-hook` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `backup-system.service`, job `backup-system` under the tick, any held `/run/lock/backup-*.lock`
- Parity (all must be green): scheduler lists job `backup-system`; jobs.toml job `backup-system` launches exactly what `backup-system.service` launches (command, user, nice, ionice, timeout, mounts, environment names); job `backup-system` has pausable = False in jobs.toml; `/var/log/backup/system-status.json` result is ok (the LEGACY driver's last run: a baseline, not proof about the umbrella); a delivered test notification in the last 30 days; `scheduler validate` is clean for `backup-system`
- Cutover does, in order:
  1. `systemctl disable --now backup-system.timer`
  2. `homelab-maint job mode backup-system managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode backup-system reset   # or the previous override`
  2. `systemctl enable --now backup-system.timer`
- After cutover: start the first run attended, `homelab-maint job run backup-system`. Whatever depends on this item waits for 2 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check backup-system`, then `homelab-maint migrate cutover backup-system` (dry run), then `... --apply`.
- Notes: The script, backup-common.sh and the status JSON are unchanged; only WHO starts it changes (the umbrella: gated, serialised with the other backup, timeout infinity, Nice=10 best-effort IO 7, as in the old unit). Cut over on a Sunday-to-Friday, never while a backup runs (refused: the unit, the tick's own run of the job, or a held /run/lock/backup-*.lock; --force cannot override that). The job is marked self_notifies = true: you keep getting the script's own report through backup-notify.sh until the wave 6 item backup-notify-wrapper turns that off. REQUIRED right after the cutover: start the first run attended, `homelab-maint job run backup-system`, and compare its status JSON with the previous one. Nothing that depends on this item counts until the tick has recorded 2 green runs of the job, and the 14 day soak starts at the first of them: calendar time with a backup the tick never launched (a pressure deferral, a bad mount, a jobs.toml the tick ignores) proves nothing, and is how the weekly backup could stop with nothing but the next backup_freshness warning to say so.

#### `backup-immich` (adapter): backup-immich (weekly Immich library backup to BK-IMMICH)

- Legacy: timer `backup-immich.timer -> /usr/local/sbin/backup-immich.sh (status /var/log/backup/immich-status.json; OnFailure=backup-failure@%n)`, Sun 01:00, up to 15 min random delay, Persistent
- Replaced by: `backup-immich` (job)
- Do not do this until: `backup-system` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run)
- Soak after cutover: 14 days before `backup-notify-wrapper`, `backup-failure-hook` may follow.
- Refuses (cutover AND rollback, not overridable) while running: `backup-immich.service`, job `backup-immich` under the tick, any held `/run/lock/backup-*.lock`
- Parity (all must be green): scheduler lists job `backup-immich`; jobs.toml job `backup-immich` launches exactly what `backup-immich.service` launches (command, user, nice, ionice, timeout, mounts, environment names); job `backup-immich` has pausable = False in jobs.toml; `/var/log/backup/immich-status.json` result is ok (the LEGACY driver's last run: a baseline, not proof about the umbrella); a delivered test notification in the last 30 days; `scheduler validate` is clean for `backup-immich`
- Cutover does, in order:
  1. `systemctl disable --now backup-immich.timer`
  2. `homelab-maint job mode backup-immich managed`
- Rollback does, in order (from the recorded prior state):
  1. `homelab-maint job mode backup-immich reset   # or the previous override`
  2. `systemctl enable --now backup-immich.timer`
- After cutover: start the first run attended, `homelab-maint job run backup-immich`. Whatever depends on this item waits for 2 green run(s) recorded by the tick, and its soak counts from the first of them.
- Commands: `homelab-maint migrate check backup-immich`, then `homelab-maint migrate cutover backup-immich` (dry run), then `... --apply`.
- Notes: Same mechanics as backup-system (including the attended first run: `homelab-maint job run backup-immich`); starts only after the tick has recorded 2 green backup-system runs and 14 days have passed since the first, so a regression is isolated to one disk.

### Wave 6: Backup wrappers (only after both backups are cut over and soaked)

#### `backup-notify-wrapper` (retire): backup-notify.sh (per-backup SMS + email through the Hermes bridge)

- Legacy: script `/usr/local/sbin/backup-notify.sh (0750; config /etc/backup/notify.conf 0600; log /var/log/backup/notify.log); called by backup-common.sh and backup-failed.sh`, event driven (end of each backup run)
- Replaced by: `notify` (route)
- Do not do this until: `backup-system` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run), `backup-immich` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run)
- Refuses (cutover AND rollback, not overridable) while running: `backup-system.service`, `backup-immich.service`, job `backup-system` under the tick, job `backup-immich` under the tick, any held `/run/lock/backup-*.lock`
- Parity (all must be green): `scheduler validate` is clean for `backup-system`, `backup-immich`; job `backup-system` has self_notifies = False in jobs.toml; job `backup-immich` has self_notifies = False in jobs.toml; job `backup-system` has notify.on_failure = 'alert' in jobs.toml; job `backup-immich` has notify.on_failure = 'alert' in jobs.toml; a delivered test notification in the last 14 days; task `backup_freshness` 3 consecutive green runs over 48 h
- Cutover does, in order:
  1. `mkdir -p /usr/local/lib/homelab-maint/legacy/backup-notify-wrapper`
  2. `mv /usr/local/sbin/backup-notify.sh /usr/local/lib/homelab-maint/legacy/backup-notify-wrapper/backup-notify.sh`
- Rollback does, in order (from the recorded prior state):
  1. `mv /usr/local/lib/homelab-maint/legacy/backup-notify-wrapper/backup-notify.sh /usr/local/sbin/backup-notify.sh`
- Commands: `homelab-maint migrate check backup-notify-wrapper`, then `homelab-maint migrate cutover backup-notify-wrapper` (dry run), then `... --apply`.
- Notes: THE interlock of the whole migration: while a job has self_notifies = true the umbrella stays silent about ordinary failures (the script alerts for itself). Moving backup-notify.sh without flipping that flag would make a failed backup SILENT. So the parity checks above read the EFFECTIVE jobs.toml: set `self_notifies = false` for backup-system and backup-immich in /etc/homelab-maint/jobs.toml first, then run `homelab-maint scheduler validate` (a typo in that file makes the tick schedule NOTHING; the scheduler_validate check refuses the cutover). After cutover backup-common.sh finds no executable backup-notify.sh and skips it quietly; the umbrella job tells you instead (email after each backup, text only for a failure). You stop getting the 'backup OK' text. Never move backup-notify-hermes.py or backup_report_html.py: notify.py imports them. /etc/backup/notify.conf is left in place.

#### `backup-failure-hook` (retire): backup-failure@.service OnFailure hook (backup-failed.sh)

- Legacy: hook `/etc/systemd/system/backup-failure@.service -> /usr/local/sbin/backup-failed.sh (writes /var/log/backup/FAILED-<unit>.txt, then backup-notify.sh); also run by the jobs' hooks.on_failure`, event driven (a backup unit fails; or a backup job fails under the tick)
- Replaced by: `backup-system`, `backup-immich` (job)
- Do not do this until: `backup-notify-wrapper` is cut over, `backup-system` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run), `backup-immich` is cut over, the tick has recorded 2 green run(s) of its job and it has soaked 14 days (counted from its first green run)
- Refuses (cutover AND rollback, not overridable) while running: `backup-system.service`, `backup-immich.service`, job `backup-system` under the tick, job `backup-immich` under the tick, any held `/run/lock/backup-*.lock`
- Parity (all must be green): `scheduler validate` is clean for `backup-system`, `backup-immich`; job `backup-system` has self_notifies = False in jobs.toml; job `backup-immich` has self_notifies = False in jobs.toml; task `backup_freshness` 3 consecutive green runs over 48 h
- Cutover does, in order:
  1. `mkdir -p /usr/local/lib/homelab-maint/legacy/backup-failure-hook`
  2. `cp -p /usr/local/sbin/backup-failed.sh /usr/local/lib/homelab-maint/legacy/backup-failure-hook/backup-failed.sh   # verified copy; the original stays until the stub replaces it`
  3. `write stub /usr/local/sbin/backup-failed.sh   # one atomic rename over the original, after the copy is verified`
- Rollback does, in order (from the recorded prior state):
  1. `mv /usr/local/lib/homelab-maint/legacy/backup-failure-hook/backup-failed.sh /usr/local/sbin/backup-failed.sh   # one atomic rename over our stub`
- Commands: `homelab-maint migrate check backup-failure-hook`, then `homelab-maint migrate cutover backup-failure-hook` (dry run), then `... --apply`.
- Notes: The stub keeps `hooks.on_failure = ["/usr/local/sbin/backup-failed.sh", ...]` in jobs.toml valid; remove those two lines at leisure. The OnFailure= lines in backup-system.service and backup-immich.service and the backup-failure@.service template stay (inert). The old FAILED-*.txt marker files are never read by backup_freshness (they persist forever).

<!-- END GENERATED RUNBOOK -->
