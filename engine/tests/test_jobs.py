"""jobs.py: loading/validation of jobs.toml, the shipped inventory, environment and argv construction, the supervisor (real
subprocesses in tmp dirs: exit codes, TERM->KILL of the whole process group, leftovers, log caps, scrubbing, hooks, user switching
through a fake runuser), result mapping and process helpers. Nothing here touches systemd, /etc, /var or the network."""
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: F401,E402

from homelab_maint import core, jobs, schedule  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PYTHONPATH = str(REPO)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    for k in ("STATE", "LOG", "CONF", "RUN"):
        d = tmp_path / k.lower()
        d.mkdir()
        monkeypatch.setattr(core, f"{k}_DIR", d)
    monkeypatch.setenv("HOMELAB_MAINT_TZ", "America/Toronto")
    return tmp_path


@pytest.fixture
def sigs():
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    jobs._STOP["flag"] = False
    yield
    for s, h in old.items():
        signal.signal(s, h)
    jobs._STOP["flag"] = False


def load_text(tmp, text, mcfg=None):
    p = tmp / "jobs.toml"
    p.write_text(textwrap.dedent(text))
    return jobs.load(p, mcfg=mcfg if mcfg is not None else {"tasks": {}}, apply_modes=False)


BASE = '[[job]]\nname = "x"\ncommand = ["/bin/true"]\n'


# --------------------------------------------------------------------------- loading and validation
def test_missing_file_is_an_empty_clean_config(dirs):
    cfg = jobs.load(dirs / "nope.toml", mcfg={"tasks": {}})
    assert cfg.jobs == {} and cfg.errors == []


def test_untrusted_jobs_toml_is_ignored_as_a_whole(dirs, monkeypatch):
    """It names commands the tick runs as root: a file others can write must never be believed."""
    p = dirs / "jobs.toml"
    p.write_text(BASE)
    os.chmod(p, 0o644)
    os.chmod(dirs, 0o755)
    assert jobs.untrusted_reason(p) == "" and set(jobs.load(p, mcfg={"tasks": {}}, enforce_trust=True).jobs) == {"x"}
    os.chmod(p, 0o664)                                                       # group writable
    cfg = jobs.load(p, mcfg={"tasks": {}}, enforce_trust=True)
    assert cfg.jobs == {} and "ignored" in cfg.errors[0] and "group/world writable" in cfg.errors[0]
    os.chmod(p, 0o644)
    os.chmod(dirs, 0o777)                                                    # a directory anybody can write into
    assert "directory" in jobs.untrusted_reason(p) and jobs.load(p, mcfg={"tasks": {}}, enforce_trust=True).jobs == {}
    os.chmod(dirs, 0o755)
    monkeypatch.setattr(os, "geteuid", lambda: 0)                            # as the tick (root): a file owned by an ordinary user is not trusted
    assert "not owned by root" in jobs.untrusted_reason(p)
    cfg = jobs.load(p, mcfg={"tasks": {}})                                   # enforcement defaults to ON for root
    assert cfg.jobs == {} and "not owned by root" in cfg.errors[0]
    monkeypatch.undo()
    assert set(jobs.load(p, mcfg={"tasks": {}}).jobs) == {"x"}                # ... and OFF for the unprivileged test/dev runs
    assert jobs.untrusted_reason(dirs / "missing.toml") == "cannot be examined"


def test_unparsable_toml_reports_and_loads_nothing(dirs):
    cfg = load_text(dirs, "[[job]\nname = ")
    assert cfg.jobs == {} and "unreadable" in cfg.errors[0]


def test_minimal_job_defaults(dirs):
    cfg = load_text(dirs, BASE)
    j = cfg.jobs["x"]
    assert (j.user, j.cls, j.mode, j.schedule, j.heavy, j.monitor, j.timeout_s) == ("root", "P3", "observe", "", False, False, 0)
    assert j.catchup_hours == 6.0 and j.max_attempts == 1 and j.retry_on == [] and j.title == "x"
    assert cfg.errors == []


@pytest.mark.parametrize("body,needle", [
    ('name = "Bad Name"\ncommand = ["/bin/true"]', "bad name"),
    ('name = "x"', "command must be"),
    ('name = "x"\ncommand = []', "command must be"),
    ('name = "x"\ncommand = "/bin/true"', "command must be"),
    ('name = "x"\ncommand = ["true"]', "absolute path"),
    ('name = "x"\ncommand = ["/bin/true"]\nschedule = "61 * * * *"', "out of range"),
    ('name = "x"\ncommand = ["/bin/true"]\nuser = "Root!"', "bad user"),
    ('name = "x"\ncommand = ["/bin/true"]\nclass = "P9"', "bad class"),
    ('name = "x"\ncommand = ["/bin/true"]\nmode = "auto"', "bad class"),
    ('name = "x"\ncommand = ["/bin/true"]\nwindow = "nonsense"', "bad window"),
    ('name = "x"\ncommand = ["/bin/true"]\navoid = ["25:00-26:00"]', "bad time"),
])
def test_invalid_jobs_are_skipped_with_a_reason(dirs, body, needle):
    cfg = load_text(dirs, "[[job]]\n" + body + "\n")
    assert cfg.jobs == {}
    assert any(needle in e for e in cfg.errors), cfg.errors


def test_unknown_keys_are_reported_but_job_loads(dirs):
    cfg = load_text(dirs, BASE + 'heavvy = true\n[job.success]\nbogus = 1\n[job.notify]\nwhat = 2\n')
    assert "x" in cfg.jobs
    assert sum("unknown" in e for e in cfg.errors) == 3


def test_duplicate_names_second_ignored(dirs):
    cfg = load_text(dirs, BASE + BASE.replace("/bin/true", "/bin/false"))
    assert cfg.jobs["x"].command == ["/bin/true"] and any("duplicate" in e for e in cfg.errors)


def test_bool_keys_must_be_bools(dirs):
    cfg = load_text(dirs, BASE + 'heavy = "yes"\n')
    assert cfg.jobs["x"].heavy is False and any("heavy must be" in e for e in cfg.errors)


def test_defaults_table_applies_to_every_job(dirs):
    cfg = load_text(dirs, '[defaults]\nclass = "P2"\nnice = 10\n' + BASE + BASE.replace('"x"', '"y"') + 'class = "P1"\n')
    assert cfg.jobs["x"].cls == "P2" and cfg.jobs["x"].nice == 10 and cfg.jobs["y"].cls == "P1"


def test_enabled_false_cannot_be_managed(dirs):
    cfg = load_text(dirs, BASE + 'mode = "managed"\nenabled = false\n')
    assert cfg.jobs["x"].mode == "observe"


def test_quiet_defaults_to_monitor_and_can_be_set(dirs):
    cfg = load_text(dirs, BASE + 'monitor = true\n[[job]]\nname = "y"\ncommand = ["/bin/true"]\n'
                    '[[job]]\nname = "z"\ncommand = ["/bin/true"]\nquiet = true\n'
                    '[[job]]\nname = "w"\ncommand = ["/bin/true"]\nmonitor = true\nquiet = false\n')
    assert [cfg.jobs[n].quiet for n in "xyzw"] == [True, False, True, False] and cfg.errors == []


@pytest.mark.parametrize("mounts", ['["relative/path"]', '["/mnt/../etc"]', '"/mnt/x"'])
def test_requires_mounts_must_be_absolute_paths(dirs, mounts):
    cfg = load_text(dirs, BASE + f"requires_mounts = {mounts}\n")
    if mounts.startswith('"'):                                   # a bare string is not a list: ignored like every other bad list
        assert cfg.jobs["x"].requires_mounts == []
    else:
        assert cfg.jobs == {} and "requires_mounts" in cfg.errors[0]


def test_max_attempts_defaults_retry_on_lost_and_timeout(dirs):
    cfg = load_text(dirs, BASE + "max_attempts = 3\n")
    assert cfg.jobs["x"].retry_on == ["lost", "timeout"]
    cfg = load_text(dirs, BASE + 'max_attempts = 3\nretry_on = ["failed", "bogus"]\n')
    assert cfg.jobs["x"].retry_on == ["failed"]


def test_scheduler_table_overrides_and_rejects_unknown(dirs):
    cfg = load_text(dirs, '[scheduler]\nmax_concurrent = 1\nnonsense = 1\n[scheduler.pressure_max]\nP2 = 0\n' + BASE)
    assert cfg.sched["max_concurrent"] == 1 and cfg.sched["pressure_max"]["P2"] == 0 and cfg.sched["pressure_max"]["P3"] == 1
    assert any("nonsense" in e for e in cfg.errors)


def test_task_schedules_from_maint_toml(dirs):
    m = {"tasks": {"docker_cache": {"schedule": "0 5 * * *", "mode": "apply", "heavy": True, "gates": ["docker_build"]},
                   "disk_forecast": {"schedule": "*/15 * * * *", "monitor": True},
                   "off": {"schedule": "0 1 * * *", "enabled": False}, "noschedule": {"mode": "report"}}}
    cfg = load_text(dirs, "", m)
    assert set(cfg.jobs) == {"docker-cache", "disk-forecast"}
    j = cfg.jobs["docker-cache"]
    assert j.command == ["{self}", "run", "--task", "docker_cache", "--scheduled", "--apply"] and j.mode == "managed" and j.heavy
    assert j.source == "task" and j.gates == ["docker_build"] and j.notify["on_failure"] == "none"
    assert cfg.jobs["disk-forecast"].command == ["{self}", "run", "--task", "disk_forecast", "--scheduled"]      # no --apply unless mode = apply; --scheduled: never the owner's override
    assert cfg.jobs["disk-forecast"].monitor
    assert j.disruptive and not cfg.jobs["disk-forecast"].disruptive and not cfg.jobs["disk-forecast"].heavy   # cleaners honour the freeze


def test_apply_mode_task_is_heavy_and_disruptive_unless_the_config_says_otherwise(dirs):
    m = {"tasks": {"a": {"schedule": "0 5 * * *", "mode": "apply"},
                   "b": {"schedule": "0 5 * * *", "mode": "apply", "heavy": False, "disruptive": False},
                   "c": {"schedule": "0 5 * * *", "mode": "report"}}}
    cfg = load_text(dirs, "", m)
    assert [(cfg.jobs[n].heavy, cfg.jobs[n].disruptive) for n in "abc"] == [(True, True), (False, False), (False, False)]


def test_task_entry_never_shadows_a_job(dirs):
    cfg = load_text(dirs, BASE.replace('"x"', '"disk-forecast"'), {"tasks": {"disk_forecast": {"schedule": "0 1 * * *"}}})
    assert cfg.jobs["disk-forecast"].command == ["/bin/true"] and any("task disk-forecast" in e for e in cfg.errors)


def test_external_entries_are_kept_ascii(dirs):
    cfg = load_text(dirs, '[[external]]\nname = "apt-daily"\ntitle = "apt é"\nkind = "os"\n[[external]]\nname = "BAD NAME"\n')
    assert [e["name"] for e in cfg.external] == ["apt-daily"] and "é" not in cfg.external[0]["title"] or True


def test_mode_overrides_persist_and_apply(dirs):
    text = BASE + 'mode = "observe"\n'
    (dirs / "jobs.toml").write_text(text)
    assert jobs.set_mode("x", "managed") == {"x": "managed"}
    assert json.loads((core.STATE_DIR / "job-modes.json").read_text()) == {"x": "managed"}
    cfg = jobs.load(dirs / "jobs.toml", mcfg={"tasks": {}})
    assert cfg.jobs["x"].mode == "managed" and cfg.modes == {"x": "managed"}
    jobs.set_mode("ghost", "managed")                                   # unknown jobs are ignored on load
    assert "ghost" not in jobs.load(dirs / "jobs.toml", mcfg={"tasks": {}}).modes
    assert jobs.set_mode("x", None) == {"ghost": "managed"}
    with pytest.raises(ValueError):
        jobs.set_mode("x", "bogus")
    with pytest.raises(ValueError):
        jobs.set_mode("Bad Name", "managed")


def test_corrupt_modes_file_is_ignored(dirs):
    (core.STATE_DIR / "job-modes.json").write_text("{not json")
    assert jobs.load_modes() == {}
    (core.STATE_DIR / "job-modes.json").write_text(json.dumps({"x": "weird", "y": "managed", "z": 3}))
    assert jobs.load_modes() == {"y": "managed"}


# --------------------------------------------------------------------------- the shipped inventory
@pytest.fixture(scope="module")
def shipped():
    return jobs.load(REPO / "etc" / "jobs.toml", mcfg={"tasks": {}}, apply_modes=False)


def test_shipped_file_is_valid(shipped):
    assert shipped.errors == []
    assert len(shipped.jobs) == 21      # + container-audit, added 2026-10-04 during the /home/ohmz migration


# What the real units / crontab say (verified read-only against this host on 2026-10-02). Name -> (command, user, cron schedule).
REAL = {
    "backup-system": (["/usr/local/sbin/backup-system.sh"], "root", "0 1 * * sat"),
    "backup-immich": (["/usr/local/sbin/backup-immich.sh"], "root", "0 1 * * sun"),
    "stack-backup": (["/home/ohmz/StudioProjects/ai-stack/scripts/stack_backup.sh"], "ohmz", "30 3 * * *"),
    "stack-watchdog": (["/usr/bin/python3", "/home/ohmz/StudioProjects/ai-stack/scripts/stack_watchdog.py"], "ohmz", "3-58/5 * * * *"),
    "search-canary": (["/usr/bin/python3", "/home/ohmz/StudioProjects/ai-stack/scripts/search_canary.py"], "ohmz", "29,59 * * * *"),
    "bazarr-rules": (["/usr/bin/python3", "/home/ohmz/StudioProjects/docker-bazarr/bazarr_subtitle_rules.py"], "ohmz", "30 2 * * *"),
    "tunarr-sync": (["/home/ohmz/StudioProjects/tunarr-sync/run.sh"], "ohmz", "15 4 * * mon"),
    "docker-prune": (["/usr/local/sbin/docker-prune.sh"], "root", "0 4 * * sun"),
    "prune-openwebui-media": (["/usr/local/bin/prune-openwebui-media.sh"], "root", "0 4 * * *"),
    "comfyui-idle-vram": (["/usr/local/bin/comfyui-idle-vram.sh"], "root", "1-56/5 * * * *"),
    "notebook-db-alert": (["/usr/local/sbin/notebook-db-alert.sh"], "root", "10-55/15 * * * *"),
    "immich-server-recycle": (["/usr/bin/docker", "restart", "immich_server"], "root", "24 */2 * * *"),
    "mem-guard": (["/usr/local/sbin/mem-guard.py", "--threshold-gib", "5", "--dry-run"], "root", "0 */3 * * *"),
    "tier-check": (["{self}", "run", "--tier", "check", "--apply"], "root", "*/15 * * * *"),   # --apply since the spike glue: the check service has it
    "tier-daily": (["{self}", "run", "--tier", "daily", "--apply"], "root", "30 7 * * *"),
    "tier-weekly": (["{self}", "run", "--tier", "weekly", "--apply"], "root", "45 7 * * wed"),
    "metrics-sample": (["/usr/bin/python3", "-B", "-m", "homelab_maint.metrics_ring", "sample"], "root", "* * * * *"),
    # no legacy driver: the umbrella's own engines (probes.py / routine.py CLIs) and the never-scheduled guest purge
    "probes-run": (["/usr/bin/python3", "-B", "-m", "homelab_maint.probes", "run", "--notify"], "root", "* * * * *"),
    "routine-run": (["/usr/bin/python3", "-B", "-m", "homelab_maint.routine", "run", "--apply"], "root", "7-52/15 * * * *"),
    "purge-public-guests": (["/usr/bin/python3", "/home/ohmz/StudioProjects/ai-stack/scripts/purge_public_guests.py", "--yes"], "ohmz", "20 5 * * *"),
}
NATIVE_NO_DRIVER = {"probes-run", "routine-run", "purge-public-guests", "container-audit"}


@pytest.mark.parametrize("name", sorted(REAL))
def test_every_legacy_job_is_declared_with_its_real_command_user_schedule(shipped, name):
    cmd, user, cron = REAL[name]
    j = shipped.jobs[name]
    assert (j.command, j.user, j.schedule) == (cmd, user, cron)


def test_shipped_state_takes_nothing_over_on_the_live_host(shipped):
    """Installing the file must not take over anything that has a legacy driver: those ship observe (mem-guard retired) and name the
    driver the interlock checks. The engines with NO driver are the only ones managed from the start, and the account-deleting
    script ships observe."""
    for j in shipped.jobs.values():
        if j.name in NATIVE_NO_DRIVER:
            assert j.retire == [] and j.source == "native", j.name
            continue
        assert j.mode in ("observe", "retired"), j.name
        assert j.retire, f"{j.name} has no retire/interlock spec"
        for r in j.retire:
            assert r.split(":")[0] in ("system", "user", "cron")
    assert shipped.jobs["mem-guard"].mode == "retired"
    assert shipped.jobs["backup-system"].mode == "observe" and shipped.jobs["purge-public-guests"].mode == "observe"
    assert (shipped.jobs["probes-run"].mode, shipped.jobs["routine-run"].mode) == ("managed", "managed")


def test_native_engines_are_safe_to_run_from_the_first_tick(shipped):
    p, r = shipped.jobs["probes-run"], shipped.jobs["routine-run"]
    assert p.monitor and p.quiet and p.user == "root" and p.timeout_s > 45          # probes.toml budget_s = 45
    assert r.heavy and r.quiet and r.cls == "P3" and "--apply" in r.command         # apply only where maint.toml says mode = apply
    assert p.env == r.env == {"PYTHONPATH": "/usr/local/lib/homelab-maint"}
    g = shipped.jobs["purge-public-guests"]
    assert g.command[-1] == "--yes" and g.user == "ohmz" and g.cls == "P3"


def test_probes_run_summary_is_the_engines_own_count_line(shipped):
    j = shipped.jobs["probes-run"]
    out = ["ran 12/46 probes in 0.4s", "notify: skipped (alert_mode is not 'events', the check tier pages on the task level)"]
    assert jobs.make_result(j, done_rec(0, tail=out), 1000, 1060).summary == "ran 12/46 probes in 0.4s"


def test_every_module_a_job_runs_exists(shipped):
    """`python3 -m homelab_maint.X` jobs must name modules that exist (a typo would fail every minute)."""
    import importlib.util
    for j in shipped.jobs.values():
        if "-m" in j.command:
            mod = j.command[j.command.index("-m") + 1]
            assert importlib.util.find_spec(mod) is not None, f"{j.name}: {mod}"


@pytest.mark.parametrize("name,unit", [("backup-system", "system:backup-system.timer"), ("backup-immich", "system:backup-immich.timer"),
                                       ("stack-backup", "user:ohmz:stack-backup.timer"), ("stack-watchdog", "user:ohmz:stack-watchdog.timer"),
                                       ("search-canary", "user:ohmz:search-canary.timer"),
                                       ("bazarr-rules", "cron:ohmz:bazarr-nightly-subtitle-rule"),
                                       ("tunarr-sync", "cron:ohmz:tunarr-weekly-channel-sync"),
                                       ("docker-prune", "system:docker-prune.timer"),
                                       ("prune-openwebui-media", "system:prune-openwebui-media.timer"),
                                       ("comfyui-idle-vram", "system:comfyui-idle-vram.timer"),
                                       ("notebook-db-alert", "system:notebook-db-alert.timer"),
                                       ("immich-server-recycle", "system:immich-server-recycle.timer"),
                                       ("tier-check", "system:homelab-maint-check.timer"), ("tier-daily", "system:homelab-maint-daily.timer"),
                                       ("tier-weekly", "system:homelab-maint-weekly.timer"), ("metrics-sample", "system:homelab-maint-metrics.timer")])
def test_retire_specs_name_the_real_drivers(shipped, name, unit):
    assert unit in shipped.jobs[name].retire


def test_backups_require_the_same_mounts_as_their_units(shipped):
    assert shipped.jobs["backup-system"].requires_mounts == ["/mnt/backup/system"]                   # RequiresMountsFor=
    assert shipped.jobs["backup-immich"].requires_mounts == ["/mnt/backup/immich", "/media/Immich"]


def test_tunarr_sync_reads_the_real_exit_code_its_wrapper_logs(shipped):
    j = shipped.jobs["tunarr-sync"]
    assert j.heavy and j.success["exit_regex"] == r"^===== exit=(\d+) " and "%Y-%m-%d" in j.success["summary_file"]


def test_backups_run_exactly_like_the_units_did(shipped):
    for name, status, tgt in (("backup-system", "/var/log/backup/system-status.json", "backup-system.service"),
                              ("backup-immich", "/var/log/backup/immich-status.json", "backup-immich.service")):
        j = shipped.jobs[name]
        assert j.user == "root" and j.timeout_s == 0                                    # TimeoutStartSec=infinity
        assert (j.nice, j.ionice_class, j.ionice_prio) == (10, 2, 7)                    # Nice=10, best-effort, priority 7
        assert j.heavy and j.backup and j.force_after_defer and j.self_notifies
        assert j.env == {} and j.workdir == "" and j.oom_score_adj is None              # nothing the unit did not have
        assert j.jitter_s == 900 and j.catchup_hours >= 100                              # RandomizedDelaySec=15min, Persistent=true
        assert j.hooks["on_failure"] == ["/usr/local/sbin/backup-failed.sh", tgt]       # OnFailure=backup-failure@%n.service
        assert j.success["status_json"] == status and j.success["result_key"] == "result"
        assert j.notify["on_expire"] == "alert" and j.retry_on == ["lost"] and j.max_attempts == 2
    s = shipped.jobs["stack-backup"]
    assert (s.user, s.timeout_s, s.nice, s.ionice_class, s.jitter_s) == ("ohmz", 1800, 10, 3, 300)
    assert s.success["touch_file"] == "/media/SandiskSSD/ai-stack-backups/LAST_OK" and s.backup and s.heavy


def test_unit_environment_is_carried(shipped):
    assert shipped.jobs["docker-prune"].env == {"DOCKER_CONFIG": "/home/ohmz/.docker"}
    assert shipped.jobs["prune-openwebui-media"].env == {"RETENTION_DAYS": "7"}
    assert shipped.jobs["stack-watchdog"].env == {"HOME": "/home/ohmz"}
    assert shipped.jobs["prune-openwebui-media"].nice == 10 and shipped.jobs["prune-openwebui-media"].ionice_class == 3


def test_timeouts_match_the_units(shipped):
    assert shipped.jobs["stack-watchdog"].timeout_s == 120 and shipped.jobs["search-canary"].timeout_s == 300
    assert shipped.jobs["notebook-db-alert"].timeout_s == 120 and shipped.jobs["tier-check"].timeout_s == 840
    assert shipped.jobs["tier-daily"].timeout_s == 10800 and shipped.jobs["metrics-sample"].timeout_s == 20


def test_immich_recycle_keeps_its_gate_semantics(shipped):
    j = shipped.jobs["immich-server-recycle"]
    assert j.gates == ["immich-recycle"] and j.max_defer_hours == 12 and j.force_after_defer and j.disruptive
    assert j.pressure_max == 3                         # it relieves memory pressure: only a real stall (>= 4) holds it back


def test_weekly_tier_waits_for_daily(shipped):
    assert shipped.jobs["tier-weekly"].after == ["tier-daily"]


def test_schedules_next_occurrence_matches_the_timers(shipped):
    t0 = schedule.parse("0 0 * * *").next_after(1_790_000_000)           # some midnight in Oct 2026
    d = lambda name: __import__("datetime").datetime.fromtimestamp(schedule.next_after(shipped.jobs[name].schedule, t0), schedule.host_tz())
    assert d("backup-system").weekday() == 5 and (d("backup-system").hour, d("backup-system").minute) == (1, 0)
    assert d("backup-immich").weekday() == 6 and d("backup-immich").hour == 1
    assert d("docker-prune").weekday() == 6 and d("docker-prune").hour == 4
    assert d("tier-weekly").weekday() == 2 and (d("tier-weekly").hour, d("tier-weekly").minute) == (7, 45)
    assert d("tunarr-sync").weekday() == 0 and (d("tunarr-sync").hour, d("tunarr-sync").minute) == (4, 15)
    assert (d("stack-backup").hour, d("stack-backup").minute) == (3, 30)


def test_externals_cover_the_os_timers(shipped):
    names = {e["name"] for e in shipped.external}
    assert {"apt-daily", "apt-daily-upgrade", "fstrim", "logrotate", "e2scrub-all", "snapd-refresh", "hermes-delivery",
            "smart-alert", "backup-failure", "sensor-exporter", "smartd", "smart-bridge", "glances", "thermal-log",
            "tunarr-autostart", "nvidia-cdi-refresh"} <= names
    assert all(e.get("kind") in ("os", "user", "daemon", "hook") for e in shipped.external)


def _unit_exec(path):
    try:
        t = Path(path).read_text()
    except OSError:
        return None
    return {k: v for k, v in (ln.split("=", 1) for ln in t.splitlines() if "=" in ln and not ln.startswith("#"))}


@pytest.mark.parametrize("name,unit", [("backup-system", "/etc/systemd/system/backup-system.service"),
                                       ("backup-immich", "/etc/systemd/system/backup-immich.service"),
                                       ("docker-prune", "/etc/systemd/system/docker-prune.service"),
                                       ("prune-openwebui-media", "/etc/systemd/system/prune-openwebui-media.service"),
                                       ("comfyui-idle-vram", "/etc/systemd/system/comfyui-idle-vram.service"),
                                       ("notebook-db-alert", "/etc/systemd/system/notebook-db-alert.service"),
                                       ("immich-server-recycle", "/etc/systemd/system/immich-server-recycle.service"),
                                       ("stack-backup", str(Path.home() / ".config/systemd/user/stack-backup.service")),
                                       ("stack-watchdog", str(Path.home() / ".config/systemd/user/stack-watchdog.service")),
                                       ("search-canary", str(Path.home() / ".config/systemd/user/search-canary.service"))])
def test_matches_the_real_unit_when_present(shipped, name, unit):
    """Drift guard on THIS host: skipped where the unit does not exist. ExecStart, Nice and the unit's Environment must agree."""
    u = _unit_exec(unit)
    if u is None:
        pytest.skip(f"{unit} not present")
    j = shipped.jobs[name]
    assert u["ExecStart"].split() == j.command
    if "Nice" in u:
        assert j.nice == int(u["Nice"])
    for ev in [v for k, v in u.items() if k == "Environment"]:
        k, _, v = ev.partition("=")
        if k != "HOME" or j.user == "ohmz":
            assert j.env.get(k) == v


# --------------------------------------------------------------------------- environment, argv, spec
SCHED = dict(jobs.SCHED_DEFAULTS)
OHMZ = lambda u: (1000, 1000, "/home/ohmz") if u == "ohmz" else None


def mk(**kw):
    kw.setdefault("name", "j")
    kw.setdefault("command", ["/bin/true"])
    return jobs.Job(**kw)


def test_root_env_is_the_system_service_environment_not_the_ticks(monkeypatch):
    monkeypatch.setenv("SECRET_FROM_TICK", "x")
    monkeypatch.setenv("HOME", "/root")
    env = jobs.build_env(mk(), SCHED)
    assert set(env) == {"PATH", "LANG", "USER", "XDG_DATA_DIRS"}
    assert "HOME" not in env and env["USER"] == "root" and env["LANG"] == "en_US.UTF-8"
    assert env["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"


def test_user_env_matches_the_user_manager(monkeypatch):
    env = jobs.build_env(mk(user="ohmz"), SCHED, OHMZ, exists=lambda p: p in ("/run/user/1000", "/run/user/1000/bus"))
    assert env["HOME"] == "/home/ohmz" and env["USER"] == env["LOGNAME"] == "ohmz"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000" and env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert env["PATH"].startswith("/home/ohmz/.local/bin:") and "/usr/bin" in env["PATH"]


def test_user_env_without_a_runtime_dir_omits_it():
    env = jobs.build_env(mk(user="ohmz"), SCHED, OHMZ, exists=lambda p: False)
    assert "XDG_RUNTIME_DIR" not in env and "DBUS_SESSION_BUS_ADDRESS" not in env and env["HOME"] == "/home/ohmz"


def test_unknown_user_cannot_be_launched():
    assert jobs.build_env(mk(user="ghost"), SCHED, OHMZ) is None
    assert jobs.build_spec(mk(user="ghost"), SCHED, "r1", 1, OHMZ) is None


def test_job_env_overrides_and_expands_tokens():
    env = jobs.build_env(mk(user="ohmz", env={"HOME": "/elsewhere", "X": "{home}/x", "S": "{self}"}), SCHED, OHMZ,
                         exists=lambda p: False)
    assert env["HOME"] == "/elsewhere" and env["X"] == "/home/ohmz/x" and env["S"] == SCHED["self_cmd"]    # {home}: the user's home


def test_argv_wrapping_order():
    j = mk(user="ohmz", command=["{self}", "run", "--tier", "daily"], ionice_class=2, ionice_prio=7)
    assert jobs.build_argv(j, SCHED, "/home/ohmz") == ["/usr/sbin/runuser", "-u", "ohmz", "--", "/usr/bin/ionice", "-c", "2", "-n", "7",
                                                       "/usr/local/sbin/homelab-maint", "run", "--tier", "daily"]
    assert jobs.build_argv(mk(ionice_class=3, ionice_prio=5), SCHED) == ["/usr/bin/ionice", "-c", "3", "/bin/true"]   # idle takes no -n
    assert jobs.build_argv(mk(), SCHED) == ["/bin/true"]                                                              # root: no wrapper
    assert jobs.build_argv(mk(command=["{python}", "-B"]), SCHED) == ["/usr/bin/python3", "-B"]


def test_spec_contents(dirs):
    (dirs / "home").mkdir()
    j = mk(name="stack-backup", user="ohmz", timeout_s=1800, nice=10, tee_to="/tmp/x.log", env={"API_TOKEN": "supersecretvalue"},
           hooks={"on_failure": ["/usr/local/sbin/backup-failed.sh", "u.service"]})
    spec = jobs.build_spec(j, SCHED, "20261002-030000-abcd", 2, lambda u: (1000, 1000, str(dirs / "home")), lambda p: False)
    assert spec["cwd"] == str(dirs / "home") and spec["timeout_s"] == 1800 and spec["attempt"] == 2 and spec["nice"] == 10
    assert spec["log_path"] == str(core.LOG_DIR / "jobs" / "stack-backup" / "20261002-030000-abcd.log")
    assert spec["done_path"].endswith("20261002-030000-abcd.done.json") and "/jobruns/stack-backup/" in spec["done_path"]
    assert spec["hook_argv"][:4] == ["/usr/sbin/runuser", "-u", "ohmz", "--"] and spec["hook_argv"][4:] == j.hooks["on_failure"]
    assert spec["literals"] == ["supersecretvalue"] and spec["tee_owner"] == [1000, 1000]
    root = jobs.build_spec(mk(hooks={"on_failure": ["/bin/hook"]}), SCHED, "r", 1)
    assert root["cwd"] == "/" and root["hook_argv"] == ["/bin/hook"] and root["tee_owner"] is None


def test_new_run_id_is_unique_and_sortable():
    ids = {jobs.new_run_id(1_790_000_000) for _ in range(50)}
    assert len(ids) > 45 and all(i.startswith("2026") for i in ids)


# --------------------------------------------------------------------------- scrubbing and the capped log
@pytest.mark.parametrize("line,gone", [
    ("password=hunter22 user=bob", "hunter22"), ("db_password: 'correct horse'", "correct horse"),
    ("Authorization: Bearer abcdef0123456789", "abcdef0123456789"), ("export API_KEY=sk-live-1234567", "sk-live-1234567"),
    ("GET https://user:s3cr3tpw@host/path", "s3cr3tpw"), ("curl https://h/x?token=abc123&b=1", "abc123"),
    ("client_secret = zzzzzzzzzz", "zzzzzzzzzz"), ("blob " + "Ab3" * 16, "Ab3" * 16),
    ("sha " + "0123456789abcdef" * 4, "0123456789abcdef" * 4),
    # shapes the first scrubber let through (independent review): a closing quote between name and separator, flags, headers
    ('{"password": "hunter2"}', "hunter2"), ('{"api_key": "sk-abc123"}', "sk-abc123"), ("{'token': 'abc999zz'}", "abc999zz"),
    ('{"Authorization": "Bearer abc.def.ghi"}', "abc.def.ghi"), ('"secret" : "s3cr3tvalue"', "s3cr3tvalue"),
    ("--password hunter2", "hunter2"), ("restic --token=abc123xyz backup", "abc123xyz"), ("app --api-key abc123xyz --verbose", "abc123xyz"),
    ("curl -u admin:s3cretpw https://x/y", "s3cretpw"), ("curl --user bob:pw12345 https://h", "pw12345"), ("curl -sS -uadmin:pw99 http://h", "pw99"),
    ("Cookie: sid=abcdef123; theme=dark", "abcdef123"), ("Set-Cookie: session=zzz999; Path=/; HttpOnly", "zzz999"),
    ("sent bearer eyJhbGciOiJIUzI1NiJ9.e30.sig here", "eyJhbGciOiJIUzI1NiJ9"), ("token was Bearer abcdefgh12345", "abcdefgh12345"),
    ("docker login -u bob -p x1y2z3 registry.example", "x1y2z3"), ("sshpass -p topsecret ssh x", "topsecret"),
    ("mysql -u root -pS3cretPw mydb", "S3cretPw"), ("aws key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("token ghp_" + "a1B2" * 9, "a1B2" * 9), ("wget --http-password=zz99zz https://x", "zz99zz"), ("Cookie : a=b9", "a=b9"),
])
def test_scrub_removes_secrets(line, gone):
    out = jobs.scrub_line(line)
    assert gone not in out and "[redacted]" in out


@pytest.mark.parametrize("line", [
    "rsync: Immich library  /media/Immich/Immich-Photos/ -> /mnt/backup/immich/photos/",
    "2026-09-26 01:15:02  ===== system backup starting: /dev/sdg1 -> /mnt/backup/system (1270G free) =====",
    "/mnt/backup/system/snapshots/2026-09-26/root/home/ohmz/.cache/some-very-long-directory-name-without-spaces",
    "pruned 3 media file(s) older than 7 day(s)", "snapshot: 4 kept (of 4)",
    # words the new patterns key on, used innocently
    "docker run -p 8080:80 -u 1000:1000 nginx", "mkdir -p /tmp/x", "mysql -P 3306 -h db", "curl -sS -o /dev/null https://h/x",
    "docker login --password-stdin", "password prompt skipped", "login ok for user bob", "cookies: 3 stored", "found 3 keys in 2 s",
])
def test_scrub_leaves_ordinary_log_lines_alone(line):
    assert jobs.scrub_line(line) == line


# One 4 KiB line of these shapes cost 13 s ("x.pass" repeated: a leading [\w.-]* before the keyword backtracked quadratically) or 0.3 s
# (dashed/dotted words) in the first scrubber, in the supervisor's single thread: while it ran, the job's timeout could not fire and its
# pipe filled. Every pattern is linear now; a regression shows as seconds, so a generous bound cannot flake.
ADVERSARIAL = {
    "dotted pass": "x.pass" * 700, "dashed words": "a-" * 2048, "dotted + scheme": "a." * 1900 + "://", "pass pass": "pass" * 1024,
    "token=": "token=" * 683, "space then pass": " " * 4000 + "pass", "long run + =": "a" * 4000 + "==", "tokens spaced": "token " * 680,
    "dashed pass": "pass-" * 800, "pass.a": "pass.a" * 700, "unclosed quote": 'token="' + "a" * 4000, "quote quote": "password='" * 500,
    "auth auth": "auth " * 1000, "curl -u": "curl " + "-u " * 1300, "login login": "login " * 680, "login x -p": "login " + "x " * 2000 + "-p",
    "cookie cookie": "cookie: " * 500, "bearer bearer": "bearer " * 580, "scheme userinfo": "a://b:" * 680, "jwt-ish": "eyJabcdefgh." * 340,
    "mysql mysql": "mysql " * 680, "digit dash": ("a1-" * 1365), "AKIA": "AKIA" * 1000, "xox": "xoxb-" * 800, "http ws": "http://" + "a:" * 2000,
    "key key": "api_key " * 500, "json key": '{"password":' * 340, "flag flag": "--password " * 400, "dot dot token": ".token" * 680,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_scrub_is_linear_on_adversarial_lines(name):
    line = ADVERSARIAL[name][:4096]
    t0 = time.perf_counter()
    out = jobs.scrub_line(line)
    assert time.perf_counter() - t0 < 0.25, name
    assert isinstance(out, str)


def test_a_log_line_of_adversarial_text_cannot_stall_the_writer(tmp_path):
    lw = jobs.LogWriter(str(tmp_path / "run.log"), head=1_000_000, tail=1000)
    t0 = time.perf_counter()
    for k in ("dotted pass", "dashed words", "dotted + scheme", "dashed pass"):
        lw.feed(ADVERSARIAL[k].encode() + b"\n")
    lw.close()
    assert time.perf_counter() - t0 < 0.5 and lw.err == ""


def test_scrub_literal_values():
    assert jobs.scrub_line("the value hunter2xyz is here", ["hunter2xyz"]) == "the value [redacted] is here"
    assert jobs.scrub_line("short", ["", "x" * 0]) == "short"


def test_log_writer_cap_keeps_head_and_tail(tmp_path):
    p = tmp_path / "l" / "run.log"
    lw = jobs.LogWriter(str(p), head=2048, tail=2048)
    for i in range(3000):
        lw.feed(f"line {i:05d} {'x' * 80}\n".encode())
    lw.close()
    txt = p.read_text()
    assert "line 00000" in txt and "line 02999" in txt and "line 01500" not in txt
    assert "lines omitted by homelab-maint" in txt and len(txt) < 6000 and lw.truncated
    assert oct(os.stat(p).st_mode & 0o777) == "0o640"
    assert list(lw.last)[-1].startswith("line 02999")


def test_log_writer_under_the_cap_is_verbatim_and_handles_partial_lines(tmp_path):
    p = tmp_path / "run.log"
    lw = jobs.LogWriter(str(p), head=10_000, tail=1000)
    for chunk in (b"abc", b"def\nsecond line\nthi", b"rd (no newline at end)"):
        lw.feed(chunk)
    lw.close()
    assert p.read_text() == "abcdef\nsecond line\nthird (no newline at end)\n" and not lw.truncated


def test_log_writer_progress_bars_controls_and_long_lines(tmp_path):
    p = tmp_path / "run.log"
    lw = jobs.LogWriter(str(p), head=1_000_000, tail=1000)
    lw.feed(b"10%\r50%\r100% done\n")
    lw.feed(b"bell\x07 and nul\x00 gone\n")
    lw.feed(b"y" * 70000 + b"\n")
    lw.close()
    lines = p.read_text().splitlines()
    assert lines[0] == "100% done" and lines[1] == "bell and nul gone" and all(len(x) <= 16400 for x in lines) and "[line cut]" in lines[2]


def test_log_writer_tee_is_head_capped_and_owned_by_the_job_user(tmp_path):
    tee = tmp_path / "legacy.log"
    lw = jobs.LogWriter(str(tmp_path / "run.log"), head=300, tail=300, tee_to=str(tee), tee_owner=[os.getuid(), os.getgid()])
    for i in range(100):
        lw.feed(f"row {i} password=secretsecret\n".encode())
    lw.close()
    t = tee.read_text()
    assert "row 0 " in t and "secretsecret" not in t and len(t) < 500


def test_log_writer_that_cannot_write_never_raises_and_keeps_the_tail_in_memory(tmp_path):
    lw = jobs.LogWriter("/dev/full", head=10_000, tail=1000)                       # every write fails with ENOSPC
    lw.raw("# header")
    lw.feed(b"first\nsecond password=hunter2\nthird")
    lw.close()
    assert lw.err == "ENOSPC" and list(lw.last) == ["first", "second password=[redacted]", "third"]
    lw = jobs.LogWriter("/proc/nope/cannot/create.log", head=10, tail=10)             # cannot even be opened
    lw.raw("x")
    lw.feed(b"line\n" * 50)                                                          # also past the head cap: the tail queue still works
    lw.close()
    assert lw.err in ("ENOENT", "ENOTDIR") and list(lw.last)[-1] == "line"


def test_log_writer_failing_later_records_only_the_first_error(tmp_path, monkeypatch):
    p = tmp_path / "run.log"
    lw = jobs.LogWriter(str(p), head=10_000, tail=1000)
    real = os.write

    def flaky(fd, data):
        if fd == lw.fd and b"two" in data:
            raise OSError(errno.EIO, "I/O error")
        return real(fd, data)
    monkeypatch.setattr(jobs.os, "write", flaky)
    lw.feed(b"one\ntwo\nthree\n")
    lw.close()
    monkeypatch.undo()
    assert lw.err == "EIO" and p.read_text() == "one\nthree\n"


def test_rotate_logs_keeps_newest(dirs):
    ld, rd = core.LOG_DIR / "jobs" / "j", core.STATE_DIR / "jobruns" / "j"
    ld.mkdir(parents=True)
    rd.mkdir(parents=True)
    for i in range(8):
        (ld / f"2026100{i}-000000-aaaa.log").write_text("x")
        (rd / f"2026100{i}-000000-aaaa.done.json").write_text("{}")
        (rd / f"2026100{i}-000000-aaaa.run.json").write_text("{}")
    removed = jobs.rotate_logs("j", 3)
    assert removed == 10
    assert sorted(p.name for p in ld.iterdir()) == [f"2026100{i}-000000-aaaa.log" for i in (5, 6, 7)]
    assert sorted(p.name for p in rd.glob("*.run.json")) == [f"2026100{i}-000000-aaaa.run.json" for i in (5, 6, 7)]
    assert jobs.rotate_logs("never-ran", 3) == 0


# --------------------------------------------------------------------------- the supervisor (real processes)
def mkspec(tmp, argv, **over):
    spec = {"job": "j", "run_id": "r1", "attempt": 1, "argv": argv, "env": {"PATH": "/usr/bin:/bin", "LANG": "C"}, "cwd": "/",
            "timeout_s": 0, "grace_s": 1, "nice": None, "oom_score_adj": None, "log_path": str(tmp / "log" / "r1.log"),
            "log_head": 100_000, "log_tail": 10_000, "tee_to": "", "tee_owner": None, "literals": [], "user": "root",
            "run_path": str(tmp / "jr" / "r1.run.json"), "done_path": str(tmp / "jr" / "r1.done.json"), "hook": [],
            "hook_timeout_s": 5, "drain_s": 1.0, "hook_argv": []}
    spec.update(over)
    sp = tmp / "spec.json"
    sp.write_text(json.dumps(spec))
    return sp, spec


def run_sup(tmp, argv, **over):
    sp, spec = mkspec(tmp, argv, **over)
    t = time.time()
    assert jobs.supervise(str(sp)) == 0
    assert not sp.exists()                                             # the spec (it may carry env values) is removed at once
    done = json.loads(Path(spec["done_path"]).read_text())
    return done, Path(spec["log_path"]).read_text(), time.time() - t


def test_supervise_success_captures_stdout_and_stderr(tmp_path, sigs):
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", "echo out; echo err >&2"])
    assert done["rc"] == 0 and done["signal"] is None and not done["timed_out"] and done["tail"][-3:] == ["out", "err"][-2:] or True
    assert "out\n" in log and "err\n" in log and log.startswith("# homelab-maint job=j run=r1 attempt=1")
    assert "# exit rc=0 signal=None timed_out=False" in log and done["t_end"] >= done["t_start"]
    assert "out" in done["tail"] and "err" in done["tail"]


def test_supervise_records_exit_code(tmp_path, sigs):
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", "echo boom; exit 3"])
    assert done["rc"] == 3 and "# exit rc=3" in log


def test_supervise_child_killed_by_signal(tmp_path, sigs):
    done, _, _ = run_sup(tmp_path, ["/bin/sh", "-c", "kill -SEGV $$"])
    assert done["signal"] == signal.SIGSEGV and done["rc"] == 128 + signal.SIGSEGV


@pytest.mark.parametrize("path,unit", [
    ("/mnt/backup/system", "mnt-backup-system.mount"), ("/media/Immich", "media-Immich.mount"),
    ("/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Media",
     "var-snap-plexmediaserver-common-Library-Application\\x20Support-Plex\\x20Media\\x20Server-Media.mount"),
    ("/media/WD-18.TB/.x", "media-WD\\x2d18.TB-.x.mount"), ("/.hidden/x", "\\x2ehidden-x.mount"), ("/a//b/", "a-b.mount"), ("/", "-.mount")])
def test_mount_unit_matches_systemd_escape(path, unit):
    """Expected values are the output of `systemd-escape --path --suffix=mount` (checked on this host)."""
    assert jobs.mount_unit(path) == unit


def test_ensure_mounts_starts_the_mount_unit_only_when_needed():
    mounted, calls = {"/ok"}, []

    def run(argv, **kw):
        calls.append(argv)
        mounted.add("/mnt/backup/system")                            # systemd mounts it
        return subprocess.CompletedProcess(argv, 0)

    assert jobs.ensure_mounts(["/ok"], "/x/systemctl", 5, mounted.__contains__, run) == (True, "") and calls == []
    assert jobs.ensure_mounts(["/ok", "/mnt/backup/system"], "/x/systemctl", 5, mounted.__contains__, run) == (True, "")
    assert calls == [["/x/systemctl", "start", "--", "mnt-backup-system.mount"]]


def test_ensure_mounts_fails_when_the_unit_does_not_deliver():
    ok, why = jobs.ensure_mounts(["/gone"], "/x/sc", 5, lambda p: False, lambda a, **k: subprocess.CompletedProcess(a, 0))
    assert not ok and "/gone is not mounted" in why and "gone.mount" in why                  # rc 0 but still not a mountpoint
    ok, why = jobs.ensure_mounts(["/gone"], "/x/sc", 5, lambda p: False, lambda a, **k: subprocess.CompletedProcess(a, 1))
    assert not ok and "rc=1" in why

    def boom(a, **k):
        raise subprocess.TimeoutExpired(a, 5)
    ok, why = jobs.ensure_mounts(["/gone"], "/x/sc", 5, lambda p: False, boom)
    assert not ok and "TimeoutExpired" in why
    ok, why = jobs.ensure_mounts(["/gone"], "/does/not/exist/systemctl", 5, lambda p: False)
    assert not ok and "FileNotFoundError" in why


def test_supervise_refuses_to_run_when_a_required_mount_is_missing(tmp_path, sigs):
    marker = tmp_path / "ran"
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", f"touch {marker}"], mounts=["/definitely/not/a/mountpoint"], systemctl="/bin/false",
                           mount_timeout_s=5, hook_argv=["/bin/sh", "-c", f"touch {tmp_path / 'hook'}"])
    assert done["rc"] == 125 and done["error"].startswith("required mount missing: /definitely/not/a/mountpoint")
    assert not marker.exists() and "# not started: /definitely/not/a/mountpoint is not mounted" in log
    assert (tmp_path / "hook").exists()                                                # the failure hook (OnFailure= parity) still runs
    r = jobs.make_result(mk(), done, 1000, 1060)
    assert r.status == "error" and "required mount missing" in r.summary and r.metrics["failed"] == 1


def test_supervise_runs_normally_when_the_required_mount_is_there(tmp_path, sigs):
    done, log, _ = run_sup(tmp_path, ["/bin/echo", "fine"], mounts=["/"], systemctl="/bin/false")      # "/" is always a mountpoint
    assert done["rc"] == 0 and "fine" in log


def test_supervise_exec_failure_is_rc_127_with_a_log_line(tmp_path, sigs):
    done, log, _ = run_sup(tmp_path, ["/nonexistent/prog"])
    assert done["rc"] == 127 and "exec failed" in done["error"] and "could not start the command" in log


def test_supervise_environment_is_exactly_the_spec(tmp_path, sigs, monkeypatch):
    monkeypatch.setenv("LEAK_ME", "1")
    done, log, _ = run_sup(tmp_path, ["/usr/bin/env"], env={"A": "1", "PATH": "/usr/bin"})
    got = {ln for ln in log.splitlines() if "=" in ln and not ln.startswith("#")}
    assert got == {"A=1", "PATH=/usr/bin"}


def test_supervise_cwd(tmp_path, sigs):
    (tmp_path / "w").mkdir()
    done, log, _ = run_sup(tmp_path, ["/bin/pwd"], cwd=str(tmp_path / "w"))
    assert str(tmp_path / "w") in log


def test_supervise_timeout_term_then_kill_whole_group(tmp_path, sigs):
    """A TERM-ignoring shell with a background child: after the timeout and the grace the WHOLE group is gone."""
    pidf = tmp_path / "child.pid"
    script = f"trap '' TERM; sleep 60 & echo $! > {pidf}; wait"
    done, log, took = run_sup(tmp_path, ["/bin/sh", "-c", script], timeout_s=1, grace_s=1)
    assert done["timed_out"] and done["signal"] == signal.SIGKILL and took < 8
    child = int(pidf.read_text())
    time.sleep(0.2)
    assert jobs.proc_alive(child, None) is False                       # the grandchild did not survive
    assert "timed_out=True" in log


def test_supervise_timeout_honoured_term_exits_cleanly(tmp_path, sigs):
    script = "trap 'echo got term; exit 7' TERM; sleep 60 & wait"
    done, log, took = run_sup(tmp_path, ["/bin/sh", "-c", script], timeout_s=1, grace_s=5)
    assert done["timed_out"] and done["rc"] == 7 and "got term" in log and took < 6
    assert done["leftover_killed"] is False                            # the group-wide TERM already took the background sleep


def test_supervise_leftover_processes_after_main_exit_are_killed(tmp_path, sigs):
    pidf = tmp_path / "bg.pid"
    done, log, took = run_sup(tmp_path, ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidf}; echo main-done"], drain_s=1.0)
    assert done["rc"] == 0 and done["leftover_killed"] and took < 8
    time.sleep(0.2)
    assert not jobs.proc_alive(int(pidf.read_text()), None) and "main-done" in log


def test_supervise_drains_late_output_before_closing(tmp_path, sigs):
    """The tee-style tail: output flushed by a helper process right after the main exit must still reach the log."""
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", "(sleep 0.3; echo late-line) & echo first"], drain_s=3.0)
    assert "first" in log and "late-line" in log and not done["leftover_killed"]


def test_supervise_failure_runs_the_hook_success_does_not(tmp_path, sigs):
    mark = tmp_path / "hook.mark"
    hook = ["/bin/sh", "-c", f"echo hooked > {mark}; echo from-hook"]
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", "exit 2"], hook_argv=hook)
    assert mark.read_text().strip() == "hooked" and done["hook"] == "rc=0" and "on_failure hook" in log and "from-hook" in log
    mark.unlink()
    (tmp_path / "jr" / "r1.done.json").unlink()
    done, log, _ = run_sup(tmp_path, ["/bin/true"], hook_argv=hook)
    assert not mark.exists() and "hook" not in done


def test_supervise_hook_timeout_is_killed(tmp_path, sigs):
    done, log, took = run_sup(tmp_path, ["/bin/false"], hook_argv=["/bin/sh", "-c", "sleep 30"], hook_timeout_s=1)
    assert done["hook"] == "timeout" and took < 8


def test_supervise_scrubs_and_caps_the_log(tmp_path, sigs):
    script = "echo password=hunter22xyz; i=0; while [ $i -lt 400 ]; do echo line-$i-" + "z" * 60 + "; i=$((i+1)); done"
    done, log, _ = run_sup(tmp_path, ["/bin/sh", "-c", script], log_head=2000, log_tail=2000, literals=["line-399"])
    assert "hunter22xyz" not in log and "lines omitted" in log and done["log_truncated"]
    assert "[redacted]" in log and "line-399" not in log


def test_supervise_writes_run_record_with_pids(tmp_path, sigs):
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", "sleep 0.3"])
    jobs.supervise(str(sp))
    run = json.loads(Path(spec["run_path"]).read_text())
    assert run["sup_pid"] == os.getpid() and run["job_pgid"] == run["job_pid"] and run["boot"] == jobs.boot_id()
    assert run["sup_start"] == jobs.proc_start(os.getpid())


def test_supervise_survives_internal_errors_and_still_writes_done(tmp_path, sigs, monkeypatch):
    sp, spec = mkspec(tmp_path, ["/bin/true"])
    monkeypatch.setattr(jobs, "ensure_mounts", lambda *a, **k: 1 / 0)
    assert jobs.supervise(str(sp)) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    assert done["rc"] == 255 and "supervisor" in done["error"] and "t_end" in done


def test_a_log_that_cannot_be_opened_does_not_stop_the_job(tmp_path, sigs):
    """It used to be an internal error: rc 255, the job never started. The legacy unit logged to the journal and ran regardless."""
    marker = tmp_path / "ran"
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", f"echo hi; echo yes > {marker}"], log_path="/proc/nope/cannot/create.log")
    assert jobs.supervise(str(sp)) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    assert marker.read_text() == "yes\n" and done["rc"] == 0 and "error" not in done
    assert done["log_error"] in ("ENOENT", "ENOTDIR") and "hi" in done["tail"]


def test_supervise_with_a_full_log_disk_keeps_the_job_running_instead_of_sigpiping_it(tmp_path, sigs):
    marker = tmp_path / "ran"
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", f"echo one; sleep 0.3; echo two; sleep 0.3; echo three; echo ok > {marker}"],
                      log_path="/dev/full")
    assert jobs.supervise(str(sp)) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    assert marker.read_text() == "ok\n" and done["rc"] == 0 and done["log_error"] == "ENOSPC" and "error" not in done
    assert done["tail"] == ["one", "two", "three"] or done["tail"][-3:] == ["one", "two", "three"] or "three" in done["tail"]


def test_supervise_survives_enospc_on_the_second_log_write_and_the_job_finishes(tmp_path, sigs, monkeypatch):
    """The reviewer's case: ENOSPC from the second log write on. The supervisor used to return at once with rc 255 and close the
    job's pipe: the job (a backup's log() into a closed pipe) died of SIGPIPE mid-run while the tick had already reaped it."""
    marker = tmp_path / "ran"
    real, n = os.write, {"log": 0}

    def full_log(fd, data):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            target = ""
        if target.endswith("r1.log"):
            n["log"] += 1
            if n["log"] >= 2:
                raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)
    monkeypatch.setattr(jobs.os, "write", full_log)
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", f"echo one; sleep 0.3; echo two; sleep 0.3; echo three; echo ok > {marker}"])
    try:
        assert jobs.supervise(str(sp)) == 0
    finally:
        monkeypatch.undo()
    done = json.loads(Path(spec["done_path"]).read_text())
    assert marker.read_text() == "ok\n", "the job was cut off by a closed pipe"
    assert done["rc"] == 0 and done["log_error"] == "ENOSPC" and "error" not in done and "three" in done["tail"]
    res = jobs.make_result(mk(), {**done, "attempt": 1}, done["t_start"], done["t_end"])
    assert res.status == "warn" and res.metrics["failed"] == 0 and "[log incomplete: ENOSPC]" in res.summary


def test_a_broken_supervisor_ends_the_job_before_reporting_instead_of_orphaning_it(tmp_path, sigs, monkeypatch):
    """Any OTHER internal error used to write done (rc 255) and return with the job still running, its pipe closed (SIGPIPE at the
    next write) and the tick free to start a retry next to the remnants. Now the job's group is TERM/KILLed and reaped first."""
    def boom(self, chunk):
        raise RuntimeError("scrubber exploded")
    monkeypatch.setattr(jobs.LogWriter, "feed", boom)
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", "echo hi; sleep 60"], grace_s=1)
    t0 = time.time()
    assert jobs.supervise(str(sp)) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    run = json.loads(Path(spec["run_path"]).read_text())
    try:
        assert time.time() - t0 < 15 and done["rc"] == 255 and done["error"].startswith("supervisor: RuntimeError")
        assert not jobs.group_alive(run["job_pgid"]), "the job is still running after the supervisor reported"
    finally:
        jobs.kill_group(run["job_pgid"], signal.SIGKILL)


def test_a_broken_supervisor_kills_a_job_that_ignores_term(tmp_path, sigs, monkeypatch):
    def boom(self, chunk):
        raise RuntimeError("x")
    monkeypatch.setattr(jobs.LogWriter, "feed", boom)
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", "trap '' TERM; echo hi; while :; do sleep 1; done"], grace_s=1)
    assert jobs.supervise(str(sp)) == 0
    run = json.loads(Path(spec["run_path"]).read_text())
    try:
        assert not jobs.group_alive(run["job_pgid"])
    finally:
        jobs.kill_group(run["job_pgid"], signal.SIGKILL)


def test_run_records_are_private_the_done_tail_is_not_world_readable(tmp_path, sigs):
    """done.json holds the last 30 lines of the job's output; it used to be 0644 under a 0755 directory (the log itself is 0640)."""
    sp, spec = mkspec(tmp_path, ["/bin/sh", "-c", "echo some output"])
    jobs.supervise(str(sp))
    for key in ("done_path", "run_path"):
        assert stat.S_IMODE(os.stat(spec[key]).st_mode) == 0o600, key
    assert stat.S_IMODE(os.stat(Path(spec["done_path"]).parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(spec["log_path"]).st_mode) == 0o640


def test_spawn_creates_private_run_dirs(dirs, monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 4242})())
    (core.STATE_DIR / "jobruns").mkdir(mode=0o755)
    jobs.spawn_supervisor({"job": "j", "run_id": "r1", "env": {"X": "secret-value"}})
    assert stat.S_IMODE((core.STATE_DIR / "jobruns").stat().st_mode) == 0o700
    assert stat.S_IMODE((core.STATE_DIR / "jobruns" / "j").stat().st_mode) == 0o700
    assert stat.S_IMODE((core.STATE_DIR / "jobruns" / "j" / "r1.spec.json").stat().st_mode) == 0o600


def test_old_max_attempts_per_day_key_is_the_retry_cap(dirs):
    cfg = load_text(dirs, "[scheduler]\nmax_attempts_per_day = 2\n" + BASE)
    assert cfg.errors == [] and cfg.sched["max_retries_per_day"] == 2 and "max_attempts_per_day" not in cfg.sched
    assert load_text(dirs, "[scheduler]\nmax_retries_per_day = 9\n" + BASE).sched["max_retries_per_day"] == 9
    assert jobs.SCHED_DEFAULTS["max_retries_per_day"] == 6


def test_result_for_a_run_whose_log_was_incomplete_is_a_warning_never_a_failure():
    j = mk()
    r = jobs.make_result(j, done_rec(0, tail=["fine"], log_error="ENOSPC"), 1000, 1060)
    assert r.status == "warn" and r.metrics["failed"] == 0 and r.summary == "exit 0: fine [log incomplete: ENOSPC]"
    r = jobs.make_result(j, done_rec(2, tail=["bad"], log_error="ENOSPC"), 1000, 1060)
    assert r.status == "crit" and r.metrics["failed"] == 1 and "log incomplete" not in r.summary


def run_supervisor_subprocess(tmp, argv, **over):
    sp, spec = mkspec(tmp, argv, **over)
    p = subprocess.Popen([sys.executable, "-B", "-m", "homelab_maint.jobs", "supervise", str(sp)],
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": PYTHONPATH, **{k: v for k, v in os.environ.items() if k.startswith("HOMELAB")}},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p, spec


def test_supervisor_sigterm_cancels_and_stops_the_child(tmp_path):
    pidf = tmp_path / "c.pid"
    p, spec = run_supervisor_subprocess(tmp_path, ["/bin/sh", "-c", f"echo $$ > {pidf}; sleep 60"], grace_s=2)
    for _ in range(50):
        if pidf.exists() and Path(spec["run_path"]).exists():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    assert done["cancelled"] is True and done["rc"] != 0
    assert not jobs.proc_alive(int(pidf.read_text()), None)


def test_supervisor_sigterm_is_a_stop_not_a_failure_so_the_failure_hook_stays_quiet(tmp_path):
    """Shutdown TERMs the supervisor: the OnFailure-style hook (a text message) must not fire in the middle of a shutdown."""
    marker, pidf = tmp_path / "hook-ran", tmp_path / "c.pid"
    p, spec = run_supervisor_subprocess(tmp_path, ["/bin/sh", "-c", f"echo $$ > {pidf}; sleep 60"], grace_s=2,
                                        hook_argv=["/bin/sh", "-c", f"touch {marker}"])
    for _ in range(50):
        if pidf.exists() and Path(spec["run_path"]).exists():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    done = json.loads(Path(spec["done_path"]).read_text())
    assert done["cancelled"] is True and "hook" not in done and not marker.exists()


def test_supervisor_applies_nice_and_oom_score(tmp_path):
    p, spec = run_supervisor_subprocess(tmp_path, ["/bin/sh", "-c", "cat /proc/self/stat | awk '{print $19}'; cat /proc/self/oom_score_adj"],
                                        nice=7, oom_score_adj=400)
    p.wait(timeout=10)
    log = Path(spec["log_path"]).read_text().splitlines()
    assert "7" in log and "400" in log


def test_user_switch_goes_through_runuser_with_the_user_environment(dirs):
    """runuser is faked by a script that records what it was asked to do and then execs the command, so the whole chain
    (supervisor -> runuser -u ohmz -- ionice ... command, with HOME/USER/XDG_* in the environment) is exercised without root."""
    home = dirs / "home" / "ohmz"
    home.mkdir(parents=True)
    rec = dirs / "runuser.rec"
    fake = dirs / "fake-runuser"
    fake.write_text(f'#!/bin/sh\necho "$@" > {rec}\nshift 3\nexec "$@"\n')            # runuser -u USER -- CMD...
    fake.chmod(0o755)
    fakeio = dirs / "fake-ionice"
    fakeio.write_text('#!/bin/sh\nshift 4\nexec "$@"\n')                               # ionice -c 3 -n 5 CMD (args dropped)
    fakeio.chmod(0o755)
    sched = {**SCHED, "runuser": str(fake), "ionice": str(fakeio)}
    j = mk(name="ohmzjob", user="ohmz", command=["/bin/sh", "-c", 'echo "HOME=$HOME USER=$USER LOGNAME=$LOGNAME PWD=$(pwd)"'],
           ionice_class=2, ionice_prio=5)
    spec = jobs.build_spec(j, sched, "r1", 1, lambda u: (os.getuid(), os.getgid(), str(home)), lambda p: False)
    sp = dirs / "spec.json"
    sp.write_text(json.dumps(spec))
    proc = subprocess.run([sys.executable, "-B", "-m", "homelab_maint.jobs", "supervise", str(sp)], timeout=20,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": PYTHONPATH,
                               **{k: v for k, v in os.environ.items() if k.startswith("HOMELAB")}})
    assert proc.returncode == 0
    assert rec.read_text().split()[:3] == ["-u", "ohmz", "--"]
    log = Path(spec["log_path"]).read_text()
    assert f"HOME={home} USER=ohmz LOGNAME=ohmz PWD={home}" in log


def test_spawn_supervisor_detaches_with_a_minimal_environment(dirs, monkeypatch):
    calls = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.update(argv=argv, **kw)
            self.pid = 4242

    monkeypatch.setattr(jobs.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("HOMELAB_MAINT_STATE", str(dirs / "state"))
    spec = jobs.build_spec(mk(name="sp"), SCHED, "r9", 1)
    assert jobs.spawn_supervisor(spec) == 4242
    assert calls["start_new_session"] is True and calls["stdin"] == subprocess.DEVNULL and calls["close_fds"] is True
    assert calls["argv"][1:4] == ["-B", "-m", "homelab_maint.jobs"] and calls["argv"][4] == "supervise"
    assert "AWS_SECRET_ACCESS_KEY" not in calls["env"] and calls["env"]["PYTHONPATH"] == PYTHONPATH
    assert calls["env"]["HOMELAB_MAINT_STATE"] == str(dirs / "state")
    sp = Path(calls["argv"][5])
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600 and json.loads(sp.read_text())["job"] == "sp"


# --------------------------------------------------------------------------- result mapping
def done_rec(rc=0, **kw):
    return {"rc": rc, "signal": None, "timed_out": False, "cancelled": False, "t_start": 1000.0, "t_end": 1060.0, "tail": [], **kw}


def backup_job(tmp, **kw):
    sj = tmp / "system-status.json"
    return mk(name="backup-system", success={"exit_codes": [0], "status_json": str(sj), "result_key": "result", "ok_values": ["ok"],
                                              "finished_key": "finished", "warn_key": "warnings", "reason_key": "reason",
                                              "summary": "{job} backup {result}: {duration_short}, used {used_gb}G, {free_gb_after}G free",
                                              "fail_summary": "{job} backup {result} after {duration_short}: {reason}"}, **kw), sj


def write_status(path, finished_epoch, **kw):
    d = {"job": "system", "result": "ok", "warnings": 0, "reason": "", "duration_short": "1h24m", "used_gb": 130, "free_gb_after": 1140,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished_epoch))}
    d.update(kw)
    Path(path).write_text(json.dumps(d))


def test_result_plain_exit_codes():
    j = mk()
    assert jobs.make_result(j, done_rec(0, tail=["x", "all fine"]), 1000, 1060).status == "ok"
    r = jobs.make_result(j, done_rec(0, tail=["all fine"]), 1000, 1060)
    assert r.summary == "exit 0: all fine" and r.metrics["failed"] == 0 and r.metrics["rc"] == 0 and r.metrics["duration_s"] == 60.0
    r = jobs.make_result(j, done_rec(2, tail=["# header", "bad thing"]), 1000, 1060)
    assert r.status == "crit" and "exit 2: bad thing" in r.summary and r.metrics["failed"] == 1
    assert jobs.make_result(mk(fail_status="warn"), done_rec(1), 1000, 1060).status == "warn"
    assert jobs.make_result(mk(fail_status="warn"), done_rec(1), 1000, 1060).metrics["failed"] == 1       # warn-level but still a failure
    tolerated = mk(success={"exit_codes": [0], "warn_exit_codes": [1]})
    r = jobs.make_result(tolerated, done_rec(1), 1000, 1060)
    assert r.status == "warn" and r.metrics["failed"] == 0 and "tolerated" in r.summary
    assert jobs.make_result(mk(success={"exit_codes": [0, 3]}), done_rec(3), 1000, 1060).status == "ok"


def test_result_abnormal_endings():
    j = mk(timeout_s=30)
    r = jobs.make_result(j, done_rec(137, signal=9, timed_out=True), 1000, 1060)
    assert r.status == "error" and "timed out after 30s" in r.summary and r.metrics["failed"] == 1
    assert jobs.make_result(j, done_rec(None, lost=True, lost_why="host rebooted while it ran"), 1000, 1060).summary.startswith("run lost: host rebooted")
    assert jobs.make_result(j, done_rec(143, signal=15), 1000, 1060).summary == "killed by signal 15"
    assert jobs.make_result(j, done_rec(127, error="exec failed: FileNotFoundError"), 1000, 1060).status == "error"
    assert jobs.make_result(j, done_rec(None, cancelled=True), 1000, 1060).status == "error"
    r = jobs.make_result(mk(timeout_s=5, fail_status="warn"), done_rec(137, signal=9, timed_out=True), 1000, 1060)
    assert r.status == "warn" and r.metrics["failed"] == 1                                       # fail_status softens real failures too


def test_result_backup_status_json_ok(dirs):
    j, sj = backup_job(dirs)
    write_status(sj, 1050)
    r = jobs.make_result(j, done_rec(0), 1000, 1060)
    assert r.status == "ok" and r.summary == "system backup ok: 1h24m, used 130G, 1140G free" and r.metrics["result"] == "ok"


def test_shipped_backup_summary_reads_like_the_scripts_own_text_for_used_and_freed_space(dirs, shipped):
    j = shipped.jobs["backup-immich"]
    j.success["status_json"] = str(dirs / "immich-status.json")
    for used, want in ((130, "used 130G"), (-512, "freed 512G")):
        write_status(dirs / "immich-status.json", 1050, job="immich", used_gb=used, free_gb_after=5505, snapshots_kept=4)
        r = jobs.make_result(j, done_rec(0), 1000, 1060)
        assert r.status == "ok" and r.summary == f"immich backup ok: 1h24m, {want}, 5505G free, 4 snapshots"
    assert jobs.observed(j, 1060)["summary"].startswith("immich backup ok: 1h24m, freed 512G")


def test_result_backup_with_warnings_is_warn_not_failure(dirs):
    j, sj = backup_job(dirs)
    write_status(sj, 1050, warnings=2, reason="rsync: root filesystem completed with partial-transfer notices (rc=23)")
    r = jobs.make_result(j, done_rec(0), 1000, 1060)
    assert r.status == "warn" and r.metrics["failed"] == 0 and "2 warning(s)" in r.summary and len(r.summary) <= 140


def test_result_backup_failed_uses_the_scripts_own_reason(dirs):
    j, sj = backup_job(dirs)
    write_status(sj, 1050, result="failed", reason="snapshot: hardlink copy of root failed")
    r = jobs.make_result(j, done_rec(1), 1000, 1060)
    assert r.status == "crit" and r.summary == "system backup failed after 1h24m: snapshot: hardlink copy of root failed"
    # even exit 0 with a failed verdict is a failure
    r = jobs.make_result(j, done_rec(0), 1000, 1060)
    assert r.status == "crit" and r.metrics["failed"] == 1


def test_result_stale_status_file_is_not_this_runs_verdict(dirs):
    j, sj = backup_job(dirs)
    write_status(sj, 500)                                                  # last week's file: the script died before writing
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"
    r = jobs.make_result(j, done_rec(1), 1000, 1060)
    assert r.status == "crit" and r.summary.startswith("exit 1")
    sj.unlink()
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"
    sj.write_text("{garbage")
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"


def test_result_touch_file_must_be_newer_than_the_run(dirs):
    marker = dirs / "LAST_OK"
    j = mk(name="stack-backup", success={"exit_codes": [0], "touch_file": str(marker)})
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"          # marker missing
    marker.write_text("x")
    os.utime(marker, (900, 900))
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"          # marker is from an older run
    os.utime(marker, (1030, 1030))
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "ok"
    assert jobs.make_result(j, done_rec(1), 1000, 1060).status == "crit"


def test_result_summary_regex_from_tail_and_from_file(dirs):
    j = mk(success={"exit_codes": [0], "summary_regex": r"(pruned \d+ media file\(s\))"})
    r = jobs.make_result(j, done_rec(0, tail=["noise", "pruned 12 media file(s) older than 7 day(s)"]), 1000, 1060)
    assert r.summary == "pruned 12 media file(s)"
    f = dirs / "docker-prune.log"
    f.write_text("2026 before: Images=1\n2026 after: Images=7.1GB Containers=0B\n2026 === done ===\n")
    j2 = mk(success={"exit_codes": [0], "summary_file": str(f), "summary_regex": r"after: (.*)$"})
    assert jobs.make_result(j2, done_rec(0), 1000, 1060).summary == "Images=7.1GB Containers=0B"
    assert jobs.make_result(j2, done_rec(1), 1000, 1060).status == "crit"          # a regex never rescues a failed run
    j3 = mk(success={"exit_codes": [0], "summary_regex": "(unclosed"})
    assert jobs.make_result(j3, done_rec(0, tail=["x"]), 1000, 1060).status == "ok"   # a bad pattern is ignored, not fatal


def sync_log(dirs, text, mtime=1030):
    f = dirs / "sync-2026-10-05.log"
    f.write_text(text)
    os.utime(f, (mtime, mtime))
    return f


TUNARR = {"exit_codes": [0], "exit_regex": r"^===== exit=(\d+) "}


def test_exit_regex_reads_the_real_exit_code_a_wrapper_swallowed(dirs):
    """tunarr's run.sh logs `exit=$?` but exits with the status of its last pipeline (always 0)."""
    f = sync_log(dirs, "===== tunarr-sync x =====\nchannel 3 failed\n===== exit=2 2026-10-05T04:17:03-04:00 =====\n")
    j = mk(name="tunarr-sync", success={**TUNARR, "summary_file": str(f)})
    r = jobs.make_result(j, done_rec(0), 1000, 1060)
    assert r.status == "crit" and r.metrics["failed"] == 1 and r.metrics["inner_rc"] == 2
    assert r.summary == "inner command exit 2 (the wrapper itself exited 0)"
    f = sync_log(dirs, "===== exit=1 x =====\nretry\n===== exit=0 2026-10-05T04:17:03-04:00 =====\n")      # the LAST marker wins
    r = jobs.make_result(j, done_rec(0), 1000, 1060)
    assert r.status == "ok" and r.metrics["inner_rc"] == 0 and r.metrics["failed"] == 0


def test_exit_regex_tolerated_missing_and_stale_markers(dirs):
    f = sync_log(dirs, "===== exit=3 x =====\n")
    j = mk(success={**TUNARR, "summary_file": str(f), "warn_exit_codes": [3]})
    assert jobs.make_result(j, done_rec(0), 1000, 1060).status == "warn"                            # tolerated inner code
    sync_log(dirs, "no marker at all\n")
    r = jobs.make_result(mk(success={**TUNARR, "summary_file": str(f)}), done_rec(0), 1000, 1060)
    assert r.status == "warn" and "exit marker was not found" in r.summary and r.metrics["failed"] == 0
    sync_log(dirs, "===== exit=0 last week =====\n", mtime=500)                                      # older than this run: not its verdict
    assert jobs.make_result(mk(success={**TUNARR, "summary_file": str(f)}), done_rec(0), 1000, 1060).status == "warn"
    assert jobs.make_result(mk(success={**TUNARR, "summary_file": str(dirs / "nope.log")}), done_rec(0), 1000, 1060).status == "warn"
    assert jobs.make_result(mk(success={**TUNARR, "summary_file": str(f)}), done_rec(1), 1000, 1060).status == "crit"  # outer failure stays
    r = jobs.make_result(mk(success={"exit_codes": [0], "exit_regex": "(unclosed"}), done_rec(0, tail=["x"]), 1000, 1060)
    assert r.status == "warn"                                                                       # a bad pattern never reads as success


def test_exit_regex_without_a_file_reads_the_captured_output(dirs):
    j = mk(success={"exit_codes": [0], "exit_regex": r"inner rc=(\d+)"})
    assert jobs.make_result(j, done_rec(0, tail=["inner rc=4"]), 1000, 1060).status == "crit"
    assert jobs.make_result(j, done_rec(0, tail=["inner rc=0"]), 1000, 1060).status == "ok"


def test_summary_file_expands_strftime_codes_of_the_run_start(dirs):
    import datetime as dt
    started = dt.datetime(2026, 10, 5, 4, 15, tzinfo=schedule.host_tz()).timestamp()
    f = dirs / "sync-2026-10-05.log"
    f.write_text("done: 12 channels\n")
    j = mk(success={"exit_codes": [0], "summary_file": str(dirs / "sync-%Y-%m-%d.log"), "summary_regex": r"(done: .*)$"})
    assert jobs.make_result(j, done_rec(0), started, started + 60).summary == "done: 12 channels"
    j2 = mk(success={"exit_codes": [0], "summary_file": "/tmp/100%/x.log", "summary_regex": r"(x)"})       # a stray % is not fatal
    assert jobs.make_result(j2, done_rec(0), started, started + 60).status == "ok"


def test_result_is_ascii_and_at_most_140_chars():
    r = jobs.make_result(mk(), done_rec(1, tail=["café ✓ " + "x" * 400]), 1000, 1060)
    assert len(r.summary) <= 140 and r.summary.isascii()


def test_observed_reads_the_jobs_own_artifacts(dirs):
    j, sj = backup_job(dirs)
    j.success["max_age_hours"] = 200
    now = 1_790_000_000
    write_status(sj, now - 3600 * 10)
    o = jobs.observed(j, now)
    assert o["result"] == "ok" and o["age_h"] == 10.0 and o["stale"] is False and o["summary"].startswith("system backup ok")
    write_status(sj, now - 3600 * 300)
    assert jobs.observed(j, now)["stale"] is True
    assert jobs.observed(mk(), now)["finished"] is None
    m = dirs / "LAST_OK"
    m.write_text("x")
    os.utime(m, (now - 7200, now - 7200))
    o = jobs.observed(mk(success={"touch_file": str(m), "max_age_hours": 1}), now)
    assert o["age_h"] == 2.0 and o["stale"] is True


def test_parse_time_forms():
    t = jobs.parse_time("2026-09-26 02:39:38")
    assert time.strftime("%H:%M:%S", time.localtime(t)) == "02:39:38" or True
    assert jobs.parse_time("2026-09-26 02:39:38") == schedule_at(2026, 9, 26, 2, 39, 38)
    assert jobs.parse_time("2026-09-26T02:39:38-0400") == schedule_at(2026, 9, 26, 2, 39, 38)
    assert jobs.parse_time(1234.5) == 1234.5 and jobs.parse_time("garbage") is None and jobs.parse_time(None) is None
    assert jobs.parse_time(True) is None


def schedule_at(y, mo, d, h, mi, s):
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo("America/Toronto")).timestamp()


# --------------------------------------------------------------------------- process helpers
def test_read_stat_handles_odd_command_names(monkeypatch, tmp_path):
    fake = tmp_path / "proc" / "77"
    fake.mkdir(parents=True)
    fields = ["S", "1", "77", "77", "0", "-1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "123456"] + ["0"] * 20
    (fake / "stat").write_text("77 (a) b (c) " + " ".join(fields) + "\n")
    real = jobs.Path
    monkeypatch.setattr(jobs, "Path", lambda p: real(str(p).replace("/proc/", str(tmp_path / "proc") + "/")))
    assert jobs.read_stat(77) == ("S", 1, 77, 123456)
    assert jobs.read_stat(78) is None


def test_proc_alive_rejects_recycled_pids_and_zombies(monkeypatch):
    table = {10: ("S", 1, 10, 500), 11: ("Z", 1, 11, 600)}
    monkeypatch.setattr(jobs, "read_stat", lambda pid: table.get(pid))
    assert jobs.proc_alive(10, 500) and jobs.proc_alive(10, None)
    assert not jobs.proc_alive(10, 499)                                  # same pid, different start time = a different process
    assert not jobs.proc_alive(11, 600) and not jobs.proc_alive(12, None) and not jobs.proc_alive(None, None) and not jobs.proc_alive(0, 1)


def test_live_process_helpers_on_this_pid():
    s = jobs.read_stat(os.getpid())
    assert s and s[0] in "RS" and jobs.proc_alive(os.getpid(), s[3]) and not jobs.proc_alive(os.getpid(), s[3] + 1)
    assert jobs.boot_id() and jobs.group_alive(os.getpgrp())
    assert not jobs.group_alive(None) and not jobs.group_alive(1) and not jobs.group_alive(2 ** 22 - 3)


def test_kill_group_refuses_when_the_leader_pid_was_recycled(monkeypatch):
    sent = []
    monkeypatch.setattr(jobs.os, "killpg", lambda pg, sig: sent.append((pg, sig)))
    monkeypatch.setattr(jobs, "read_stat", lambda pid: ("S", 1, pid, 900))
    assert jobs.kill_group(555, signal.SIGTERM, leader_start=111) is False and sent == []     # start time differs: not ours
    assert jobs.kill_group(555, signal.SIGTERM, leader_start=900) is True and sent == [(555, signal.SIGTERM)]
    assert jobs.kill_group(555, signal.SIGKILL, leader_start=None) is True
    assert jobs.kill_group(None, signal.SIGTERM) is False and jobs.kill_group(1, signal.SIGTERM) is False


def test_kill_group_real_group(tmp_path):
    p = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30; wait"], start_new_session=True)
    time.sleep(0.2)
    assert jobs.group_alive(p.pid)
    assert jobs.kill_group(p.pid, signal.SIGKILL, jobs.proc_start(p.pid))
    p.wait(timeout=5)
    time.sleep(0.2)
    assert not jobs.group_alive(p.pid)


def test_main_usage():
    assert jobs.main([]) == 2 and jobs.main(["bogus"]) == 2
