"""Tests for homelab_maint/acks.py and etc/ack.toml (SPEC5): acknowledged known issues.

What is covered
  * Fingerprints: stable across the numbers, sizes, ages, orders and "+N more" tails that merely MOVE, different for genuinely different
    errors (another unit, mount, backup, probe, disk, container), on the REAL summary formats of every task (including a live check run
    of this host), Result / status entry / string agree, explicit issue_key wins, hostile input never raises, etc/ack.toml equals the
    baseline in the code and every rule names a registered task and compiles.
  * Severity ceiling and escalation, expiry to the second, unacknowledge, the 90-day default, caps, the status.json flags (`fp`, `acked`,
    `overall` without acked tasks), the suppressed counter.
  * Tokens: 256-bit, only the SHA-256 is stored/exported/logged (the plaintext is searched for in EVERY file the run leaves behind),
    single use, expiry, binding to fingerprint + severity, forgery, no consumption on a refused request.
  * The inbox: HMAC (tamper with every field, wrong/missing/unsafe key), replay (also after a crash between save and delete), +-10 min
    freshness, size limit, malformed/odd JSON, strict schema, symlinks/FIFOs/directories/stray names, rate limit, per-run cap, quarantine
    (named by a fixed reason, capped, 0600), concurrent processors (flock), a crash in the middle of a write, a corrupt or unreadable
    store, the handler registry.
  * Expiry and the single notice bookkeeping, the public exports (shape, perms, nothing secret), the command line, and the hand-off to
    notify (real notify.send with a fake transport: held, escalation breaks it, expiry notice, core.Notifier state stays consistent).
Everything runs on tmp dirs. Any subprocess call fails the test and the real notification transports are replaced by a fake."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import copy
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import threading
import time
import tomllib
import types
from pathlib import Path

import pytest

from homelab_maint import acks, core, notify

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_790_000_000.0                                   # 2026-09-21 13:33 UTC
DAY = 86400.0
_REAL = {n: getattr(notify, n) for n in ("send", "enqueue", "flush_pending", "notifier_send", "notify_expired")}    # banned by the autouse fixture;
_REAL_SEND = _REAL["send"]                              # the integration tests put them back, with a fake transport
BASE_ORIG = copy.deepcopy(acks._BASE)                   # the shipped baseline, before the fixture below relaxes the one policy switch


# =========================================================================== world
@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    """tmp STATE/LOG/CONF/RUN, no subprocess, no real transport, the audit log captured instead of logger(1)."""
    for name, sub in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("CONF_DIR", "conf"), ("RUN_DIR", "run")):
        (tmp_path / sub).mkdir()
        monkeypatch.setattr(core, name, tmp_path / sub)
    # Most tests below acknowledge made-up tasks ("t", "anything_new"): the baseline's `require_rule = true` (only exact-error tasks may be
    # acknowledged) is lifted for them. TestPolicy / TestReviewRegressions put it back with an override file, which wins over this.
    monkeypatch.setitem(acks._BASE["ack"], "require_rule", False)
    # Likewise the shipped severity policy (["warn"]) is widened here: many tests below acknowledge a crit issue to exercise tokens, the
    # inbox and the caps, not the policy. Tests of the policy itself write [ack] severities back and win over this.
    monkeypatch.setitem(acks._BASE["ack"], "severities", ["warn", "crit"])

    def banned(*a, **k):
        raise AssertionError(f"acks must never run a command or send anything: {a[:1]}")
    monkeypatch.setattr(subprocess, "run", banned)
    monkeypatch.setattr(subprocess, "Popen", banned)
    monkeypatch.setattr(core, "sh", banned)
    for fn in ("send", "enqueue", "flush_pending", "hermes_transport", "bridge_transport", "notifier_send", "notify_expired"):
        monkeypatch.setattr(notify, fn, banned)
    log: list[tuple] = []
    monkeypatch.setattr(core, "audit", lambda *a: log.append(a))
    return types.SimpleNamespace(tmp=tmp_path, state=tmp_path / "state", conf=tmp_path / "conf", log=log)


def write_status(tasks: dict, now: float = NOW) -> None:
    core.write_json_atomic(core.STATE_DIR / "status.json", {"schema": 1, "generated_at": now, "tasks": tasks})


def ent(status: str, summary: str, title: str = "T", **kw) -> dict:
    return {"title": title, "klass": "C0", "tier": "check", "status": status, "summary": summary, "alert": True, "last_run": NOW, **kw}


def fp_of(task: str, summary: str, sev: str = "warn", **kw) -> str:
    return str(acks.fingerprint(task, ent(sev, summary, **kw), sev))


def ready() -> bytes:
    """Create the ack directory tree and return the HMAC key."""
    acks.init_dirs()
    return (core.STATE_DIR / "ack" / "web.key").read_text().strip().encode()


def inbox() -> Path:
    return core.STATE_DIR / "ack" / "inbox"


def submit(req: dict, key: bytes | None = None, *, name: str | None = None, sign: bool = True, raw: bytes | None = None) -> str:
    """Drop one request into the inbox the way the container does: temp name, then rename. Returns the file name."""
    if raw is None:
        req = dict(req)
        if sign and key is not None:
            req["sig"] = acks.sign(req, key)
        raw = json.dumps(req).encode()
    name = name or f"{int(NOW * 1000)}-{os.urandom(4).hex()}.json"
    tmp = inbox() / f".w-{os.urandom(3).hex()}"
    tmp.write_bytes(raw)
    os.replace(tmp, inbox() / name)
    return name


def web_req(fp: str, sev: str = "warn", days: int = 30, note: str = "", ts: float = NOW, **kw) -> dict:
    return {"v": 1, "kind": "ack", "source": "web", "fp": fp, "severity": sev, "days": days, "note": note, "ts": ts, **kw}


def email_req(token: str, ts: float = NOW, **kw) -> dict:
    return {"v": 1, "kind": "ack", "source": "email", "token_hash": acks.token_hash(token), "ts": ts, **kw}


def run_inbox(now: float = NOW):
    return acks.process_inbox(now)


def store() -> dict:
    return json.loads((core.STATE_DIR / "acks.json").read_text())


def audit_events() -> list[dict]:
    p = core.STATE_DIR / "acks.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def rejected_files() -> list[Path]:
    d = inbox() / "rejected"
    return sorted(d.iterdir()) if d.is_dir() else []


# the REAL summaries of this host's check tier (a live read-only run, scratch dirs) and the formats the task code builds
LIVE = {
    "alert_path_health": ("ok", "alert path ok; last SMART send 10-02 12:27 (1 earlier failures recovered)"),
    "backup_freshness": ("ok", "ok: immich 5d15h, system 6d14h, stack-backup 13h31m"),
    "disk_forecast": ("ok", "ok: 6 mounts watched; lowest free /mnt/backup/system 22% (1.1 TiB)"),
    "docker_df": ("ok", "ok: images 96.6 GiB (13.1 GiB reclaimable), build cache 2.2 GiB, volumes 25.5 GiB"),
    "docker_prune_exposure": ("crit", "docker-prune.timer ON (next in 35.2 h): deletes container comfyui + image comfyui-local:tier2-gcc (14.9 GiB); natives keep"),
    "failed_units": ("ok", "ok: no failed units, 73 containers fine"),
    "growth_watch": ("info", "growth: measuring 3 paths (rates need 12 h of history); 1/4 paths unmeasured (missing/no access/cut off)"),
    "memory_health": ("ok", "ok: 71.3 GiB avail; cache 38.4 GiB is reclaimable; swap 30.4 GiB used is harmless alone; mem stall 0.1%, io 13%"),
    "orphan_report": ("info", "1 zombie (0 B anon)"),
    "os_jobs": ("ok", "OS jobs: 13/13 on schedule (0 not installed)"),
    "plex_media_mount_check": ("ok", "ok: Plex Media is mounted from /media/SandiskSSD/plex/Media"),
    "pressure_state": ("ok", "L0 normal: mem stall 0.1%, io wait 13%, 71.4 GiB avail"),
    "smart_trend": ("ok", "SMART ok: 10 disks, no 5/197/198/CRC growth, hottest nvme0 51C"),
    "stuck_detector": ("info", "collecting samples: 1/6"),
    "surrealdb_health": ("ok", "SurrealDB ok: WAL 38 MB, store 2 GB, 972 GB free, oom 0, restarts 0"),
}

# (task, summary A, summary B): the SAME error, only things that move differ
SAME = [
    ("disk_forecast", "warn: /mnt/x 4% free (210.0 GiB), full in 5d; /home 9% free (1.0 TiB); +2 more",
     "warn: /home 11% free (900.0 GiB); /mnt/x 3% free (190.0 GiB), full in 2d; +2 more"),
    ("disk_forecast", "crit: / 3% free (20.1 GiB)", "crit: / 4% free (24.0 GiB), full in 6d"),
    ("failed_units", "warn: 2 failed unit(s): a.service, b.service; 1 unhealthy: kavita; 2 restarting: x, y",
     "warn: 2 failed unit(s): b.service, a.service; 1 unhealthy: kavita; 2 restarting: y, x"),
    ("backup_freshness", "crit: backup-system FAILED (6d10h ago); stack-backup 28h old (limit 26h)",
     "crit: backup-system FAILED (6d11h ago); stack-backup 29h old (limit 26h)"),
    ("memory_health", "warn: memory stall 6.1%, 8.0 GiB available (30.0 GiB avail; cache 38.4 GiB is reclaimable; io 13%)",
     "warn: memory stall 7.9%, 7.0 GiB available (31.0 GiB avail; cache 38.4 GiB is reclaimable; io 3%)"),
    ("docker_prune_exposure", LIVE["docker_prune_exposure"][1],
     "docker-prune.timer ON (next in 12.0 h): deletes container comfyui + image comfyui-local:tier2-gcc (14.9 GiB); natives keep"),
    ("smart_trend", "SMART: sda realloc +2, 72C; nvme0 70C (+1 more)", "SMART: nvme0 71C; sda realloc +5, 73C (+1 more)"),
    ("os_jobs", "OS jobs: 2 of 13 need attention: logrotate overdue; fstrim failed (reason)",
     "OS jobs: 2 of 13 need attention: fstrim failed (other reason); logrotate overdue"),
    ("growth_watch", "growth over limit: kavita/logs +1.20 GiB/d, immich/x 2.30 GiB/d written; 1/4 paths unmeasured (missing/no access/cut off)",
     "growth over limit: immich/x 3.30 GiB/d written, kavita/logs +1.50 GiB/d; 2/4 paths unmeasured (missing/no access/cut off)"),
    ("growth_watch", "growth blind >24h: 2/4 paths unmeasured (missing/no access/cut off): a/b, c/d",
     "growth blind >24h: 2/5 paths unmeasured (missing/no access/cut off): c/d, a/b"),
    ("probes", "4 down: Plex, Radarr, Sonarr +1; 1 degraded: X; 25/27 up", "4 down: Sonarr, Radarr, Plex +1; 1 degraded: X; 24/27 up, 1 skipped"),
    ("surrealdb_health", "SurrealDB: WAL is 1500 MB; container hit its memory cap 2x", "SurrealDB: WAL is 1900 MB; container hit its memory cap 3x"),
    ("alert_path_health", "alert path: bridge missing; 3 smart hook sends failed rc=1: boom", "alert path: bridge missing; 5 smart hook sends failed rc=2: other error"),
    ("stuck_detector", "2 candidate(s), 1 actionable: tunarr 4.1 GiB leak/runaway; mem pressure crit",
     "3 candidate(s), 1 actionable: tunarr 6.0 GiB leak/runaway"),
    ("orphan_report", "1 idle gradle daemon, 3 zombies (1.2 GiB anon)", "2 idle gradle daemons, 9 zombies (3.4 GiB anon)"),
    ("plex_media_mount_check", "crit: Media dir is not mounted (SSD path exists); Plex would regenerate Media on the root disk (12.0 GiB there now)",
     "crit: Media dir is not mounted (SSD path vanished); Plex would regenerate Media on the root disk"),
    ("plex_media_mount_check", "warn: Plex Media mounted from /dev/sdh1, expected under /media/SandiskSSD/plex",
     "warn: Plex Media mounted from /dev/sdg1, expected under /media/SandiskSSD/plex"),
    ("pressure_state", "L2 memory squeeze: memory stall 6.1%, 8.0 GiB avail; top: tunarr P3", "L2 memory squeeze: memory stall 7.9%, 7.0 GiB avail; top: ollama P1"),
    ("pressure_response", "L3 slow batch: 1 done, 1 failed", "L2 reclaim: 3 done, 2 failed"),
    ("docker_df", "warn: build cache 30.1 GiB (>= 25 GiB)", "warn: images reclaimable 45.0 GiB (>= 40 GiB)"),
    ("tool_caches", "cap reached: tool_caches: per-run cap reached (5.0 GiB / 40 items)", "cap reached: tool_caches: per-run cap reached (6.2 GiB / 41 items)"),
    ("anything_new", "warn: 12 files older than 30 days in /var/x (4.0 GiB) at 12:30", "warn: 15 files older than 31 days in /var/x (5.0 GiB) at 13:45"),
    ("anything_new", "error: timed out after 120s", "error: timed out after 90s"),
]
# (task, summary A, summary B): genuinely DIFFERENT errors
DIFFERENT = [
    ("disk_forecast", "warn: /mnt/x 4% free (210.0 GiB)", "warn: /mnt/y 4% free (210.0 GiB)"),
    ("disk_forecast", "warn: /mnt/x 4% free (210.0 GiB)", "warn: /mnt/x 4% free (210.0 GiB); /home 9% free (1.0 TiB)"),
    ("failed_units", "warn: 1 failed unit(s): a.service", "warn: 1 failed unit(s): b.service"),
    ("failed_units", "warn: 1 unhealthy: kavita", "warn: 1 restarting: kavita"),
    ("failed_units", "warn: 1 unhealthy: kavita", "warn: 1 unhealthy: kavita; 1 failed unit(s): a.service"),
    ("backup_freshness", "crit: backup-system FAILED (1d ago)", "crit: backup-immich FAILED (1d ago)"),
    ("memory_health", "warn: memory stall 6.1%, 8.0 GiB available (x)", "warn: memory stall 6.1% (x)"),
    ("memory_health", "warn: memory stall 6.1% (x)", "warn: 2 OOM kill(s) in last 6h (x)"),
    ("smart_trend", "SMART: nvme0 70C", "SMART: nvme1 70C"),
    ("smart_trend", "SMART: sda 72C", "SMART: sda Reallocated_Sector_Ct +2, 72C"),
    ("os_jobs", "OS jobs: 1 of 13 need attention: logrotate overdue", "OS jobs: 1 of 13 need attention: fstrim overdue"),
    ("os_jobs", "OS jobs: 1 of 13 need attention: fstrim overdue", "OS jobs: 1 of 13 need attention: fstrim failed (x)"),
    ("growth_watch", "growth over limit: kavita/logs +1.20 GiB/d", "growth over limit: immich/x +1.20 GiB/d"),
    ("growth_watch", "growth over limit: kavita/logs +1.20 GiB/d", "growth blind >24h: 1/4 paths unmeasured (missing/no access/cut off): kavita/logs"),
    ("probes", "1 down: Plex; 25/27 up", "1 down: Radarr; 25/27 up"),
    ("probes", "1 down: Plex; 25/27 up", "1 degraded: Plex; 25/27 up"),
    ("stuck_detector", "1 candidate(s), 1 actionable: tunarr 4.1 GiB leak", "1 candidate(s), 1 actionable: ollama 4.1 GiB leak"),
    ("orphan_report", "1 zombie (0 B anon)", "1 stray server (0 B anon)"),
    ("docker_prune_exposure", "docker-prune.timer ON (next in 35.2 h): deletes container comfyui; natives keep",
     "docker-prune.timer ON (next in 35.2 h): deletes container tunarr; natives keep"),
    ("plex_media_mount_check", "crit: Media dir is not mounted (SSD path exists)", "warn: Plex Media mounted from /dev/sdh1, expected under /x"),
    ("surrealdb_health", "SurrealDB: WAL is 1500 MB", "SurrealDB: store is 30.1 GB (limit 25 GB)"),
    ("anything_new", "warn: /var/a is full", "warn: /var/b is full"),
    ("anything_new", "warn: disk sda is failing", "warn: disk sdb is failing"),
    ("anything_new", "warn: nvme0 hot", "warn: nvme1 hot"),
    ("failed_units", "warn: 1 unhealthy: kavita", "warn: 1 unhealthy: kavita2"),
]


# =========================================================================== configuration
class TestConfig:
    def test_baseline_is_valid_and_complete(self):
        cfg, errors = acks.load_config(True)
        assert errors == [] and cfg["ack"]["days"] == 90 and cfg["ack"]["escalation_breaks"] is True
        assert cfg["inbox"]["require_signature"] is True and cfg["inbox"]["unsigned_sources"] == []
        for task, rule in cfg["key"].items():
            assert rule is not None and rule["mode"] in ("text", "task", "regex"), task
            assert rule["mode"] != "regex" or (1 <= len(rule["regex"]) <= 10 and all(re.compile(p, re.I).groups >= 1 for p in rule["regex"])), task

    def test_the_shipped_template_equals_the_baseline_in_the_code(self):
        """etc/ack.toml is the commented copy of the baseline: a rule changed in one place only would silently fork the fingerprints."""
        shipped = tomllib.loads((ROOT / "etc" / "ack.toml").read_text())
        assert shipped == BASE_ORIG
        assert acks.validate() == []

    def test_the_shipped_policy_acknowledges_warnings_only(self):
        """SPEC5: a critical issue must keep alerting, so the shipped policy acknowledges warn and never crit. The world fixture
        widens this key for the tests that exercise tokens/inbox/caps, so read the shipped values directly."""
        assert BASE_ORIG["ack"]["severities"] == ["warn"]                                                # the baseline in the code
        assert tomllib.loads((ROOT / "etc" / "ack.toml").read_text())["ack"]["severities"] == ["warn"]    # the shipped file
        ship = {"ack": {"severities": ["warn"]}}
        assert acks.severity_allowed("warn", ship) and not acks.severity_allowed("crit", ship)
        assert not acks.severity_allowed("error", ship) and not acks.severity_allowed("", ship)
        assert acks.severity_allowed("crit", {"ack": {"severities": ["warn", "crit"]}})                   # an override restores it

    def test_installing_the_template_changes_nothing(self, world, monkeypatch):
        monkeypatch.setitem(acks._BASE["ack"], "require_rule", True)                   # the shipped value (the fixture relaxes it)
        monkeypatch.setitem(acks._BASE["ack"], "severities", ["warn"])                 # likewise
        base = acks.load_config()
        (world.conf / "ack.toml").write_text((ROOT / "etc" / "ack.toml").read_text())
        assert acks.load_config() == base

    def test_every_rule_names_a_registered_task(self):
        from homelab_maint import cli
        cli.load_tasks()
        unknown = sorted(set(acks._BASE["key"]) - set(core.REGISTRY))
        assert unknown == [], f"etc/ack.toml has rules for tasks that do not exist: {unknown}"

    def test_override_file_changes_values_and_rules_per_table(self, world):
        (world.conf / "ack.toml").write_text('[ack]\ndays = 30\nmax_days = 120\n[inbox]\nskew_s = 60\n[key.disk_forecast]\nmode = "task"\n'
                                             '[key.mine]\nmode = "text"\nsort = true\n')
        cfg = acks.load_config()
        assert cfg["ack"]["days"] == 30 and cfg["ack"]["max_days"] == 120 and cfg["inbox"]["skew_s"] == 60
        assert cfg["key"]["disk_forecast"]["mode"] == "task" and cfg["key"]["mine"]["sort"] is True
        assert cfg["key"]["failed_units"]["mode"] == "regex"                       # untouched tables keep the baseline
        assert acks.fingerprint("disk_forecast", "warn: /a 1% free", "warn") == acks.fingerprint("disk_forecast", "warn: /b 2% free", "warn")

    def test_a_file_that_is_not_toml_is_ignored_as_a_whole(self, world):
        (world.conf / "ack.toml").write_text("[ack\ndays = 1\n")
        cfg, errors = acks.load_config(True)
        assert cfg["ack"]["days"] == 90 and any("not valid TOML" in e for e in errors)
        assert acks.main(["validate"]) == 1

    @pytest.mark.parametrize("body", ['[ack]\ndays = 0\n', '[ack]\ndays = 1000\n', '[ack]\ndays = "90"\n', '[ack]\ndays = true\n',
                                      '[ack]\nescalation_breaks = "no"\n', '[inbox]\nskew_s = 5\n', '[inbox]\nmax_bytes = 10\n',
                                      '[inbox]\nrequire_signature = 0\n', '[inbox]\nunsigned_sources = ["root"]\n', '[ack]\nmax_tokens = 1\n'])
    def test_out_of_range_or_mistyped_values_are_ignored_one_by_one(self, world, body):
        (world.conf / "ack.toml").write_text(body + "[ack]\nmin_days = 2\n" if "[ack]" not in body[:6] else body)
        cfg, errors = acks.load_config(True)
        assert errors and cfg["ack"]["days"] == 90 and cfg["inbox"]["skew_s"] == 600 and cfg["inbox"]["require_signature"] is True
        assert cfg["ack"]["escalation_breaks"] is True and cfg["inbox"]["unsigned_sources"] == []

    @pytest.mark.parametrize("body", ['[key.x]\nmode = "regex"\nregex = ["("]\n', '[key.x]\nmode = "regex"\nregex = []\n',
                                      '[key.x]\nmode = "regex"\n', '[key.x]\nmode = "bogus"\n', '[key.x]\nmode = "regex"\nregex = [1]\n',
                                      '[key.x]\nmode = "regex"\nregex = ["' + "a" * 300 + '"]\n',
                                      '[key.x]\nmode = "regex"\nregex = [' + ",".join(['"a"'] * 11) + ']\n'])
    def test_a_rule_that_does_not_compile_or_fit_is_ignored_and_the_default_text_rule_applies(self, world, body):
        (world.conf / "ack.toml").write_text(body)
        cfg, errors = acks.load_config(True)
        assert "x" not in cfg["key"] and len(errors) == 1
        assert acks.fingerprint("x", "warn: 4 failed", "warn")                     # still fingerprinted, as text

    def test_a_bad_override_for_a_shipped_rule_keeps_the_shipped_rule(self, world):
        (world.conf / "ack.toml").write_text('[key.failed_units]\nmode = "regex"\nregex = ["(("]\n')
        assert acks.load_config()["key"]["failed_units"] == acks.load_config()["key"]["failed_units"]
        assert acks.load_config()["key"]["failed_units"]["regex"] == acks._BASE["key"]["failed_units"]["regex"]

    def test_min_and_max_days_stay_consistent(self, world):
        (world.conf / "ack.toml").write_text("[ack]\nmin_days = 100\nmax_days = 50\ndays = 90\n")
        a = acks.load_config()["ack"]
        assert a["min_days"] <= a["days"] <= a["max_days"]

    def test_unreadable_override_file_means_the_baseline(self, world):
        p = world.conf / "ack.toml"
        p.write_bytes(b"\xff\xfe\x00bad")
        cfg, errors = acks.load_config(True)
        assert cfg["ack"]["days"] == 90 and errors


# =========================================================================== normalisation and fingerprints
class TestNormalize:
    @pytest.mark.parametrize("text,want", [
        ("warn: memory stall 6.1%, 30.0 GiB available (+2 more)", "memory stall #, # available +2"),
        ("crit: backup-system FAILED (6d10h ago)", "backup-system failed (# ago)"),
        ("28h old (limit 26h)", "# old (limit #)"),
        ("restarted 3 times at 2026-10-02 13:04:05 and 13:04", "restarted # times at # and #"),
        ("container 3f9a1c2b7d4e died; image sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "container # died; image sha256:#"),
        ("uuid 123e4567-e89b-12d3-a456-426614174000 gone", "uuid # gone"),
        ("nvme0 70C, sda1 full", "nvme0 #c, sda1 full"),                                           # the digit of a NAME stays
        ("  many    spaces\tand\nlines  ", "many spaces and lines"),
        ("OS jobs: 2 of 13 need attention: a overdue (+2)", "os jobs: # of # need attention: a overdue +2"),
        ("defaced facade deadbeef", "defaced facade deadbeef"),                                    # hex-looking WORDS (no digit) are words
        ("ok: 5 x 3.5 MiB = 17.5 MB", "# x # = #"),
    ])
    def test_examples(self, text, want):
        assert acks.normalize(text) == want

    def test_cut_at_160_and_never_raises_on_odd_input(self):
        assert len(acks.normalize("word " * 500)) == 160
        for junk in (None, 5, b"bytes", ["a"], {"a": 1}, "\x00\x01\x02", "\ud800", "a" * 100000):
            assert isinstance(acks.normalize(junk), str)

    def test_is_fast_on_pathological_input(self):
        t0 = time.monotonic()
        acks.fingerprint("disk_forecast", "/" + "a /" * 50000 + " 1% free", "warn")
        acks.fingerprint("probes", "1 down: " + "x, " * 50000, "warn")
        acks.normalize("1" * 200000 + "1.1." * 50000)
        assert time.monotonic() - t0 < 2.0


class TestFingerprint:
    @pytest.mark.parametrize("task,a,b", SAME)
    def test_the_same_error_keeps_its_fingerprint(self, task, a, b):
        fa, fb = acks.fingerprint(task, a, "warn"), acks.fingerprint(task, b, "warn")
        assert fa and fa == fb, (fa.key, fb.key)

    @pytest.mark.parametrize("task,a,b", DIFFERENT)
    def test_a_genuinely_different_error_has_another_fingerprint(self, task, a, b):
        fa, fb = acks.fingerprint(task, a, "warn"), acks.fingerprint(task, b, "warn")
        assert fa and fb and fa != fb, (fa.key, fb.key)

    def test_the_formula_is_sha1_of_task_and_key(self):
        fp = acks.fingerprint("failed_units", "warn: 1 unhealthy: kavita", "warn")
        assert fp.key == "4:1|5:kavita" and fp.mode == "regex" and fp.task == "failed_units" and fp.severity == "warn"
        assert str(fp) == hashlib.sha1(b"failed_units|4:1|5:kavita").hexdigest()[:16] and re.fullmatch(r"[0-9a-f]{16}", fp)
        assert fp.fp == str(fp) == fp.id and isinstance(fp, str)

    def test_the_task_name_is_part_of_the_identity(self):
        assert acks.fingerprint("a", "warn: x failed", "warn") != acks.fingerprint("b", "warn: x failed", "warn")

    def test_severity_is_not_part_of_the_identity(self):
        assert acks.fingerprint("t", "warn: x failed", "warn") == acks.fingerprint("t", "warn: x failed", "crit")
        assert acks.fingerprint("t", "x", "crit").severity == "crit" and acks.fingerprint("t", "x", "info").severity == ""

    def test_result_entry_and_string_agree(self):
        res = core.Result("warn", "warn: 2 failed unit(s): a.service, b.service")
        entry = ent("warn", res.summary)
        assert acks.fingerprint("failed_units", res, "warn") == acks.fingerprint("failed_units", entry, "warn") \
            == acks.fingerprint("failed_units", res.summary, "warn")

    def test_an_explicit_issue_key_wins_over_the_summary_and_the_rules(self):
        class R:
            summary, issue_key = "warn: totally different text 9", "disk:/mnt/x"
        a = acks.fingerprint("failed_units", R(), "warn")
        assert a.mode == "explicit" and a.key == "disk:/mnt/x"
        assert a == acks.fingerprint("failed_units", {"summary": "something else", "issue_key": "disk:/mnt/x"}, "warn")
        assert a != acks.fingerprint("failed_units", {"summary": "something else", "issue_key": "disk:/mnt/y"}, "warn")
        assert acks.fingerprint("failed_units", {"summary": "warn: 1 unhealthy: kavita", "issue_key": "  "}, "warn").mode == "regex"
        assert acks.fingerprint("t", {"summary": "x", "issue_key": 5}, "warn").mode == "text"                     # not a string: ignored

    def test_a_rule_that_matches_nothing_falls_back_to_the_more_specific_text(self):
        fp = acks.fingerprint("failed_units", "warn: probe failed: systemctl rc=1", "warn")
        assert fp.mode == "text" and fp.key == "probe failed: systemctl rc=1"                    # an exit code NAMES the failure: it stays
        assert fp != acks.fingerprint("failed_units", "warn: probe failed: docker ps rc=1", "warn")

    def test_task_mode_is_the_task_alone(self):
        a, b = acks.fingerprint("docker_df", "warn: x", "warn"), acks.fingerprint("docker_df", "anything at all", "crit")
        assert a == b and a.key == "*" and a.mode == "task"

    def test_sorting_text_rule_ignores_order_but_not_content(self):
        a = acks.fingerprint("smart_trend", "SMART: sda x; nvme0 y; sdb z", "warn")
        assert a == acks.fingerprint("smart_trend", "SMART: sdb z; sda x; nvme0 y", "warn")
        assert a != acks.fingerprint("smart_trend", "SMART: sdb z; sda x; nvme0 w", "warn")

    @pytest.mark.parametrize("task", ["", " ", None, 5, "a" * 81, "a\nb", " a", "a "])
    def test_a_task_name_that_cannot_be_an_identity_gives_the_empty_fingerprint(self, task):
        fp = acks.fingerprint(task, "warn: x", "warn")
        assert fp == "" and not fp and isinstance(fp, str)

    @pytest.mark.parametrize("subject", [None, 5, [], {}, object(), b"x", float("nan"), "\ud800"])
    def test_hostile_subjects_never_raise(self, subject):
        fp = acks.fingerprint("t", subject, "warn")
        assert isinstance(fp, str)

    def test_a_broken_baseline_rule_object_can_not_raise_either(self, monkeypatch):
        monkeypatch.setattr(acks, "load_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert acks.fingerprint("t", "warn: x", "warn") == ""

    def test_the_live_check_run_of_this_host_fingerprints_every_task(self):
        seen = {}
        for task, (status, summary) in LIVE.items():
            fp = acks.fingerprint(task, ent(status, summary), status)
            assert re.fullmatch(r"[0-9a-f]{16}", fp), task
            seen[task] = fp
        assert len(set(seen.values())) == len(seen)
        assert acks.fingerprint("docker_prune_exposure", LIVE["docker_prune_exposure"][1], "crit").key == "0:comfyui|1:comfyui-local:tier2-gcc"

    def test_explain_prints_the_rule_the_key_and_the_id(self, world, capsys):
        write_status({"failed_units": ent("warn", "warn: 2 failed unit(s): b.service, a.service")})
        assert acks.main(["explain", "failed_units"]) == 0
        out = capsys.readouterr().out
        assert "mode=regex" in out and "0:2|1:a.service|1:b.service" in out and acks.fingerprint("failed_units", "warn: 2 failed unit(s): a.service, b.service") in out
        assert acks.main(["explain", "nope"]) == 1 and "no status entry" in capsys.readouterr().err
        assert acks.main(["explain", "disk_forecast", "--summary", "warn: /a 4% free"]) == 0 and "0:/a" in capsys.readouterr().out
        assert acks.main(["explain"]) == 2


# =========================================================================== is_acked, the severity ceiling, expiry
def make_ack(task="failed_units", summary="warn: 1 unhealthy: kavita", sev="warn", days=90, now=NOW, **kw):
    write_status({task: ent(sev, summary)})
    return acks.add(task, days, kw.pop("note", ""), None, kw.pop("by", "cli"), now)


class TestIsAcked:
    def test_covers_the_acknowledged_severity_and_below_never_above(self, world):
        a = make_ack(sev="warn")
        assert acks.is_acked(a.fp, "warn", NOW + 1).until == NOW + 90 * DAY
        assert acks.is_acked(a.fp, "crit", NOW + 1) is None                          # escalation breaks the ack
        b = make_ack("disk_forecast", "crit: / 3% free", "crit")
        assert acks.is_acked(b.fp, "crit", NOW + 1) and acks.is_acked(b.fp, "warn", NOW + 1)

    def test_escalation_breaks_can_be_switched_off(self, world):
        a = make_ack(sev="warn")
        (world.conf / "ack.toml").write_text("[ack]\nescalation_breaks = false\n")
        assert acks.is_acked(a.fp, "crit", NOW + 1) is not None

    @pytest.mark.parametrize("sev", ["ok", "info", "skipped", "", None, "bogus", 5, "OK"])
    def test_a_severity_that_is_not_warn_or_crit_is_never_covered(self, world, sev):
        a = make_ack()
        assert acks.is_acked(a.fp, sev, NOW + 1) is None

    def test_error_counts_as_crit_and_case_is_ignored(self, world):
        a = make_ack(sev="warn")
        assert acks.is_acked(a.fp, "ERROR", NOW + 1) is None and acks.is_acked(a.fp, " WARN ", NOW + 1) is not None
        assert acks.is_acked(a.fp.upper(), "warn", NOW + 1) is not None

    def test_the_second_it_ends(self, world):
        a = make_ack(days=7)
        until = NOW + 7 * DAY
        assert acks.is_acked(a.fp, "warn", until - 0.001) is not None
        assert acks.is_acked(a.fp, "warn", until) is None and acks.is_acked(a.fp, "warn", until + 1) is None

    @pytest.mark.parametrize("fp", [None, "", "abc", "z" * 16, "0" * 15, "0" * 17, 5, [], "0123456789abcdeg"])
    def test_a_bad_fingerprint_is_never_acked(self, world, fp):
        make_ack()
        assert acks.is_acked(fp, "warn", NOW + 1) is None

    def test_the_fp_object_works_and_other_fingerprints_do_not_match(self, world):
        a = make_ack()
        assert acks.is_acked(acks.fingerprint("failed_units", "warn: 1 unhealthy: kavita"), "warn", NOW + 1) is not None
        assert acks.is_acked(acks.fingerprint("failed_units", "warn: 1 unhealthy: other"), "warn", NOW + 1) is None
        assert acks.is_acked("0" * 16, "warn", NOW + 1) is None and a.fp

    def test_no_store_unreadable_or_corrupt_store_means_not_acked(self, world):
        a = make_ack()
        for junk in (b"", b"{", b"[]", b'{"v": 2}', b"null", b'{"v": 1, "acks": 5}', b"\xff\xfe", b'{"v":1,"acks":{"' + a.fp.encode() + b'":5}}'):
            (core.STATE_DIR / "acks.json").write_bytes(junk)
            assert acks.is_acked(a.fp, "warn", NOW + 1) is None
        (core.STATE_DIR / "acks.json").unlink()
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None

    def test_a_record_with_odd_fields_is_dropped_not_trusted(self, world):
        a = make_ack()
        doc = store()
        doc["acks"][a.fp]["until"] = "never"
        (core.STATE_DIR / "acks.json").write_text(json.dumps(doc))
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None
        doc["acks"][a.fp].update(until=float("inf"), severity="warn")
        (core.STATE_DIR / "acks.json").write_text(json.dumps(doc))
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None
        doc["acks"][a.fp].update(until=NOW - 5, acked_at=NOW)                          # ends before it starts
        (core.STATE_DIR / "acks.json").write_text(json.dumps(doc))
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None

    def test_it_never_raises(self, world, monkeypatch):
        a = make_ack()
        monkeypatch.setattr(acks, "_read_store", lambda: (_ for _ in ()).throw(OSError("disk gone")))
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None

    def test_info_object_carries_the_record(self, world):
        a = make_ack(note="known flap", by="cli")
        i = acks.is_acked(a.fp, "warn", NOW + 2 * DAY)
        assert (i.task, i.severity, i.by, i.note, i.first_ack, i.acked_at) == ("failed_units", "warn", "cli", "known flap", NOW, NOW)
        assert i.days_left(NOW + 2 * DAY) == 88 and i.as_dict()["fp"] == a.fp

    def test_record_suppressed_counts_and_audits_once_an_hour(self, world):
        a = make_ack()
        for k in range(5):
            acks.record_suppressed(a.fp, NOW + 10 + k)
        rec = store()["acks"][a.fp]
        assert rec["count_suppressed"] == 5 and rec["last_seen"] == NOW + 14
        assert sum(1 for e in audit_events() if e["ev"] == "suppressed") == 1
        acks.record_suppressed(a.fp, NOW + 3611)
        assert sum(1 for e in audit_events() if e["ev"] == "suppressed") == 2 and store()["acks"][a.fp]["count_suppressed"] == 6

    @pytest.mark.parametrize("fp", ["0" * 16, "", None, "bad", 5])
    def test_record_suppressed_for_an_unknown_issue_is_a_quiet_no_op(self, world, fp):
        make_ack()
        before = (core.STATE_DIR / "acks.json").read_bytes()
        acks.record_suppressed(fp, NOW + 5)
        assert (core.STATE_DIR / "acks.json").read_bytes() == before

    def test_list_acks_orders_by_expiry_and_hides_the_ended(self, world):
        a = make_ack(days=30)
        write_status({"disk_forecast": ent("warn", "warn: /x 3% free")})
        b = acks.add("disk_forecast", 7, now=NOW)
        assert [r.fp for r in acks.list_acks(NOW + 1)] == [b.fp, a.fp]
        assert [r.fp for r in acks.list_acks(NOW + 8 * DAY)] == [a.fp]


# =========================================================================== status.json
class TestStatus:
    def test_apply_marks_covered_entries_and_keeps_the_true_status(self, world):
        a = make_ack(sev="warn")
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("crit", "crit: / 3% free"),
                        "memory_health": ent("ok", "ok: fine"), "junk": "x", "docker_df": ent("info", "x")}}
        res = acks.apply_to_status(st, NOW + 10)
        t = st["tasks"]
        assert res == {"acked": 1, "failing": 2} and st["acked_n"] == 1
        assert t["failed_units"]["status"] == "warn" and t["failed_units"]["fp"] == a.fp
        assert t["failed_units"]["acked"] == {"fp": a.fp, "until": NOW + 90 * DAY, "by": "cli", "note": "", "severity": "warn", "since": NOW}
        assert "acked" not in t["disk_forecast"] and re.fullmatch(r"[0-9a-f]{16}", t["disk_forecast"]["fp"])
        assert "fp" not in t["memory_health"] and "acked" not in t["memory_health"] and "fp" not in t["docker_df"]
        assert st["overall"] == "crit" and t["junk"] == "x"                       # disk_forecast is still crit; the acked warn does not count

    def test_overall_ignores_acked_and_alert_false_tasks(self):
        tasks = {"a": {"status": "crit", "alert": True, "acked": {"fp": "x", "until": NOW + DAY}}, "b": {"status": "crit", "alert": False},
                 "c": {"status": "warn"}, "d": {"status": "error"}, "e": {"status": "skipped"}}
        assert acks.overall(tasks, NOW) == "crit"                                      # d: error counts as crit
        del tasks["d"]
        assert acks.overall(tasks, NOW) == "warn"
        del tasks["c"]
        assert acks.overall(tasks, NOW) == "ok" and acks.overall({}, NOW) == "ok"

    def test_an_escalated_task_is_no_longer_acked_and_a_recovered_one_loses_its_flags(self, world):
        a = make_ack(sev="warn")
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita")}}
        acks.apply_to_status(st, NOW + 1)
        assert "acked" in st["tasks"]["failed_units"]
        st["tasks"]["failed_units"]["status"] = "crit"
        acks.apply_to_status(st, NOW + 2)
        assert "acked" not in st["tasks"]["failed_units"] and st["overall"] == "crit"
        st["tasks"]["failed_units"].update(status="ok", summary="ok: fine")
        acks.apply_to_status(st, NOW + 3)
        assert "fp" not in st["tasks"]["failed_units"] and st["overall"] == "ok"
        assert acks.is_acked(a.fp, "warn", NOW + 4)                                    # the ack itself survives the recovery

    def test_the_ack_survives_a_recovery_and_covers_the_recurrence(self, world):
        a = make_ack()
        st = {"tasks": {"failed_units": ent("ok", "ok: fine")}}
        acks.apply_to_status(st, NOW + DAY)
        st["tasks"]["failed_units"] = ent("warn", "warn: 1 unhealthy: kavita")
        acks.apply_to_status(st, NOW + 30 * DAY)
        assert st["tasks"]["failed_units"]["acked"]["fp"] == a.fp and st["overall"] == "ok"

    def test_an_expired_ack_clears_the_flag_and_the_colour_returns(self, world):
        make_ack(days=7)
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita")}}
        acks.apply_to_status(st, NOW + 7 * DAY + 1)
        assert "acked" not in st["tasks"]["failed_units"] and st["overall"] == "warn"

    def test_stale_flags_written_by_someone_else_are_cleared_not_trusted(self, world):
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita", acked={"fp": "0" * 16, "until": NOW + 99 * DAY})}}
        acks.apply_to_status(st, NOW)
        assert "acked" not in st["tasks"]["failed_units"] and st["overall"] == "warn"

    def test_a_missing_or_corrupt_store_marks_nothing(self, world):
        a = make_ack()
        (core.STATE_DIR / "acks.json").write_text("{")
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita")}}
        assert acks.apply_to_status(st, NOW + 1)["acked"] == 0 and st["overall"] == "warn" and a.fp

    @pytest.mark.parametrize("st", [{}, {"tasks": None}, {"tasks": []}, {"tasks": {"x": 5}}, {"tasks": {"x": {}}}, {"tasks": {"x": {"status": ["warn"]}}}])
    def test_garbage_status_does_not_raise(self, world, st):
        assert acks.apply_to_status(st, NOW)["acked"] == 0

    def test_mark_entry_is_idempotent_and_uses_the_entry_issue_key(self, world):
        write_status({"t": ent("warn", "warn: x", issue_key="k1")})
        a = acks.add("t", 10, now=NOW)
        e = ent("warn", "warn: totally other words", issue_key="k1")
        assert acks.mark_entry("t", e, NOW + 1)["fp"] == a.fp and acks.mark_entry("t", e, NOW + 1)["fp"] == a.fp
        e2 = ent("warn", "warn: x", issue_key="k2")
        assert acks.mark_entry("t", e2, NOW + 1) is None and e2["fp"] != a.fp

    def test_cli_glue_pattern_never_raises_even_when_acks_is_broken(self, world, monkeypatch):
        monkeypatch.setattr(acks, "_read_store", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        e = ent("warn", "warn: x", acked={"fp": "0" * 16})
        assert acks.mark_entry("t", e, NOW) is None and "acked" not in e


# =========================================================================== add / remove
class TestAddRemove:
    def test_default_is_90_days_by_task_name_and_by_fingerprint(self, world):
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        a = acks.add("failed_units", now=NOW)
        assert a.until == NOW + 90 * DAY and a.severity == "warn" and a.by == "cli" and a.first_ack == NOW
        b = acks.add(a.fp, 30, "again", now=NOW + 100)
        assert b.fp == a.fp and b.until == NOW + 100 + 30 * DAY and b.first_ack == NOW and b.note == "again"
        assert len(acks.list_acks(NOW + 101)) == 1

    @pytest.mark.parametrize("days", [0, -1, 366, 1000, 1.5, "30", True, False, [], float("nan")])
    def test_days_outside_1_to_365_are_refused(self, world, days):
        write_status({"t": ent("warn", "warn: x")})
        with pytest.raises(acks.AckError) as e:
            acks.add("t", days, now=NOW)
        assert e.value.code == "days" and acks.list_acks(NOW) == []

    @pytest.mark.parametrize("days", [1, 7, 30, 90, 365])
    def test_days_inside_the_range_are_accepted(self, world, days):
        write_status({"t": ent("warn", "warn: x")})
        assert acks.add("t", days, now=NOW).until == NOW + days * DAY

    def test_only_what_the_host_knows_can_be_acknowledged(self, world):
        write_status({"t": ent("warn", "warn: x"), "ok": ent("ok", "ok: x")})
        for target, code in (("0" * 16, "unknown_issue"), ("nope", "not_failing"), ("ok", "not_failing"), ("", "not_failing")):
            with pytest.raises(acks.AckError) as e:
                acks.add(target, 5, now=NOW)
            assert e.value.code == code

    def test_the_note_is_cleaned_and_cut(self, world):
        write_status({"t": ent("warn", "warn: x")})
        a = acks.add("t", 5, "line1\nline2\x00\t\x1b[31m " + "x" * 400, now=NOW)
        assert "\n" not in a.note and "\x00" not in a.note and "\x1b" not in a.note and len(a.note) <= 200

    def test_severity_options(self, world):
        write_status({"t": ent("warn", "warn: x")})
        assert acks.add("t", 5, severity="crit", now=NOW).severity == "crit"                 # the owner may pre-accept the escalation (CLI)
        write_status({"t": ent("crit", "crit: x")})
        with pytest.raises(acks.AckError) as e:
            acks.add("t", 5, severity="warn", now=NOW)                                       # warn would not even cover what is firing
        assert e.value.code == "severity_changed"
        for bad in ("bogus", "", "info", 3):
            with pytest.raises(acks.AckError):
                acks.add("t", 5, severity=bad, now=NOW)
        with pytest.raises(acks.AckError):
            acks.add("t", 5, severity="warn", now=NOW, strict_severity=True)
        assert acks.add("t", 5, severity="crit", now=NOW, strict_severity=True).severity == "crit"

    def test_by_must_be_known(self, world):
        write_status({"t": ent("warn", "warn: x")})
        with pytest.raises(acks.AckError):
            acks.add("t", 5, by="root", now=NOW)

    def test_the_active_cap(self, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_active = 2\n")
        write_status({f"t{i}": ent("warn", f"warn: err{i}x") for i in range(3)})
        acks.add("t0", 5, now=NOW)
        acks.add("t1", 5, now=NOW)
        with pytest.raises(acks.AckError) as e:
            acks.add("t2", 5, now=NOW)
        assert e.value.code == "too_many"
        acks.add("t1", 9, now=NOW)                                                           # renewing an existing one is not a new slot
        acks.expire(NOW + 6 * DAY)                                                           # t0 ended
        assert acks.add("t2", 5, now=NOW + 6 * DAY)

    def test_remove_restores_alerting_at_once(self, world):
        a = make_ack()
        assert acks.is_acked(a.fp, "warn", NOW + 1)
        assert acks.remove(a.fp, "web", NOW + 2) is True
        assert acks.is_acked(a.fp, "warn", NOW + 3) is None and acks.remove(a.fp, now=NOW + 4) is False
        assert acks.remove("zzz") is False and acks.remove(None) is False
        ev = [e["ev"] for e in audit_events()]
        assert ev == ["ack", "unack"]

    def test_the_audit_trail_has_every_change_and_no_secrets(self, world):
        a = make_ack(note="password=hunter2 mail me@example.com")
        acks.remove(a.fp, now=NOW + 5)
        text = (core.STATE_DIR / "acks.jsonl").read_text()
        assert "hunter2" not in text and "me@example.com" not in text and a.fp in text
        assert (core.STATE_DIR / "acks.jsonl").stat().st_mode & 0o777 == 0o600
        assert any(x[0] == "acks" and x[1] in ("ack", "unack") for x in world.log)            # and the project's audit log hears of it

    def test_the_store_is_private_and_atomic(self, world):
        make_ack()
        assert (core.STATE_DIR / "acks.json").stat().st_mode & 0o777 == 0o600
        assert not list(core.STATE_DIR.glob(".ack-*.tmp"))

    def test_torn_last_line_of_the_audit_log_does_not_swallow_the_next_event(self, world):
        a = make_ack()
        p = core.STATE_DIR / "acks.jsonl"
        p.write_bytes(p.read_bytes() + b'{"t": 1, "ev": "ack", "fp')
        acks.remove(a.fp, now=NOW + 5)
        lines = p.read_bytes().split(b"\n")
        assert json.loads(lines[-2])["ev"] == "unack"

    def test_the_audit_log_is_compacted_when_it_grows(self, world, monkeypatch):
        monkeypatch.setattr(acks, "LOG_MAX", 4000)
        write_status({"t": ent("warn", "warn: x")})
        for i in range(60):
            acks.add("t", 5, "n" * 100, now=NOW + i)
        p = core.STATE_DIR / "acks.jsonl"
        assert p.stat().st_size < 4200 and all(json.loads(x) for x in p.read_text().splitlines())


# =========================================================================== tokens
class TestTokens:
    def issue(self, **kw):
        a = {"fp": "0123456789abcdef", "task": "failed_units", "title": "Services", "summary": "1 unhealthy: kavita", "severity": "warn", "now": NOW}
        a.update(kw)
        return acks.issue_token(**a)

    def test_a_token_is_256_bits_of_urlsafe_text_and_unique(self, world):
        toks = {self.issue() for _ in range(50)}
        assert len(toks) == 50 and all(re.fullmatch(r"[A-Za-z0-9_-]{43}", t) for t in toks)

    def test_only_the_hash_is_stored_and_the_plaintext_is_nowhere(self, world):
        tok = self.issue()
        ready()
        acks.export_tokens(NOW)
        acks.export_public(NOW)
        h = acks.token_hash(tok)
        assert h == hashlib.sha256(tok.encode()).hexdigest() and h in store()["tokens"]
        for p in core.STATE_DIR.rglob("*"):
            if p.is_file():
                assert tok.encode() not in p.read_bytes(), p
        assert not any(tok in json.dumps(x) for x in world.log)

    def test_tokens_json_is_the_verify_only_view(self, world):
        tok = self.issue()
        p = core.STATE_DIR / "ack" / "tokens.json"
        assert p.stat().st_mode & 0o777 == 0o644 and p.parent.stat().st_mode & 0o777 == 0o755
        doc = json.loads(p.read_text())
        assert set(doc) == {acks.token_hash(tok)}
        row = doc[acks.token_hash(tok)]
        assert set(row) == {"fp", "exp", "used", "title", "summary", "severity", "state", "until", "reason"}
        assert row["fp"] == "0123456789abcdef" and row["exp"] == NOW + 30 * DAY and row["used"] is False and row["state"] == "pending"

    @pytest.mark.parametrize("kw", [{"fp": "short"}, {"fp": ""}, {"fp": None}, {"fp": "G" * 16}, {"severity": "ok"}, {"severity": ""},
                                    {"severity": "info"}, {"task": ""}, {"task": "  "}])
    def test_a_token_that_cannot_be_bound_is_not_issued(self, world, kw):
        with pytest.raises(ValueError):
            self.issue(**kw)
        assert not (core.STATE_DIR / "acks.json").exists()

    def test_ttl_defaults_to_the_config_and_is_clamped(self, world):
        t1, t2, t3, t4 = self.issue(), self.issue(ttl_days=7), self.issue(ttl_days=500), self.issue(ttl_days=0)
        exp = {h: r["exp"] - NOW for h, r in store()["tokens"].items()}
        assert exp[acks.token_hash(t1)] == 30 * DAY and exp[acks.token_hash(t2)] == 7 * DAY
        assert exp[acks.token_hash(t3)] == 90 * DAY and exp[acks.token_hash(t4)] == 30 * DAY

    def test_severity_is_normalised_and_error_is_crit(self, world):
        tok = self.issue(severity="error")
        assert store()["tokens"][acks.token_hash(tok)]["severity"] == "crit"

    def test_title_and_summary_are_redacted_before_they_are_stored(self, world):
        tok = self.issue(title="Mail me@example.com", summary="failed at https://x.example/p?token=abc123 password=hunter2 call +1 416 555 0199")
        row = json.dumps(json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text())[acks.token_hash(tok)])
        assert "example.com" not in row and "abc123" not in row and "hunter2" not in row and "555" not in row

    def test_the_cap_drops_spent_and_expired_tokens_first_then_the_oldest(self, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_tokens = 10\n")
        old = [self.issue(now=NOW + i) for i in range(10)]
        with acks._txn(NOW + 20) as st:
            st["tokens"][acks.token_hash(old[5])]["used"] = True
        new = self.issue(now=NOW + 30)
        hs = set(store()["tokens"])
        assert len(hs) == 10 and acks.token_hash(old[5]) not in hs and acks.token_hash(new) in hs and acks.token_hash(old[0]) in hs
        newer = self.issue(now=NOW + 31)
        hs = set(store()["tokens"])
        assert len(hs) == 10 and acks.token_hash(old[0]) not in hs and acks.token_hash(newer) in hs

    def test_spent_tokens_are_purged_two_days_after_they_end(self, world):
        tok = self.issue(ttl_days=1)
        assert acks.expire(NOW + 2.9 * DAY) == [] and acks.token_hash(tok) in store()["tokens"]
        acks.expire(NOW + 3.1 * DAY)
        assert acks.token_hash(tok) not in store()["tokens"]

    def test_verify_token_is_the_websites_check(self, world):
        tok = self.issue()
        assert acks.verify_token(tok, NOW + 1)["fp"] == "0123456789abcdef"
        assert acks.verify_token(tok, NOW + 31 * DAY) is None                               # expired
        for bad in (tok[:-1], tok + "x", "", None, "A" * 43, tok.upper(), "../" + tok[3:]):
            assert acks.verify_token(bad, NOW + 1) is None
        acks.revoke_token(tok, NOW + 2)
        assert acks.verify_token(tok, NOW + 3) is None and acks.revoke_token(tok) is False

    def test_lookup_compares_in_constant_time(self, world, monkeypatch):
        self.issue()
        self.issue()
        calls = []
        real = hmac.compare_digest
        monkeypatch.setattr(acks.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
        acks._find_token(acks._read_store()[0], "f" * 64)
        assert len(calls) == 2                                                                # every stored hash is compared, whether or not one matches

    def test_the_issued_token_is_single_use_through_the_inbox(self, world):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        tok = acks.issue_token(fp, "failed_units", "Services", "1 unhealthy: kavita", "warn", NOW)
        submit(email_req(tok), key)
        assert run_inbox().applied == [fp] and acks.is_acked(fp, "warn", NOW + 1)
        submit(email_req(tok, ts=NOW + 5), key)
        rep = run_inbox(NOW + 5)
        assert rep.applied == [] and rep.rejected == ["used"]
        assert store()["tokens"][acks.token_hash(tok)]["used"] is True

    def test_an_expired_token_is_refused(self, world):
        key = ready()
        tok = self.issue(ttl_days=1)
        submit(email_req(tok, ts=NOW + 2 * DAY), key)
        rep = run_inbox(NOW + 2 * DAY)
        assert rep.rejected == ["expired"] and acks.list_acks(NOW + 2 * DAY) == []

    def test_unknown_forged_and_rebound_tokens_are_refused(self, world):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        tok = acks.issue_token(fp, "failed_units", "S", "x", "warn", NOW)
        other = acks.issue_token("f" * 16, "disk_forecast", "D", "y", "warn", NOW)
        bad = [email_req("A" * 43), email_req(tok[:-1] + ("A" if tok[-1] != "A" else "B")),
               {**email_req(tok), "fp": "f" * 16}, {**email_req(tok), "severity": "crit"}, {**email_req(other), "fp": fp}]
        for r in bad:
            submit(r, key)
        rep = run_inbox()
        assert rep.applied == [] and sorted(rep.rejected) == ["binding", "binding", "binding", "unknown_token", "unknown_token"]
        assert acks.list_acks(NOW) == [] and not any(t["used"] for t in store()["tokens"].values())

    def test_the_public_words_for_unknown_and_rebound_are_the_same(self, world):
        key = ready()
        tok = self.issue()
        submit({**email_req(tok), "fp": "f" * 16}, key)
        run_inbox()
        row = json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text())[acks.token_hash(tok)]
        assert row["state"] == "rejected" and row["reason"] == "invalid" and row["used"] is False
        submit(email_req(tok, ts=NOW + 1), key)                                              # the genuine click still works afterwards
        run_inbox(NOW + 1)
        row = json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text())[acks.token_hash(tok)]
        assert row["state"] == "applied" and row["used"] is True

    def test_tokens_doc_states(self, world):
        tok = self.issue(ttl_days=1)
        assert acks.tokens_doc(NOW)[acks.token_hash(tok)]["state"] == "pending"
        assert acks.tokens_doc(NOW + 2 * DAY)[acks.token_hash(tok)]["state"] == "expired"


# =========================================================================== the inbox
class TestInbox:
    @pytest.fixture()
    def web(self, world):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("crit", "crit: / 3% free")})
        return types.SimpleNamespace(key=key, fp=fp_of("failed_units", "warn: 1 unhealthy: kavita"),
                                     dfp=fp_of("disk_forecast", "crit: / 3% free", "crit"))

    def test_a_signed_web_request_is_applied_and_the_file_is_gone(self, web):
        submit(web_req(web.fp, days=7, note="ok by me"), web.key)
        rep = run_inbox()
        assert rep.applied == [web.fp] and rep.rejected == [] and list(inbox().glob("*.json")) == []
        a = acks.is_acked(web.fp, "warn", NOW + 1)
        assert a.by == "web" and a.until == NOW + 7 * DAY and a.note == "ok by me" and a.task == "failed_units"
        assert audit_events()[-1]["ev"] == "ack" and audit_events()[-1]["by"] == "web"
        assert json.loads((core.STATE_DIR / "public" / "acks.json").read_text())["acks"][0]["id"] == web.fp

    def test_days_default_to_the_configured_ones(self, web):
        r = web_req(web.fp)
        del r["days"]
        submit(r, web.key)
        run_inbox()
        assert acks.is_acked(web.fp, "warn", NOW + 1).until == NOW + 90 * DAY

    def test_an_email_request_binds_the_token_issue_and_ignores_nothing_else(self, web):
        tok = acks.issue_token(web.fp, "failed_units", "Services", "1 unhealthy: kavita", "warn", NOW)
        submit(email_req(tok, days=30, note="from the mail"), web.key)
        assert run_inbox().applied == [web.fp]
        a = acks.is_acked(web.fp, "warn", NOW + 1)
        assert a.by == "email" and a.until == NOW + 30 * DAY and a.title == "Services"

    def test_an_email_ack_works_after_the_issue_cleared(self, web):
        tok = acks.issue_token(web.fp, "failed_units", "Services", "1 unhealthy: kavita", "warn", NOW)
        write_status({"failed_units": ent("ok", "ok: fine")})
        submit(email_req(tok), web.key)
        assert run_inbox().applied == [web.fp]

    def test_unack(self, web):
        acks.add("failed_units", 30, now=NOW)
        submit({"v": 1, "kind": "unack", "source": "web", "fp": web.fp, "ts": NOW}, web.key)
        rep = run_inbox()
        assert rep.unacked == [web.fp] and acks.is_acked(web.fp, "warn", NOW + 1) is None
        submit({"v": 1, "kind": "unack", "source": "web", "fp": web.fp, "ts": NOW + 1}, web.key)
        assert run_inbox(NOW + 1).rejected == ["unknown_issue"]

    def test_unack_is_never_rate_limited_and_never_comes_from_email(self, web, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_acks_per_day = 1\n")
        for i in range(5):
            acks.add("failed_units", 30, now=NOW)
            submit({"v": 1, "kind": "unack", "source": "web", "fp": web.fp, "ts": NOW + i}, web.key)
            assert run_inbox(NOW + i).unacked == [web.fp]
        submit({"v": 1, "kind": "unack", "source": "email", "fp": web.fp, "ts": NOW}, web.key)
        assert run_inbox().rejected == ["schema"]

    def test_a_web_ack_needs_a_known_failing_issue_or_an_existing_ack(self, web):
        submit(web_req("0" * 16), web.key)
        assert run_inbox().rejected == ["unknown_issue"] and acks.list_acks(NOW) == []
        acks.add("failed_units", 30, now=NOW)
        write_status({"failed_units": ent("ok", "ok: fine")})                                # cleared: the existing ack can still be renewed
        submit(web_req(web.fp, days=60, ts=NOW + 5), web.key)
        assert run_inbox(NOW + 5).applied == [web.fp] and acks.is_acked(web.fp, "warn", NOW + 6).until == NOW + 5 + 60 * DAY

    def test_the_severity_must_be_the_one_on_the_card(self, web):
        submit(web_req(web.fp, "crit"), web.key)                                             # it is warn now: a pre-emptive crit ack is refused
        submit(web_req(web.dfp, "warn"), web.key)                                            # it is crit now: a warn ack would not cover it
        assert sorted(run_inbox().rejected) == ["severity_changed", "severity_changed"] and acks.list_acks(NOW) == []

    def test_freshness_window(self, web, world):
        skew = acks.load_config()["inbox"]["skew_s"]
        submit(web_req(web.fp, ts=NOW - skew), web.key)
        assert run_inbox().applied == [web.fp]                                               # exactly at the edge is fine
        acks.remove(web.fp, now=NOW)
        submit(web_req(web.fp, ts=NOW - skew - 1), web.key)
        submit(web_req(web.fp, ts=NOW + skew + 1), web.key)
        submit(web_req(web.fp, ts=NOW - 86400), web.key)
        rep = run_inbox()
        assert sorted(rep.rejected) == ["future", "stale", "stale"] and acks.list_acks(NOW) == []

    def test_replays_are_recognised(self, web):
        name = submit(web_req(web.fp), web.key)
        raw = (inbox() / name).read_bytes()
        assert run_inbox().applied == [web.fp]
        first = acks.is_acked(web.fp, "warn", NOW + 1)
        submit(None, raw=raw)
        rep = run_inbox(NOW + 60)
        assert rep.applied == [] and rep.duplicates == 1 and rep.rejected == []
        assert acks.is_acked(web.fp, "warn", NOW + 61).until == first.until                     # not extended by the replay
        assert sum(1 for e in audit_events() if e["ev"] == "ack") == 1

    def test_a_crash_between_save_and_delete_repeats_nothing(self, web, monkeypatch):
        submit(web_req(web.fp), web.key)
        real = acks._remove
        monkeypatch.setattr(acks, "_remove", lambda *a: (_ for _ in ()).throw(RuntimeError("killed")))
        with pytest.raises(RuntimeError):
            run_inbox()
        monkeypatch.setattr(acks, "_remove", real)
        assert len(list(inbox().glob("*.json"))) == 1 and acks.is_acked(web.fp, "warn", NOW + 1)
        until = acks.is_acked(web.fp, "warn", NOW + 1).until
        rep = acks.process_inbox(NOW + 30)
        assert rep.duplicates == 1 and rep.applied == [] and list(inbox().glob("*.json")) == []
        assert acks.is_acked(web.fp, "warn", NOW + 31).until == until

    def test_a_crash_in_the_middle_of_the_store_write_keeps_the_old_store(self, web, monkeypatch):
        acks.add("failed_units", 30, now=NOW)
        before = (core.STATE_DIR / "acks.json").read_bytes()
        submit(web_req(web.dfp, "crit"), web.key)
        real = os.replace

        def die(src, dst, *a, **k):
            if str(dst).endswith("acks.json"):
                raise OSError("power cut")
            return real(src, dst, *a, **k)
        monkeypatch.setattr(os, "replace", die)
        with pytest.raises(OSError):
            run_inbox()
        monkeypatch.setattr(os, "replace", real)
        assert (core.STATE_DIR / "acks.json").read_bytes() == before and not list(core.STATE_DIR.glob(".ack-*.tmp"))
        assert len(list(inbox().glob("*.json"))) == 1                                         # the request was NOT lost
        assert run_inbox(NOW + 1).applied == [web.dfp]

    def test_a_killed_writers_temp_file_is_swept_later_not_at_once(self, web):
        stale, young = core.STATE_DIR / ".ack-dead.tmp", core.STATE_DIR / ".ack-alive.tmp"
        stale.write_text("x")
        young.write_text("x")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        acks.add("failed_units", 30, now=NOW)
        assert not stale.exists() and young.exists()

    def test_every_field_is_covered_by_the_signature(self, web):
        good = web_req(web.fp, days=7, note="n")
        sig = acks.sign(good, web.key)
        tampered = [{**good, "fp": web.dfp}, {**good, "days": 365}, {**good, "note": "m"}, {**good, "severity": "crit"},
                    {**good, "ts": good["ts"] + 1}, {**good, "source": "email"}, {**good, "kind": "unack"}, {**good, "v": 2}]
        for r in tampered:
            r["sig"] = sig
            submit(r, web.key, sign=False)
        rep = run_inbox()
        assert rep.applied == [] and acks.list_acks(NOW) == []
        assert all(c in ("bad_signature", "schema") for c in rep.rejected) and len(rep.rejected) == len(tampered)
        assert sum(1 for c in rep.rejected if c == "bad_signature") >= 5

    def test_a_wrong_or_missing_signature_is_refused(self, web):
        submit(web_req(web.fp), key=b"x" * 64)                                              # signed with another key
        submit(web_req(web.fp, ts=NOW + 1), sign=False)                                     # not signed at all
        submit({**web_req(web.fp, ts=NOW + 2), "sig": "0" * 64}, sign=False)
        submit({**web_req(web.fp, ts=NOW + 3), "sig": "zz"}, sign=False)
        submit({**web_req(web.fp, ts=NOW + 4), "sig": 5}, sign=False)
        rep = run_inbox()
        assert rep.applied == [] and sorted(rep.rejected) == ["bad_signature", "bad_signature", "bad_signature", "schema", "schema"]

    def test_the_signature_is_over_canonical_json_so_key_order_and_spaces_do_not_matter(self, web):
        req = web_req(web.fp, days=9)
        req["sig"] = acks.sign(req, web.key)
        raw = json.dumps(dict(reversed(list(req.items()))), indent=3).encode()
        submit(None, raw=raw)
        assert run_inbox().applied == [web.fp]
        assert acks.canonical(req) == acks.canonical({**req, "sig": "other"}) and b'"sig"' not in acks.canonical(req)

    def test_an_uppercase_hex_signature_is_accepted(self, web):
        req = web_req(web.fp)
        req["sig"] = acks.sign(req, web.key).upper()
        submit(req, sign=False)
        assert run_inbox().applied == [web.fp]

    def test_without_a_usable_key_everything_signed_is_refused(self, web):
        keyp = core.STATE_DIR / "ack" / "web.key"
        bad_modes = [(0o644, "world readable"), (0o666, "world writable"), (0o660, "group writable")]
        for i, (mode, why) in enumerate(bad_modes):
            os.chmod(keyp, mode)
            submit(web_req(web.fp, ts=NOW + i), web.key)
            assert run_inbox(NOW + i).rejected == ["no_key"], why
        os.chmod(keyp, 0o640)
        keyp.write_text("short\n")
        submit(web_req(web.fp), web.key)
        assert run_inbox().rejected == ["no_key"]
        keyp.unlink()
        submit(web_req(web.fp), web.key)
        assert run_inbox().rejected == ["no_key"]
        keyp.symlink_to(core.STATE_DIR / "ack" / "other.key")
        (core.STATE_DIR / "ack" / "other.key").write_text("k" * 64)
        os.chmod(core.STATE_DIR / "ack" / "other.key", 0o640)
        submit(web_req(web.fp), b"k" * 64)
        assert run_inbox().rejected == ["no_key"] and acks.list_acks(NOW) == []

    def test_unsigned_sources_is_the_documented_escape_for_email_only(self, web, world):
        (world.conf / "ack.toml").write_text('[inbox]\nunsigned_sources = ["email"]\n')
        tok = acks.issue_token(web.fp, "failed_units", "S", "x", "warn", NOW)
        submit(email_req(tok), sign=False)
        submit(web_req(web.dfp, "crit"), sign=False)                                          # web stays signed
        rep = run_inbox()
        assert rep.applied == [web.fp] and rep.rejected == ["bad_signature"]

    def test_require_signature_false_turns_the_hmac_off_for_all(self, web, world):
        (world.conf / "ack.toml").write_text("[inbox]\nrequire_signature = false\n")
        submit(web_req(web.fp), sign=False)
        assert run_inbox().applied == [web.fp]

    def test_oversized_files_are_removed_unread(self, web, world, monkeypatch):
        limit = acks.load_config()["inbox"]["max_bytes"]
        submit(None, raw=b"{" + b" " * limit + b"}", name=f"{int(NOW * 1000)}-aaaaaaaa.json")
        submit(None, raw=b"x" * (3 << 20), name=f"{int(NOW * 1000)}-bbbbbbbb.json")
        opened, real_open = [], os.open
        monkeypatch.setattr(os, "open", lambda path, *a, **k: (opened.append(str(path)), real_open(path, *a, **k))[1])
        t0 = time.monotonic()
        rep = run_inbox()
        assert not [n for n in opened if n.endswith(".json")]                                   # never even opened: lstat's size told enough
        assert rep.rejected == ["too_large", "too_large"] and list(inbox().glob("*.json")) == [] and time.monotonic() - t0 < 1.0
        assert rejected_files() == []                                                           # not even kept: it was never read
        assert submit(None, raw=b'{"v":1}' + b" " * (limit - 8)) and run_inbox().rejected == ["schema"]      # exactly at the limit is read

    @pytest.mark.parametrize("raw", [b"", b"not json", b'{"v": 1', b"[]", b"1", b'"str"', b"null", b"\x00\x01\x02", b"\xff\xfe{}", b'{"v":1,"v":1}',
                                     b'{"v": NaN}', b'{"ts": Infinity}', b'{"ts": -Infinity}', b"[" * 900 + b"]" * 900, b'{"a":' * 300 + b"1" + b"}" * 300,
                                     b'{"v":1,"kind":"ack"}garbage', b"{'v': 1}", b'{"ts": 1e999}', b'{"days": ' + b"9" * 500 + b"}"])
    def test_malformed_or_odd_json_is_refused_and_quarantined(self, web, raw):
        submit(None, raw=raw)
        rep = run_inbox()
        assert rep.applied == [] and len(rep.rejected) == 1 and rep.rejected[0] in ("malformed", "schema") and list(inbox().glob("*.json")) == []
        assert len(rejected_files()) == 1

    @pytest.mark.parametrize("patch", [
        {"v": 2}, {"v": "1"}, {"v": True}, {"v": None}, {"kind": "delete"}, {"kind": None}, {"kind": ["ack"]}, {"source": "root"}, {"source": None},
        {"extra": 1}, {"ts": "now"}, {"ts": None}, {"ts": True}, {"ts": float("nan")}, {"days": True}, {"days": "30"}, {"days": 0}, {"days": 366},
        {"days": 1.5}, {"days": -3}, {"days": None}, {"note": 5}, {"note": "x" * 201}, {"note": None}, {"severity": "info"}, {"severity": None},
        {"severity": "CRIT"}, {"fp": "short"}, {"fp": "G" * 16}, {"fp": "0" * 17}, {"fp": 5}, {"fp": None}, {"token_hash": "0" * 64}, {"token_hash": 5},
    ])
    def test_schema_is_strict_for_web_requests(self, web, patch):
        req = {**web_req(web.fp), **patch}
        if any(v is float("nan") or (isinstance(v, float) and v != v) for v in patch.values()):
            submit(None, raw=json.dumps({**req, "ts": None}).replace("null", "NaN").encode())
        else:
            submit(req, web.key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected and acks.list_acks(NOW) == [], rep

    @pytest.mark.parametrize("patch", [{"token_hash": "0" * 63}, {"token_hash": "G" * 64}, {"token_hash": "A" * 64}, {"token_hash": None}, {"days": 0},
                                       {"fp": "x"}, {"severity": "info"}, {"note": 5}, {"extra": 1}])
    def test_schema_is_strict_for_email_requests(self, web, patch):
        tok = acks.issue_token(web.fp, "failed_units", "S", "x", "warn", NOW)
        submit({**email_req(tok), **patch}, web.key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected and acks.list_acks(NOW) == []

    def test_a_web_request_may_not_carry_a_token_and_an_email_one_must(self, web):
        tok = acks.issue_token(web.fp, "failed_units", "S", "x", "warn", NOW)
        submit({**web_req(web.fp), "token_hash": acks.token_hash(tok)}, web.key)
        r = email_req(tok)
        del r["token_hash"]
        submit(r, web.key)
        assert run_inbox().rejected == ["schema", "schema"]

    def test_hostile_directory_entries_are_removed_and_never_read(self, web, tmp_path):
        outside = tmp_path / "outside.json"
        req = web_req(web.fp)
        req["sig"] = acks.sign(req, web.key)
        outside.write_text(json.dumps(req))
        os.symlink(outside, inbox() / f"{int(NOW * 1000)}-11111111.json")                      # a symlink to a perfectly valid request
        os.mkdir(inbox() / f"{int(NOW * 1000)}-22222222.json")                                 # a directory with a request-like name
        os.mkfifo(inbox() / f"{int(NOW * 1000)}-33333333.json")                                # a FIFO: opening it for reading would block
        for junk in ("README", "x.json", "../escape.json", "a b.json", "1-2.json", "9" * 200 + ".json"):
            if "/" not in junk:
                (inbox() / junk).write_text("x")
        t0 = time.monotonic()
        rep = run_inbox()
        assert time.monotonic() - t0 < 2.0 and rep.applied == [] and rep.rejected == ["not_regular"] * 3
        assert acks.list_acks(NOW) == [] and outside.exists() and rep.junk == 5
        assert [p.name for p in inbox().iterdir() if p.name != "rejected"] == []

    def test_a_fresh_dotfile_is_a_writer_in_flight_and_a_stale_one_is_litter(self, web):
        young, old = inbox() / ".w-young", inbox() / ".w-old"
        young.write_text("{")
        old.write_text("{")
        os.utime(old, (NOW - 3600, NOW - 3600))
        os.utime(young, (NOW - 5, NOW - 5))
        run_inbox()
        assert young.exists() and not old.exists()

    def test_the_rejected_directory_is_kept_out_of_the_way(self, web):
        run_inbox()
        d = inbox() / "rejected"
        assert d.is_dir() and d.stat().st_mode & 0o777 == 0o700

    def test_refused_files_are_quarantined_by_reason_with_private_permissions(self, web):
        submit(web_req(web.fp, ts=NOW - 86400), web.key)
        submit(None, raw=b"garbage")
        run_inbox()
        names = [p.name for p in rejected_files()]
        assert sorted(n.rsplit(".", 1)[1] for n in names) == ["malformed", "stale"]
        assert all(re.fullmatch(r"\d+-[0-9a-f]{8}\.[a-z_]+", n) for n in names)                  # the reason is a fixed word, never request data
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in rejected_files())
        assert web.fp.encode() in next(p for p in rejected_files() if p.name.endswith(".stale")).read_bytes()

    def test_the_quarantine_is_capped_to_the_newest(self, web, world):
        (world.conf / "ack.toml").write_text("[inbox]\nrejected_keep = 3\n")
        for i in range(8):
            submit(None, raw=b"junk %d" % i)
            run_inbox(NOW + i)
            time.sleep(0.002)
        assert len(rejected_files()) == 3
        (world.conf / "ack.toml").write_text("[inbox]\nrejected_keep = 0\n")
        for p in rejected_files():
            p.unlink()
        submit(None, raw=b"junk")
        run_inbox()
        assert rejected_files() == []

    def test_a_rate_limit_stops_the_51st_ack_a_day_and_spends_no_token(self, web, world):
        write_status({f"t{i}": ent("warn", f"warn: failing thing {chr(97 + i % 26)}{chr(97 + i // 26)}") for i in range(60)})
        fps = [fp_of(f"t{i}", f"warn: failing thing {chr(97 + i % 26)}{chr(97 + i // 26)}") for i in range(60)]
        for fp in fps[:50]:
            submit(web_req(fp), web.key)
        assert len(run_inbox().applied) == 50
        tok = acks.issue_token(fps[50], "t50", "T", "x", "warn", NOW)
        submit(web_req(fps[51]), web.key)
        submit(email_req(tok, ts=NOW + 1), web.key)
        rep = run_inbox(NOW + 1)
        assert rep.applied == [] and rep.rejected == ["rate_limited", "rate_limited"]
        assert store()["tokens"][acks.token_hash(tok)]["used"] is False                          # the owner can click again later
        assert json.loads((core.STATE_DIR / "public" / "acks.json").read_text())["rejected"][0] == {"id": fps[51], "kind": "ack", "reason": "rate_limited",
                                                                                                    "at": NOW + 1}
        submit(email_req(tok, ts=NOW + DAY + 2), web.key)
        assert run_inbox(NOW + DAY + 2).applied == [fps[50]]                                       # a day later the window has moved on

    def test_the_rate_limit_is_configurable(self, web, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_acks_per_day = 1\n")
        submit(web_req(web.fp), web.key)
        submit(web_req(web.dfp, "crit", ts=NOW + 1), web.key)
        assert run_inbox(NOW + 1).rejected == ["rate_limited"]

    def test_at_most_max_files_per_run_and_the_rest_waits(self, web, world):
        (world.conf / "ack.toml").write_text("[inbox]\nmax_files_per_run = 2\n")
        write_status({f"t{i}": ent("warn", f"warn: thing {chr(97 + i)}") for i in range(5)})
        for i in range(5):
            submit(web_req(fp_of(f"t{i}", f"warn: thing {chr(97 + i)}")), web.key, name=f"{int(NOW * 1000) + i}-{i:08x}.json")
        assert len(run_inbox().applied) == 2 and len(list(inbox().glob("*.json"))) == 3
        assert len(run_inbox().applied) == 2 and len(run_inbox().applied) == 1 and list(inbox().glob("*.json")) == []

    def test_a_flood_of_junk_does_not_starve_real_requests(self, web, world):
        for i in range(300):
            (inbox() / f"junk{i}").write_text("x")
        submit(web_req(web.fp), web.key)
        rep = run_inbox()
        assert rep.applied == [web.fp] and rep.junk == 300

    def test_the_max_active_cap_refuses_a_new_issue(self, web, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_active = 1\n")
        submit(web_req(web.fp), web.key)
        submit(web_req(web.dfp, "crit", ts=NOW + 1), web.key)
        assert run_inbox(NOW + 1).rejected == ["too_many"] and len(acks.list_acks(NOW + 2)) == 1

    def test_authentic_but_refused_web_requests_are_listed_for_the_site_forged_ones_are_not(self, web):
        submit(web_req("0" * 16), web.key)                                                       # authentic, refused: the site must revert the card
        submit(web_req(web.fp, ts=NOW), key=b"y" * 64)                                           # forged: no word about it anywhere public
        run_inbox()
        doc = json.loads((core.STATE_DIR / "public" / "acks.json").read_text())
        assert doc["rejected"] == [{"id": "0" * 16, "kind": "ack", "reason": "unknown_issue", "at": NOW}]
        assert acks.public_doc(NOW + 3601)["rejected"] == []                                      # and the entry ages out

    def test_no_inbox_directory_is_not_an_error(self, world):
        rep = acks.process_inbox(NOW)
        assert rep.note == "no inbox" and rep.applied == []

    def test_concurrent_processors_apply_every_request_exactly_once(self, web, world):
        n = 30
        write_status({f"t{i}": ent("warn", f"warn: thing {chr(97 + i // 26)}{chr(97 + i % 26)}") for i in range(n)})
        for i in range(n):
            submit(web_req(fp_of(f"t{i}", f"warn: thing {chr(97 + i // 26)}{chr(97 + i % 26)}")), web.key)
        reports, errors = [], []

        def worker():
            try:
                reports.append(acks.process_inbox(NOW))
            except BaseException as exc:                                                          # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert not errors and len(reports) == 8
        applied = [fp for r in reports for fp in r.applied]
        assert len(applied) == n == len(set(applied)) and len(acks.list_acks(NOW + 1)) == n
        assert sum(r.duplicates for r in reports) == 0 and list(inbox().glob("*.json")) == []

    def test_concurrent_processors_respect_the_rate_limit_exactly(self, web, world):
        (world.conf / "ack.toml").write_text("[ack]\nmax_acks_per_day = 7\n")
        write_status({f"t{i}": ent("warn", f"warn: thing {chr(97 + i)}") for i in range(20)})
        for i in range(20):
            submit(web_req(fp_of(f"t{i}", f"warn: thing {chr(97 + i)}")), web.key)
        reports = []
        ts = [threading.Thread(target=lambda: reports.append(acks.process_inbox(NOW))) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert sum(len(r.applied) for r in reports) == 7 and len(acks.list_acks(NOW + 1)) == 7

    def test_concurrent_writers_of_every_kind_lose_nothing(self, web, world):
        write_status({f"t{i}": ent("warn", f"warn: thing {chr(97 + i)}") for i in range(10)})
        fps = [fp_of(f"t{i}", f"warn: thing {chr(97 + i)}") for i in range(10)]
        errors = []

        def adds(i):
            try:
                acks.add(f"t{i}", 30, now=NOW)
                for _ in range(5):
                    acks.record_suppressed(fps[i], NOW + 1)
                acks.issue_token(fps[i], f"t{i}", "T", "x", "warn", NOW)
            except BaseException as exc:                                                          # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=adds, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        doc = store()
        assert not errors and len(doc["acks"]) == 10 and len(doc["tokens"]) == 10
        assert all(a["count_suppressed"] == 5 for a in doc["acks"].values())

    def test_a_corrupt_store_is_set_aside_and_the_inbox_still_works(self, web):
        acks.add("failed_units", 30, now=NOW)
        (core.STATE_DIR / "acks.json").write_text("{ torn")
        assert acks.is_acked(web.fp, "warn", NOW + 1) is None                                   # fail closed: the alert goes out
        submit(web_req(web.dfp, "crit"), web.key)
        assert run_inbox().applied == [web.dfp]
        assert (core.STATE_DIR / "acks.json.corrupt").read_text() == "{ torn" and acks.is_acked(web.fp, "warn", NOW + 1) is None
        assert any(e["ev"] == "store-corrupt" for e in audit_events())

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_an_unreadable_store_is_never_overwritten(self, web):
        acks.add("failed_units", 30, now=NOW)
        os.chmod(core.STATE_DIR / "acks.json", 0)
        try:
            submit(web_req(web.dfp, "crit"), web.key)
            rep = run_inbox()
            assert rep.note == "store_unreadable" and rep.applied == [] and len(list(inbox().glob("*.json"))) == 1
            assert acks.is_acked(web.fp, "warn", NOW + 1) is None
        finally:
            os.chmod(core.STATE_DIR / "acks.json", 0o600)
        assert acks.is_acked(web.fp, "warn", NOW + 1) is not None and run_inbox().applied == [web.dfp]

    def test_the_replay_digests_are_bounded_and_forgotten(self, web):
        submit(web_req(web.fp), web.key)
        run_inbox()
        assert len(store()["meta"]["seen"]) == 1
        acks.expire(NOW + 3 * DAY)                                                              # housekeeping runs on the idle path too
        with acks._txn(NOW + 3 * DAY):
            pass
        assert store()["meta"]["seen"] == {}

    def test_the_processor_holds_no_lock_while_it_updates_status_json(self, web, monkeypatch):
        """Lock order is state.lock -> acks.lock: the post-change refresh must run after the acks lock is released."""
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        seen = []
        monkeypatch.setattr(acks, "_after_change", lambda now: seen.append(acks._Lock(wait=0.2).acquire()))
        submit(web_req(web.fp), web.key)
        acks.process_inbox(NOW, refresh=True)
        assert seen == [True]

    def test_after_change_updates_status_json_overall_and_the_public_files(self, web, monkeypatch):
        acks.add("failed_units", 30, now=NOW)
        calls = []
        import homelab_maint.incidents as inc, homelab_maint.publish as pub
        monkeypatch.setattr(inc, "update", lambda st, h, now: calls.append(("incidents", st["tasks"]["failed_units"].get("acked") is not None)))
        monkeypatch.setattr(pub, "publish", lambda st, now: calls.append(("publish", st["overall"])))
        acks._after_change(NOW + 1)
        st = json.loads((core.STATE_DIR / "status.json").read_text())
        assert st["tasks"]["failed_units"]["acked"]["fp"] == web.fp and st["overall"] == "crit" and st["acked_n"] == 1
        assert calls == [("incidents", True), ("publish", "crit")]
        assert (core.STATE_DIR / "public" / "acks.json").exists() and (core.STATE_DIR / "ack" / "tokens.json").exists()

    def test_a_failing_refresh_step_does_not_stop_the_others(self, web, monkeypatch):
        import homelab_maint.incidents as inc, homelab_maint.publish as pub
        monkeypatch.setattr(inc, "update", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        calls = []
        monkeypatch.setattr(pub, "publish", lambda st, now: calls.append(1))
        acks._after_change(NOW)
        assert calls == [1]

    def test_handlers_ride_the_same_envelope(self, web, monkeypatch):
        monkeypatch.setattr(acks, "_HANDLERS", {})
        got = []

        def handler(req, now):
            got.append((req, acks.add("failed_units", 5, now=now).fp))                          # may call acks.* : the lock is not held
            return True, "ok"
        acks.register_inbox_handler("rule_change", handler, allowed_keys=("rule", "value"))
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, "rule": "r1", "value": 3}, web.key)
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, "rule": "r1", "value": 3, "evil": 1}, web.key)       # undeclared key
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW - 99999, "rule": "r1"}, web.key)                      # stale
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, "rule": "r1"}, sign=False)                           # unsigned
        submit({"v": 1, "kind": "other_kind", "source": "web", "ts": NOW}, web.key)                                          # no handler
        rep = run_inbox()
        assert rep.handled == ["rule_change"] and len(got) == 1 and got[0][0] == {"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, "rule": "r1", "value": 3}
        assert sorted(rep.rejected) == ["bad_signature", "schema", "schema", "stale"] and acks.is_acked(got[0][1], "warn", NOW + 1)

    def test_a_handler_that_raises_is_a_refusal_and_the_inbox_goes_on(self, web, monkeypatch):
        monkeypatch.setattr(acks, "_HANDLERS", {})
        acks.register_inbox_handler("boom", lambda req, now: 1 / 0)
        submit({"v": 1, "kind": "boom", "source": "web", "ts": NOW}, web.key)
        submit(web_req(web.fp, ts=NOW + 1), web.key)
        rep = run_inbox(NOW + 1)
        assert rep.rejected == ["handler_error"] and rep.applied == [web.fp]

    @pytest.mark.parametrize("patch", [{"v": 2}, {"v": True}, {"source": "root"}, {"source": None}, {"ts": None}, {"ts": "x"}, {"extra": 1}])
    def test_handler_requests_get_the_same_strict_envelope(self, web, monkeypatch, patch):
        monkeypatch.setattr(acks, "_HANDLERS", {})
        calls = []
        acks.register_inbox_handler("rule_change", lambda r, n: calls.append(r) or (True, "ok"))
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, **patch}, web.key)
        rep = run_inbox()
        assert calls == [] and rep.rejected and rep.handled == []

    def test_a_signature_that_is_not_ascii_cannot_crash_the_batch(self, web, monkeypatch):
        """compare_digest raises TypeError on a non-ASCII str; one hostile file must be ITS refusal, never the end of the run."""
        monkeypatch.setattr(acks, "_HANDLERS", {})
        acks.register_inbox_handler("rule_change", lambda r, n: (True, "ok"))
        submit({"v": 1, "kind": "rule_change", "source": "web", "ts": NOW, "sig": "\u00e9" * 64}, sign=False)
        submit(web_req(web.fp, ts=NOW + 1), web.key)
        rep = run_inbox(NOW + 1)
        assert rep.rejected == ["schema"] and rep.applied == [web.fp]

    def test_an_idle_minute_takes_no_lock_and_writes_nothing(self, web):
        before = sorted(p.name for p in core.STATE_DIR.iterdir())
        for _ in range(3):
            assert run_inbox().applied == []
        assert sorted(p.name for p in core.STATE_DIR.iterdir()) == before and not (core.STATE_DIR / "acks.lock").exists()

    def test_the_directory_listing_is_bounded(self, web, monkeypatch):
        monkeypatch.setattr(acks, "LIST_MAX", 5)
        for i in range(12):
            (inbox() / f"junk{i:02d}").write_text("x")
        rep = run_inbox()
        assert rep.junk == 5 and len([p for p in inbox().iterdir() if p.name != "rejected"]) == 7

    @pytest.mark.parametrize("kind", ["ack", "unack", "Bad", "", "x" * 30, "a-b"])
    def test_handler_kinds_are_validated(self, kind):
        with pytest.raises(ValueError):
            acks.register_inbox_handler(kind, lambda r, n: (True, ""))


# =========================================================================== expiry and the single notice
class TestExpiry:
    def test_expire_reports_each_ended_ack_once_and_keeps_the_record(self, world):
        a = make_ack(days=7)
        assert acks.expire(NOW + 7 * DAY - 1) == []
        assert acks.expire(NOW + 7 * DAY) == [a.fp] and acks.expire(NOW + 7 * DAY + 5) == []
        rec = store()["acks"][a.fp]
        assert rec["expired_at"] == NOW + 7 * DAY and rec["task"] == "failed_units" and rec["count_suppressed"] == 0
        assert acks.is_acked(a.fp, "warn", NOW + 7 * DAY) is None and acks.list_acks(NOW + 8 * DAY) == []
        assert [r.fp for r in acks.list_acks(NOW + 8 * DAY, include_expired=True)] == [a.fp]
        assert any(e["ev"] == "expire" and e["fp"] == a.fp for e in audit_events())

    def test_the_idle_minute_touches_nothing(self, world):
        make_ack()
        before = (core.STATE_DIR / "acks.json").stat().st_mtime_ns
        for i in range(5):
            assert acks.expire(NOW + 60 * i) == []
        assert (core.STATE_DIR / "acks.json").stat().st_mtime_ns == before

    def test_expired_acks_stay_listed_for_a_month_and_count_in_the_stats(self, world):
        a = make_ack(days=7)
        acks.expire(NOW + 8 * DAY)
        assert acks.public_doc(NOW + 9 * DAY)["stats"] == {"active": 0, "expired_30d": 1}
        acks.expire(NOW + 7 * DAY + 31 * DAY)
        assert a.fp not in store()["acks"] and acks.public_doc(NOW + 40 * DAY)["stats"]["expired_30d"] == 0

    def test_resuming_the_same_error_after_expiry_alerts_and_a_fresh_ack_starts_over(self, world):
        a = make_ack(days=7, note="first")
        for i in range(3):
            acks.record_suppressed(a.fp, NOW + i)
        acks.expire(NOW + 8 * DAY)
        b = acks.add("failed_units", 30, "second", now=NOW + 9 * DAY)
        assert b.fp == a.fp and b.count_suppressed == 0 and b.first_ack == NOW and b.note == "second"
        assert a.fp not in store()["meta"]["notice_pending"] and acks.is_acked(a.fp, "warn", NOW + 10 * DAY)

    def test_expire_refreshes_the_public_views(self, world):
        a = make_ack(days=1)
        tok = acks.issue_token(a.fp, "failed_units", "S", "x", "warn", NOW, ttl_days=1)
        assert acks.expire(NOW + 4 * DAY) == [a.fp]                                       # the ack ended and the token is long spent
        assert json.loads((core.STATE_DIR / "public" / "acks.json").read_text())["stats"] == {"active": 0, "expired_30d": 1}
        assert json.loads((core.STATE_DIR / "ack" / "tokens.json").read_text()) == {} and acks.token_hash(tok) not in store()["tokens"]

    def test_notice_bookkeeping_is_retried_then_given_up(self, world):
        a = make_ack(days=1)
        acks.expire(NOW + 2 * DAY)
        assert acks.pending_notices() == [a.fp]
        for i in range(4):
            acks.notice_done([a.fp], False, NOW + 3 * DAY)
            assert acks.pending_notices() == [a.fp]
        acks.notice_done([a.fp], False, NOW + 3 * DAY)                                           # the 5th failure: give up (notice_attempts)
        assert acks.pending_notices() == []
        b = make_ack("disk_forecast", "warn: /x 4% free", days=1, now=NOW + 3 * DAY)
        acks.expire(NOW + 5 * DAY)
        acks.notice_done([b.fp], True, NOW + 5 * DAY)
        assert acks.pending_notices() == []

    def test_expire_is_safe_with_a_broken_store(self, world):
        make_ack(days=1)
        (core.STATE_DIR / "acks.json").write_text("{")
        assert acks.expire(NOW + 5 * DAY) == []

    def test_run_once_does_the_whole_minute(self, world, monkeypatch):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("crit", "crit: / 3% free")})
        a = acks.add("disk_forecast", 1, now=NOW)
        submit(web_req(fp_of("failed_units", "warn: 1 unhealthy: kavita"), ts=NOW + 2 * DAY), key)
        sent, refreshed = [], []
        monkeypatch.setattr(acks, "_notify_expired", lambda due, now: sent.append(list(due)) or True)
        monkeypatch.setattr(acks, "_after_change", lambda now: refreshed.append(now))
        r = acks.run_once(NOW + 2 * DAY)
        assert r == {"applied": 1, "unacked": 0, "rejected": 0, "duplicates": 0, "expired": 1, "notices": 1, "skipped": False}
        assert sent == [[a.fp]] and refreshed == [NOW + 2 * DAY] and acks.pending_notices() == []
        assert acks.run_once(NOW + 2 * DAY + 60)["notices"] == 0 and sent == [[a.fp]]               # exactly one notice

    def test_a_notice_that_could_not_be_handed_over_is_retried_next_minute(self, world, monkeypatch):
        a = make_ack(days=1)
        results = iter([False, True])
        calls = []
        monkeypatch.setattr(acks, "_notify_expired", lambda due, now: calls.append(list(due)) or next(results))
        monkeypatch.setattr(acks, "_after_change", lambda now: None)
        acks.run_once(NOW + 2 * DAY)
        assert acks.pending_notices() == [a.fp]
        acks.run_once(NOW + 2 * DAY + 60)
        assert acks.pending_notices() == [] and calls == [[a.fp], [a.fp]]

    def test_notify_expired_failure_is_contained(self, world, monkeypatch):
        a = make_ack(days=1)
        monkeypatch.setattr(notify, "notify_expired", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
        assert acks._notify_expired([a.fp], NOW) is False and any(e["ev"] == "notice-failed" for e in audit_events())


# =========================================================================== exports
class TestExports:
    def test_public_doc_shape_and_values(self, world):
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("ok", "ok: fine")})
        a = acks.add("failed_units", 30, "known flap", now=NOW)
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("warn", "warn: /x 4% free")})
        b = acks.add("disk_forecast", 7, now=NOW)
        write_status({"failed_units": ent("ok", "ok: fine")})
        acks.record_suppressed(a.fp, NOW + 5)
        doc = acks.public_doc(NOW + DAY)
        assert set(doc) == {"generated_at", "acks", "rejected", "stats"} and doc["stats"] == {"active": 2, "expired_30d": 0}
        assert [r["id"] for r in doc["acks"]] == [b.fp, a.fp]                                      # soonest expiry first
        row = doc["acks"][1]
        assert set(row) == {"id", "task", "title", "summary", "severity", "acked_at", "until", "days_left", "by", "note", "suppressed", "active"}
        assert (row["task"], row["severity"], row["days_left"], row["suppressed"], row["note"], row["by"]) == ("failed_units", "warn", 29, 1, "known flap", "cli")
        assert row["active"] is False and doc["acks"][0]["active"] is False                         # neither is failing in the current status.json

    def test_active_means_the_error_is_failing_right_now_at_or_below_the_ceiling(self, world):
        a = make_ack(sev="warn")
        assert acks.public_doc(NOW)["acks"][0]["active"] is True
        write_status({"failed_units": ent("crit", "crit: 1 unhealthy: kavita")})
        assert acks.public_doc(NOW)["acks"][0]["active"] is False                                   # escalated past the ceiling: it is alerting again
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: other")})
        assert acks.public_doc(NOW)["acks"][0]["active"] is False and a.fp

    def test_files_and_permissions(self, world):
        make_ack()
        acks.export_public(NOW)
        acks.export_tokens(NOW)
        assert (core.STATE_DIR / "public" / "acks.json").stat().st_mode & 0o777 == 0o644
        assert (core.STATE_DIR / "ack" / "tokens.json").stat().st_mode & 0o777 == 0o644
        assert (core.STATE_DIR / "public").stat().st_mode & 0o777 == 0o755 and (core.STATE_DIR / "ack").stat().st_mode & 0o777 == 0o755
        json.loads((core.STATE_DIR / "public" / "acks.json").read_text())

    def test_the_directories_are_never_removed_or_recreated(self, world):
        make_ack()
        acks.export_public(NOW)
        acks.export_tokens(NOW)
        ino = (core.STATE_DIR / "public").stat().st_ino, (core.STATE_DIR / "ack").stat().st_ino
        marker = core.STATE_DIR / "ack" / "keep.me"
        marker.write_text("x")
        for _ in range(3):
            acks.export_public(NOW)
            acks.export_tokens(NOW)
        assert ((core.STATE_DIR / "public").stat().st_ino, (core.STATE_DIR / "ack").stat().st_ino) == ino and marker.exists()

    def test_a_crash_while_exporting_keeps_the_previous_file(self, world, monkeypatch):
        make_ack()
        acks.export_public(NOW)
        before = (core.STATE_DIR / "public" / "acks.json").read_bytes()
        real = os.replace
        monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("power cut")))
        with pytest.raises(OSError):
            acks.export_public(NOW + DAY)
        monkeypatch.setattr(os, "replace", real)
        assert (core.STATE_DIR / "public" / "acks.json").read_bytes() == before

    def test_text_is_redacted_in_the_public_file(self, world):
        write_status({"t": ent("warn", "warn: failed for ops@example.com at https://x.example/p?token=SECRET1 password=hunter2 "
                                       "key AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCd call +1 416 555 0199", title="Mail ops@example.com")})
        acks.add("t", 5, "api_key=sk-12345678901234567890 text 416-555-0199", now=NOW)
        text = (acks.export_public(NOW)).read_text()
        for leak in ("example.com", "SECRET1", "hunter2", "AbCdEfGh", "555", "sk-1234"):
            assert leak not in text, leak

    def test_the_public_file_holds_no_token_hash_signature_or_key(self, world):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        tok = acks.issue_token(fp, "failed_units", "S", "x", "warn", NOW)
        req = email_req(tok)
        req["sig"] = acks.sign(req, key)
        submit(req, sign=False)
        run_inbox()
        text = (core.STATE_DIR / "public" / "acks.json").read_text()
        for secret in (tok, acks.token_hash(tok), req["sig"], key.decode(), acks.token_hash(tok)[:16]):
            assert secret not in text

    def test_nothing_secret_reaches_any_file_log_or_report(self, world, capsys):
        """The whole life of an acknowledgement, then every byte it left behind is searched for the plaintext token, the HMAC key, the
        signatures and (outside acks.json and tokens.json, which hold hashes by design) the token hash."""
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "disk_forecast": ent("crit", "crit: / 3% free")})
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        tok = acks.issue_token(fp, "failed_units", "S", "x", "warn", NOW)
        h = acks.token_hash(tok)
        reqs = []
        for r in (email_req(tok), web_req(fp_of("disk_forecast", "crit: / 3% free", "crit"), "crit"), email_req("B" * 43, ts=NOW + 1)):
            r = dict(r)
            r["sig"] = acks.sign(r, key)
            reqs.append(r)
            submit(r, sign=False)
        rep = run_inbox()
        acks.expire(NOW + 100 * DAY)
        acks.main(["list", "--all"])
        out = capsys.readouterr().out + json.dumps(rep.as_dict()) + json.dumps(world.log, default=str)
        secrets_ = [tok, key.decode()] + [r["sig"] for r in reqs]
        for p in core.STATE_DIR.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                for s in secrets_:
                    if p.name == "web.key":
                        continue
                    assert s.encode() not in data, (p, s[:6])
                if p.name not in ("acks.json", "tokens.json"):
                    assert h.encode() not in data, p
        for s in secrets_ + [h]:
            assert s not in out
        assert any(h[:8] in e.get("th", "") for e in audit_events())                                # a short prefix identifies the token in the trail

    def test_export_cli_writes_both_files(self, world, capsys):
        make_ack()
        assert acks.main(["export"]) == 0
        out = capsys.readouterr().out
        assert "acks.json" in out and "tokens.json" in out


# =========================================================================== command line
class TestCli:
    def test_list_add_remove(self, world, capsys):
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        assert acks.main(["list"]) == 0 and "0 acknowledged" in capsys.readouterr().out
        assert acks.main(["add", "failed_units", "--days", "30", "--note", "known flap", "--severity", "crit"]) == 0
        out = capsys.readouterr().out
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        assert fp in out and "crit" in out
        assert acks.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "1 acknowledged" in out and fp in out and "known flap" in out and "by cli" in out
        assert acks.main(["remove", fp]) == 0 and "normal alerting resumes" in capsys.readouterr().out
        assert acks.main(["remove", fp]) == 1 and "no such" in capsys.readouterr().out

    def test_errors_and_usage(self, world, capsys):
        write_status({"t": ent("warn", "warn: x")})
        assert acks.main(["add", "nope"]) == 1 and "not failing" in capsys.readouterr().err
        assert acks.main(["add", "t", "--days", "0"]) == 1
        assert acks.main(["add", "t", "--days", "x"]) == 1 and "ValueError" in capsys.readouterr().err
        assert acks.main(["add", "t", "--days"]) == 1
        assert acks.main(["add"]) == 2 and acks.main(["add", "a", "b"]) == 2 and acks.main(["bogus"]) == 2 and acks.main(["remove"]) == 2
        assert "homelab-maint ack" in capsys.readouterr().err

    def test_process_prints_counts_and_nothing_secret(self, world, capsys):
        key = ready()
        write_status({"t": ent("warn", "warn: x")})
        tok = acks.issue_token(fp_of("t", "warn: x"), "t", "T", "x", "warn", NOW)
        submit(email_req(tok, ts=time.time()), key)
        assert acks.run_once(time.time(), refresh=False)["applied"] == 1
        out = capsys.readouterr().out
        assert tok not in out

    def test_process_command(self, world, capsys, monkeypatch):
        monkeypatch.setattr(acks, "_after_change", lambda now: None)
        assert acks.main(["process"]) == 0
        assert capsys.readouterr().out.strip() == "acks: 0 applied, 0 removed, 0 rejected, 0 expired"

    def test_issue_token_prints_the_url_once_and_stores_only_the_hash(self, world, capsys, monkeypatch):
        write_status({"t": ent("warn", "warn: x")})
        fp = fp_of("t", "warn: x")
        assert acks.main(["issue-token", fp, "--ttl-days", "7", "--days", "30"]) == 0
        url = capsys.readouterr().out.strip()
        m = re.fullmatch(rf"https://maintainer\.ohmzhomelab\.ca/ack\?id={fp}&t=([A-Za-z0-9_-]{{43}})&d=30&s=warn", url)
        assert m, url
        tok = m.group(1)
        assert acks.token_hash(tok) in store()["tokens"] and tok.encode() not in (core.STATE_DIR / "acks.json").read_bytes()
        assert store()["tokens"][acks.token_hash(tok)]["exp"] - store()["tokens"][acks.token_hash(tok)]["issued_at"] == 7 * DAY
        assert acks.main(["issue-token", "t"]) == 0

    def test_issue_token_keeps_nothing_when_no_link_can_be_built(self, world, capsys, monkeypatch):
        write_status({"t": ent("warn", "warn: x")})
        monkeypatch.setattr(acks, "_link", lambda *a: "")
        assert acks.main(["issue-token", "t"]) == 1
        assert "no token kept" in capsys.readouterr().err and store()["tokens"] == {}

    def test_init_creates_the_tree_once(self, world, capsys):
        assert acks.main(["init"]) == 0
        out = capsys.readouterr().out
        assert "created web.key" in out
        d = core.STATE_DIR / "ack"
        assert d.stat().st_mode & 0o777 == 0o750 and (d / "inbox").stat().st_mode & 0o7777 == 0o1730
        assert (d / "inbox" / "rejected").stat().st_mode & 0o777 == 0o700 and (d / "web.key").stat().st_mode & 0o777 == 0o640
        key = (d / "web.key").read_text()
        assert re.fullmatch(r"[0-9a-f]{64}\n", key) and (d / "tokens.json").exists()
        assert acks.main(["init"]) == 0 and "nothing to do" in capsys.readouterr().out and (d / "web.key").read_text() == key

    def test_validate_command(self, world, capsys):
        assert acks.main(["validate"]) == 0 and "ack.toml ok" in capsys.readouterr().out
        (world.conf / "ack.toml").write_text("[ack]\ndays = 0\n")
        assert acks.main(["validate"]) == 1 and "warning" in capsys.readouterr().out

    def test_entry_points(self):
        src = (ROOT / "homelab_maint" / "acks.py").read_text()
        assert 'if __name__ == "__main__":' in src and "sys.exit(main())" in src


class TestDoctor:
    def rows(self):
        return {label: (ok, hint) for label, ok, hint in acks.doctor(NOW)}

    def test_a_fresh_install_says_what_is_missing(self, world):
        r = self.rows()
        assert r["ack.toml valid"][0] and r["acks.json readable"][0] and r["no expiry notice is stuck"][0]
        assert not r["ack inbox present"][0] and "ack init" in r["ack inbox present"][1]
        assert not r["ack HMAC key usable"][0]
        assert acks.main(["doctor"]) == 1

    def test_everything_ok_after_init(self, world, capsys):
        ready()
        assert all(ok for ok, _ in self.rows().values())
        assert acks.main(["doctor"]) == 0
        assert capsys.readouterr().out.count("[ok]") == 6 and "FAIL" not in capsys.readouterr().out

    def test_a_request_nobody_processes_is_flagged(self, world):
        key = ready()
        name = submit(web_req("0" * 16), key)
        os.utime(inbox() / name, (NOW - 3600, NOW - 3600))
        ok, hint = self.rows()["ack inbox is being processed"]
        assert not ok and "1 request(s) waited" in hint and "ack-process" in hint
        run_inbox()
        assert self.rows()["ack inbox is being processed"][0]

    def test_a_corrupt_store_a_bad_config_a_bad_key_and_a_stuck_notice_are_flagged(self, world):
        ready()
        (core.STATE_DIR / "acks.json").write_text("{")
        (world.conf / "ack.toml").write_text("[ack]\ndays = 0\n")
        os.chmod(core.STATE_DIR / "ack" / "web.key", 0o644)
        r = self.rows()
        assert not r["acks.json readable"][0] and not r["ack.toml valid"][0] and not r["ack HMAC key usable"][0]
        (core.STATE_DIR / "acks.json").unlink()
        make_ack(days=1)
        acks.expire(NOW + 2 * DAY)
        assert not self.rows()["no expiry notice is stuck"][0]

    def test_it_never_raises_and_never_creates_anything(self, world, monkeypatch):
        monkeypatch.setattr(acks, "validate", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        rows = acks.doctor(NOW)
        assert rows[-1][0] == "acknowledgements self-check" and not rows[-1][1] and not (core.STATE_DIR / "ack").exists()


class TestInstallHelpers:
    def test_init_survives_a_chown_that_is_refused(self, world, monkeypatch, capsys):
        """A staging run in a user namespace cannot chown to an unmapped group: that is a message, not a crash."""
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        def refuse(*a, **k):
            raise PermissionError(22, "Invalid argument")
        monkeypatch.setattr(os, "chown", refuse)
        done = acks.init_dirs(10001)
        assert "created web.key" in done and sum("cannot chown" in d for d in done) == 3
        assert (core.STATE_DIR / "ack" / "web.key").exists() and (core.STATE_DIR / "ack" / "tokens.json").exists()

    def test_an_unknown_group_name_is_an_error_message(self, world, monkeypatch, capsys):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert acks.main(["init", "--group", "no-such-group-xyz"]) == 1
        assert "KeyError" in capsys.readouterr().err

    def test_init_without_a_group_or_as_a_normal_user_never_chowns(self, world, monkeypatch):
        monkeypatch.setattr(os, "chown", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chown called")))
        acks.init_dirs()
        acks.init_dirs(10001)                                                    # not root here: ignored


# =========================================================================== hand-off to notify (real notify.send, fake transport)
class FakeTransport:
    def __init__(self):
        self.calls: list[notify.Message] = []

    def __call__(self, msg, nc):
        self.calls.append(msg)
        return notify.TransportResult(True, {c: "sent" for c in msg.channels}, {}, "", 0, "fake")


@pytest.fixture()
def wire(world, monkeypatch):
    """notify.send is real again, with acks.py as its acknowledgements module and a transport that records instead of sending."""
    for n, fn in _REAL.items():
        monkeypatch.setattr(notify, n, fn)                  # (hermes_transport and bridge_transport stay banned: only the fake can deliver)
    monkeypatch.setattr(notify, "acks_loader", lambda: acks)
    monkeypatch.setattr(notify, "playbook_loader", lambda task: [])
    tr = FakeTransport()
    cfg = {"notify": {"site": {"host_label": "testhost"}, "quiet_hours": {"tz": "America/Toronto", "enabled": False},
                      "ack": {"button": True, "mint_url": ""}}}     # (button "auto" waits for the site; mint_url "" = mint locally, not at the hub)
    return types.SimpleNamespace(tr=tr, cfg=cfg, send=lambda ev, now: notify.send(ev, cfg, now, transport=tr))


def alert(summary="warn: 1 unhealthy: kavita", sev="warn", task="failed_units", title="Services & containers"):
    return notify.Event("alert", sev, title, summary, task=task, dedupe_key=task)


def log_rows():
    p = core.STATE_DIR / "notifications.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class TestNotifyHandOff:
    def test_an_acknowledged_alert_is_held_counted_and_logged(self, wire):
        a = make_ack()
        d = wire.send(alert(), NOW + 60)
        assert d.skipped == "acknowledged" and d.handled and not d.ok and wire.tr.calls == []
        assert f"acknowledged until {time.strftime('%Y-%m-%d', time.localtime(a.until))}" in d.note
        assert store()["acks"][a.fp]["count_suppressed"] == 1
        assert any("suppressed: acknowledged until" in str(r.get("note")) for r in log_rows())

    def test_other_errors_and_other_tasks_still_page(self, wire):
        make_ack()
        d = wire.send(alert("warn: 1 unhealthy: radarr"), NOW + 60)
        assert d.ok and len(wire.tr.calls) == 1
        d = wire.send(alert("warn: 1 unhealthy: kavita", task="probes", title="Probes"), NOW + 61)
        assert d.ok and len(wire.tr.calls) == 2

    def test_escalation_breaks_the_ack_and_pages_with_a_fresh_button(self, wire):
        a = make_ack(sev="warn")
        assert wire.send(alert(sev="warn"), NOW + 60).skipped == "acknowledged"
        d = wire.send(alert("crit: 1 unhealthy: kavita", "crit"), NOW + 120)
        assert d.ok and len(wire.tr.calls) == 1 and a.fp
        assert "kavita" in wire.tr.calls[0].plain

    def test_a_crit_alert_carries_no_acknowledge_offer(self, wire, world):
        (world.conf / "ack.toml").write_text('[ack]\nseverities = ["warn"]\n')
        d = wire.send(alert("crit: 1 unhealthy: kavita", "crit"), NOW)
        assert d.ok and len(wire.tr.calls) == 1
        plain = wire.tr.calls[0].plain
        assert "/ack?" not in plain and "Issue ID" not in plain
        assert not (core.STATE_DIR / "acks.json").exists() or not store()["tokens"]
        d = wire.send(alert("warn: 1 unhealthy: kavita", "warn"), NOW + 1)
        assert d.ok and len(wire.tr.calls) == 2 and "/ack?" in wire.tr.calls[1].plain

    def test_a_crit_ack_expired_notice_offers_no_second_button(self, wire, world):
        """An acknowledgement that ended because the issue escalated to crit must not invite a re-acknowledgement: the notice travels its
        fingerprint in facts.ack_fp, past the fingerprint policy, so the offer itself has to refuse a crit severity."""
        a = make_ack(sev="warn", days=7)
        write_status({"failed_units": ent("crit", "crit: 1 unhealthy: kavita")})            # escalated: now crit
        (world.conf / "ack.toml").write_text('[ack]\nseverities = ["warn"]\n')
        assert acks.expire(NOW + 7 * DAY + 1) == [a.fp]
        out = notify.notify_expired(NOW + 7 * DAY + 2, wire.cfg, expired=[a.fp], transport=wire.tr)
        assert len(out) == 1 and out[0].ok and len(wire.tr.calls) == 1
        plain = wire.tr.calls[0].plain
        assert "still failing" in plain and "/ack?" not in plain and "Issue ID" not in plain
        assert "button above" not in plain and "ack add" not in plain       # nothing to re-acknowledge with: the wording must not promise one

    def test_the_number_and_age_moving_do_not_page_again(self, wire):
        make_ack("disk_forecast", "warn: /mnt/x 4% free (210.0 GiB), full in 5d", "warn")
        d = wire.send(alert("warn: /mnt/x 3% free (190.0 GiB), full in 2d", task="disk_forecast", title="Disk space"), NOW + 90)
        assert d.skipped == "acknowledged" and wire.tr.calls == []

    def test_expiry_resumes_paging_and_sends_one_notice(self, wire, monkeypatch):
        a = make_ack(days=7)
        assert wire.send(alert(), NOW + 3 * DAY).skipped == "acknowledged"
        assert acks.expire(NOW + 7 * DAY + 1) == [a.fp]
        d = wire.send(alert(), NOW + 7 * DAY + 2)
        assert d.ok and len(wire.tr.calls) == 1 and "Acknowledge" in wire.tr.calls[0].plain       # alerts again, with a fresh button
        monkeypatch.setattr(notify, "send", _REAL_SEND)
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        out = notify.notify_expired(NOW + 7 * DAY + 3, wire.cfg, expired=[a.fp], transport=wire.tr)
        assert len(out) == 1 and out[0].ok and len(wire.tr.calls) == 2
        assert "ended" in wire.tr.calls[1].plain and "still failing" in wire.tr.calls[1].plain

    def test_an_alert_that_was_delivered_has_its_recovery_held_once_acknowledged(self, wire):
        d = wire.send(alert(), NOW)
        assert d.ok and len(wire.tr.calls) == 1
        a = make_ack()                                                                              # the owner clicks the button in that e-mail
        rec = notify.Event("recovery", "ok", "Services & containers", "ok: fine", task="failed_units", dedupe_key="failed_units")
        d = wire.send(rec, NOW + 900)
        assert d.skipped == "acknowledged" and len(wire.tr.calls) == 1 and a.fp

    def test_the_issued_token_in_a_real_alert_email_is_bound_and_hashed(self, wire, monkeypatch):
        d = wire.send(alert(), NOW)
        assert d.ok
        link = re.search(r"https://\S+/ack\?id=([0-9a-f]{16})&t=([A-Za-z0-9_-]{43})&d=\d+&s=warn", wire.tr.calls[0].plain)
        assert link, "the alert e-mail carries no Acknowledge link (notify.py / acks.issue_token out of step)"
        fp, tok = link.groups()
        assert fp == fp_of("failed_units", "warn: 1 unhealthy: kavita")
        rec = store()["tokens"][acks.token_hash(tok)]
        assert rec["fp"] == fp and rec["severity"] == "warn" and rec["exp"] == pytest.approx(NOW + 30 * DAY, abs=1) and tok not in json.dumps(log_rows())

    def test_core_notifier_state_stays_consistent_through_an_acknowledged_episode(self, wire, monkeypatch):
        """The debounce state machine runs exactly as before; only delivery is held. After the recovery there is no residue: alerted 0, level 0."""
        monkeypatch.setattr(notify, "hermes_transport", wire.tr)
        monkeypatch.setattr(notify, "bridge_transport", wire.tr)
        cfg = {**wire.cfg, "global": {"alert_confirm_runs": 2, "alert_reminder_hours": 24}}
        n = core.Notifier(cfg)
        res = core.Result("warn", "warn: 1 unhealthy: kavita")
        ok = core.Result("ok", "ok: fine")
        a = make_ack()                                                                              # acknowledged BEFORE it ever pages
        t = NOW
        for r in (res, res, res):
            n.evaluate("failed_units", "Services & containers", r, t)
            t += 900
        st = n.s["tasks"]["failed_units"]
        assert st["level"] == 1 and st["alerted"] == 1 and wire.tr.calls == []                       # confirmed, "sent" (held), nothing on the wire
        n.evaluate("failed_units", "Services & containers", core.Result("crit", "crit: 1 unhealthy: kavita"), t)
        n.evaluate("failed_units", "Services & containers", core.Result("crit", "crit: 1 unhealthy: kavita"), t + 900)
        assert len(wire.tr.calls) == 1 and n.s["tasks"]["failed_units"]["alerted"] == 2             # the escalation pages
        t += 1800
        for r in (res, res):                                                                         # back to warn: the ack covers it again
            n.evaluate("failed_units", "Services & containers", r, t)
            t += 900
        for r in (ok, ok):
            n.evaluate("failed_units", "Services & containers", r, t)
            t += 900
        st = n.s["tasks"]["failed_units"]
        assert st["level"] == 0 and st["alerted"] == 0
        assert a.fp and acks.is_acked(a.fp, "warn", t)

    def test_acknowledged_issue_recovery_stays_silent_end_to_end(self, wire, monkeypatch):
        monkeypatch.setattr(notify, "hermes_transport", wire.tr)
        monkeypatch.setattr(notify, "bridge_transport", wire.tr)
        n = core.Notifier({**wire.cfg, "global": {"alert_confirm_runs": 2}})
        make_ack()
        t = NOW
        for r in ("warn: 1 unhealthy: kavita",) * 2 + ("ok: fine",) * 2:
            n.evaluate("failed_units", "Services & containers", core.Result("ok" if r.startswith("ok") else "warn", r), t)
            t += 900
        assert wire.tr.calls == [] and n.s["tasks"]["failed_units"]["alerted"] == 0 and n.s["tasks"]["failed_units"]["level"] == 0


# =========================================================================== review round 2: regressions
# An independent review found five ways an acknowledgement could outlive the thing that was acknowledged. Each class below is the exact
# failure scenario the reviewer verified, as a test (and the neighbouring cases that must keep working).
def strict(world, extra: str = "") -> None:
    """The shipped policy (`require_rule = true`): only exact-error tasks may be acknowledged. The autouse fixture relaxes it for the rest."""
    (world.conf / "ack.toml").write_text("[ack]\nrequire_rule = true\n" + extra)


def fpm(task: str, summary: str, sev: str = "warn", metrics: dict | None = None, **kw):
    """Fingerprint of a status entry the way mark_entry sees it (status, summary, metrics)."""
    return acks.fingerprint(task, ent(sev, summary, **({"metrics": metrics} if metrics is not None else {}), **kw), sev)


# (task, the acknowledged summary, same decade (must stay the same error), next decade(s) (must alert again)): the severity never moves
MAGNITUDE = [
    ("backup_freshness", "warn: stack-backup 28h old (limit 26h)", ["warn: stack-backup 30h old (limit 26h)", "warn: stack-backup 99h old (limit 26h)"],
     ["warn: stack-backup 4d4h old (limit 26h)", "warn: stack-backup 30d old (limit 26h)", "warn: stack-backup 400d old (limit 26h)"]),
    ("smart_trend", "SMART: sda realloc +2, 72C", ["SMART: sda realloc +7, 80C"], ["SMART: sda realloc +12, 72C", "SMART: sda realloc +8000, 72C"]),
    ("smart_trend", "SMART: sda pending +1", ["SMART: sda pending +9"], ["SMART: sda pending +5000"]),
    ("smart_trend", "SMART: sda CRC +1", ["SMART: sda CRC +9"], ["SMART: sda CRC +900"]),
    ("growth_watch", "growth over limit: kavita/logs +1.20 GiB/d", ["growth over limit: kavita/logs +9.99 GiB/d"],
     ["growth over limit: kavita/logs +12.00 GiB/d", "growth over limit: kavita/logs +300.00 GiB/d"]),
    ("memory_health", "warn: 1 OOM kill(s) in last 6h (x)", ["warn: 9 OOM kill(s) in last 6h (x)"], ["warn: 50 OOM kill(s) in last 6h (x)"]),
    ("memory_health", "warn: swap-in 140 pages/s (x)", ["warn: swap-in 999 pages/s (x)"], ["warn: swap-in 14000 pages/s (x)"]),
    ("orphan_report", "3 zombies (0 B anon)", ["9 zombies (0 B anon)"], ["30 zombies (0 B anon)", "3000 zombies (0 B anon)"]),
    ("orphan_report", "1 idle gradle daemon (1.2 GiB anon)", ["1 idle gradle daemon (9.0 GiB anon)"], ["1 idle gradle daemon (40.0 GiB anon)"]),
    ("stuck_detector", "1 candidate(s), 1 actionable: tunarr 4.1 GiB leak/runaway", ["1 candidate(s), 1 actionable: tunarr 9.9 GiB leak/runaway"],
     ["1 candidate(s), 1 actionable: tunarr 40.0 GiB leak/runaway"]),
]


class TestMagnitude:
    """Finding 1 (high): backup age, SMART counters, growth rate, OOM kills, zombies and container size can only ever be `warn`, so the
    severity ceiling never fired and a worsening problem stayed acknowledged for 90-365 days."""

    @pytest.mark.parametrize("task,base,same,worse", MAGNITUDE)
    def test_same_decade_is_the_same_error_the_next_decade_is_another(self, task, base, same, worse):
        fa = fpm(task, base)
        assert fa and fa.mode == "regex"
        for s in same:
            assert fpm(task, s) == fa, (fa.key, fpm(task, s).key)
        for s in worse:
            assert fpm(task, s) != fa, (fa.key, fpm(task, s).key)

    @pytest.mark.parametrize("task,base,same,worse", MAGNITUDE)
    def test_a_worsening_problem_is_not_covered_by_the_old_acknowledgement(self, world, task, base, same, worse):
        """End to end: acknowledge, let the problem grow a decade at the SAME severity: status.json shows it un-acked and notify would page."""
        write_status({task: ent("warn", base)})
        a = acks.add(task, 90, now=NOW)
        st = {"tasks": {task: ent("warn", same[0])}}
        acks.apply_to_status(st, NOW + DAY)
        assert st["tasks"][task]["acked"]["fp"] == a.fp and st["overall"] == "ok"
        st = {"tasks": {task: ent("warn", worse[-1])}}
        acks.apply_to_status(st, NOW + 30 * DAY)
        assert "acked" not in st["tasks"][task] and st["overall"] == "warn" and st["tasks"][task]["fp"] != a.fp
        assert acks.is_acked(st["tasks"][task]["fp"], "warn", NOW + 30 * DAY) is None
        write_status({task: ent("warn", base)})
        assert acks.is_acked(a.fp, "warn", NOW + 31 * DAY)                      # it improves back to what was acknowledged: still acknowledged

    def test_the_decade_is_counted_in_hours_and_gibibytes(self):
        for text, want in (("28h", "b2"), ("5d15h", "b3"), ("30d", "b3"), ("400d", "b4"), ("90m", "b1"), ("+8000", "b4"), ("+2", "b1"), ("0.4", "b0"),
                           ("4.1 GiB", "b1"), ("40 GiB", "b2"), ("512 MiB", "b0"), ("1.5 TiB", "b4"), ("0 B", "b0")):
            assert acks._bucket(text) == want, text
        assert acks._bucket("n/a") == "n/a" and acks._bucket("9" * 40) != "b40"        # not a quantity (or absurd): kept as text, more specific

    def test_a_backup_that_failed_is_its_own_error_and_a_late_one_is_keyed_by_name_and_lateness(self):
        failed = fpm("backup_freshness", "crit: backup-system FAILED (6d10h ago)", "crit")
        assert failed == fpm("backup_freshness", "crit: backup-system FAILED (7d1h ago)", "crit")
        assert failed != fpm("backup_freshness", "crit: backup-system UNKNOWN (6d10h ago)", "crit")
        assert fpm("backup_freshness", "warn: stack-backup 28h old (limit 26h)") != fpm("backup_freshness", "warn: backup-immich 28h old (limit 26h)")
        both = fpm("backup_freshness", "crit: backup-system FAILED (6d ago); stack-backup 28h old (limit 26h)", "crit")
        assert both != fpm("backup_freshness", "crit: backup-system FAILED (6d ago); stack-backup 30d old (limit 26h)", "crit")

    def test_smart_trend_keys_the_disk_and_the_attribute_not_the_value(self):
        a = fpm("smart_trend", "SMART: sda realloc +2, 72C; nvme0 70C")
        assert a == fpm("smart_trend", "SMART: nvme0 71C; sda realloc +5, 73C")           # order, values inside the decade, temperatures
        assert a != fpm("smart_trend", "SMART: sda pending +2, 72C; nvme0 70C")           # another attribute
        assert a != fpm("smart_trend", "SMART: sdb realloc +2, 72C; nvme0 70C")           # another disk
        assert a != fpm("smart_trend", "SMART: sda realloc +2, 72C")                      # a disk less
        assert fpm("smart_trend", "SMART: sda Reallocated_Sector_Ct +2") != fpm("smart_trend", "SMART: sda Reallocated_Sector_Ct +2000")
        assert fpm("smart_trend", "SMART: sda smartd data 7h old") != fpm("smart_trend", "SMART: sda 72C")

    def test_smart_trend_follows_the_physical_disk_when_the_kernel_name_swaps(self):
        """sda/sdb can swap between boots: the acknowledgement belongs to the DISK (model + serial tail from the status metrics)."""
        wd, ssd = "WDC WD40EFRX 1A2B", "Samsung SSD 870 9Z9Z"
        boot1 = {"devices": [{"dev": "sda", "model": wd}, {"dev": "sdb", "model": ssd}]}
        boot2 = {"devices": [{"dev": "sda", "model": ssd}, {"dev": "sdb", "model": wd}]}
        wd_before = fpm("smart_trend", "SMART: sda realloc +2", metrics=boot1)
        assert "wdc wd40efrx 1a2b" in wd_before.key and "sda" not in wd_before.key
        assert fpm("smart_trend", "SMART: sdb realloc +2", metrics=boot2) == wd_before            # the same WD disk, now called sdb
        assert fpm("smart_trend", "SMART: sda realloc +2", metrics=boot2) != wd_before            # a DIFFERENT disk is now sda: not acknowledged
        res = core.Result("warn", "SMART: sdb realloc +2", metrics=boot2)
        assert acks.fingerprint("smart_trend", res, "warn") == wd_before                           # a Result and a status entry agree
        assert fpm("smart_trend", "SMART: sdb realloc +2", metrics={"devices": []}).key.startswith("more") is False   # no row: the raw name, never a crash
        assert fpm("smart_trend", "SMART: sdb realloc +2", metrics={"devices": "junk"}) and fpm("smart_trend", "SMART: sdb realloc +2", metrics=5)

    def test_smart_trend_with_a_swapped_name_is_not_acked_by_the_other_disks_acknowledgement(self, world):
        boot1 = {"devices": [{"dev": "sda", "model": "WDC WD40EFRX 1A2B"}, {"dev": "sdb", "model": "Samsung SSD 870 9Z9Z"}]}
        write_status({"smart_trend": ent("warn", "SMART: sda realloc +2", metrics=boot1)})
        a = acks.add("smart_trend", 90, now=NOW)
        boot2 = {"devices": [{"dev": "sda", "model": "Samsung SSD 870 9Z9Z"}, {"dev": "sdb", "model": "WDC WD40EFRX 1A2B"}]}
        st = {"tasks": {"smart_trend": ent("warn", "SMART: sda realloc +2", metrics=boot2)}}          # the SSD is sda now
        acks.apply_to_status(st, NOW + DAY)
        assert "acked" not in st["tasks"]["smart_trend"] and st["tasks"]["smart_trend"]["fp"] != a.fp
        st = {"tasks": {"smart_trend": ent("warn", "SMART: sdb realloc +2", metrics=boot2)}}          # the WD disk, wherever it is
        acks.apply_to_status(st, NOW + DAY)
        assert st["tasks"]["smart_trend"]["acked"]["fp"] == a.fp

    def test_the_rule_options_are_validated(self, world):
        for body in ('mode = "regex"\nregex = ["(a)"]\nsplit = ""\n', 'mode = "regex"\nregex = ["(a)"]\nsplit = 5\n',
                     'mode = "regex"\nregex = ["(a)"]\nalias = ["x", "y"]\n', 'mode = "regex"\nregex = ["(a)"]\nalias = ["x", "y", "z w"]\n',
                     'mode = "regex"\nregex = ["(a)"]\nalias = "devices"\n'):
            (world.conf / "ack.toml").write_text("[key.x]\n" + body)
            cfg, errors = acks.load_config(True)
            assert "x" not in cfg["key"] and len(errors) == 1, body
        (world.conf / "ack.toml").write_text('[key.x]\nmode = "regex"\nregex = ["(?P<mag>\\\\d+) (a)"]\nsplit = " | "\nalias = ["rows", "k", "v"]\n')
        cfg, errors = acks.load_config(True)
        assert errors == [] and cfg["key"]["x"]["split"] == " | " and cfg["key"]["x"]["alias"] == ["rows", "k", "v"]
        assert acks.fingerprint("x", "3 a", "warn") != acks.fingerprint("x", "300 a", "warn")        # a user's own magnitude rule works too

    def test_every_shipped_pattern_compiles_and_the_magnitude_rules_exist(self):
        for task in ("backup_freshness", "smart_trend", "growth_watch", "memory_health", "orphan_report", "stuck_detector"):
            rule = BASE_ORIG["key"][task]
            assert any("(?P<mag>" in p for p in rule["regex"]), task
            for p in rule["regex"]:
                assert re.compile(p, re.I)
        assert BASE_ORIG["key"]["smart_trend"]["alias"] == ["devices", "dev", "model"]


class TestCutOffLists:
    """Finding 2 (medium): a key built from a clipped summary cannot see what the task cut off. The counts the summary DOES carry are keyed."""

    @pytest.mark.parametrize("task,a,b", [
        ("failed_units", "warn: 4 failed unit(s): a.service, b.service, c.service, d.service", "warn: 5 failed unit(s): a.service, b.service, c.service, d.service"),
        ("failed_units", "warn: 3 unhealthy: a, b, c", "warn: 4 unhealthy: a, b, c"),
        ("failed_units", "warn: 3 restarting: a, b, c", "warn: 7 restarting: a, b, c"),
        ("failed_units", "warn: 3 unexpected exited: a, b, c", "warn: 4 unexpected exited: a, b, c"),
        ("disk_forecast", "warn: /a 4% free (1.0 GiB); /b 4% free (1.0 GiB); /c 4% free (1.0 GiB)",
         "warn: /a 4% free (1.0 GiB); /b 4% free (1.0 GiB); /c 4% free (1.0 GiB); +1 more"),
        ("disk_forecast", "warn: /a 4% free (1 GiB); +2 more", "warn: /a 4% free (1 GiB); +9 more"),
        ("probes", "3 down: A, B, C; 25/27 up", "4 down: A, B, C +1; 24/27 up"),
        ("probes", "4 down: A, B, C +1", "9 down: A, B, C +6"),
        ("probes", "2 degraded: A, B; 1 flapping: X", "3 degraded: A, B +1; 1 flapping: X"),
        ("docker_prune_exposure", "t.timer ON (next in 3 h): deletes container a, b, c +2 + image x, y +1 (1.0 GiB); natives keep",
         "t.timer ON (next in 3 h): deletes container a, b, c +9 + image x, y +7 (1.0 GiB); natives keep"),
        ("docker_prune_exposure", "t.timer ON (next in 3 h): deletes container a, b, c; natives keep",
         "t.timer ON (next in 3 h): deletes container a, b, c +1; natives keep"),
        ("os_jobs", "OS jobs: 4 of 13 need attention: a overdue; b failed; c inactive; d unknown", "OS jobs: 5 of 13 need attention: a overdue; b failed; c inactive; d unknown (+1)"),
        ("growth_watch", "growth over limit: a +1.0 GiB/d (+1 more)", "growth over limit: a +1.0 GiB/d (+5 more)"),
        ("growth_watch", "growth blind >24h: 3/4 paths unmeasured (x): a, b, c", "growth blind >24h: 4/4 paths unmeasured (x): a, b, c"),
        ("stuck_detector", "2 candidate(s), 2 actionable: tunarr 4.1 GiB leak", "3 candidate(s), 3 actionable: tunarr 4.1 GiB leak"),
    ])
    def test_a_longer_hidden_list_is_another_error(self, task, a, b):
        fa, fb = fpm(task, a), fpm(task, b)
        assert fa and fb and fa != fb, (fa.key, fb.key)

    def test_the_same_list_in_another_order_or_with_other_noise_is_the_same_error(self):
        assert fpm("probes", "3 down: A, B, C; 25/27 up") == fpm("probes", "3 down: C, A, B; 22/27 up, 2 skipped")
        assert fpm("disk_forecast", "warn: /a 4% free (1 GiB); /b 9% free (2 GiB); +2 more") == fpm("disk_forecast", "warn: /b 8% free (3 GiB); /a 3% free (9 GiB); +2 more")

    def test_normalize_keeps_the_overflow_count(self):
        assert acks.normalize("warn: x broke (+2 more)") == "x broke +2"
        assert acks.normalize("x broke (+2 more)") != acks.normalize("x broke (+9 more)") != acks.normalize("x broke")
        assert acks.normalize("a; +3 more") != acks.normalize("a; +4 more")
        assert acks.normalize("x (+1) (+2)") == "x +1 +2"

    def test_a_sorted_text_rule_keeps_the_overflow_count_too(self, world):
        (world.conf / "ack.toml").write_text('[key.mine]\nmode = "text"\nsort = true\n')
        a = acks.fingerprint("mine", "list: b x; a y (+1 more)", "warn")
        assert a == acks.fingerprint("mine", "list: a y; b x (+1 more)", "warn") != acks.fingerprint("mine", "list: a y; b x (+4 more)", "warn")

    def test_an_explicit_issue_key_is_hashed_whole_not_cut(self):
        head = "k" * 319
        a, b = (acks.fingerprint("t", {"summary": "x", "issue_key": head + tail}, "warn") for tail in "XY")
        assert a != b and a.mode == "explicit" and a.key == head + "X"
        long_a, long_b = "z" * 5000 + "a", "z" * 5000 + "b"
        fa, fb = (acks.fingerprint("t", {"summary": "x", "issue_key": k}, "warn") for k in (long_a, long_b))
        assert fa != fb and len(fa.key) < 4100 and fa == acks.fingerprint("t", {"summary": "y", "issue_key": long_a}, "warn")
        huge = acks.fingerprint("t", {"summary": "x", "issue_key": "q" * 3_000_000}, "warn")
        assert huge and huge != acks.fingerprint("t", {"summary": "x", "issue_key": "q" * 3_000_001}, "warn")

    def test_a_long_regex_key_is_hashed_whole_too(self):
        names = ", ".join(f"u{i:03d}.svc" for i in range(70))
        a = acks.fingerprint("failed_units", f"warn: 70 failed unit(s): {names}", "warn")
        b = acks.fingerprint("failed_units", f"warn: 70 failed unit(s): {names.replace('u069', 'u999')}", "warn")
        assert "#" in a.key and len(a.key) <= 600 + 33 and a != b                         # (past 600 characters the rest is hashed in)

    def test_the_stored_issue_key_is_display_only_and_short(self, world):
        write_status({"t": ent("warn", "warn: x", issue_key="k" * 700)})
        a = acks.add("t", 5, now=NOW)
        assert len(store()["acks"][a.fp]["issue_key"]) <= 200 and acks.is_acked(a.fp, "warn", NOW + 1)
        st = {"tasks": {"t": ent("warn", "warn: x", issue_key="k" * 699 + "Z")}}
        acks.apply_to_status(st, NOW + 2)
        assert "acked" not in st["tasks"]["t"]                                   # same first 200 characters, another key


class TestOverallStaysGreen:
    """Finding 3 (medium): status["overall"] is rewritten by scheduler.merge_status every tick from the raw statuses. Everything that
    writes or reads it must skip acknowledged tasks, or the hero turns yellow again a minute after `ack process` made it green."""

    def acked_tasks(self, until=NOW + DAY):
        return {"failed_units": ent("warn", "warn: x", acked={"fp": "0" * 16, "until": until, "by": "web", "note": "", "severity": "warn", "since": NOW}),
                "image_ledger": ent("ok", "ok: fine")}

    def test_acks_overall_skips_live_flags_only(self):
        assert acks.overall(self.acked_tasks(), NOW) == "ok"
        assert acks.overall(self.acked_tasks(until=NOW - 1), NOW) == "warn"                          # the flag outlived its `until` in the file: the colour returns
        t = self.acked_tasks()
        t["failed_units"]["acked"] = {"fp": "0" * 16}                                              # a malformed flag silences nothing
        assert acks.overall(t, NOW) == "warn"
        t["failed_units"]["acked"] = True
        assert acks.overall(t, NOW) == "warn"
        assert acks.flag_live({"until": NOW + 5}, NOW) and not acks.flag_live({"until": float("nan")}, NOW) and not acks.flag_live(None, NOW)

    def test_apply_to_status_leaves_the_overall_green_and_a_second_pass_keeps_it(self, world):
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        acks.add("failed_units", 30, now=NOW)
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita"), "image_ledger": ent("ok", "ok: fine")}, "overall": "warn"}
        acks.apply_to_status(st, NOW + 1)
        assert st["overall"] == "ok"
        acks.apply_to_status(st, NOW + 61)                                                         # the next minute's pass over the same file
        assert st["overall"] == "ok" and st["tasks"]["failed_units"]["acked"]

    def test_scheduler_merge_status_overall_skips_acknowledged_tasks(self):
        from homelab_maint import scheduler
        soon = time.time() + DAY                                                                   # (the scheduler reads the real clock)
        assert scheduler._overall(self.acked_tasks(soon)) == "ok"
        t = self.acked_tasks(soon)
        t["disk_forecast"] = ent("crit", "crit: / 3% free")
        assert scheduler._overall(t) == "crit"                                                     # an unacknowledged problem still counts

    def test_payloads_show_an_acknowledged_entry_muted(self):
        from homelab_maint import payloads
        assert payloads._lvl(self.acked_tasks(time.time() + DAY)["failed_units"]) == "info"
        assert payloads._lvl(ent("warn", "warn: x")) == "warn"


class TestPolicy:
    """Finding 4 (medium): ONE policy (acks.policy_ok) for add(), the inbox, issue_token(), mark_entry(), is_acked() and the public files:
    explicit key | [key.<task>] rule | allow_tasks, minus deny_tasks / deny_prefixes."""

    def test_the_shipped_default_is_strict(self):
        assert BASE_ORIG["ack"]["require_rule"] is True and BASE_ORIG["ack"]["allow_tasks"] == []
        assert BASE_ORIG["ack"]["deny_tasks"] == ["smart_event"] and BASE_ORIG["ack"]["deny_prefixes"] == ["job:"]

    def test_which_tasks_may_be_acknowledged(self, world):
        strict(world)
        assert acks.fingerprint("failed_units", "warn: 1 unhealthy: kavita", "warn").ackable                 # a rule
        assert acks.fingerprint("pressure_state", "L3 x: memory stall 1%", "warn").ackable                   # a rule (mode regex)
        assert acks.fingerprint("docker_df", "warn: x", "warn").ackable                                       # a rule (mode task)
        assert acks.fingerprint("worker", {"summary": "x", "issue_key": "worker-1"}, "warn").ackable         # an explicit issue_key
        assert not acks.fingerprint("worker", "warn: worker-1 failed", "warn").ackable                       # the number-blind text fallback
        assert not acks.fingerprint("surrealdb_health", "SurrealDB: WAL is 1500 MB", "warn").ackable
        assert not acks.fingerprint("", "x", "warn").ackable and not acks.Fp().ackable
        assert acks.ackable("failed_units") and acks.ackable("worker", "explicit") and not acks.ackable("worker") and not acks.ackable("")
        assert acks.ackable("worker", acks.fingerprint("worker", {"summary": "x", "issue_key": "k"}, "warn"))

    @pytest.mark.parametrize("task,subject", [
        ("failed_units", "warn: 1 unhealthy: kavita"), ("docker_df", "warn: x"), ("pressure_state", "L3 x: memory stall 1%"),
        ("worker", "warn: worker-1 failed"), ("worker", {"summary": "x", "issue_key": "worker-1"}), ("smart_event", "warn: sda failing"),
        ("smart_event", {"summary": "x", "issue_key": "k"}), ("job:backup", "failed rc=1"), ("surrealdb_health", "SurrealDB: WAL is 1500 MB"),
    ])
    def test_notify_and_acks_agree_on_which_tasks_may_be_acknowledged(self, world, task, subject):
        """notify._ack_allowed delegates to acks.ackable (one policy) and its own notify.toml copy is only the fallback for a module without it:
        both answers must agree with the shipped defaults, or the dashboard says "acknowledged" while the e-mails keep coming (or the reverse)."""
        strict(world)
        fp = acks.fingerprint(task, subject, "warn")
        assert notify._ack_allowed(acks, task, fp, notify.ack_cfg(notify.load_config())) is fp.ackable is acks.ackable(task, fp)

    def test_the_two_shipped_policy_blocks_are_equal(self):
        """notify.toml [ack] still carries a deprecated copy of ack.toml's policy (notify delegates to acks.ackable, the copy only applies to a
        module without it): they must not drift, or a fallback would split the dashboard from the pager, so the shipped copies must match."""
        n = tomllib.loads((ROOT / "etc" / "notify.toml").read_text())["ack"]
        a = tomllib.loads((ROOT / "etc" / "ack.toml").read_text())["ack"]
        keys = ("days", "escalation_breaks", "token_ttl_days", "require_rule", "allow_tasks", "deny_tasks", "deny_prefixes")
        assert {k: n[k] for k in keys} == {k: a[k] for k in keys}

    def test_deny_lists_beat_rules_explicit_keys_and_allow_tasks(self, world):
        strict(world, 'allow_tasks = ["smart_event", "job:x"]\n[key.smart_event]\nmode = "task"\n')
        for task in ("smart_event", "job:nightly", "job:x"):
            fp = acks.fingerprint(task, {"summary": "x", "issue_key": "k"}, "warn")
            assert fp and not fp.ackable and not acks.ackable(task, "explicit"), task
        assert acks.fingerprint("failed_units", "warn: 1 unhealthy: a", "warn").ackable

    def test_allow_tasks_and_require_rule_false_open_the_text_fallback_on_purpose(self, world):
        strict(world, 'allow_tasks = ["worker"]\n')
        assert acks.fingerprint("worker", "warn: worker-1 failed", "warn").ackable and not acks.fingerprint("other", "warn: x", "warn").ackable
        (world.conf / "ack.toml").write_text("[ack]\nrequire_rule = false\n")
        assert acks.fingerprint("other", "warn: x", "warn").ackable

    def test_policy_lists_are_validated_and_a_bad_value_keeps_the_baseline(self, world):
        for body in ('[ack]\ndeny_tasks = "smart_event"\n', '[ack]\ndeny_prefixes = [1]\n', '[ack]\nallow_tasks = [""]\n',
                     '[ack]\nrequire_rule = "no"\n', '[ack]\ndeny_tasks = [' + ",".join(['"a"'] * 201) + ']\n'):
            (world.conf / "ack.toml").write_text(body)
            cfg, errors = acks.load_config(True)
            assert errors and cfg["ack"]["deny_tasks"] == ["smart_event"] and cfg["ack"]["deny_prefixes"] == ["job:"], body
            assert cfg["ack"]["allow_tasks"] == []

    def test_a_task_without_a_rule_gets_no_id_and_no_acked_flag_in_status_json(self, world):
        strict(world)
        write_status({"worker": ent("warn", "warn: worker-1 failed"), "failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        st = core.read_json(core.STATE_DIR / "status.json")
        st["tasks"]["worker"].update(fp="0123456789abcdef", acked={"fp": "0123456789abcdef", "until": NOW + DAY})      # stale flags from an older release
        res = acks.apply_to_status(st, NOW)
        assert "fp" not in st["tasks"]["worker"] and "acked" not in st["tasks"]["worker"] and res == {"acked": 0, "failing": 2}
        assert re.fullmatch(r"[0-9a-f]{16}", st["tasks"]["failed_units"]["fp"])
        assert acks.entry_ackable("failed_units", st["tasks"]["failed_units"]) and not acks.entry_ackable("worker", st["tasks"]["worker"])
        assert not acks.entry_ackable("failed_units", ent("ok", "ok: fine")) and not acks.entry_ackable("x", 5)

    def test_the_cli_and_add_refuse_a_task_without_a_rule_by_name_and_by_id(self, world, capsys):
        strict(world)
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        fp = str(acks.fingerprint("worker", ent("warn", "warn: worker-1 failed"), "warn"))
        for target in ("worker", fp):
            with pytest.raises(acks.AckError) as e:
                acks.add(target, 5, now=NOW)
            assert e.value.code in ("not_ackable", "unknown_issue"), target
        with pytest.raises(acks.AckError) as e:
            acks.add("worker", 5, now=NOW)
        assert e.value.code == "not_ackable" and "cannot be acknowledged" in str(e.value)
        assert acks.main(["add", "worker"]) == 1 and "cannot be acknowledged" in capsys.readouterr().err
        assert acks.list_acks(NOW) == [] and not (core.STATE_DIR / "acks.json").exists()

    def test_an_explicit_key_task_can_be_acknowledged_without_a_rule(self, world):
        strict(world)
        write_status({"worker": ent("warn", "warn: worker-1 failed", issue_key="worker-1")})
        a = acks.add("worker", 5, now=NOW)
        assert a.mode == "explicit" and store()["acks"][a.fp]["mode"] == "explicit"
        st = {"tasks": {"worker": ent("warn", "warn: totally different", issue_key="worker-1")}}
        acks.apply_to_status(st, NOW + 1)
        assert st["tasks"]["worker"]["acked"]["fp"] == a.fp
        st = {"tasks": {"worker": ent("warn", "warn: totally different", issue_key="worker-2")}}
        acks.apply_to_status(st, NOW + 1)
        assert "acked" not in st["tasks"]["worker"]

    def test_a_signed_web_request_for_a_task_without_a_rule_is_refused(self, world):
        strict(world)
        key = ready()
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        fp = str(acks.fingerprint("worker", ent("warn", "warn: worker-1 failed"), "warn"))
        submit(web_req(fp, "warn"), key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected == ["unknown_issue"]
        assert not (core.STATE_DIR / "acks.json").exists() or store()["acks"] == {}

    def test_a_signed_web_renewal_of_a_stored_record_of_a_denied_task_is_refused_in_public_words(self, world):
        key = ready()
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        a = acks.add("worker", 5, now=NOW)                                                                  # (the relaxed fixture policy)
        strict(world)
        submit(web_req(a.fp, "warn", days=30), key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected == ["not_ackable"] and store()["acks"][a.fp]["until"] == NOW + 5 * DAY
        assert [r["reason"] for r in acks.public_doc(NOW)["rejected"]] == ["invalid"]                       # no oracle: the site only says "invalid"
        submit({"v": 1, "kind": "unack", "source": "web", "fp": a.fp, "ts": NOW}, key, name=f"{int(NOW * 1000) + 1}-bbbbbbbb.json")
        assert run_inbox().unacked == [a.fp]                                                                # un-acknowledging is always possible

    def test_explain_says_when_a_task_cannot_be_acknowledged(self, world, capsys):
        strict(world)
        assert acks.main(["explain", "worker", "--summary", "warn: worker-1 failed"]) == 0
        assert "ackable:  no" in capsys.readouterr().out
        assert acks.main(["explain", "failed_units", "--summary", "warn: 1 unhealthy: a"]) == 0 and "ackable" not in capsys.readouterr().out

    def test_a_stored_acknowledgement_of_a_task_that_may_no_longer_be_acknowledged_silences_nothing(self, world):
        a = make_ack()                                                                                       # failed_units: ruled
        assert acks.is_acked(a.fp, "warn", NOW + 1)
        (world.conf / "ack.toml").write_text('[ack]\ndeny_tasks = ["smart_event", "failed_units"]\n')
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None                                                  # notify holds nothing
        st = {"tasks": {"failed_units": ent("warn", "warn: 1 unhealthy: kavita")}}
        acks.apply_to_status(st, NOW + 1)
        assert "acked" not in st["tasks"]["failed_units"] and "fp" not in st["tasks"]["failed_units"]
        assert acks.public_doc(NOW + 1)["acks"] == [] and [r.fp for r in acks.list_acks(NOW + 1)] == [a.fp]    # the CLI still lists the record
        with pytest.raises(acks.AckError):
            acks.add(a.fp, 5, now=NOW + 2)                                                                   # and it cannot be renewed
        (world.conf / "ack.toml").write_text("")
        assert acks.is_acked(a.fp, "warn", NOW + 1)

    def test_a_record_of_a_text_fallback_task_in_the_store_does_not_cover_under_the_strict_policy(self, world):
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        a = acks.add("worker", 5, now=NOW)                                                                  # (the relaxed fixture policy)
        assert acks.is_acked(a.fp, "warn", NOW + 1)
        strict(world)
        assert acks.is_acked(a.fp, "warn", NOW + 1) is None

    def test_issue_token_refuses_a_task_without_a_rule(self, world):
        strict(world)
        write_status({"worker": ent("warn", "warn: worker-1 failed"), "failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        fp = str(acks.fingerprint("worker", ent("warn", "warn: worker-1 failed"), "warn"))
        with pytest.raises(ValueError):
            acks.issue_token(fp, "worker", "Worker", "worker-1 failed", "warn", NOW)
        assert not (core.STATE_DIR / "acks.json").exists() or store()["tokens"] == {}
        ok = acks.issue_token(fp_of("failed_units", "warn: 1 unhealthy: kavita"), "failed_units", "S", "x", "warn", NOW)
        assert len(ok) == 43
        with pytest.raises(ValueError):
            acks.issue_token("0" * 16, "smart_event", "S", "x", "warn", NOW, mode="explicit")               # denied beats an explicit mode
        tok = acks.issue_token("0" * 16, "worker", "S", "x", "warn", NOW, mode="explicit")                 # the caller knows the fingerprint is explicit
        assert len(tok) == 43 and store()["tokens"][acks.token_hash(tok)]["mode"] == "explicit"

    def test_a_token_whose_task_lost_its_rule_is_refused_and_stays_unspent(self, world):
        key = ready()
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        fp = fp_of("failed_units", "warn: 1 unhealthy: kavita")
        tok = acks.issue_token(fp, "failed_units", "S", "x", "warn", NOW)
        (world.conf / "ack.toml").write_text('[ack]\ndeny_tasks = ["failed_units"]\n')
        submit(email_req(tok), key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected == ["not_ackable"]
        assert store()["tokens"][acks.token_hash(tok)]["used"] is False and acks.is_acked(fp, "warn", NOW + 1) is None
        (world.conf / "ack.toml").write_text("")
        submit(email_req(tok), key, name=f"{int(NOW * 1000) + 1}-aaaaaaaa.json")
        assert run_inbox().applied == [fp]

    def test_publish_style_exports_list_only_what_silences(self, world):
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        acks.add("worker", 5, now=NOW)
        assert [a["task"] for a in acks.public_doc(NOW + 1)["acks"]] == ["worker"]
        strict(world)
        assert acks.public_doc(NOW + 1)["acks"] == [] and acks.public_doc(NOW + 1)["stats"]["active"] == 0


class TestSeverityPolicy:
    """SPEC5: `[ack] severities = ["warn"]` (the shipped default) keeps critical issues alerting: no id, no button, no token, no ack, from
    the dashboard, the CLI or the e-mail link. `error` (the check itself failed) counts as crit. The world fixture widens the key for the
    other classes, so each test here writes the policy it wants."""

    def shipped(self, world, severities: str = '["warn"]') -> None:
        (world.conf / "ack.toml").write_text(f"[ack]\nseverities = {severities}\n")

    def test_a_crit_issue_gets_no_id_no_ack_and_no_token(self, world):
        self.shipped(world)
        write_status({"failed_units": ent("crit", "crit: 1 unhealthy: kavita")})
        st = core.read_json(core.STATE_DIR / "status.json")
        acks.apply_to_status(st, NOW)
        assert "fp" not in st["tasks"]["failed_units"] and "acked" not in st["tasks"]["failed_units"]
        assert st["tasks"]["failed_units"]["status"] == "crit" and st["overall"] == "crit"
        assert acks._current_issues(None, acks.load_config()) == {}
        fo = acks.fingerprint("failed_units", ent("crit", "crit: 1 unhealthy: kavita"), "crit")
        assert len(str(fo)) == 16 and not fo.ackable
        with pytest.raises(acks.AckError) as e:
            acks.add(str(fo), 5, now=NOW)
        assert e.value.code in ("severity_not_ackable", "not_ackable") and "always alert" in str(e.value)
        with pytest.raises(acks.AckError) as e2:
            acks.add("failed_units", 5, now=NOW)                          # by TASK name too: same refusal, same words
        assert e2.value.code == "severity_not_ackable" and "always alert" in str(e2.value)
        with pytest.raises(ValueError):
            acks.issue_token(str(fo), "failed_units", "S", "crit: 1 unhealthy: kavita", "crit", NOW)
        assert not (core.STATE_DIR / "acks.json").exists() or store()["acks"] == {}

    def test_the_warn_problem_of_the_same_task_is_still_ackable(self, world):
        self.shipped(world)
        write_status({"failed_units": ent("warn", "warn: 1 unhealthy: kavita")})
        a = acks.add("failed_units", 5, now=NOW)
        assert a.severity == "warn" and acks.is_acked(a.fp, "warn", NOW + 1)
        st = core.read_json(core.STATE_DIR / "status.json")
        acks.apply_to_status(st, NOW)
        assert st["tasks"]["failed_units"]["acked"]["fp"] == a.fp and st["overall"] == "ok"

    def test_a_crit_web_request_is_refused(self, world):
        key = ready()
        write_status({"failed_units": ent("crit", "crit: 1 unhealthy: kavita")})
        self.shipped(world)
        fp = str(acks.fingerprint("failed_units", ent("crit", "crit: 1 unhealthy: kavita"), "crit"))
        submit(web_req(fp, "crit"), key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected == ["unknown_issue"]
        assert not (core.STATE_DIR / "acks.json").exists() or store()["acks"] == {}

    def test_an_email_token_minted_before_the_policy_tightened_is_refused(self, world):
        key = ready()
        write_status({"failed_units": ent("crit", "crit: 1 unhealthy: kavita")})
        self.shipped(world, '["warn", "crit"]')                       # the old policy: a crit link was still minted
        fp = str(acks.fingerprint("failed_units", ent("crit", "crit: 1 unhealthy: kavita"), "crit"))
        tok = acks.issue_token(fp, "failed_units", "S", "crit: 1 unhealthy: kavita", "crit", NOW)
        self.shipped(world)                                           # tightened: the link must stop working
        submit(email_req(tok), key)
        rep = run_inbox()
        assert rep.applied == [] and rep.rejected == ["not_ackable"] and acks.is_acked(fp, "crit", NOW + 1) is None

    @pytest.mark.parametrize("body,want", [('severities = ["crit"]\n', ["crit"]), ('severities = ["warn", "warn"]\n', ["warn"])])
    def test_severities_accepts_a_nonempty_subset_of_warn_crit(self, world, body, want):
        (world.conf / "ack.toml").write_text("[ack]\n" + body)
        cfg, errors = acks.load_config(True)
        assert errors == [] and cfg["ack"]["severities"] == want

    @pytest.mark.parametrize("body", ['severities = []\n', 'severities = "warn"\n', 'severities = ["info"]\n', 'severities = ["warn", 1]\n'])
    def test_a_bad_severities_value_is_ignored_and_the_baseline_stays(self, world, body):
        (world.conf / "ack.toml").write_text("[ack]\n" + body)
        cfg, errors = acks.load_config(True)
        assert errors and cfg["ack"]["severities"] == acks._BASE["ack"]["severities"]

    def test_explain_reflects_the_severity_policy(self, world, capsys):
        self.shipped(world)
        assert acks.main(["explain", "failed_units", "--summary", "warn: 1 unhealthy: a"]) == 0
        assert "ackable:  no" not in capsys.readouterr().out
        assert acks.main(["explain", "failed_units", "--summary", "crit: 1 unhealthy: a"]) == 0
        out = capsys.readouterr().out
        assert "ackable:  no" in out and "always alert" in out


class TestNamesKeepTheirDigits:
    """Finding 4 (second half): digits glued to a NAME belong to it; only standalone numbers are volatile values."""

    @pytest.mark.parametrize("a,b", [
        ("immich-1 failed", "immich-2 failed"), ("pool tank-0 degraded", "pool tank-1 degraded"),
        ("foo@1.service failed", "foo@2.service failed"), ("/mnt/2tb is full", "/mnt/8tb is full"), ("/mnt/disk1 full", "/mnt/disk2 full"),
        ("10.0.0.5 unreachable", "10.0.0.9 unreachable"), ("10.0.0.5 unreachable", "11.0.0.5 unreachable"), ("version 1.2.3 broke", "version 1.2.4 broke"),
        ("port 5432 refused", "port 22 refused"), ("error 500 from api", "error 404 from api"), ("exit code 1", "exit code 137"),
        ("failed rc=1", "failed rc=137"), ("killed by signal 9", "killed by signal 15"), ("host:5432 down", "host:22 down"),
        ("nvme0 hot", "nvme1 hot"), ("sda1 full", "sda2 full"), ("job:backup-3 failed", "job:backup-4 failed"),
    ])
    def test_different_names_and_codes_stay_different(self, a, b):
        assert acks.normalize(a) != acks.normalize(b)
        assert acks.fingerprint("t", f"warn: {a}", "warn") != acks.fingerprint("t", f"warn: {b}", "warn")

    @pytest.mark.parametrize("a,b", [
        ("restarted 3 times", "restarted 30 times"), ("25/27 up", "24/27 up"), ("memory stall 6.1%", "memory stall 7.9%"), ("12 files older than 30 days", "15 files older than 31 days"),
        ("x=5 y=6", "x=7 y=8"), ("took 1.5 s", "took 12 s"), ("(+2 more) 3 left", "(+2 more) 4 left"), ("2x slower", "3x slower"), ("at 12:30", "at 13:45"),
        ("free 4.0 GiB of 100 GiB", "free 5.5 GiB of 101 GiB"), ("a -5 b", "a -7 b"),
    ])
    def test_standalone_numbers_are_still_masked(self, a, b):
        assert acks.normalize(a) == acks.normalize(b)

    def test_the_mask_is_fast_and_never_raises_on_pathological_input(self):
        t0 = time.monotonic()
        acks.normalize("a-1 " * 20000 + "1." * 20000 + "port " * 10000 + "1")
        assert time.monotonic() - t0 < 2.0


class TestErrorIsNeverCoveredByACondition:
    """Finding 5 (medium): status "error" means the CHECK ITSELF failed. It has its own key, so acknowledging a condition the check reports
    (an L4 pressure episode) cannot silence "I can no longer measure"."""
    BLIND = "cannot read /proc/pressure (memory/io): pressure level unknown"

    def test_an_acknowledged_critical_pressure_does_not_cover_a_blind_monitor(self, world):
        write_status({"pressure_state": ent("crit", "L4 critical: memory stall 40%, 1.0 GiB avail; top: tunarr P3")})
        a = acks.add("pressure_state", 90, now=NOW)
        st = {"tasks": {"pressure_state": ent("crit", "L4 critical: memory stall 55%, 0.9 GiB avail; top: ollama P1")}}
        acks.apply_to_status(st, NOW + DAY)
        assert st["tasks"]["pressure_state"]["acked"]["fp"] == a.fp                                           # the same episode is still acknowledged
        st = {"tasks": {"pressure_state": ent("error", self.BLIND)}}
        acks.apply_to_status(st, NOW + 2 * DAY)
        e = st["tasks"]["pressure_state"]
        assert "acked" not in e and e["fp"] != a.fp and st["overall"] == "crit"
        assert acks.is_acked(e["fp"], "crit", NOW + 2 * DAY) is None

    def test_the_error_is_a_separate_acknowledgeable_issue(self, world):
        write_status({"pressure_state": ent("error", self.BLIND)})
        b = acks.add("pressure_state", 7, now=NOW)
        assert b.severity == "crit" and b.fp != fp_of("pressure_state", "L4 critical: memory stall 40%", "crit")
        st = {"tasks": {"pressure_state": ent("error", self.BLIND)}}
        acks.apply_to_status(st, NOW + 1)
        assert st["tasks"]["pressure_state"]["acked"]["fp"] == b.fp

    def test_pressure_level_and_cause_are_part_of_the_error(self):
        base = fpm("pressure_state", "L3 slow batch: memory stall 11.0%, 8.0 GiB avail; top: ollama P1", "warn")
        assert base == fpm("pressure_state", "L3 slow batch: memory stall 13.0%, 6.0 GiB avail; top: tunarr P3 (io: sda x2)", "warn")
        assert base != fpm("pressure_state", "L2 memory squeeze: memory stall 6.1%, 8.0 GiB avail", "warn")       # another level
        assert base != fpm("pressure_state", "L3 slow batch: swap-in 900/s", "warn")                              # another cause
        assert base != fpm("pressure_state", "L3 slow batch: memory stall 11.0%", "warn")                         # a cause less
        assert fpm("pressure_state", "L4 critical: memory stall 40%", "crit") != base

    @pytest.mark.parametrize("task", ["pressure_state", "memory_health", "probes", "docker_df", "failed_units", "anything_new"])
    def test_status_error_never_shares_a_key_with_a_warning_or_critical(self, task):
        for text in ("x broke", "cannot read /proc: boom", "MONITORING BLIND: no probes are running"):
            assert fpm(task, text, "error") != fpm(task, text, "crit") and fpm(task, text, "error") != fpm(task, text, "warn")
            assert fpm(task, text, "error").key.endswith("|error") and fpm(task, text, "error") == fpm(task, text, "error")

    def test_the_summary_alone_says_error_too(self):
        """notify may only have the text (an Event): the "error:" prefix is as good as the status."""
        assert acks.fingerprint("pressure_state", "error: " + self.BLIND, "crit") == fpm("pressure_state", self.BLIND, "error")
        assert acks.fingerprint("pressure_state", "error: " + self.BLIND, "crit") != acks.fingerprint("pressure_state", "L4 critical: x", "crit")

    def test_a_result_object_and_an_explicit_key_follow_the_same_rule(self):
        res = core.Result("error", self.BLIND)
        assert acks.fingerprint("pressure_state", res, "crit") == fpm("pressure_state", self.BLIND, "error")
        e = acks.fingerprint("t", {"summary": "x", "status": "error", "issue_key": "disk:/mnt/x"}, "crit")
        assert e.key == "disk:/mnt/x|error" and e != acks.fingerprint("t", {"summary": "x", "status": "crit", "issue_key": "disk:/mnt/x"}, "crit")

    def test_the_other_crit_cases_are_unchanged(self):
        assert fpm("failed_units", "crit: 1 unhealthy: kavita", "crit") == fpm("failed_units", "warn: 1 unhealthy: kavita", "warn")
        assert acks.sev_of("error") == "crit" and acks.sev_of("crit") == "crit"


class TestReviewFixesThroughRealNotify:
    """The same fixes, seen from the e-mail path (notify.send with acks.py as its module and a recording transport)."""

    def test_a_worsening_decade_pages_while_the_same_decade_stays_held(self, wire):
        for task, base, same, worse, title in (("backup_freshness", "warn: stack-backup 28h old (limit 26h)", "warn: stack-backup 30h old (limit 26h)",
                                                "warn: stack-backup 30d old (limit 26h)", "Backups"),
                                               ("smart_trend", "SMART: sda realloc +2, 72C", "SMART: sda realloc +7, 80C",
                                                "SMART: sda realloc +8000, 72C", "SMART trend")):
            write_status({task: ent("warn", base)})
            a = acks.add(task, 90, now=NOW)
            before = len(wire.tr.calls)
            assert wire.send(alert(same, task=task, title=title), NOW + 60).skipped == "acknowledged" and len(wire.tr.calls) == before
            d = wire.send(alert(worse, task=task, title=title), NOW + 120)
            assert d.ok and not d.skipped and len(wire.tr.calls) == before + 1 and a.fp, task
            assert acks.is_acked(a.fp, "warn", NOW + 121)                                  # the old acknowledgement is untouched (and still listed)

    def test_a_longer_hidden_list_pages(self, wire):
        write_status({"failed_units": ent("warn", "warn: 4 failed unit(s): a.service, b.service, c.service, d.service")})
        acks.add("failed_units", 90, now=NOW)
        held = wire.send(alert("warn: 4 failed unit(s): a.service, b.service, c.service, d.service"), NOW + 60)
        assert held.skipped == "acknowledged"
        assert wire.send(alert("warn: 5 failed unit(s): a.service, b.service, c.service, d.service"), NOW + 120).ok

    BLIND = "cannot read /proc/pressure (memory/io): pressure level unknown"

    def test_a_blind_monitor_pages_although_the_pressure_episode_is_acknowledged(self, wire):
        write_status({"pressure_state": ent("crit", "L4 critical: memory stall 40%, 1.0 GiB avail")})
        acks.add("pressure_state", 90, now=NOW)
        ev = lambda s, key="pressure_state", **kw: notify.Event("alert", "crit", "Load pressure level", s, task="pressure_state", dedupe_key=key,
                                                                 facts=kw or None)
        assert wire.send(ev("L4 critical: memory stall 55%, 0.9 GiB avail"), NOW + 60).skipped == "acknowledged"
        assert wire.send(ev("error: " + self.BLIND, "blind-1"), NOW + 120).ok                # the prefix alone says "the check failed"
        fp = notify._result_fp("pressure_state", core.Result("error", self.BLIND), 2, notify.load_config(wire.cfg))
        assert fp and fp == str(fpm("pressure_state", self.BLIND, "error"))                  # and so does the Result notifier.py gets from the runner
        assert wire.send(ev(self.BLIND, "blind-2", ack_fp=fp), NOW + 7200).ok

    def test_a_task_without_a_rule_is_neither_held_nor_offered_a_button(self, wire):
        (core.CONF_DIR / "ack.toml").write_text("[ack]\nrequire_rule = true\n")
        write_status({"worker": ent("warn", "warn: worker-1 failed")})
        d = wire.send(alert("warn: worker-1 failed", task="worker", title="Worker"), NOW + 60)
        assert d.ok and wire.tr.calls[0].ack_url == "" and "Issue ID" not in wire.tr.calls[0].plain
