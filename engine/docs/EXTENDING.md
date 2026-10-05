# Extending homelab-maint

homelab-maint is the one place that schedules, runs, monitors and reports maintenance on this host. Adding something means
adding a table to a TOML file or one small Python file. It never means a new systemd timer, a new cron line or a new script that
sends its own mail. This page shows each extension point with a worked example, how to test it, and the checklist to read before
you let anything change the machine.

Every `homelab-maint X` command below can also be run without the entry script, from the installed tree, as
`sudo PYTHONPATH=/usr/local/lib/homelab-maint python3 -m homelab_maint.MODULE ...` (the module is named where it matters).
That form works in a checkout too: `cd /home/ohmz/homelab-maint && python3 -m homelab_maint.probes validate`.

## 1. Which extension point?

| You want to...                                              | Add                                       | File                                    | Who starts it                         |
|-------------------------------------------------------------|-------------------------------------------|-----------------------------------------|---------------------------------------|
| watch something and be told when it is wrong                 | a check (C0 task)                         | `homelab_maint/tasks/NAME.py` or a plugin | the tier job (`check` every 15 min)   |
| clean something up                                          | a cleaner (C1 task)                       | same                                    | the daily/weekly tier, per the routine |
| clean something risky, but only after you looked            | a plan task (C2)                          | same                                    | weekly tier makes the plan, you approve |
| run a script or command on a schedule                       | a job                                     | `jobs.toml` `[[job]]`                   | the one-minute tick                   |
| know whether a service, port, container or file is alive    | a probe                                   | `probes.toml` `[[probe]]`               | the probe plane (check tier)          |
| change who is told what, when, and how often                | a route                                   | `notify.toml`                           | every message goes through `notify.py` |
| put a task into the daily/weekly/monthly routine            | a routine step                            | `routine.toml` `steps = [...]`          | the routine engine, inside its window |
| tell yourself what to do when it breaks                     | a playbook                                | `playbooks.toml` `[playbook.NAME]`      | shown in alerts and on the Incidents tab |
| decide what gets sacrificed first under memory pressure     | a service class                           | `classes.toml`                          | the spike manager                     |
| show it on the Homarr board or the website                  | a payload and a widget / a public file    | `payloads.py`, `widgets/`, `publish.py` | the publisher after every check run   |
| retire something this replaces                              | an item                                   | `legacy-retirement.toml`                | you, with `homelab-maint migrate`     |

Rule of thumb: if it is a command that already exists and works, make it a **job**. If it answers "is it up", make it a **probe**.
If you need judgement over numbers (thresholds, trends, history), write a **task**.

## 2. Where things live

| What                | Source tree (`/home/ohmz/homelab-maint`) | Installed / live                                         |
|---------------------|-------------------------------------------|-----------------------------------------------------------|
| code                | `homelab_maint/`                          | `/usr/local/lib/homelab-maint/homelab_maint` (root:root)   |
| entry script        | `homelab-maint`                           | `/usr/local/sbin/homelab-maint`                           |
| config              | `etc/*.toml`                              | `/etc/homelab-maint/` (copied only if absent; a changed shipped default lands next to yours as `NAME.dist`) |
| rules registry      | `etc/rules.d/`                            | `/etc/homelab-maint/rules.d/*.toml` (root-owned; see the note below) |
| owner plugins       | -                                         | `/etc/homelab-maint/plugins.d/*.py` (root-owned)           |
| extra probes        | -                                         | `/etc/homelab-maint/probes.d/*.toml`                       |
| state               | -                                         | `/var/lib/homelab-maint` (`status.json`, `history.jsonl`, `sched.json`, `migration.json`, `public/`) |
| logs and audit      | -                                         | `/var/log/homelab-maint` (`audit.jsonl` has every mutation attempt, `jobs/<job>/<run>.log` has job output) |
| kill switches       | -                                         | `/etc/homelab-maint/PAUSE`, `PAUSE.<task or job>`, `FREEZE`, `NOTIFY_MUTE` |

After editing source, `sudo ./install.sh --dry-run` shows what would change, `sudo ./install.sh` installs it. Config edits under
`/etc/homelab-maint` take effect on the next run; nothing needs a restart.

**Once the rules registry is adopted** (README, "The rules registry"), `maint.toml`, `routine.toml`, `jobs.toml`, `probes.toml`,
`classes.toml`, `notify.toml`, `ack.toml` and `protected.toml` are GENERATED from `rules.d`: every extension point below is then a
`[[rule]]` in `rules.d/NN-category.toml` (an `id`, `why`, `does`, and the `file`, `target` and `params` that feed the same tables the
examples show), checked with `homelab-maint rules check` and applied by the tick within a minute. The examples keep showing the
generated tables because that is what the runner reads. Before adoption (and for `playbooks.toml`, `legacy-retirement.toml` and the
`*.d` directories, which are never generated) you edit the files directly. A cleaner's paths, `unprotect` and `mode = "apply"` belong
to a rule flagged `destructive = true`; globs are relative; every `unprotect` regex must be on the baseline allow-list in
`00-baseline-invariants.toml` (pinned in the code), or be accepted in `99-owner-overrides.toml`.

## 3. Add a check (C0)

A check is read-only by construction: the runner forces `apply` off for class C0, so it cannot change the host even by mistake.

Scaffold it, then edit. The generator writes two files, the task and a test stub, so point `--out` at a scratch directory and move them
(a `test_*.py` inside `homelab_maint/tasks/` would be imported by the runner, which imports every module there):

```sh
cd /home/ohmz/homelab-maint
mkdir -p /tmp/scaffold && python3 -m homelab_maint.scaffold new task inode_watch --klass C0 --tier check --title "Inodes" --out /tmp/scaffold
mv /tmp/scaffold/inode_watch.py homelab_maint/tasks/ && mv /tmp/scaffold/test_inode_watch.py tests/
```

(`homelab-maint new task inode_watch ...` is the same command.) It refuses to overwrite a file and refuses a name that already exists as a task, job or probe. A finished example:

```python
"""inode_watch: warn before a filesystem runs out of inodes (a disk can have free space and still refuse new files)."""
import os

from homelab_maint.core import Ctx, Result, task


@task("inode_watch", klass="C0", tier="check", title="Inodes", timeout=30)
def run(ctx: Ctx) -> Result:
    mounts = [m for m in ctx.opt("mounts", ["/", "/media/SandiskSSD"]) if isinstance(m, str)]
    warn, crit = float(ctx.opt("warn_used_pct", 85)), float(ctx.opt("crit_used_pct", 95))
    rows = []
    for m in mounts:
        try:
            st = os.statvfs(m)
        except OSError:
            continue                          # not mounted: plex_media_mount_check owns that question
        if not st.f_files:                    # btrfs and friends report no inode limit
            continue
        used = 100.0 * (st.f_files - st.f_favail) / st.f_files
        level = "crit" if used >= crit else "warn" if used >= warn else "ok"
        rows.append({"mount": m, "used_pct": round(used, 1), "level": level})
    if not rows:
        return Result("skipped", "Inodes: no watched mount is readable", alert=False)
    worst = max(rows, key=lambda r: r["used_pct"])
    status = "crit" if any(r["level"] == "crit" for r in rows) else "warn" if any(r["level"] == "warn" for r in rows) else "ok"
    return Result(status, f"Inodes: {worst['mount']} {worst['used_pct']:.0f}% used (warn at {warn:g}%)",
                  metrics={"worst_pct": worst["used_pct"]}, items=rows[:12])
```

The contract, in the order people trip over it:

* **`Result.summary`**: one line, ASCII, at most 140 characters. It can become a text message.
* **Fail closed.** An unreadable file, a timeout, an unparsable answer or an empty selector means "report a problem" (for a check) or "do
  nothing" (for a cleaner). It never means "all fine" and never "everything".
* **Status**: `ok`, `info` (never pages), `warn`, `crit`, `skipped`, `error`. `alert=False` shows the result on the dashboard but never pages.
* **Options** come from `[tasks.inode_watch]` in `/etc/homelab-maint/maint.toml` through `ctx.opt("key", default)`. Per-task state that
  must survive between runs goes in `ctx.state` (a dict, saved for you). Time series go to `core.append_history`.
* **Cadence** is the `tier`: `check` runs every 15 minutes (job `tier-check`), `daily` at 07:30, `weekly` on Wednesday 07:45. A task can also
  carry its own cron line: `[tasks.inode_watch] schedule = "*/5 * * * *"` makes the tick run it separately.
* **Paging** is debounced by the runner: a problem must be seen on 2 runs in a row before the first alert, then it reminds every 24 h and
  sends one recovery. You do not write that logic.
* **Metrics** must be small scalars (they are shipped to the browser). Items are at most 12 rows.
* **Never** print or store a secret. Never shell out with a string; pass an argv list to `core.sh([...], timeout=...)`.

Try it without installing anything. This runs the task once the way the runner does, reads your real config, sends nothing and (as a
normal user) cannot write state:

```sh
python3 - <<'PY'
import homelab_maint.cli as cli, homelab_maint.core as core
cli.load_tasks()
res, secs = core.run_task(core.REGISTRY["inode_watch"], core.load_config(), apply=False)
print(res.status, res.summary)
PY
```

After `sudo ./install.sh`, `homelab-maint run --task inode_watch --dry-run` does the same inside the runner (status and history are recorded,
and a confirmed problem pages), and `homelab-maint status | grep inode_watch` shows the result.

Test it (section 14), then `sudo ./install.sh`. Optional extras: a playbook (`[playbook.inode_watch]` in `playbooks.toml`: what it means,
first checks, safe fixes, what not to do; the alert email and the Incidents tab show it), an SLO objective (`[[slo]]` in the same file),
and a route override (section 8).

## 4. Add a cleaner (C1)

A cleaner changes the host, so it follows three rules the runner enforces:

1. every mutation goes through `ctx.act(what, target, size, fn, protect_names=...)`;
2. it does nothing unless `[tasks.NAME] mode = "apply"` **and** the run was started with `--apply` **and** no `PAUSE` file exists;
3. in report mode `ctx.act` writes a `dry-run` line to `audit.jsonl` and returns `False`, so the dry-run list is exactly what apply would do.

`ctx.act` also refuses an empty target, a target matching `protected.toml` (databases, Plex, Immich, ComfyUI, the backups ...), and anything
past the per-run caps (`max_gib_per_run`, `max_items_per_run`).

```sh
python3 -m homelab_maint.scaffold new task scratch_clean --klass C1 --tier daily --title "Scratch dir" --out /tmp/scaffold   # then move the two files as above
```

The generated C1 module selects nothing until `[tasks.scratch_clean] path = "..."` is set, looks one level deep, never follows symlinks, and
sorts its victims so two runs print the same list. Enabling it, in this order:

1. `homelab-maint run --task scratch_clean --dry-run`, then read what it would do: `grep scratch_clean /var/log/homelab-maint/audit.jsonl | tail`.
2. Watch it in report mode for a few days. A C1 task with no `mode` key (or `mode = "report"`) only reports, in any window, even when the runner was started with `--apply`.
3. List it in `routine.toml` (section 9) so the routine engine applies the daily window, the evening freeze, the gates, the canary and the post-check to it.
4. Set `mode = "apply"`. The first apply run is a canary at 10% of the caps; later runs use the full caps. After a change the routine
   re-runs the named post-check tasks (`failed_units`, `disk_forecast`, ...) and marks the change "not verified" if one got worse.
5. `homelab-maint pause scratch_clean` is the per-task kill switch; `homelab-maint pause` stops everything that mutates, and that includes every `pausable` job the tick would start (backups too: see the checklist).

Never free memory or disk by killing or deleting something the protected list or the service classes name. If a cleaner must touch a
protected thing on purpose (a disposable cache inside a protected app directory), say so with an explicit, narrow `unprotect = ["regex"]`
under that task in `maint.toml`; `retention` and `caps` already do.

## 5. Add a plan task (C2)

For things you want to read before they go: `Result(plan={"items": [...sorted...], "total_bytes": n})`. The runner hashes the plan.
`homelab-maint plan NAME` prints it and the hash; `homelab-maint approve NAME HASH` re-plans, refuses if the plan changed, writes an
approval file for that hash and applies. `c2_candidates` is the model; the C2 scaffold has the shape (`core.approved(ctx.name, hash)`).

## 6. Add a job (an existing command, on a schedule)

Use a job when the work is already a script. The umbrella then schedules, times out, gates, logs and reports it; the script keeps its own logic.
Append to `/etc/homelab-maint/jobs.toml` (or generate a snippet: `homelab-maint new job NAME`).

Worked example. Say you deployed Paperless-ngx (the same app the probe example in section 7 watches) and want its nightly document export:

```toml
[[job]]
name = "paperless-export"
title = "Paperless-ngx nightly export"
command = ["/usr/bin/docker", "exec", "paperless", "document_exporter", "../export"]
user = "root"                      # docker needs it; use "ohmz" for anything that only touches your home directory
schedule = "10 3 * * *"            # five-field cron, host time zone (America/Toronto); also "daily 03:10", "every 15m", "@daily"
mode = "managed"                   # the tick runs it
source = "native"                  # nothing legacy drives it, so there is no `retire` list and no interlock
class = "P2"                       # batch: it yields when the host is under pressure
gates = ["backup"]                 # never start while a backup is running
window = "02:00-05:00"             # a catch-up after downtime starts only inside this window
timeout_s = 1800
jitter_s = 600
success = { exit_codes = [0] }
notify = { on_failure = "alert", on_success = "none" }
```

Existing scripts are adopted the same way. `purge-public-guests` (the script that reaps guest accounts on the public Open WebUI, which nothing scheduled before) already ships in
`jobs.toml` in `observe` mode: it deletes accounts, so you adopt it deliberately with `homelab-maint migrate cutover purge-public-guests`. It is the model for a script with no legacy driver.

What the keys mean:

| Key | Meaning |
|-----|---------|
| `command` | argv list, absolute path first. `{self}` is `homelab-maint`, `{python}` is `python3`. No shell: pipes, `&&` and `$VAR` do nothing. |
| `user` | `root` or a login user; non-root runs through `runuser` with a clean environment like the one systemd gives a user unit. |
| `schedule` | cron or the readable forms. DST-correct: a fixed-time job runs once on the spring-forward and fall-back days. |
| `mode` | `managed` the tick runs it. `observe` it is only shown (a legacy timer or cron line still drives it). `retired` it never runs. |
| `source` | a label for the website: `native` (the umbrella owns it), `adapter` (a legacy command). `scheduler validate` flags a `managed` adapter job that has no `retire` list: that list is what stops a half finished cutover from running the job twice. |
| `class` | `P0`..`P3`. `P2`/`P3` do not start while the host is under pressure level 2 or more. |
| `heavy`, `backup` | `heavy` jobs run one at a time and never while a backup runs; a `backup` job holds that lock while it runs. |
| `disruptive` | restarts something people use: waits out the evening freeze (18:00-23:30). |
| `gates` | busy probes that must be idle to start: `backup`, `apt`, `plex`, `comfyui`, `ollama`, `immich`, `docker_build`, `gradle`, `any`. A gate that errors counts as busy. |
| `window` | catch-up runs start only inside this window; `max_defer_hours` and `force_after_defer` bound how long a busy gate may postpone it. |
| `timeout_s` | `0` means none. Otherwise TERM, then KILL of the whole process group after `kill_grace_s`. |
| `success` | beyond the exit code: `status_json`/`result_key`/`ok_values` (a status file the script writes), `touch_file`, `summary_file`. |
| `notify` | `on_failure` = `alert`/`none`, `on_success` = `maintenance`/`none`, `detail_file` is the long form for the email. |
| `self_notifies` | the legacy script already alerts by itself (the backups): the umbrella stays quiet about ordinary failures to avoid double messages. |
| `retire` | `["system:UNIT", "user:ohmz:UNIT", "cron:ohmz:TAG"]`: the tick refuses to run a `managed` job while any of these is still enabled, so a half finished cutover can never run it twice. |
| `after`, `max_attempts`, `retry_on`, `retry_backoff_s` | ordering after another job, and bounded retries of lost, timed out or failed runs. |

Check and try it (the tick starts managed jobs by itself; nothing needs enabling):

```sh
python3 -m homelab_maint.scheduler validate          # config problems, missing executables, unknown gates, bad schedules
homelab-maint schedule | grep paperless               # the unified schedule: next run and why it waits
homelab-maint job run paperless-export               # start it once now; still honours the interlock, overlap, the heavy mutex and PAUSE
homelab-maint job run paperless-export --force       # additionally skips pressure, freeze, gates and windows
ls /var/log/homelab-maint/jobs/paperless-export/     # one log per run, secrets scrubbed, size capped
```

Run the command once by hand first, as the user the job will run as.
A job result appears in `status.json` like a task (class `J`), feeds the SLOs and the incident ledger, and its failure alert goes through
`notify.py`. If the job replaces a legacy timer or cron line, do not just flip it: write the item in `legacy-retirement.toml` (section 16).

## 7. Add a probe (is it alive?)

A probe only reads: an HTTP GET/HEAD, a TCP connect, `docker ps`, `systemctl show`, a file's age, a JSON state file or an argv command.
A probe must fail twice in a row before it counts as down, and recover twice before it counts as up again; flapping is damped; availability
is kept for 30 days and feeds the SLOs.

```toml
# /etc/homelab-maint/probes.d/paperless.toml  (root-owned, not group/world writable, or it is ignored)
[[probe]]
name = "paperless"
title = "Paperless-ngx"
type = "http"
target = "http://127.0.0.1:8010/api/"
expect = { status = [200, 401], max_ms = 3000 }      # 401 means "up, wants a login": the answer a healthy API gives an anonymous GET
interval_s = 300
timeout_s = 5
class = "P1"                       # what a person waits on; P0 platform, P2 batch, P3 best effort
confirm = 2
tags = ["app"]
# slo = 99.5                       # availability objective in percent over 30 days
# kuma_push_key = "paperless"      # also heartbeat the Uptime Kuma push monitor of that name (token in /etc/homelab-maint/kuma.toml, 0600)
# optional = true                  # a probe that was never seen up is "skipped", not an alert (the app is not deployed yet)
```

```sh
python3 -m homelab_maint.probes validate                       # "N probes valid, 0 problems"
python3 -m homelab_maint.probes run --only paperless --force --all
```

Types: `http`, `tcp`, `docker` (a container, `"@daemon"` or `"*"` for the fleet), `systemd`, `command` (argv list), `file_age`, `json`. The
engine's header comment in `homelab_maint/probes.py` lists every `expect` key. Uptime Kuma stays as it is (Homarr reads it); the probe plane is the
source of truth and can heartbeat Kuma push monitors. Never point anything at the live Kuma database; the Kuma comparison tool reads a copy.

## 8. Notifications: routes, channels, templates

Every message the umbrella sends (alerts, recoveries, maintenance updates, the daily digest, the weekly report, incidents, job results,
smart events, migration cutovers) goes through `notify.send`, then through your existing Hermes transports (SMS over the carrier gateway, Gmail
through `alert_transports`). No credentials live in homelab-maint; they stay in `~ohmz/.hermes/alert_transports.env`. Root jobs call Hermes as
`ohmz`. A text is one ASCII segment of at most 130 characters with no link; the email is HTML in the "Ohmz Cloud" palette with facts, what was
done, what to do (the playbook) and a link to https://maintainer.ohmzhomelab.ca.

Change who hears what in `/etc/homelab-maint/notify.toml`:

```toml
[routes]
maintenance = "email"                 # cleanups, cutovers, restarts: email only (text too when the event is marked significant)
[routes.alert]
crit = "both"                         # sms + email
warn = "email"                        # a warning texts only after it survived a reminder ([escalation].warn_sms_after)

[task_routes]
docker_df = "email"                   # an explicit route is final; "none" silences a noisy check without disabling it
inode_watch = "email"

[todo]
inode_watch = ["$ df -i /", "$ sudo du --inodes -x / | sort -n | tail"]   # the "what to do" lines of the alert email; wins over the task's playbook
```

Look before you leave it: `python3 -m homelab_maint.notify route alert.crit` prints the channels a kind uses (no send);
`python3 -m homelab_maint.notify render --out /tmp/previews` writes sample emails; `homelab-maint notify-test` sends one clearly labelled
TEST of each kind to your phone and inbox (run it as root; it is the proof the migration waits for). Budgets, quiet hours (23:30-07:00, texts
only), dedupe windows and the kill switch (`touch /etc/homelab-maint/NOTIFY_MUTE` silences everything except critical) are in the same file.

From a shell hook or an `OnFailure=` unit, use the same path instead of mailing yourself:

```sh
python3 -m homelab_maint.notify send alert warn "nightly-sync failed" "rsync exited 23" --task nightly-sync --key nightly-sync
```

A new channel (a webhook or ntfy) is a code change, not a config change: add its name to `CHANNELS` in `notify.py`, give it a transport
callable `(Message, config) -> TransportResult`, and route to it in `notify.toml`. Tests inject fake transports; the real ones refuse to run under pytest.

## 9. Add a step to the routine

`routine.toml` says when each maintenance task may run and in what order. It never switches a cleaner on; `maint.toml` decides `report` or `apply`.

```toml
[[routine]]
name = "daily"
cadence = "daily"
window = "daily"                    # [windows] daily = "07:30-09:30"
steps = [
  "spike_review", "docker_cache", "docker_images", "apt_clean",
  "scratch_clean",                   # <- your new cleaner: a task name is enough
  { task = "c2_candidates", disruptive = true },   # the table form adds per-step options
  "verify", "report",
]
```

Short names such as `verify`, `report` and `spike_review` are built-in steps (`BUILTIN_STEPS` in `routine.py`); anything else must be a task name. `homelab-maint routine check` warns about a step whose task is not registered (`task scratch_clean is not registered` until the module is
installed). `homelab-maint routine plan` shows what runs when, with the freeze decisions. A disruptive step (an apply-mode cleaner, a restart)
waits for its window, for `gates` (`backup`, `pressure`, or any name `tasks/gates.py` knows) and out of the evening freeze; read-only steps
are never frozen. `touch /etc/homelab-maint/FREEZE` freezes everything disruptive right now.

## 10. Playbooks and objectives

`[playbook.NAME]` in `playbooks.toml` takes `title`, `class`, `meaning`, `impact`, and lists `checks`, `fixes`, `avoid`. A line starting with
`$ ` is a command; every command outside `fixes` must be read-only (of `homelab-maint` itself only `status`, `plan`, `doctor` and `routine status|plan|due|check` qualify: a `run` or a `gate` records something). Nothing in that file may look like a secret (a test lints it). The text
appears in alert emails and on the Incidents tab. `[[slo]]` rows list the checks that count toward an availability objective.

## 11. Service classes and the protected list

`classes.toml` maps container and unit name patterns to `P0` platform (never touched), `P1` interactive (protected), `P2` batch, `P3` best
effort. Unknown means `P2`. Add a new container's pattern when you deploy it, anchored (`^name$`). `protected.toml` is a second, blunter list of
names and paths no automatic action may delete, kill or restart. A malformed pattern in either one protects more, never less.

## 12. Dashboards

* **Homarr widget.** Add a payload function to `homelab_maint/payloads.py` (a pure function of `status.json` returning under 4 KB, with
  human strings and colour words precomputed) and register it in `ROUTES`; add the `.jsx` template under `widgets/` and its entry in
  `widgets/build_widgets.py`; `python3 widgets/build_widgets.py --check` renders it against ok/warn/crit/stale payloads and enforces the
  Homarr template limits (see `widgets/CONVENTIONS.md`). The server answers on `127.0.0.1:9111`, GET only.
* **Website (retired).** The old read-only site (`web/app.py`, `web/static/*`, `web/fixtures/`) is retired with the
  `maintenance-web` container; there is no `web/` directory any more. Its data still comes from `homelab_maint/publish.py`
  (`STATE_DIR/public/*.json`), which the OhmzMaintainer beszel hub reads; to add a dashboard view, extend the hub instead
  (`../../docs/EXTENDING-BESZEL.md`).

## 13. Plugins: owner-written tasks without touching the package

A file in `/etc/homelab-maint/plugins.d/NAME.py` that calls `@task(...)` is loaded on every run. It is **root code**, so discovery is strict:

* the directory and every parent are real directories owned by root and not group/world writable (`/tmp`-style sticky parents are tolerated);
* the file is a regular file (a symlink is refused even to a root-owned target), owned by root, not group/world writable, at most 256 KiB,
  with a plain module name (`[A-Za-z][A-Za-z0-9_]*.py`);
* the bytes executed are the bytes of the descriptor that passed those checks (no check-then-open race);
* a plugin cannot replace or remove a built-in task, and its tasks are validated (name, class, tier, callable, timeout 1-3600 s).

When a plugin fails to load (syntax error, import error, `sys.exit`, a hang longer than 5 s), everything it registered is rolled back and it is reported as an `error` task
named `plugin_NAME` in `status.json`, which pages you, while every other plugin and built-in keeps running. An unsafe directory refuses all
plugins and reports one error task, `plugins_dir`. Check what the runner would do: `homelab-maint plugins` prints `loaded` or `REFUSED` per file.

```sh
sudo install -m 0644 -o root -g root inode_watch.py /etc/homelab-maint/plugins.d/inode_watch.py
homelab-maint plugins
```

Plugins are for your own checks and cleaners. Anything you would want to keep, test and review belongs in `homelab_maint/tasks/`. The isolation covers the task registry and the
import, not what a plugin does to the rest of the process while it runs: it is root code that you wrote and reviewed.

## 14. Testing

```sh
cd /home/ohmz/homelab-maint
python3 -m pytest tests/test_inode_watch.py -q          # your file
python3 -m pytest tests -q                              # everything (about two minutes)
```

`tests/conftest.py` points `HOMELAB_MAINT_STATE`, `_LOG`, `_RUN` and `_CONF` at temp directories before `homelab_maint.core` is imported, so a test
can never touch `/var/lib` or `/etc`. Import it first in every test file: `import conftest  # noqa: F401`. Then:

* give the task a `Ctx` built from a config dict: `Ctx({"tasks": {"inode_watch": {...}}, "caps": {}, "protected": {"patterns": []}}, "inode_watch", apply=False)`;
* mock subprocesses by monkeypatching `homelab_maint.core.sh` (or the module-local `sh`); do not run `docker`, `systemctl` or `rm` for real;
* for a cleaner assert three things: report mode changes nothing and `ctx.freed == 0`; apply mode removes exactly the expected files; a `PAUSE`
  file in `core.CONF_DIR` stops it;
* for anything that notifies, inject a fake transport. Real transports refuse to run under pytest.

`python3 -m homelab_maint.scaffold new task NAME` generates a test stub that already checks the contract (registered with the right class and
tier, summary is one short ASCII line, report mode never mutates).

## 15. Safety checklist (read before enabling anything that changes the host)

- [ ] Is it the right kind? A command that already exists is a job; a yes/no question is a probe; only judgement over numbers is a task.
- [ ] Class: C0 if it only looks. C1 only if every change goes through `ctx.act`. C2 if you want to read the plan first.
- [ ] Empty, unreadable or unparsable input does nothing (cleaners) or reports a problem (checks). No selector means no selection.
- [ ] Paths resolve (`realpath`) inside an allowed root; symlinks are not followed; files modified in the last 10 minutes are skipped.
- [ ] Nothing in `protected.toml` can be touched, and the service class of every container it touches is `P2` or `P3`.
- [ ] Caps are set (`max_gib_per_run`, `max_items_per_run`), and the first apply run is the 10% canary.
- [ ] It ships in `mode = "report"`. You read the dry-run list in `audit.jsonl` for a few days before flipping it.
- [ ] It is in the routine window (not the evening freeze), behind the right gates (`backup`, `pressure`, `plex`, ...), and has a post-check.
- [ ] It honours `PAUSE`. (`ctx.act` and the scheduler do; a job script that mutates should be `pausable`, the default.) The exception is a backup: a global `PAUSE` forgotten for a week would silently stop it, so a backup job says `pausable = false` (`PAUSE.<job>` still stops that one deliberately; `migrate status` shouts whenever a kill switch is on).
- [ ] The result summary is one ASCII line of at most 140 characters, and nothing prints or stores a secret.
- [ ] It has a test with a mocked executor and tmp dirs; the rollback story is one sentence long.
- [ ] If it replaces a legacy timer, cron line or script: an item in `legacy-retirement.toml`, a parity check (for an adapter `unit_equiv` against the legacy service, and the tick must record green runs before anything depends on it), and the cutover from `docs/MIGRATION.md`. Never retire `homelab-maint-check.timer`: it is the runner that does not depend on the tick.

## 16. Retiring a legacy thing you just replaced

The inventory of every legacy scheduled thing, hook and notifier is `etc/legacy-retirement.toml`; `docs/MIGRATION.md` is the runbook. Add an
`[[item]]` for anything new you replace, with a `parity_check`, the `retire_actions`, and `depends_on`. Put your own items in
`/etc/homelab-maint/legacy-retirement.d/NAME.toml` (root-owned, 0644, in a root-owned directory; anything else refuses the whole inventory) so an upgrade of the shipped
`legacy-retirement.toml` never touches them. `homelab-maint migrate audit` is the other half: it lists any timer, cron line or cron file on the host that no item accounts for, so something added outside the umbrella later is noticed. `homelab-maint migrate validate` checks
the file (it refuses, for example, to ever disable the umbrella's own services). Never delete the old thing: a cutover disables the unit,
moves the script into `/usr/local/lib/homelab-maint/legacy/NAME/` with a README, comments the cron line with a tag, and remembers the exact prior
state so `homelab-maint migrate rollback NAME --apply` puts it back.
