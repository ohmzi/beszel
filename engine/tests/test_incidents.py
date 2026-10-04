"""Tests for homelab_maint/incidents.py and homelab_maint/data/playbooks.toml (SPEC3 S3): the incident ledger, playbooks, SLOs, postmortems.

What is covered
  * Replay: a synthetic day of 15-minute runs (96 ticks, every check) driven through update() tick by tick, with exact
    expectations for open / escalate / improve / resolve times, severities, MTTD / MTTA / MTTR, mitigation, correlation
    grouping, sev1, postmortems and the 30-day stats; the SAME history replayed in one batch gives identical incidents.
  * Differential: random status sequences run through the REAL core.Notifier (sending mocked) and through update(); every page
    the notifier would send coincides with an incident event (same debounce, which is the contract).
  * Idempotence: a second update never duplicates; concurrent updates serialise; a lost or corrupt state file duplicates nothing.
  * Crash recovery: a truncated last line, garbage in the ledger, a crash at EVERY byte boundary of a multi-event batch, state
    ahead of / behind the ledger, a full disk. After recovery the incidents equal those of a run that never crashed.
  * Public exports: shape and caps, and that no secret, e-mail, phone, token, URL query string or traceback reaches the
    ledger, the snapshots, the postmortems or the playbooks.
  * SLO maths and the playbook file (every registered task has one, commands in `checks` are read-only, hints fire on the real
    wording of the tasks, overrides merge per key).
  * Review fixes: the redactor on ENV_STYLE_NAMES / any-scheme URL credentials / -pPASS / Cookie / sk-ant- tokens, in the exports AND in
    the ledger file; playbook commands that record (`homelab-maint run|gate`); wall-clock steps (future cursors, future samples, nothing
    counted twice); replayed transitions reported as `backfilled`, not live; the baseline playbooks shipped inside the package with an
    overrides-only /etc file.
Everything runs on tmp dirs; any subprocess call fails the test (the module never runs a command)."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import types
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from homelab_maint import core, incidents

_REAL_POPEN = subprocess.Popen                      # the autouse fixture bans subprocess for the module under test, not for this file's own use
ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Toronto")
STEP = 900                                         # one check-tier run every 15 minutes


def at(h, m=0, s=0, d=2, mo=10, y=2026):
    return datetime(y, mo, d, h, m, s, tzinfo=TZ).timestamp()


T0 = at(0)                                         # Fri 2026-10-02 00:00 EDT


def tk(i):
    """Time of tick i of the synthetic day (tick 0 = 00:00, tick 40 = 10:00, tick 95 = 23:45)."""
    return T0 + STEP * i


CHECKS = ["alert_path_health", "backup_freshness", "disk_forecast", "docker_df", "failed_units", "growth_watch", "image_ledger",
          "memory_health", "orphan_report", "os_jobs", "plex_media_mount_check", "pressure_state", "probes", "smart_trend",
          "spike_sampler", "stuck_detector"]
TITLES = {"disk_forecast": "Disk space", "memory_health": "Memory pressure", "failed_units": "Services & containers",
          "backup_freshness": "Backups", "pressure_state": "Load pressure level", "probes": "Monitoring probes",
          "os_jobs": "OS maintenance timers", "smart_trend": "SMART trend", "growth_watch": "Growth watch",
          "docker_df": "Docker disk"}
ALERT_OFF = {"docker_df", "config_drift"}          # always alert = false (the SLO/incident "informational" list)

SUMMARY = {
    ("disk_forecast", "warn"): "warn: /media/SandiskSSD 9% free (170.0 GiB), full in 12d",
    ("memory_health", "warn"): "warn: memory stall 6.1% (30.0 GiB avail; cache 40.0 GiB is reclaimable; io 3%)",
    ("memory_health", "crit"): "crit: memory stall 16.2%, only 3.1 GiB available (3.1 GiB avail; io 22%)",
    ("failed_units", "crit"): "warn: 2 unhealthy: immich_postgres, kavita",
    ("pressure_state", "crit"): "crit: level 4, memory PSI full 14%",
    ("probes", "warn"): "1 degraded: Plex; 24/25 up",
    ("backup_freshness", "crit"): "crit: backup-system FAILED (1d3h ago)",
    ("os_jobs", "warn"): "OS jobs: 1 of 13 need attention: logrotate overdue",
}
ITEMS = {
    "failed_units": [{"kind": "unhealthy", "name": "immich_postgres", "level": "warn"},
                     {"kind": "unhealthy", "name": "kavita", "level": "warn"},
                     {"kind": "exited", "name": "fine-container", "level": "ok"}],
    "disk_forecast": [{"mount": "/media/SandiskSSD", "level": "warn"}, {"mount": "/", "level": "ok"}],
    "probes": [{"name": "plex", "title": "Plex", "state": "warn", "sev": "warn"}, {"name": "homarr", "state": "up", "sev": "info"}],
    "os_jobs": [{"name": "logrotate", "state": "overdue"}, {"name": "fstrim", "state": "ok"}],
}


def plan(i):
    """Status of every check at tick i. The day: backup crit 02:00-03:15 with skipped runs, a one-run blip, a flapper, a disk
    warning 10:00-11:15, memory warn -> crit -> warn 14:00-15:30, a correlated trio at 20:00-21:30, os_jobs from 23:00."""
    s = {c: "ok" for c in CHECKS}
    s["docker_df"] = "warn"                                   # alert=false all day: never an incident
    s["orphan_report"] = "info"
    s["backup_freshness"] = "crit" if i in (8, 9) else "skipped" if 10 <= i <= 13 else "ok"
    s["smart_trend"] = "warn" if i == 12 else "ok"            # one-run blip
    s["growth_watch"] = "warn" if i in (20, 22) else "ok"     # flapping: bad, ok, bad, ok
    if 40 <= i <= 45:
        s["disk_forecast"] = "warn"                           # 10:00 - 11:15
    s["memory_health"] = ("warn" if i in (56, 57, 61, 62) else "crit" if i in (58, 59, 60) else "ok")   # 14:00 - 15:30
    s["failed_units"] = "crit" if 80 <= i <= 85 else "ok"     # 20:00 - 21:15
    s["pressure_state"] = "crit" if 81 <= i <= 85 else "ok"   # 20:15 - 21:15
    s["probes"] = "warn" if 81 <= i <= 86 else "ok"           # 20:15 - 21:30
    s["os_jobs"] = "warn" if i >= 92 else "ok"                # 23:00 -> end of day, still open
    return s


# Audit trail of the day: (time, task, action, target, bytes, outcome). Only rows that happened before "now" are on disk at a tick.
AUDIT = [
    (at(2, 16, 10), "notify", "send", "backup_freshness: CRIT Backups", 0, "failed rc=1 Authorization: Bearer abcdef1234567890SECRETSECRET"),
    (at(10, 16, 5), "notify", "send", "disk_forecast: WARN Disk space", 0, "sent"),
    (at(10, 20), "apt_clean", "apt-get clean", "/var/cache/apt", 0, "dry-run"),
    (at(10, 40), "docker_cache", "buildx-prune", "immaculaterr-builder", 21 * core.GIB, "done"),
    (at(11, 46), "notify", "send", "disk_forecast: OK Disk space: recovered", 0, "sent"),
    (at(14, 16, 30), "notify", "send", "memory_health: WARN Memory pressure", 0, "sent"),
    (at(14, 46, 30), "notify", "send", "memory_health: CRIT Memory pressure", 0, "sent"),
    (at(15, 0), "pressure_response", "unload-model", "qwen3:32b", 0, "done"),
    (at(20, 31), "notify", "send", "failed_units: CRIT Services & containers", 0, "sent"),
    (at(20, 31, 30), "notify", "budget-exhausted", "pressure_state", 0, "dropped"),
    (at(20, 40), "pressure_response", "docker-update", "tunarr-host-net", 0, "dry-run"),
]


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


class World:
    """A tmp STATE/LOG/CONF tree plus helpers that write realistic runner inputs into it the way cli.cmd_run does."""

    def __init__(self, root: Path, mp):
        self.root, self.mp = root, mp
        self.state, self.log, self.conf = root / "state", root / "log", root / "conf"
        for d in (self.state, self.log, self.conf):
            d.mkdir(parents=True, exist_ok=True)
        for name, d in (("STATE_DIR", self.state), ("LOG_DIR", self.log), ("CONF_DIR", self.conf)):
            mp.setattr(core, name, d)
        self.status: dict = {"tasks": {}}
        self.audit_queue: list[tuple] = []
        self.audit_written = 0

    # -- inputs ------------------------------------------------------------------------------------------------------
    def hist(self, t, task, status, **extra):
        rec = {"t": t, "kind": "task", "task": task, "status": status, "reclaimed": 0, "dur": 0.1, "metrics": {}}
        rec.update(extra)
        with open(self.state / "history.jsonl", "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def entry(self, t, task, status, summary=None, items=None, **extra):
        e = {"title": TITLES.get(task, task), "klass": "C0", "tier": "check", "status": status,
             "summary": summary or SUMMARY.get((task, status)) or f"{status}: {task} check", "last_run": t, "duration_s": 0.1,
             "reclaimed_bytes": 0, "metrics": {}, "items": items if items is not None else ITEMS.get(task, []) if status not in ("ok", "info") else [],
             "alert": task not in ALERT_OFF, "mode": "check"}
        e.update(extra)
        self.status["tasks"][task] = e
        self.status["generated_at"] = t + 1
        return e

    def queue_audit(self, rows):
        self.audit_queue = sorted(rows, key=lambda r: r[0])

    def flush_audit(self, upto):
        rows = [r for r in self.audit_queue if r[0] <= upto]
        new = rows[self.audit_written:]
        if new:
            with open(self.log / "audit.jsonl", "a") as f:
                for t, task, action, target, size, outcome in new:
                    f.write(json.dumps({"ts": stamp(t), "task": task, "action": action, "target": target, "bytes": size,
                                        "outcome": outcome}) + "\n")
            self.audit_written = len(rows)

    def tick(self, t, results, now=None, run=True):
        """One runner tick: history record + status entry per task, audit rows due, then incidents.update()."""
        now = t + 5 if now is None else now
        for task, spec in results.items():
            status, summary, items = (spec, None, None) if isinstance(spec, str) else (tuple(spec) + (None, None))[:3]
            self.hist(t, task, status)
            self.entry(t, task, status, summary, items)
        self.flush_audit(now)
        return incidents.update(self.status, None, now) if run else None

    def day(self, upto=96, run=True):
        for i in range(upto):
            self.tick(tk(i), plan(i), run=run)

    # -- outputs -----------------------------------------------------------------------------------------------------
    def events(self):
        out = []
        for ln in (self.state / "incidents.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def pub(self, now=None):
        return incidents.export_incidents(now if now is not None else tk(95) + 5)

    def by_task(self, now=None):
        p = self.pub(now)
        return {i["task"]: i for i in p["open"] + p["recent"]}

    def ledger_bytes(self):
        return (self.state / "incidents.jsonl").read_bytes()


def total(out, kind):
    """Transitions of one kind in an update() result: the live ones plus the backfilled (replayed) ones."""
    return len(out[kind]) + len(out["backfilled"][kind])


def lfold(w):
    """Facts of every incident straight from the ledger (the capped public export may legitimately shed old ones)."""
    L = incidents._Ledger(w.events())
    return sorted((r["id"], r["task"], r["severity"], r["state"], r["started_at"], r["detected_at"], r["resolved_at"], r["parent"],
                   r["acknowledged_at"], r["mitigated_at"], r["level"]) for r in L.incs.values())


def fold(pub):
    """The facts of every incident that must survive a crash/replay unchanged (timeline wording and ts of group lines may differ)."""
    return sorted((i["id"], i["task"], i["severity"], i["status"], i["since"], i["detected_at"], i.get("resolved_at"), i["parent"],
                   i["acknowledged_at"], i["mitigated_at"], i["level"]) for i in pub["open"] + pub["recent"])


@pytest.fixture(autouse=True)
def tz_and_no_commands(monkeypatch):
    """Fixed local time zone (incident ids and timeline text use local time) and a hard ban on running commands."""
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()

    def banned(*a, **k):
        raise AssertionError(f"incidents must never run a command: {a[:1]}")
    monkeypatch.setattr(subprocess, "run", banned)
    monkeypatch.setattr(subprocess, "Popen", banned)
    monkeypatch.setattr(core, "sh", banned)
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def day_tree(tmp_path_factory):
    """The synthetic day is replayed ONCE (96 updates); every test gets its own copy of the resulting state."""
    mp = pytest.MonkeyPatch()
    mp.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        w = World(tmp_path_factory.mktemp("day"), mp)
        w.queue_audit(AUDIT)
        w.day()
        return w.root, json.loads(json.dumps(w.status))
    finally:
        mp.undo()
        time.tzset()


@pytest.fixture()
def day(day_tree, tmp_path, monkeypatch):
    """A world after the full synthetic day (a private copy)."""
    root, status = day_tree
    shutil.copytree(root, tmp_path / "day")
    w = World(tmp_path / "day", monkeypatch)
    w.status = json.loads(json.dumps(status))
    w.queue_audit(AUDIT)
    w.audit_written = len(AUDIT)
    return w


# ====================================================================================================== replay of a day
class TestReplayDay:
    def test_which_incidents_exist(self, day):
        inc = day.by_task()
        assert sorted(inc) == ["backup_freshness", "disk_forecast", "failed_units", "memory_health", "os_jobs", "pressure_state", "probes"]
        # never: a one-run blip, a flapper, alert=false tasks, info statuses, healthy checks
        for t in ("smart_trend", "growth_watch", "docker_df", "orphan_report", "spike_sampler", "stuck_detector"):
            assert t not in inc
        assert [i["id"] for i in sorted(inc.values(), key=lambda i: i["id"])] == [f"INC-20261002-{n:03d}" for n in range(1, 8)]

    def test_ids_follow_the_order_incidents_were_confirmed(self, day):
        ids = {t: i["id"] for t, i in day.by_task().items()}
        assert ids == {"backup_freshness": "INC-20261002-001", "disk_forecast": "INC-20261002-002",
                       "memory_health": "INC-20261002-003", "failed_units": "INC-20261002-004",
                       "pressure_state": "INC-20261002-005", "probes": "INC-20261002-006", "os_jobs": "INC-20261002-007"}

    def test_disk_warning_open_resolve_mttd_mttr_ack_mitigation(self, day):
        i = day.by_task()["disk_forecast"]
        assert i["severity"] == "sev3" and i["status"] == "resolved"
        assert i["since"] == tk(40)                                        # first failing sample 10:00
        assert i["detected_at"] == tk(41)                                  # confirmed by the second run 10:15
        assert i["mttd_s"] == 900
        assert i["resolved_at"] == tk(46)                                  # first healthy run of the recovery 11:30
        assert i["mttr_s"] == 90 * 60 and i["duration_s"] == 90 * 60
        assert i["acknowledged_at"] == at(10, 16, 5)                       # the WARN page, not the OK one at 11:46
        assert i["mitigated_at"] == at(10, 40)                             # docker_cache really did something ...
        acts = {(a["task"], a["outcome"]): a for a in i["related_actions"]}
        assert acts[("docker_cache", "done")]["bytes"] == 21 * core.GIB
        assert ("apt_clean", "would") in acts                              # ... and the dry-run is listed but never counts
        assert i["postmortem_md"] == ""                                    # sev3: no postmortem stub
        assert i["service_class"] == "P0"
        assert i["summary"] == SUMMARY[("disk_forecast", "warn")]
        assert "sandisk" in i["cause_hint"].lower() or "SSD" in i["cause_hint"]

    def test_memory_warn_to_crit_to_warn_to_resolved(self, day):
        i = day.by_task()["memory_health"]
        assert (i["since"], i["detected_at"]) == (tk(56), tk(57))
        kinds = [(e["kind"], e["t"]) for e in i["timeline"]]
        assert [k for k, _ in kinds if k in ("first_seen", "open", "escalate", "improve", "resolve")] == \
            ["first_seen", "open", "escalate", "improve", "resolve"]
        esc = next(e for e in i["timeline"] if e["kind"] == "escalate")
        assert esc["t"] == tk(59)                                          # second crit run 14:45
        imp = next(e for e in i["timeline"] if e["kind"] == "improve")
        assert imp["t"] == tk(62)                                          # second warn run 15:30
        assert i["severity"] == "sev2"                                     # the peak: a crit incident stays sev2 after it improves
        assert i["resolved_at"] == tk(63) and i["mttr_s"] == int(tk(63) - tk(56)) == 105 * 60
        assert i["acknowledged_at"] == at(14, 16, 30)                      # first page only (the CRIT page later is not the ack)
        assert i["mitigated_at"] == at(15, 0)                              # pressure_response unloaded a model (related task)
        assert i["level"] == 0 and i["status"] == "resolved"

    def test_postmortem_stub_for_sev2(self, day):
        pm = day.by_task()["memory_health"]["postmortem_md"]
        assert pm.startswith("# Postmortem: Memory pressure (INC-20261002-003)")
        for h in ("## Summary", "## Impact", "## Timeline", "## Detection gap", "## What went well", "## What went badly", "## Follow-ups"):
            assert h in pm
        assert "sev2" in pm and "MTTR 1h 45m" in pm and "MTTD 15m" in pm
        assert "time to page 16m" in pm or "time to page 17m" in pm        # 14:00 -> 14:16:30
        assert "- [ ]" in pm                                               # follow-ups are a checklist
        assert "pressure_response" in pm                                   # the recorded action is part of the story
        assert "<" not in pm                                               # Markdown only, no HTML

    def test_skipped_runs_are_neutral_and_failed_page_is_no_ack(self, day):
        i = day.by_task()["backup_freshness"]
        assert (i["since"], i["detected_at"]) == (tk(8), tk(9))
        assert i["resolved_at"] == tk(14) and i["mttr_s"] == 90 * 60       # four skipped runs neither closed nor extended it
        assert i["severity"] == "sev2"
        assert i["acknowledged_at"] is None                                # the page FAILED: no ack
        assert "No page was recorded" in i["postmortem_md"]
        assert "SECRET" not in json.dumps(day.pub())

    def test_correlated_trio_grouped_under_the_earliest_and_sev1(self, day):
        inc = day.by_task()
        fu, ps, pr = inc["failed_units"], inc["pressure_state"], inc["probes"]
        assert (fu["since"], ps["since"], pr["since"]) == (tk(80), tk(81), tk(81))
        assert fu["parent"] is None and fu["children"] == sorted([ps["id"], pr["id"]])
        assert ps["parent"] == fu["id"] and pr["parent"] == fu["id"]
        assert fu["severity"] == "sev1" and ps["severity"] == "sev1"       # crit with two related incidents
        assert pr["severity"] == "sev3"                                    # a warn never becomes sev1
        esc = [e for e in day.events() if e["ev"] == "escalate" and e.get("severity") == "sev1"]
        assert {e["id"] for e in esc} == {fu["id"], ps["id"]} and all(e["ts"] == tk(82) for e in esc)
        # resolution times follow each check's own recovery
        assert (fu["resolved_at"], ps["resolved_at"], pr["resolved_at"]) == (tk(86), tk(86), tk(87))
        assert (fu["mttr_s"], ps["mttr_s"], pr["mttr_s"]) == (90 * 60, 75 * 60, 90 * 60)
        assert fu["acknowledged_at"] == at(20, 31) and ps["acknowledged_at"] is None   # budget exhausted: no page for ps
        assert "Related incidents in the same group" in fu["postmortem_md"] and "escalated to sev1" in fu["postmortem_md"]
        assert pr["postmortem_md"] == ""
        assert "A host stall" in pr["cause_hint"] and f"(see {ps['id']})" in pr["cause_hint"] and f"(see {fu['id']})" in pr["cause_hint"]

    def test_still_open_incident_at_end_of_day(self, day):
        p = day.pub()
        assert [i["task"] for i in p["open"]] == ["os_jobs"]
        o = p["open"][0]
        assert o["status"] == "open" and o["since"] == tk(92) and o["detected_at"] == tk(93)
        assert o["duration_s"] == int(tk(95) + 5 - tk(92))
        assert o["last_checked"] == tk(95)
        assert "mttr_s" not in o and "postmortem_md" not in o and o["level"] == 1
        assert o["playbook"]["title"] == "OS maintenance timers" and any(c.startswith("$ ") for c in o["playbook"]["checks"])
        assert "logrotate overdue" in o["summary"]

    def test_stats_30d(self, day):
        s = day.pub()["stats"]
        assert s["incidents_30d"] == 7 and s["resolved_30d"] == 6 and s["open_count"] == 1
        assert s["mttd_s_30d"] == 900                                      # every incident needed exactly two runs
        assert s["mttr_s_30d"] == (5400 + 5400 + 6300 + 5400 + 4500 + 5400) // 6 == 5400
        assert s["mtta_s_30d"] == (965 + 990 + 1860) // 3                  # only the three incidents that were paged
        assert s["by_severity_30d"] == {"sev1": 2, "sev2": 2, "sev3": 3}

    def test_mttd_counts_open_incidents_but_mttr_only_resolved_ones(self, w):
        (w.conf / "maint.toml").write_text("[global]\nalert_confirm_runs = 2\n[tasks.surrealdb_health]\nalert_confirm_runs = 1\n")
        for i, s in enumerate(["warn", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})                       # resolved: MTTD 900 s (two runs), MTTR 3600 s? no: 2 runs = 1800 s
        for i in range(4, 6):
            w.tick(tk(i), {"surrealdb_health": "crit"})               # open: confirmed on its first run, MTTD 0
        st = w.pub(tk(5) + 5)["stats"]
        assert st["incidents_30d"] == 2 and st["resolved_30d"] == 1 and st["open_count"] == 1
        assert st["mttd_s_30d"] == 450                                # (900 + 0) / 2: the open one counts for detection ...
        assert st["mttr_s_30d"] == 1800                               # ... only the resolved one for recovery (tk(2) - tk(0))

    def test_slo_for_the_day(self, day):
        slo = {o["name"]: o for o in incidents.export_slo(None, tk(95) + 5)["objectives"]}
        assert slo["Services up"]["bad_minutes"] == 6 * 15 and slo["Services up"]["samples"] == 96
        assert slo["Services up"]["availability_pct"] == round(100 * 90 / 96, 3)
        # 99.5% over 30 days allows 14.4 bad slots; 6 were used, and the last day burned the budget 12.5x too fast
        assert slo["Services up"]["budget_remaining_pct"] == round(100 * (1 - 6 / 14.4), 1) == 58.3
        assert slo["Services up"]["burn_rate_1d"] == 12.5 and slo["Services up"]["status"] == "at_risk"
        assert slo["Storage headroom"]["bad_minutes"] == 6 * 15 + 2 * 15   # disk 10:00-11:15 (6 slots) + two growth_watch blips
        assert slo["Platform mounts"]["status"] == "ok" and slo["Platform mounts"]["bad_minutes"] == 0
        assert slo["Monitoring probes"]["bad_minutes"] == 6 * 15
        assert slo["Scheduled jobs"]["bad_minutes"] == 4 * 15              # os_jobs 23:00-23:45
        assert slo["Backups fresh"]["bad_minutes"] == 2 * 15               # skipped runs are not samples, crit runs are bad

    def test_public_files_written_every_update(self, day):
        for name in ("incidents.json", "slo.json"):
            p = day.state / name
            assert p.exists() and (p.stat().st_mode & 0o777) == 0o644
            json.loads(p.read_text())
        assert json.loads((day.state / "incidents.json").read_text())["stats"]["open_count"] == 1


class TestBatchEqualsTick:
    def test_one_big_replay_gives_the_same_incidents(self, tmp_path, monkeypatch, day):
        tick_fold = fold(day.pub())
        w2 = World(tmp_path / "batch", monkeypatch)
        w2.queue_audit(AUDIT)
        for i in range(96):
            for task, status in plan(i).items():
                w2.hist(tk(i), task, status)
        for task, status in plan(95).items():
            w2.entry(tk(95), task, status)
        w2.flush_audit(tk(95) + 5)
        out = incidents.update(w2.status, None, tk(95) + 5)
        assert out["ok"] and total(out, "opened") == 7
        assert fold(w2.pub()) == tick_fold

    def test_state_loss_and_full_replay_adds_nothing(self, day):
        before = fold(day.pub())
        n = len(day.events())
        (day.state / "incidents-state.json").unlink()
        out = incidents.update(day.status, None, tk(95) + 10)
        assert out["ok"] and out["opened"] == [] and out["resolved"] == []
        assert len(day.events()) == n and fold(day.pub(tk(95) + 10)) == before


# ====================================================================================================== lifecycle rules
class TestLifecycle:
    def test_one_run_blip_never_opens(self, w):
        for i, s in enumerate(["ok", "warn", "ok", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        assert w.pub()["open"] == [] and w.pub()["recent"] == [] and not (w.state / "incidents.jsonl").exists()

    def test_flapping_never_opens(self, w):
        for i, s in enumerate(["warn", "ok"] * 8):
            w.tick(tk(i), {"disk_forecast": s})
        assert w.pub()["stats"]["incidents_30d"] == 0

    def test_checks_failing_on_neighbouring_runs_are_grouped_but_not_three_runs_apart(self, w):
        """Real onset is only known to within one 15-minute run: 15 minutes apart in the samples can be 3 minutes apart in reality."""
        for i in range(8):
            w.tick(tk(i), {"disk_forecast": "warn", "memory_health": "warn" if i >= 1 else "ok", "failed_units": "warn" if i >= 4 else "ok"})
        by = {i["task"]: i for i in w.pub(tk(7) + 5)["open"]}
        assert by["memory_health"]["parent"] == by["disk_forecast"]["id"]          # one run apart: grouped
        assert by["failed_units"]["since"] - by["disk_forecast"]["since"] == 4 * STEP
        assert by["failed_units"]["parent"] is None                                # 60 min after the first: a separate cause
        assert by["failed_units"]["id"] not in by["disk_forecast"]["children"]

    def test_two_runs_open_two_runs_resolve(self, w):
        w.tick(tk(0), {"disk_forecast": "warn"})
        assert w.pub()["open"] == []
        out = w.tick(tk(1), {"disk_forecast": "warn"})
        assert len(out["opened"]) == 1
        w.tick(tk(2), {"disk_forecast": "ok"})
        assert len(w.pub()["open"]) == 1                                   # one healthy run is not recovery
        out = w.tick(tk(3), {"disk_forecast": "ok"})
        assert len(out["resolved"]) == 1 and w.pub()["open"] == []

    def test_recovery_streak_broken_by_a_bad_run_stays_open(self, w):
        for i, s in enumerate(["warn", "warn", "ok", "warn", "ok", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        r = w.pub()["recent"][0]
        assert r["resolved_at"] == tk(6)                                   # the streak that finally held started at run 6
        assert w.pub()["stats"]["incidents_30d"] == 1

    def test_new_episode_after_resolution_is_a_new_incident(self, w):
        for i, s in enumerate(["warn", "warn", "ok", "ok", "warn", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        p = w.pub()
        ids = sorted(i["id"] for i in p["recent"])
        assert ids == ["INC-20261002-001", "INC-20261002-002"]
        a, b = sorted(p["recent"], key=lambda i: i["id"])
        assert b["since"] > a["resolved_at"]                               # episodes never overlap
        assert "Recurring: 1 earlier incident" in b["cause_hint"]

    def test_error_status_counts_like_crit(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": ("error", "RuntimeError: statvfs failed", [])})
        i = w.pub()["open"][0]
        assert i["severity"] == "sev2" and "the check itself errored" in i["cause_hint"].lower()

    def test_warn_then_crit_escalates_crit_then_warn_improves_but_keeps_peak(self, w):
        seq = ["warn", "warn", "crit", "crit", "warn", "warn", "warn"]
        for i, s in enumerate(seq):
            w.tick(tk(i), {"memory_health": s})
        i = w.pub()["open"][0]
        assert i["severity"] == "sev2" and i["level"] == 1
        evs = [(e["ev"], e["ts"]) for e in w.events() if e["ev"] in ("open", "escalate", "improve")]
        assert evs == [("open", tk(1)), ("escalate", tk(3)), ("improve", tk(5))]

    def test_alert_false_never_opens_even_when_history_has_no_flag(self, w):
        """history.jsonl records carry no alert flag; the `informational` task list is what keeps a history-only replay honest."""
        for i in range(6):
            for t, st in (("docker_df", "warn"), ("qos_classes", "warn"), ("c2_candidates", "crit"), ("config_drift", "warn")):
                w.hist(tk(i), t, st)
        out = incidents.update({"tasks": {}}, None, tk(6))
        assert out["opened"] == [] and w.pub(tk(6))["stats"]["incidents_30d"] == 0

    def test_status_entry_alert_false_wins_over_a_task_that_is_not_listed(self, w):
        """cmd_run writes the history line and the status entry with the same timestamp; the entry (which has the flag) wins."""
        for i in range(4):
            w.hist(tk(i), "stuck_detector", "warn")                            # history: no flag, task not on the informational list
            w.entry(tk(i), "stuck_detector", "warn", "1 candidate(s), 0 actionable", [], alert=False)
            assert incidents.update(w.status, None, tk(i) + 5)["opened"] == []
        assert w.pub(tk(4))["stats"]["incidents_30d"] == 0

    def test_explicit_alert_flag_wins_over_the_informational_list(self, w):
        for i in range(2):                                                 # per-run flag true on a normally informational task
            w.hist(tk(i), "docker_df", "warn", alert=True)
        incidents.update({"tasks": {}}, None, tk(2))
        assert [x["task"] for x in w.pub(tk(2))["open"]] == ["docker_df"]

    def test_explicit_alert_false_in_history_keeps_a_normal_task_quiet(self, w):
        for i in range(4):
            w.hist(tk(i), "stuck_detector", "warn", alert=False)
        incidents.update({"tasks": {}}, None, tk(4))
        assert w.pub(tk(4))["open"] == []

    def test_info_and_ok_never_open(self, w):
        for i in range(6):
            w.tick(tk(i), {"orphan_report": "info", "memory_health": "ok"})
        assert w.pub()["stats"]["incidents_30d"] == 0

    def test_confirm_runs_follow_the_pagers_global_setting(self, w):
        (w.conf / "maint.toml").write_text("[global]\nalert_confirm_runs = 3\n")
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        assert w.pub()["open"] == []
        w.tick(tk(2), {"disk_forecast": "warn"})
        assert [i["since"] for i in w.pub()["open"]] == [tk(0)]

    def test_per_task_confirm_override(self, w):
        (w.conf / "maint.toml").write_text("[global]\nalert_confirm_runs = 2\n[tasks.surrealdb_health]\nalert_confirm_runs = 1\n")
        w.tick(tk(0), {"surrealdb_health": "crit", "disk_forecast": "warn"})
        assert [i["task"] for i in w.pub()["open"]] == ["surrealdb_health"]

    def test_resolve_runs_can_differ_from_confirm_runs(self, w):
        (w.conf / "playbooks.toml").write_text("[incidents]\nresolve_runs = 3\n")
        for i, s in enumerate(["warn", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        assert len(w.pub()["open"]) == 1
        w.tick(tk(4), {"disk_forecast": "ok"})
        assert w.pub()["open"] == []

    def test_group_window_is_configurable_and_two_far_apart_checks_are_not_grouped(self, w):
        for i in range(8):
            w.tick(tk(i), {"disk_forecast": "warn", "memory_health": "warn" if i >= 3 else "ok"})   # 45 min apart > 10 min + 15 min
        p = w.pub()
        assert [i["parent"] for i in p["open"]] == [None, None]
        w.conf.joinpath("playbooks.toml").write_text("[incidents]\ngroup_window_s = 3600\n")
        incidents.update(w.status, None, tk(8))
        by = {i["task"]: i for i in w.pub(tk(8))["open"]}
        assert by["memory_health"]["parent"] == by["disk_forecast"]["id"]

    def test_chained_group_forms_one_flat_group(self, w):
        # starts 15 min apart. With group_window_s = 700 the compared window is 700 + one 15-min sampling interval = 1600 s:
        # A-B (900 s) and B-C (900 s) are inside it, A-C (1800 s) is not, so C joins A's group only through the chain via B.
        starts = {"disk_forecast": 0, "memory_health": 1, "failed_units": 2}
        (w.conf / "playbooks.toml").write_text("[incidents]\ngroup_window_s = 700\n")
        for i in range(6):
            w.tick(tk(i), {t: ("warn" if i >= s else "ok") for t, s in starts.items()})
        by = {i["task"]: i for i in w.pub(tk(5))["open"]}
        root = by["disk_forecast"]
        assert root["parent"] is None
        assert by["memory_health"]["parent"] == root["id"] and by["failed_units"]["parent"] == root["id"]   # flat, not a chain

    def test_sev1_needs_a_crit_and_two_related(self, w):
        for i in range(3):
            w.tick(tk(i), {"disk_forecast": "warn", "memory_health": "warn", "failed_units": "warn"})
        assert {i["severity"] for i in w.pub()["open"]} == {"sev3"}        # three warns together are still sev3
        for i in range(3, 5):
            w.tick(tk(i), {"disk_forecast": "crit", "memory_health": "warn", "failed_units": "warn"})
        sev = {i["task"]: i["severity"] for i in w.pub()["open"]}
        assert sev == {"disk_forecast": "sev1", "memory_health": "sev3", "failed_units": "sev3"}

    def test_orphan_incident_closes_when_the_check_disappears(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn", "memory_health": "ok"})
        w.status["tasks"].pop("disk_forecast")                              # check removed from status.json
        later = tk(1) + 5 * 3600
        out = incidents.update(w.status, None, later)
        assert out["resolved"] == [] and len(w.pub(later)["open"]) == 1     # not yet: 6 h of grace
        later = tk(1) + 7 * 3600
        out = incidents.update(w.status, None, later)
        assert len(out["resolved"]) == 1
        r = w.pub(later)["recent"][0]
        assert "no longer reported" in r["timeline"][-1]["text"]

    def test_empty_status_does_not_close_anything(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        out = incidents.update({"tasks": {}}, None, tk(1) + 30 * 3600)
        assert out["resolved"] == [] and len(w.pub(tk(1) + 30 * 3600)["open"]) == 1

    def test_pager_confirmed_incident_we_never_saw_is_opened(self, w):
        w.entry(tk(3), "failed_units", "warn", "warn: 1 unhealthy: kavita", ITEMS["failed_units"])
        (w.state / "alerts.json").write_text(json.dumps({"sent": [], "tasks": {"failed_units": {"level": 1, "alerted": 1, "last_sent": tk(3), "pending": 1, "pending_streak": 0}}}))
        out = incidents.update(w.status, None, tk(3) + 5)
        assert len(out["opened"]) == 1
        i = w.pub(tk(3) + 5)["open"][0]
        assert i["task"] == "failed_units" and i["since"] == tk(3)
        assert i["acknowledged_at"] == tk(3)                                # alerts.json says the pager sent at that time
        assert incidents.update(w.status, None, tk(3) + 10)["opened"] == []  # and a second pass does not repeat it

    def test_ack_falls_back_to_alerts_json_when_the_audit_log_is_missing(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        (w.state / "alerts.json").write_text(json.dumps({"sent": [], "tasks": {"disk_forecast": {"level": 1, "alerted": 1, "last_sent": tk(1) + 3, "pending": 1, "pending_streak": 0}}}))
        incidents.update(w.status, None, tk(1) + 20)
        assert w.pub(tk(1) + 20)["open"][0]["acknowledged_at"] == tk(1) + 3

    def test_recovery_page_is_not_an_acknowledgement(self, w):
        w.queue_audit([(tk(5), "notify", "send", "disk_forecast: OK Disk space: recovered", 0, "sent")])
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        w.tick(tk(5), {"disk_forecast": "warn"})
        assert w.pub(tk(5) + 5)["open"][0]["acknowledged_at"] is None

    @pytest.mark.parametrize("target,expected", [
        ("alert: Disk space", True),                       # notify.send (the unified notifier): "<kind>: <title>"
        ("incident_open: Disk space", True),
        ("alert: DISK SPACE low", True),
        ("recovery: Disk space", False), ("incident_resolved: Disk space", False), ("digest_daily: Disk space", False),
        ("alert: Memory pressure", False),                 # a page about another check
        ("disk_forecast: CRIT Disk space", True),          # core.Notifier._send
        ("memory_health: WARN Memory pressure", False),
        ("disk_forecast: OK Disk space: recovered", False),
    ])
    def test_which_notify_rows_acknowledge_an_incident(self, w, target, expected):
        w.queue_audit([(tk(1) + 30, "notify", "send", target, 0, "sent"), (tk(1) + 31, "notify", "send-leg", "alert: sms", 0, "failed rc=1 x")])
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        w.tick(tk(2), {"disk_forecast": "warn"})
        assert (w.pub(tk(2) + 5)["open"][0]["acknowledged_at"] == tk(1) + 30) is expected

    def test_dropped_and_failed_sends_never_acknowledge(self, w):
        w.queue_audit([(tk(1) + 30, "notify", "send", "alert: Disk space", 0, "failed rc=1 smtp"),
                       (tk(1) + 31, "notify", "budget-exhausted", "alert: Disk space", 0, "dropped")])
        for i in range(3):
            w.tick(tk(i), {"disk_forecast": "warn"})
        assert w.pub(tk(2) + 5)["open"][0]["acknowledged_at"] is None

    def test_mitigation_by_a_done_action_on_a_named_entity(self, w):
        w.queue_audit([(tk(1) + 60, "docker_images", "image-rm", "immich_postgres", 0, "done"),
                       (tk(1) + 90, "docker_images", "image-rm", "something-else", 0, "done")])
        for i in range(3):
            w.tick(tk(i), {"failed_units": "warn"})
        i = w.pub(tk(2) + 5)["open"][0]
        assert i["mitigated_at"] == tk(1) + 60                              # matched the failing item's name, not the other row

    def test_unrelated_done_action_is_not_mitigation(self, w):
        w.queue_audit([(tk(1) + 60, "trash", "trash-rm", "/home/ohmz/.local/share/Trash/files/x", 5, "done")])
        for i in range(3):
            w.tick(tk(i), {"failed_units": "warn"})
        assert w.pub(tk(2) + 5)["open"][0]["mitigated_at"] is None

    def test_late_cleanup_after_recovery_still_counts(self, w):
        w.queue_audit([(tk(5) + 600, "docker_cache", "buildx-prune", "builder", 7, "done")])    # 10 min after the resolve
        for i, s in enumerate(["warn", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        w.tick(tk(5), {"disk_forecast": "ok"})
        r = w.pub(tk(5) + 5)["recent"][0]
        assert r["resolved_at"] == tk(2) and r["mitigated_at"] is None     # the cleanup happened after "now": not seen yet

    def test_cleanup_within_the_grace_window_of_the_resolve_is_recorded(self, w):
        w.queue_audit([(tk(3) + 400, "docker_cache", "buildx-prune", "builder", 7, "done")])
        for i, s in enumerate(["warn", "warn", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        w.tick(tk(3), {"disk_forecast": "ok"}, now=tk(3) + 500)
        r = w.pub(tk(3) + 600)["recent"][0]
        assert r["mitigated_at"] == tk(3) + 400 and any(a["task"] == "docker_cache" for a in r["related_actions"])

    def test_many_audit_rows_collapse_into_one_line(self, w):
        w.queue_audit([(tk(1) + k, "docker_images", "image-rm", f"sha{k}", 1000, "done") for k in range(300)])
        for i in range(3):
            w.tick(tk(i), {"disk_forecast": "warn"})
        acts = w.pub(tk(2) + 5)["open"][0]["related_actions"]
        assert len(acts) == 1 and acts[0]["count"] == 300 and acts[0]["bytes"] == 300000

    def test_caps_on_timeline_and_related_actions(self, w):
        w.queue_audit([(tk(1) + k, f"retention", f"act{k}", f"t{k}", 0, "done") for k in range(40)])
        for i in range(60):
            w.tick(tk(i), {"disk_forecast": "crit" if (i // 2) % 2 else "warn"})    # escalate / improve ping-pong
        o = w.pub(tk(59) + 5)["open"][0]
        assert len(o["timeline"]) <= 20 and len(o["related_actions"]) <= 10
        assert any("omitted" in e["text"] for e in o["timeline"])
        assert [e["t"] for e in o["timeline"]] == sorted(e["t"] for e in o["timeline"])

    def test_title_falls_back_to_the_playbook_title_then_the_task_name(self, w):
        for i in range(2):
            w.hist(tk(i), "probes", "warn")
            w.hist(tk(i), "some_new_check", "warn")
        incidents.update({"tasks": {}}, None, tk(2))
        by = {x["task"]: x["title"] for x in w.pub(tk(2))["open"]}
        assert by == {"probes": "Monitoring probes", "some_new_check": "some_new_check"}

    def test_a_cause_that_ended_long_before_the_effect_began_is_not_offered(self, w):
        """Real case from the first run on this host: a disk warning that had resolved an hour earlier was offered as the cause of
        unhealthy containers ("free space before restarting anything")."""
        seq = ["warn", "warn", "ok", "ok"] + ["ok"] * 8
        for i, s in enumerate(seq):
            w.tick(tk(i), {"disk_forecast": s, "failed_units": "warn" if i >= 8 else "ok", "backup_freshness": "crit" if i >= 8 else "ok"})
        by = {x["task"]: x for x in w.pub(tk(11) + 5)["open"]}
        assert "free space before restarting" not in by["failed_units"]["cause_hint"]       # ended 1h15m before: default lag 30 min
        assert "A full filesystem breaks backups" in by["backup_freshness"]["cause_hint"]   # backups lag: lag_s = 12 h in the shipped rules

    def test_a_cause_that_is_still_open_or_just_ended_is_offered(self, w):
        for i in range(6):                                  # disk warns first and is still open when the effect starts
            w.tick(tk(i), {"disk_forecast": "warn", "failed_units": "warn" if i >= 2 else "ok"})
        by = {x["task"]: x for x in w.pub(tk(5) + 5)["open"]}
        assert f"(see {by['disk_forecast']['id']})" in by["failed_units"]["cause_hint"]

    def test_the_cause_must_not_start_after_the_effect(self, w):
        for i in range(6):
            w.tick(tk(i), {"failed_units": "warn", "disk_forecast": "warn" if i >= 2 else "ok"})   # the "cause" began later
        by = {x["task"]: x for x in w.pub(tk(5) + 5)["open"]}
        assert "(see" not in by["failed_units"]["cause_hint"]

    def test_healthy_rows_are_not_entities(self):
        ent = incidents._entities({"items": ITEMS["failed_units"] + ITEMS["probes"] + ITEMS["os_jobs"]
                                   + [{"name": "ok-state", "state": "running"}, {"name": "fine", "sev": "info"}, "junk", None]})
        assert ent == ["immich_postgres", "kavita", "plex", "logrotate"]


# ====================================================================================================== idempotence
class TestIdempotence:
    def test_second_update_with_the_same_inputs_changes_nothing(self, day):
        files = {n: (day.state / n).read_bytes() for n in ("incidents.jsonl", "incidents-state.json", "incidents.json", "slo.json")}
        out = incidents.update(day.status, None, tk(95) + 5)               # same `now`
        assert out["events"] == 0 and out["opened"] == [] and out["resolved"] == []
        for n, b in files.items():
            assert (day.state / n).read_bytes() == b, n

    def test_history_with_every_record_duplicated_opens_nothing_twice(self, w):
        for i in range(6):
            for _ in range(2):
                w.hist(tk(i), "disk_forecast", "warn" if 1 <= i <= 3 else "ok")
        out = incidents.update({"tasks": {}}, None, tk(6))
        assert total(out, "opened") == 1 and total(out, "resolved") == 1
        assert incidents.update({"tasks": {}}, None, tk(6))["events"] == 0

    def test_explicit_history_argument_and_file_history_are_equivalent(self, w):
        rows = [{"t": tk(i), "kind": "task", "task": "disk_forecast", "status": "warn" if 1 <= i <= 3 else "ok"} for i in range(8)]
        out = incidents.update({"tasks": {}}, rows, tk(8))
        assert total(out, "opened") == 1 and total(out, "resolved") == 1

    def test_older_history_than_the_cursor_is_ignored(self, w):
        for i in range(4):
            w.tick(tk(i), {"disk_forecast": "warn" if i >= 1 else "ok"})
        n = len(w.events())
        old = [{"t": tk(i), "kind": "task", "task": "disk_forecast", "status": "ok"} for i in range(0, 3)]
        incidents.update(w.status, old, tk(4))
        assert len(w.events()) == n and len(w.pub(tk(4))["open"]) == 1

    def test_concurrent_updates_serialise(self, w):
        for i in range(3):
            w.hist(tk(i), "disk_forecast", "warn")
        w.hist(tk(3), "disk_forecast", "ok")
        w.hist(tk(4), "disk_forecast", "ok")
        results, errs = [], []

        def go():
            try:
                results.append(incidents.update({"tasks": {}}, None, tk(5)))
            except Exception as exc:  # pragma: no cover
                errs.append(exc)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs and all(r["ok"] for r in results)
        assert sum(total(r, "opened") for r in results) == 1 and sum(total(r, "resolved") for r in results) == 1
        assert [e["ev"] for e in w.events()].count("open") == 1

    def test_corrupt_state_file_duplicates_nothing(self, day):
        before = fold(day.pub())
        (day.state / "incidents-state.json").write_text("{not json")
        out = incidents.update(day.status, None, tk(95) + 5)
        assert out["ok"] and out["opened"] == []
        assert fold(day.pub()) == before

    def test_hand_edited_junk_in_state_is_dropped(self, day):
        st = json.loads((day.state / "incidents-state.json").read_text())
        st["tasks"]["junk"] = "not a dict"
        st["tasks"]["junk2"] = {"cursor": "x"}
        st["tasks"]["disk_forecast"] = {"cursor": tk(1), "seen": "not a list", "fnote": "yes"}
        st["tasks"]["memory_health"] = {"cursor": tk(1), "seen": [None, "x", tk(0), True]}
        st["live"] = {"INC-x": "str"}
        (day.state / "incidents-state.json").write_text(json.dumps(st))
        assert incidents.update(day.status, None, tk(95) + 5)["ok"]

    def test_export_functions_are_read_only(self, day):
        snap = {p.name: p.read_bytes() for p in day.state.iterdir() if p.is_file()}
        incidents.export_incidents(tk(95) + 100)
        incidents.export_slo(None, tk(95) + 100)
        assert {p.name: p.read_bytes() for p in day.state.iterdir() if p.is_file()} == snap

    def test_update_writes_only_under_state_dir(self, tmp_path, monkeypatch):
        w = World(tmp_path, monkeypatch)
        w.queue_audit(AUDIT)
        w.tick(tk(0), {"disk_forecast": "warn"})
        log_before = {p.name: p.read_bytes() for p in w.log.iterdir()}
        conf_before = sorted(p.name for p in w.conf.iterdir())
        w.tick(tk(1), {"disk_forecast": "warn"})
        assert {p.name: p.read_bytes() for p in w.log.iterdir()} == log_before and sorted(p.name for p in w.conf.iterdir()) == conf_before


# ====================================================================================================== crash recovery
class TestCrashRecovery:
    def _one_incident(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})

    def test_truncated_last_line_is_skipped_and_the_next_append_starts_a_fresh_line(self, w):
        self._one_incident(w)
        raw = w.ledger_bytes()
        lines = raw.splitlines(keepends=True)
        cut = raw[: len(raw) - len(lines[-1]) + len(lines[-1]) // 2]        # tear the final (resolve) line in half
        (w.state / "incidents.jsonl").write_bytes(cut)
        assert not cut.endswith(b"\n")
        assert len(incidents._read_events()) == len(lines) - 1              # torn line ignored, everything else read
        pub = incidents.export_incidents(tk(4))                             # exports survive a torn ledger
        assert [i["task"] for i in pub["open"]] == ["disk_forecast"]
        w.tick(tk(4), {"disk_forecast": "ok", "memory_health": "ok"})       # state is current: the resolve is re-derived
        parsed = [json.loads(ln) for ln in (w.state / "incidents.jsonl").read_text().splitlines() if ln.strip().startswith("{") and ln.strip().endswith("}")]
        assert any(e["ev"] == "resolve" for e in parsed)
        assert w.pub(tk(4) + 5)["open"] == [] and w.pub(tk(4) + 5)["recent"][0]["resolved_at"] == tk(2)
        for ln in (w.state / "incidents.jsonl").read_text().splitlines():  # no line is glued onto the garbage
            try:
                json.loads(ln)
            except ValueError:
                assert ln == lines[-1].decode()[: len(lines[-1]) // 2]

    def test_garbage_lines_and_foreign_json_in_the_ledger_are_ignored(self, w):
        self._one_incident(w)
        good = fold(w.pub(tk(4)))
        with open(w.state / "incidents.jsonl", "ab") as f:
            f.write(b"\xff\xfe not json\n[1,2,3]\n\"str\"\n{\"ev\":\"open\"}\n{\"id\":5,\"ev\":\"open\",\"ts\":1}\n"
                    b"{\"id\":\"INC-ghost\",\"ev\":\"resolve\",\"ts\":1}\n{\"id\":\"INC-20261002-001\",\"ev\":\"unknown-event\",\"ts\":9}\n\n")
        assert fold(incidents.export_incidents(tk(4))) == good
        assert incidents.update(w.status, None, tk(4))["ok"]

    def test_crash_before_the_state_write_with_a_complete_ledger_duplicates_nothing(self, w):
        """Ledger fsynced, process killed before the state file was written: the state is OLDER than the ledger."""
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        shutil.copy(w.state / "incidents-state.json", w.root / "state-copy.json")           # the state as of tick 1
        for i in range(2, 5):
            w.tick(tk(i), {"disk_forecast": "ok" if i >= 3 else "warn"})                    # ... then the ledger moves on
        before = fold(w.pub(tk(4) + 5))
        shutil.copy(w.root / "state-copy.json", w.state / "incidents-state.json")           # roll the state file back
        out = incidents.update(w.status, None, tk(4) + 5)
        assert out["ok"] and out["opened"] == [] and out["resolved"] == [] and fold(w.pub(tk(4) + 5)) == before
        kinds = [e["ev"] for e in w.events()]
        assert kinds.count("open") == 1 and kinds.count("resolve") == 1

    def test_multi_episode_backlog_replayed_after_a_crash_does_not_duplicate_episodes(self, w):
        seq = ["warn", "warn", "ok", "ok", "warn", "warn", "ok", "ok", "warn", "warn"]
        for i, s in enumerate(seq):
            w.hist(tk(i), "disk_forecast", s)
        incidents.update({"tasks": {}}, None, tk(10))                      # first install: the whole backlog in one go
        full = fold(w.pub(tk(10)))
        assert len(full) == 3
        (w.state / "incidents-state.json").write_text(json.dumps({"v": 1, "tasks": {"disk_forecast": {"cursor": tk(0)}}, "live": {}}))
        out = incidents.update({"tasks": {}}, None, tk(10))               # crash after the ledger fsync: state is old
        assert out["opened"] == [] and fold(w.pub(tk(10))) == full

    def test_state_ahead_of_the_ledger_a_lost_resolve_line_is_rederived(self, w):
        self._one_incident(w)
        want = fold(w.pub(tk(4)))
        raw = w.ledger_bytes()
        last = raw.splitlines(keepends=True)[-1]
        (w.state / "incidents-state.json").write_bytes((w.state / "incidents-state.json").read_bytes())   # state is current
        (w.state / "incidents.jsonl").write_bytes(raw[: len(raw) - len(last)])                          # the resolve line vanished
        assert [i["status"] for i in incidents.export_incidents(tk(4))["open"]] == ["open"]
        out = incidents.update(w.status, None, tk(4))
        assert len(out["resolved"]) == 1
        got = fold(w.pub(tk(4)))
        assert [g[:6] for g in got] == [x[:6] for x in want] and got[0][6] == tk(2)    # same resolved_at

    def test_state_ahead_of_the_ledger_a_lost_open_line_is_rederived(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        want = fold(w.pub(tk(1) + 5))
        (w.state / "incidents.jsonl").unlink()                              # the open line (and everything) is gone, state is current
        out = incidents.update(w.status, None, tk(1) + 10)
        assert len(out["opened"]) == 1
        got = incidents.export_incidents(tk(1) + 10)["open"][0]
        assert got["since"] == tk(0) and got["detected_at"] == tk(1) and got["id"] == want[0][0]

    def test_state_ahead_of_the_ledger_a_lost_escalate_line_is_rederived(self, w):
        for i, s in enumerate(["warn", "warn", "crit", "crit"]):
            w.tick(tk(i), {"memory_health": s})
        evs = w.events()
        assert evs[-1]["ev"] == "escalate"
        raw = w.ledger_bytes()
        (w.state / "incidents.jsonl").write_bytes(raw[: len(raw) - len(raw.splitlines(keepends=True)[-1])])
        assert incidents.export_incidents(tk(3))["open"][0]["level"] == 1
        incidents.update(w.status, None, tk(3) + 10)
        o = w.pub(tk(3) + 10)["open"][0]
        assert o["level"] == 2 and o["severity"] == "sev2"

    def test_a_lost_improve_line_is_rederived_without_raising_the_severity(self, w):
        for i, s in enumerate(["crit", "crit", "warn", "warn"]):
            w.tick(tk(i), {"memory_health": s})
        raw = w.ledger_bytes()
        (w.state / "incidents.jsonl").write_bytes(raw[: len(raw) - len(raw.splitlines(keepends=True)[-1])])
        incidents.update(w.status, None, tk(3) + 10)
        o = w.pub(tk(3) + 10)["open"][0]
        assert o["level"] == 1 and o["severity"] == "sev2"

    @pytest.mark.parametrize("target_tick", [82])
    def test_crash_at_every_byte_boundary_of_a_multi_event_batch(self, tmp_path, monkeypatch, target_tick):
        """Tick 82 (20:30) opens two incidents, groups them under the third and escalates two to sev1: 6 ledger lines in one
        batch. Tear that batch at every line start, mid-line and line end; after recovery the incidents must equal a clean run."""
        base = World(tmp_path / "base", monkeypatch)
        base.queue_audit(AUDIT)
        base.day(target_tick)                                              # ticks 0 .. target_tick-1
        snap = tmp_path / "snap"
        shutil.copytree(base.root, snap)
        before = len(base.ledger_bytes())
        base.tick(tk(target_tick), plan(target_tick))
        batch = base.ledger_bytes()[before:]
        clean = fold(base.pub(tk(target_tick) + 5))
        assert len(batch.splitlines()) >= 5, batch
        cuts, pos = {0}, 0
        for ln in batch.splitlines(keepends=True):
            cuts |= {pos + 1, pos + len(ln) // 2, pos + len(ln) - 1, pos + len(ln)}
            pos += len(ln)
        cuts = sorted(c for c in cuts if c <= len(batch))
        assert len(cuts) >= 15
        for k, cut in enumerate(cuts):
            root = tmp_path / f"crash{k}"
            shutil.copytree(snap, root)
            monkeypatch.undo()
            monkeypatch.setenv("TZ", "America/Toronto")
            time.tzset()
            w = World(root, monkeypatch)
            w.status = json.loads(json.dumps(base.status))
            real = incidents._append_events

            def torn(events, cut=cut):
                data = "".join(json.dumps(e, separators=(",", ":"), sort_keys=True, default=str) + "\n" for e in events).encode()
                with open(core.STATE_DIR / "incidents.jsonl", "ab") as f:
                    f.write(data[:cut])
                raise OSError("simulated crash")
            # this tick's inputs exactly as the clean run had them, then the crash while appending, then recovery
            w.queue_audit(AUDIT)
            w.flush_audit(tk(target_tick - 1) + 5)
            for task, status in plan(target_tick).items():
                w.hist(tk(target_tick), task, status)
                w.entry(tk(target_tick), task, status)
            w.flush_audit(tk(target_tick) + 5)
            monkeypatch.setattr(incidents, "_append_events", torn)
            out = incidents.update(w.status, None, tk(target_tick) + 5)
            assert out["ok"] is False                                      # update() reports, never raises
            monkeypatch.setattr(incidents, "_append_events", real)
            assert incidents.update(w.status, None, tk(target_tick) + 5)["ok"]
            assert fold(w.pub(tk(target_tick) + 5)) == clean, f"cut at byte {cut} of {len(batch)}"
            assert incidents.update(w.status, None, tk(target_tick) + 5)["events"] == 0     # and it is stable afterwards

    def test_full_disk_while_appending_loses_nothing(self, w, monkeypatch):
        w.tick(tk(0), {"disk_forecast": "warn"})
        real = incidents._append_events
        monkeypatch.setattr(incidents, "_append_events", lambda ev: (_ for _ in ()).throw(OSError(28, "No space left on device")))
        out = w.tick(tk(1), {"disk_forecast": "warn"})
        assert out["ok"] is False and "No space" in out["error"]
        assert not (w.state / "incidents.jsonl").exists()
        monkeypatch.setattr(incidents, "_append_events", real)
        w.tick(tk(2), {"disk_forecast": "warn"})
        o = w.pub(tk(2) + 5)["open"][0]
        assert o["since"] == tk(0) and o["detected_at"] == tk(1)           # the failed tick's observation was not lost

    def test_unwritable_state_dir_never_raises(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setattr(core, "STATE_DIR", blocker / "state")
        out = incidents.update({"tasks": {"a": {"status": "crit", "last_run": 5.0}}}, None, 10.0)
        assert out["ok"] is False and isinstance(out["error"], str)
        assert incidents.write_public(10.0) == [] and incidents.export_incidents(10.0)["open"] == []

    def test_compaction_drops_old_resolved_incidents_only(self, w, monkeypatch):
        monkeypatch.setattr(incidents, "LEDGER_COMPACT_BYTES", 1)
        (w.conf / "playbooks.toml").write_text("[incidents]\nkeep_days = 1\n")
        for i, s in enumerate(["warn", "warn", "ok", "ok"]):
            w.tick(tk(i), {"disk_forecast": s})
        for i in range(4, 6):
            w.tick(tk(i), {"memory_health": "warn", "disk_forecast": "ok"})
        old_id = "INC-20261002-001"
        assert old_id in w.ledger_bytes().decode()
        week = tk(6) + 3 * 86400
        w.status["tasks"]["disk_forecast"]["last_run"] = week                # keep the tick coherent, then update much later
        w.status["tasks"]["memory_health"]["last_run"] = week
        incidents.update(w.status, [{"t": week, "kind": "task", "task": "memory_health", "status": "warn"}], week + 5)
        text = w.ledger_bytes().decode()
        assert old_id not in text and "INC-20261002-002" in text            # resolved long ago: gone; still open: kept
        assert [i["task"] for i in incidents.export_incidents(week + 5)["open"]] == ["memory_health"]


# ====================================================================================================== differential: Notifier
class TestMatchesTheNotifier:
    """The contract is "the same debounce as core.Notifier": run the real Notifier (sending mocked) and the incident engine on the
    same random status sequences; every page the notifier would send is an incident event at the same time, and the reverse."""

    @pytest.mark.parametrize("seed", range(10))
    def test_pages_and_incident_events_coincide(self, tmp_path, monkeypatch, seed):
        rng = random.Random(seed)
        w = World(tmp_path, monkeypatch)
        confirm = rng.choice([2, 2, 2, 3, 1])
        (w.conf / "maint.toml").write_text(f"[global]\nalert_confirm_runs = {confirm}\n")
        notif = core.Notifier({"global": {"alert_confirm_runs": confirm, "alert_daily_budget": 10 ** 6}})
        pages = []
        monkeypatch.setattr(notif, "_send", lambda name, subject, body, now: pages.append((now, name, subject.split()[0])) or True)
        tasks = ["disk_forecast", "memory_health", "failed_units"]
        stay = {t: rng.choice([0.55, 0.7, 0.85]) for t in tasks}
        cur = {t: "ok" for t in tasks}
        for i in range(130):
            res = {}
            for t in tasks:
                if rng.random() > stay[t]:
                    cur[t] = rng.choice(["ok", "ok", "warn", "warn", "crit", "error", "info"])
                res[t] = cur[t]
            t_now = tk(i)
            for task, status in res.items():
                notif.evaluate(task, TITLES[task], core.Result(status, f"{status} {task}"), t_now)
            w.tick(t_now, res)
        mine, task_of, level_of = [], {}, {}
        for e in w.events():
            if e["ev"] == "open":
                task_of[e["id"]], level_of[e["id"]] = e["task"], e["level"]
                mine.append((e["ts"], e["task"], "WARN" if e["level"] == 1 else "CRIT"))
            elif e["ev"] in ("escalate", "improve") and e["level"] != level_of[e["id"]]:     # a sev1 escalation changes no level
                level_of[e["id"]] = e["level"]
                mine.append((e["ts"], task_of[e["id"]], "WARN" if e["level"] == 1 else "CRIT"))
            elif e["ev"] == "resolve":
                mine.append((e["ts"], task_of[e["id"]], "OK"))
        assert sorted(mine) == sorted(pages), f"seed {seed} confirm {confirm}"
        assert len(pages) > 10, "the random run must actually exercise the state machine"

    @pytest.mark.parametrize("seed", range(4))
    def test_invariants_and_tick_vs_batch_on_random_runs(self, tmp_path, monkeypatch, seed):
        rng = random.Random(1000 + seed)
        w = World(tmp_path / "tick", monkeypatch)
        tasks = ["disk_forecast", "memory_health", "failed_units", "probes", "backup_freshness"]
        cur, seq = {t: "ok" for t in tasks}, []
        for i in range(200):
            for t in tasks:
                if rng.random() < 0.18:
                    cur[t] = rng.choice(["ok", "ok", "warn", "crit", "skipped"])
            seq.append((tk(i), dict(cur)))
            w.tick(tk(i), dict(cur))
        pub = w.pub(tk(199) + 5)
        allx = pub["open"] + pub["recent"]
        assert len([i for i in pub["open"]]) == len({i["task"] for i in pub["open"]})        # one open incident per task
        by_task = {}
        for i in allx:
            by_task.setdefault(i["task"], []).append(i)
            assert i["detected_at"] >= i["since"]
            if i["status"] == "resolved":
                assert i["resolved_at"] >= i["since"] and i["mttr_s"] >= i["mttd_s"] - 1
        for task, xs in by_task.items():
            xs.sort(key=lambda i: i["since"])
            for a, b in zip(xs, xs[1:]):
                assert a["status"] == "resolved" and b["since"] > a["resolved_at"]            # episodes never overlap
        # the same history replayed in one go equals the tick-by-tick result
        w2 = World(tmp_path / "batch", monkeypatch)
        for t, st in seq:
            for task, s in st.items():
                w2.hist(t, task, s)
        for task, s in seq[-1][1].items():
            w2.entry(tk(199), task, s)
        incidents.update(w2.status, None, tk(199) + 5)
        assert lfold(w2) == lfold(w)
        assert len(lfold(w)) > 20, "the random run must produce plenty of incidents"


# ====================================================================================================== robustness
class TestRobustness:
    @pytest.mark.parametrize("status", [None, {}, [], "x", {"tasks": None}, {"tasks": []}, {"tasks": {"a": None, "b": "s", "c": 5}},
                                        {"tasks": {"a": {"status": None, "last_run": "soon", "items": "junk", "summary": 5}}},
                                        {"tasks": {"a": {"status": "crit", "last_run": float("nan")}}},
                                        {"tasks": {"a": {"status": "crit", "last_run": float("inf")}}},
                                        {"tasks": {"a": {"status": "crit", "last_run": True}}}])
    def test_garbage_status_never_raises(self, w, status):
        out = incidents.update(status, None, tk(0))
        assert out["ok"] is True

    def test_garbage_history_records_never_raise(self, w):
        rows = [None, 5, "x", [], {}, {"kind": "task"}, {"kind": "task", "task": 5, "t": 1}, {"kind": "task", "task": "a", "t": "x"},
                {"kind": "task", "task": "a", "t": float("nan"), "status": "crit"}, {"kind": "task", "task": "a", "t": -5, "status": "crit"},
                {"kind": "task", "task": "a", "t": 1e18, "status": "crit"}, {"kind": "disk", "t": 1}, {"kind": "task", "task": "a", "t": 10, "status": {"x": 1}}]
        assert incidents.update({"tasks": {}}, rows, tk(0))["ok"] is True

    def test_garbage_audit_lines_are_skipped(self, w):
        (w.log / "audit.jsonl").write_text("not json\n{}\n[1]\n" + json.dumps({"ts": "garbage", "task": "x"}) + "\n"
                                           + json.dumps({"ts": stamp(tk(1)), "task": "docker_cache", "action": "a", "target": 5, "bytes": "x", "outcome": None}) + "\n")
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        assert len(w.pub(tk(1) + 5)["open"]) == 1

    def test_missing_files_everywhere_still_exports_an_empty_but_valid_document(self, w):
        pub = incidents.export_incidents(100.0)
        assert pub == {"generated_at": 100.0, "open": [], "recent": [],
                       "stats": {"mttd_s_30d": None, "mttr_s_30d": None, "mtta_s_30d": None, "incidents_30d": 0, "resolved_30d": 0,
                                 "open_count": 0, "by_severity_30d": {"sev1": 0, "sev2": 0, "sev3": 0}}}

    def test_broken_playbooks_toml_override_does_not_stop_tracking(self, w):
        (w.conf / "playbooks.toml").write_text("[[[ this is not toml")
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        assert len(w.pub(tk(1) + 5)["open"]) == 1

    def test_hostile_playbooks_toml_values_are_tolerated(self, w):
        (w.conf / "playbooks.toml").write_text('[playbook.disk_forecast]\nchecks = [1, "$ ok"]\nrelated_tasks = "x"\nhint = [1, {match="(", text="t"}, {match="x"}]\n'
                                               '[incidents]\nconfirm_runs = "two"\ngroup_window_s = -5\nrecent_max = 0\n'
                                               '[[slo]]\nname = 5\n[[slo]]\nname = "ok"\ntarget_pct = "x"\nchecks = [1, "disk_forecast"]\n')
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        o = w.pub(tk(1) + 5)["open"][0]
        assert o["playbook"]["checks"] == ["$ ok"]
        names = [o["name"] for o in incidents.export_slo(None, tk(2))["objectives"]]
        assert "ok" in names and 5 not in names and "Services up" in names             # the junk table is skipped, the shipped ones stay

    def test_public_files_stay_under_the_size_cap(self, w, monkeypatch):
        monkeypatch.setattr(incidents, "_new_id", lambda L, ts, n=[0]: (n.__setitem__(0, n[0] + 1), f"INC-20261002-{n[0]:03d}")[1])
        big = "x " * 600
        for k in range(62):
            w.tick(tk(2 * k), {"memory_health": "crit"})
            w.tick(tk(2 * k + 1), {"memory_health": "crit"})
            e = w.status["tasks"]["memory_health"]
            e["summary"] = big
            for j in range(2):
                w.tick(tk(2 * k + 1) + 400 + j, {"memory_health": "ok"}, run=False)
            incidents.update(w.status, None, tk(2 * k + 1) + 410)
        # the incidents.json cap: <= 50 recent and < 200 KB whatever they contain
        raw = (w.state / "incidents.json").read_text()
        assert len(raw) < 200_000
        assert len(json.loads(raw)["recent"]) <= 50

    def test_size_cap_sheds_old_detail_first_then_old_incidents_never_the_open_ones(self, w, monkeypatch):
        """The public file must stay under its cap however busy the month was: oldest postmortems/timelines shrink first, then the
        oldest resolved incidents go; open incidents and the stats are never touched."""
        for i in range(120):
            w.tick(tk(i), {t: ("crit" if (i // 2) % 2 == 0 else "ok") for t in ("disk_forecast", "memory_health", "failed_units")})
        w.tick(tk(120), {"os_jobs": "warn"})
        w.tick(tk(121), {"os_jobs": "warn"})
        full = w.pub(tk(121) + 5)
        full_bytes = len(json.dumps(full, separators=(",", ":")))
        assert len(full["recent"]) == 50 and full_bytes > 60_000
        for cap in (full_bytes - 5_000, 60_000, 30_000, 12_000):
            monkeypatch.setattr(incidents, "PUBLIC_MAX_BYTES", cap)
            pub = incidents.export_incidents(tk(121) + 5)
            size = len(json.dumps(pub, separators=(",", ":")))
            assert size <= max(cap, len(json.dumps({**pub, "recent": []}, separators=(",", ":")))), (cap, size)
            assert [o["id"] for o in pub["open"]] == [o["id"] for o in full["open"]] and pub["open"][0]["playbook"]["checks"]
            assert pub["stats"] == full["stats"]
            assert [r["id"] for r in pub["recent"]] == [r["id"] for r in full["recent"]][:len(pub["recent"])]      # newest kept
        small = incidents.export_incidents(tk(121) + 5)
        assert len(small["recent"]) < 50 and len(small["recent"]) >= 1
        monkeypatch.setattr(incidents, "PUBLIC_MAX_BYTES", full_bytes - 5_000)
        mid = incidents.export_incidents(tk(121) + 5)
        assert len(mid["recent"]) == 50 and any(len(r.get("postmortem_md", "")) <= 400 + 20 for r in mid["recent"][-3:])   # detail shrank first

    def test_file_modes(self, day):
        for n in ("incidents.json", "slo.json", "incidents-state.json"):
            assert (day.state / n).stat().st_mode & 0o777 == 0o644

    def test_perf_one_tick_on_two_weeks_of_history(self, w):
        names = [f"task_{i}" for i in range(37)]
        t0 = tk(0)
        with open(w.state / "history.jsonl", "w") as f:
            for i in range(14 * 96):
                for n in names:
                    f.write(json.dumps({"t": t0 + i * STEP, "kind": "task", "task": n, "status": "warn" if (i % 200 < 3 and n == "task_3") else "ok",
                                        "reclaimed": 0, "dur": 0.1, "metrics": {"a": 1, "b": 2.5}}) + "\n")
        t = time.perf_counter()
        incidents.update({"tasks": {}}, None, t0 + 14 * 86400)           # first install: the whole backlog
        first = time.perf_counter() - t
        w.hist(t0 + 14 * 86400 + 60, "task_3", "ok")
        t = time.perf_counter()
        incidents.update({"tasks": {}}, None, t0 + 14 * 86400 + 120)     # a normal tick
        tick = time.perf_counter() - t
        assert first < 20 and tick < 4, (first, tick)


# ====================================================================================================== public exports: shape
class TestExportShape:
    def test_incidents_json_shape(self, day):
        pub = json.loads((day.state / "incidents.json").read_text())
        assert set(pub) == {"generated_at", "open", "recent", "stats"}
        assert {"mttd_s_30d", "mttr_s_30d", "incidents_30d", "open_count"} <= set(pub["stats"])
        o = pub["open"][0]
        assert {"id", "task", "title", "severity", "since", "duration_s", "summary", "playbook", "timeline", "related_actions"} <= set(o)
        assert set(o["playbook"]) == {"task", "title", "class", "meaning", "impact", "checks", "fixes", "avoid", "ask"}
        r = pub["recent"][0]
        assert {"mttr_s", "postmortem_md", "resolved_at", "mttd_s", "resolution"} <= set(r)
        for i in pub["open"] + pub["recent"]:
            assert i["severity"] in ("sev1", "sev2", "sev3") and re.fullmatch(r"INC-\d{8}-\d{3}", i["id"])
            assert isinstance(i["since"], float) and isinstance(i["duration_s"], int)
            assert len(i["timeline"]) <= 20 and len(i["related_actions"]) <= 10
            for e in i["timeline"]:
                assert set(e) == {"t", "kind", "text"} and isinstance(e["t"], float)
            assert len(i.get("postmortem_md", "")) <= 3000 + len("\n... (truncated)")

    def test_recent_is_newest_closed_first(self, day):
        closed_at = {e["id"]: e["ts"] for e in day.events() if e["ev"] == "resolve"}
        rec = day.pub()["recent"]
        assert len(rec) == 6
        order = [closed_at[i["id"]] for i in rec]
        assert order == sorted(order, reverse=True) and order[0] == tk(88)             # probes closed last, at 22:00

    def test_open_incidents_sorted_by_severity_then_age(self, w):
        for i in range(6):                           # os_jobs warns first (oldest, sev3); the correlated crit trio starts later
            w.tick(tk(i), {"os_jobs": "warn", "failed_units": "crit" if i >= 2 else "ok", "pressure_state": "crit" if i >= 2 else "ok",
                           "probes": "warn" if i >= 2 else "ok", "smart_trend": "warn" if i >= 1 else "ok"})
        order = [(i["task"], i["severity"]) for i in w.pub(tk(5) + 5)["open"]]
        assert order == [("failed_units", "sev1"), ("pressure_state", "sev1"), ("os_jobs", "sev3"), ("smart_trend", "sev3"), ("probes", "sev3")]

    def test_postmortem_is_truncated_to_3kb_in_the_export_but_kept_in_the_ledger(self, w):
        big = "# Postmortem\n" + "".join(f"- line {k} of a very long postmortem\n" for k in range(190))
        assert 5000 < len(big) < 8000
        evs = [{"ts": tk(1), "id": "INC-20261002-001", "ev": "open", "task": "disk_forecast", "title": "Disk space", "level": 2,
                "severity": "sev2", "word": "crit", "started_at": tk(0), "summary": "crit: x", "entities": []},
               {"ts": tk(3), "id": "INC-20261002-001", "ev": "resolve", "resolved_at": tk(2), "resolution": "ok", "actions": [],
                "postmortem_md": big}]
        (w.state / "incidents.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
        r = w.pub(tk(5))["recent"][0]
        assert r["postmortem_md"].startswith("# Postmortem\n- line 0") and r["postmortem_md"].endswith("... (truncated)")
        assert len(r["postmortem_md"]) == 3000 + len("\n... (truncated)")
        assert incidents._Ledger(w.events()).incs["INC-20261002-001"]["postmortem_md"] == big       # the ledger keeps it whole

    def test_slo_json_shape(self, day):
        s = json.loads((day.state / "slo.json").read_text())
        assert set(s) == {"generated_at", "window_days", "objectives"} and s["window_days"] == 30
        names = [o["name"] for o in s["objectives"]]
        for n in ("Platform mounts", "Services up", "Host health", "Storage headroom", "Backups fresh", "Pressure ladder"):
            assert n in names
        for o in s["objectives"]:
            assert {"name", "target_pct", "class", "checks", "availability_pct", "budget_remaining_pct", "burn_rate_1d", "status"} <= set(o)
            assert o["status"] in ("ok", "at_risk", "breached")

    def test_write_public(self, day):
        names = incidents.write_public(tk(95) + 5)
        assert sorted(names) == ["incidents.json", "slo.json"]
        pub = day.state / "public"
        assert pub.stat().st_mode & 0o777 == 0o755
        for n in names:
            assert (pub / n).stat().st_mode & 0o777 == 0o644 and json.loads((pub / n).read_text())
        assert not list(pub.glob("*.tmp"))


# ====================================================================================================== SLO maths
class TestSlo:
    @staticmethod
    def conf(w, target=99.0, extra=""):
        (w.conf / "playbooks.toml").write_text(f'[[slo]]\nname = "T"\nclass = "P1"\ntarget_pct = {target}\nchecks = ["a", "b"]\n{extra}')

    @staticmethod
    def rec(slot, task, status, **kw):
        return {"t": slot * STEP + 3, "kind": "task", "task": task, "status": status, **kw}

    NOW = 2000 * STEP * 30                                          # an arbitrary slot-aligned instant

    def slo(self, hist, now=None):
        return next(o for o in incidents.export_slo(hist, now or self.NOW)["objectives"] if o["name"] == "T")

    def slots(self, n, back=0):
        base = self.NOW // STEP - back
        return [base - k for k in range(n)]

    def test_all_healthy(self, w):
        self.conf(w)
        o = self.slo([self.rec(s, "a", "ok") for s in self.slots(100)])
        assert (o["availability_pct"], o["budget_remaining_pct"], o["burn_rate_1d"], o["status"]) == (100.0, 100.0, 0.0, "ok")
        assert o["samples"] == 100 and o["observed_h"] == 25.0 and o["budget_minutes"] == 432

    def test_budget_burn_and_statuses(self, w):
        self.conf(w, 99.0)                                          # budget = 30 d * 96 * 1% = 28.8 slots
        base = [self.rec(s, "a", "ok") for s in self.slots(200)]
        # 5 old bad slots (> 1 day ago): budget 82.6% left, no burn in the last day
        old = [self.rec(s, "a", "crit") for s in self.slots(5, back=150)]
        o = self.slo(base + old)
        assert o["budget_remaining_pct"] == round(100 * (1 - 5 / 28.8), 1) and o["burn_rate_1d"] == 0.0 and o["status"] == "ok"
        # 10 recent bad slots: burning ~10x too fast
        o = self.slo(base + [self.rec(s, "a", "warn") for s in self.slots(10)])
        # the trailing day holds 97 slots here (96 back plus the current one), 10 of them bad
        assert o["status"] == "at_risk" and o["burn_rate_1d"] == round((10 / 97) / 0.01, 2) and o["bad_minutes"] == 150
        assert o["availability_pct"] == round(100 * 190 / 200, 3)
        # budget gone
        o = self.slo(base + [self.rec(s, "a", "crit") for s in self.slots(30, back=150)])
        assert o["status"] == "breached" and o["budget_remaining_pct"] < 0
        # clamped
        o = self.slo([self.rec(s, "a", "error") for s in self.slots(200)])
        assert o["budget_remaining_pct"] == -100.0 and o["availability_pct"] == 0.0 and o["status"] == "breached"

    def test_breached_exactly_when_the_budget_is_gone_not_before(self, w):
        self.conf(w, 98.75)                                          # 30 d * 96 * 1.25% = exactly 36 bad slots allowed
        base = [self.rec(s, "a", "ok") for s in self.slots(300)]
        o = self.slo(base + [self.rec(s, "a", "crit") for s in self.slots(35, back=150)])
        assert o["budget_remaining_pct"] == round(100 * (1 - 35 / 36), 1) == 2.8 and o["status"] == "at_risk"     # 2.8% left: not breached
        o = self.slo(base + [self.rec(s, "a", "crit") for s in self.slots(36, back=150)])
        assert o["budget_remaining_pct"] == 0.0 and o["status"] == "breached"                                      # exactly spent: breached
        o = self.slo(base + [self.rec(s, "a", "crit") for s in self.slots(37, back=150)])
        assert o["budget_remaining_pct"] < 0 and o["status"] == "breached"

    def test_low_budget_alone_is_at_risk(self, w):
        self.conf(w, 99.0)
        hist = [self.rec(s, "a", "ok") for s in self.slots(200)] + [self.rec(s, "a", "crit") for s in self.slots(23, back=150)]
        o = self.slo(hist)
        assert 0 < o["budget_remaining_pct"] < 25 and o["burn_rate_1d"] == 0.0 and o["status"] == "at_risk"

    def test_union_of_checks_per_slot(self, w):
        self.conf(w)
        s = self.slots(10)
        hist = [self.rec(x, "a", "ok") for x in s[2:]] + [self.rec(x, "b", "ok") for x in s[2:]]
        hist += [self.rec(s[0], "a", "ok"), self.rec(s[0], "b", "warn"), self.rec(s[1], "a", "crit"), self.rec(s[1], "b", "ok")]
        o = self.slo(hist)
        assert o["bad_minutes"] == 30 and o["samples"] == 10       # slot 0 bad through b, slot 1 bad through a: each counted once
        hist.append(self.rec(s[0], "a", "crit"))                   # both checks bad in the same slot still count once
        assert self.slo(hist)["bad_minutes"] == 30

    def test_two_runs_in_one_slot_count_once_and_bad_wins(self, w):
        self.conf(w)
        s = self.slots(4)
        o = self.slo([self.rec(s[0], "a", "ok"), self.rec(s[0], "a", "crit"), self.rec(s[1], "a", "ok"), self.rec(s[1], "a", "ok")])
        assert o["samples"] == 2 and o["bad_minutes"] == 15

    def test_skipped_and_other_kinds_are_not_samples(self, w):
        self.conf(w)
        s = self.slots(4)
        hist = [self.rec(s[0], "a", "skipped"), self.rec(s[1], "a", "ok"), {"t": s[2] * STEP, "kind": "disk", "task": "a"},
                {"t": s[3] * STEP, "kind": "sample", "task": "a", "status": "crit"}]
        assert self.slo(hist)["samples"] == 1

    def test_informational_tasks_never_count_and_explicit_flags_win(self, w):
        self.conf(w, extra="")
        (w.conf / "playbooks.toml").write_text('[slo_defaults]\ninformational = ["a"]\n[[slo]]\nname = "T"\ntarget_pct = 99\nchecks = ["a", "b"]\n')
        s = self.slots(4)
        hist = [self.rec(s[0], "a", "crit"), self.rec(s[1], "a", "crit", alert=True), self.rec(s[2], "b", "warn", alert=False), self.rec(s[3], "b", "ok")]
        assert self.slo(hist)["bad_minutes"] == 15                 # only the explicit alert=True crit on an informational task

    def test_samples_outside_the_window_are_ignored(self, w):
        self.conf(w)
        hist = [self.rec(self.NOW // STEP - 31 * 96, "a", "crit"), self.rec(self.NOW // STEP - 1, "a", "ok")]
        o = self.slo(hist)
        assert o["samples"] == 1 and o["bad_minutes"] == 0

    def test_no_samples_means_collecting_data_not_green_numbers(self, w):
        self.conf(w)
        o = self.slo([])
        assert o["availability_pct"] is None and o["budget_remaining_pct"] is None and o["burn_rate_1d"] is None
        assert o["note"] == "collecting data" and o["status"] == "ok" and o["samples"] == 0

    def test_target_is_clamped_and_class_defaults(self, w):
        (w.conf / "playbooks.toml").write_text('[[slo]]\nname = "A"\ntarget_pct = 100\nchecks = ["a"]\n[[slo]]\nname = "B"\ntarget_pct = 0\nchecks = ["a"]\n')
        objs = {o["name"]: o for o in incidents.export_slo([self.rec(self.NOW // STEP - 1, "a", "ok")], self.NOW)["objectives"]}
        assert [objs["A"]["target_pct"], objs["B"]["target_pct"]] == [99.999, 1.0] and objs["A"]["class"] == "P2"

    def test_shipped_objectives_only_name_real_tasks_and_sane_targets(self):
        cfg = incidents.load_config()
        known = registered_tasks()
        assert len(cfg["slo"]) >= 6
        for o in cfg["slo"]:
            assert o["name"] and 90 <= o["target_pct"] < 100 and o["class"] in ("P0", "P1", "P2", "P3"), o
            assert o["checks"] and set(o["checks"]) <= known, o
            assert not set(o["checks"]) & set(cfg["slo_defaults"]["informational"]), o       # an SLO on a never-alerting task is meaningless


# ====================================================================================================== redaction
SECRETS = [
    "hunter2hunter2", "sk-live-ABCDEF0123456789", "abcdef1234567890SECRETSECRET", "owner@example.com", "555-867-5309",
    "tok_9f8e7d6c5b4a39281706f5e4d3c2b1a0", "s3cr3tpassw0rd", "key=ZZZ-hidden-value", "AKIAABCDEFGHIJKLMNOP", "queryleak123",
]
HOSTILE = ("RuntimeError: smtp login failed password: hunter2hunter2 for owner@example.com call 555-867-5309; "
           "Authorization: Bearer abcdef1234567890SECRETSECRET; token=tok_9f8e7d6c5b4a39281706f5e4d3c2b1a0 "
           "https://api.example.com/v1/x?key=queryleak123&b=2 Traceback (most recent call last):\n  File \"/usr/lib/python3.12/x.py\", line 3, in f\n    raise X")


class TestNoSecretsOrTracebacks:
    def _all_public_text(self, w, *extra):
        texts = [(w.state / n).read_text() for n in ("incidents.jsonl", "incidents.json", "slo.json", "incidents-state.json")]
        texts += [json.dumps(incidents.export_incidents(tk(10))), json.dumps(incidents.export_slo(None, tk(10)))]
        texts += [p.read_text() for p in (w.state / "public").glob("*.json")] if (w.state / "public").exists() else []
        return "\n".join(texts + list(extra))

    def _hostile_world(self, w):
        w.queue_audit([
            (tk(1) + 10, "retention", "rm", "/home/ohmz/StudioProjects/app/deep/er/secret-file.txt?token=queryleak123", 5, "done"),
            (tk(1) + 11, "notify", "send", "disk_forecast: WARN Disk space", 0, "failed rc=1 password: s3cr3tpassw0rd AKIAABCDEFGHIJKLMNOP"),
            (tk(1) + 12, "docker_cache", "prune", "https://user:s3cr3tpassw0rd@registry.example.com/x?key=ZZZ-hidden-value", 9, "done"),
        ])
        items = [{"name": "kavita-owner@example.com", "level": "warn"}, {"path": "/home/ohmz/a/b/c/d/e/f.txt", "level": "warn"}]
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": ("crit", HOSTILE, items)})
        w.status["tasks"]["disk_forecast"]["metrics"] = {"traceback": 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: s3cr3tpassw0rd'}
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        # a second, still open incident carries the hostile text through the live overlay
        for i in range(4, 6):
            w.tick(tk(i), {"memory_health": ("warn", HOSTILE, items)})

    def test_ledger_snapshots_and_exports_are_clean(self, w):
        self._hostile_world(w)
        incidents.write_public(tk(10))
        text = self._all_public_text(w)
        for s in SECRETS:
            assert s not in text, s
        assert "Traceback" not in text and 'File "' not in text and "most recent call last" not in text
        assert "ValueError" not in text                                    # status metrics (where run_task puts the traceback) are never read
        assert "[redacted]" in text                                        # and the redaction is visible, not silent

    def test_postmortem_and_timeline_are_clean_too(self, w):
        self._hostile_world(w)
        r = w.pub(tk(5))["recent"][0]
        assert r["postmortem_md"] and not any(s in r["postmortem_md"] for s in SECRETS)
        assert not any(s in json.dumps(r["timeline"]) + json.dumps(r["related_actions"]) for s in SECRETS)
        assert "/home/ohmz/StudioProjects/app/deep" not in json.dumps(r["related_actions"]) or "..." in json.dumps(r["related_actions"])

    def test_error_text_of_the_failing_notifier_is_never_copied(self, w):
        self._hostile_world(w)
        text = self._all_public_text(w)
        assert "s3cr3tpassw0rd" not in text and "AKIAABCDEFGHIJKLMNOP" not in text

    def test_redact_function(self):
        r = incidents.redact
        assert "hunter2" not in r("password: hunter2 trailing words") and r("Username and Password not accepted") == "Username and Password not accepted"
        assert r("see https://a.b/c?x=1&y=2") == "see https://a.b/c"       # the query string never reaches the page
        assert r("user@host.example.com") == "[redacted]" and r("call 555-867-5309 now") == "call [redacted] now"
        assert "Traceback" not in r("ok\nTraceback (most recent call last):\n  File \"/x.py\", line 1\nBoom") and r("ok\nTraceback (most recent call last):\n x").startswith("ok")
        assert r("a" * 40) == "[redacted]" and r("/home/ohmz/a/b/c/d/e") == "/home/ohmz/a/b/c/..."
        assert r("/home/ohmz/a/b/c/d/e", paths=False) == "/home/ohmz/a/b/c/d/e"        # trusted playbook text keeps its paths
        assert "bearer abc" not in r("Authorization: Bearer abcdefghijkl").lower()
        assert r(None) == "" and r(5) == "5" and r("lone \ud800 surrogate") == "lone ? surrogate"
        assert "--password" in r("--password hunter2 x") and "hunter2" not in r("--password hunter2 x")

    def test_line_is_single_line_bounded_and_control_free(self):
        s = incidents._line("a\nb\tc\x00d\x1b[31m " + "zzzz " * 100, 60)
        assert len(s) <= 60 and "\n" not in s and "\x00" not in s and "\x1b" not in s and s.endswith("..")

    def test_status_summary_with_unicode_and_surrogates_does_not_break_json(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": ("warn", "warn: /media/Café 😀 \udc80 full", [])})
        json.loads((w.state / "incidents.json").read_text())
        (w.state / "incidents.jsonl").read_text()                         # valid UTF-8 on disk


# (text, the secret that must not survive it): the shapes the first redactor let through. publish.clean() covers all of them.
LEAKS = [
    ("crit: immich_postgres failing DB_PASSWORD=hunter2xyz", "hunter2xyz"),
    ("MARIADB_ROOT_PASSWORD=Sup3rS3cretRoot", "Sup3rS3cretRoot"),
    ("PLEX_TOKEN=abcd1234efgh", "abcd1234efgh"),
    ("ACCESS_TOKEN=tok_live_zz9988", "tok_live_zz9988"),
    ("SECRET_KEY=zzzzSecretKeyValue", "zzzzSecretKeyValue"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("dbPassword=camelSecret99", "camelSecret99"),
    ("X-Plex-Token: abcdEFGH12345678", "abcdEFGH12345678"),
    ("Authorization: Bearer abcdefghijkl1234567890", "abcdefghijkl1234567890"),
    ("postgres://admin:S3cr3tPw@db:5432/x", "S3cr3tPw"),
    ("mongodb+srv://u:Mongo9Pass@cluster0.example.net/db", "Mongo9Pass"),
    ("redis://:RedisPw77@cache:6379/0", "RedisPw77"),
    ("mysql -pTopSecret99 -u root", "TopSecret99"),
    ("mysqldump -u root -p TopSecret88 appdb", "TopSecret88"),
    ("Cookie: session=abc123def456; csrftoken=zzz999yyy", "zzz999yyy"),
    ("Set-Cookie: sid=Sup3rSidValue", "Sup3rSidValue"),
    ("sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "AbCdEfGhIjKlMnOp"),
    ("xoxb-" + "123456789012-abcdefghijklmnop", "abcdefghijklmnop"),
    ("ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnop"),
    ("password: my secret pass phrase here", "phrase"),
    ("--api-key hunter22xx go", "hunter22xx"),
    ("smtp login failed password: hunter2hunter2 for owner@example.com", "hunter2hunter2"),
]


class TestRedactionGaps:
    """The first redactor: `\b` before pass/token/secret never fires after an underscore (DB_PASSWORD=, PLEX_TOKEN=), the URL
    credential rule was http(s) only, and -pPASS, Cookie: and sk-ant-/xoxb- tokens were missed. It is the last gate before an
    internet-facing file, so the ledger, the snapshots and the postmortems are tested with every shape."""

    @pytest.mark.parametrize("text,secret", LEAKS)
    def test_redact_removes_it(self, text, secret):
        assert secret not in incidents.redact(text), text
        assert secret not in incidents._line(text, 400)
        assert incidents.redact(text) != text                          # (URL userinfo is dropped, the rest is replaced by [redacted])

    @pytest.mark.parametrize("text,secret", LEAKS)
    def test_the_local_layer_alone_still_removes_it(self, text, secret, monkeypatch):
        """If publish cannot be imported, layer 2 is the whole defence (and `paths=False` trusted text only ever gets layer 2)."""
        monkeypatch.setattr(incidents, "_clean_fn", False)
        assert secret not in incidents.redact(text), text
        assert secret not in incidents.redact(text, paths=False), text

    @pytest.mark.parametrize("text,secret", LEAKS)
    def test_redaction_is_idempotent(self, text, secret):
        once = incidents.redact(text)
        assert incidents.redact(once) == once

    def test_it_is_publish_clean_that_does_the_work(self, monkeypatch):
        """Delegation, not a copy: a hole closed in publish.clean() is closed here. The stub is applied per line."""
        seen = []
        monkeypatch.setattr(incidents, "_clean_fn", lambda ln, n=160: (seen.append(ln), "STUB")[1])
        assert incidents.redact("one\ntwo") == "STUB\nSTUB" and seen == ["one", "two"]

    def test_a_failing_publish_import_or_call_falls_back_to_the_local_rules(self, monkeypatch):
        monkeypatch.setattr(incidents, "_clean_fn", None)               # forces the lazy import path
        monkeypatch.setattr(incidents, "_publish_clean", lambda: None)
        assert "hunter2xyz" not in incidents.redact("DB_PASSWORD=hunter2xyz")

        def boom(*a, **k):
            raise RuntimeError("clean broke")
        monkeypatch.setattr(incidents, "_publish_clean", lambda: boom)
        assert "hunter2xyz" not in incidents.redact("DB_PASSWORD=hunter2xyz")

    def test_real_task_summaries_and_names_pass_through_unchanged(self):
        for v in SUMMARY.values():
            assert incidents.redact(v) == v, v
        for v in ("INC-20261002-001", "open-notebook-surrealdb-1", "immich-public-proxy", "Username and Password not accepted",
                  "/media/SandiskSSD 9% free (170.0 GiB), full in 12d"):
            assert incidents.redact(v) == v, v

    def test_regex_work_is_bounded_on_a_hostile_blob(self):
        """The e-mail rule was quadratic: 'a.' * 8000 took 0.6 s, so 40 KB of it took seconds. Input is cut before any regex runs."""
        for blob in ("a." * 40000, "a" * 80000, "1-" * 40000, "x@" * 40000, "/home/u/" * 10000):
            t0 = time.perf_counter()
            incidents.redact(blob)
            incidents._line(blob, 140)
            assert time.perf_counter() - t0 < 1.0, blob[:6]

    def test_line_structure_survives_for_the_markdown_postmortem(self):
        assert incidents.redact("# T\n\n- a DB_PASSWORD=hunter2xyz\n- b") == "# T\n\n- a DB_PASSWORD=[redacted]\n- b"

    def test_trusted_playbook_text_keeps_commands_and_paths(self):
        cmd = "$ ls -lS /home/ohmz/docker-container-data/open-notebook/surreal_data/mydatabase.db | head -5 && curl -s http://127.0.0.1:11434/api/ps"
        assert incidents.redact(cmd, paths=False) == cmd
        assert "hunter2xyz" not in incidents._public_pb({**incidents.playbook_for("disk_forecast"), "ask": "ask DB_PASSWORD=hunter2xyz",
                                                         "fixes": ["x postgres://u:hunter2xyz@h/d"]})["ask"]

    # ---- end to end: the three secrets of the review's scenario must reach neither the ledger FILE nor a snapshot nor a postmortem
    SUMMARY_LEAK = "crit: immich_postgres failing DB_PASSWORD=hunter2 PLEX_TOKEN=abcd1234efgh postgres://admin:S3cr3t@db:5432/x"

    def _run(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": ("crit", self.SUMMARY_LEAK, [{"mount": "/media/x-PLEX_TOKEN=abcd1234efgh", "level": "crit"}])})
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})

    def _texts(self, w):
        incidents.write_public(tk(5))
        files = [(w.state / n).read_text() for n in ("incidents.jsonl", "incidents.json", "slo.json", "incidents-state.json")]
        files += [p.read_text() for p in (w.state / "public").glob("*.json")]
        return "\n".join(files + [json.dumps(incidents.export_incidents(tk(5)))])

    def test_secrets_in_a_task_summary_reach_no_file(self, w):
        self._run(w)
        text = self._texts(w)
        for secret in ("hunter2", "abcd1234efgh", "S3cr3t"):
            assert secret not in text, secret
        assert w.pub(tk(5))["recent"][0]["postmortem_md"]

    def test_the_ledger_file_itself_is_redacted_at_ingest(self, w):
        """Not only the exports: the folded record was always clean, the line WRITTEN to incidents.jsonl was not."""
        self._run(w)
        lines = (w.state / "incidents.jsonl").read_text().splitlines()
        assert lines and all("hunter2" not in ln and "abcd1234efgh" not in ln and "S3cr3t" not in ln for ln in lines)
        open_ev = next(json.loads(ln) for ln in lines if '"ev":"open"' in ln)
        assert "[redacted]" in open_ev["summary"] and "[redacted]" in open_ev["text"]
        assert all("hunter2" not in x and "abcd1234efgh" not in x for x in open_ev["entities"])
        res = next(json.loads(ln) for ln in lines if '"ev":"resolve"' in ln)
        assert "hunter2" not in res["postmortem_md"] and "S3cr3t" not in res["postmortem_md"]

    def test_a_ledger_written_by_older_code_is_still_redacted_when_exported(self, w):
        """Lines already on disk with raw text (written before this fix) must not reach the website on the next export."""
        with open(w.state / "incidents.jsonl", "w") as f:
            f.write(json.dumps({"id": "INC-20261002-001", "ev": "open", "ts": tk(1), "task": "disk_forecast", "title": "Disk space",
                                "level": 2, "severity": "sev2", "word": "crit", "started_at": tk(0), "summary": self.SUMMARY_LEAK,
                                "entities": ["PLEX_TOKEN=abcd1234efgh"], "text": "Confirmed crit: " + self.SUMMARY_LEAK}) + "\n")
        text = json.dumps(incidents.export_incidents(tk(2)))
        assert "hunter2" not in text and "abcd1234efgh" not in text and "S3cr3t" not in text and "[redacted]" in text

    def test_audit_text_in_related_actions_is_redacted_too(self, w):
        w.queue_audit([(tk(1) + 10, "docker_cache", "prune", "postgres://u:S3cr3tPw@h/db?DB_PASSWORD=hunter2xyz mysql -pTopSecret99", 5, "done")])
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        text = self._texts(w)
        for secret in ("S3cr3tPw", "hunter2xyz", "TopSecret99"):
            assert secret not in text, secret


# ====================================================================================================== playbooks file
def registered_tasks():
    """Every @task("name") / @register_task("name") / core.task("name") in the package (also a task a module registers from a function,
    e.g. registry.register_tasks), plus the per-cadence f"routine_verify_{cadence}" expansion; found statically so a module that fails
    to import cannot hide a task."""
    names = set()
    for p in (ROOT / "homelab_maint").rglob("*.py"):
        txt = p.read_text()
        names |= set(re.findall(r'^\s*(?:@task|@register_task|core\.task|register_task)\(\s*"([a-z0-9_]+)"', txt, re.M))
        if m := re.search(r'^CADENCES = \(([^)]*)\)', txt, re.M):
            for pre in re.findall(r'register_task\(f"([a-z0-9_]+)\{cadence\}"', txt):
                names |= {pre + c for c in re.findall(r'"(\w+)"', m[1])}
    return names


BASELINE = ROOT / "homelab_maint" / "data" / "playbooks.toml"                  # the shipped playbooks live INSIDE the package
PB = tomllib.loads(BASELINE.read_text())
PLAYBOOKS = {k: v for k, v in PB["playbook"].items() if not k.startswith("_")}
READONLY_BIN = {"homelab-maint", "df", "docker", "journalctl", "systemctl", "ls", "python3", "cat", "tail", "head", "grep", "findmnt",
                "lsblk", "du", "free", "vmstat", "ps", "curl", "nvidia-smi", "stat", "snap", "sensors", "pgrep", "pstree", "git",
                "lsof", "find", "crontab", "tune2fs", "smartctl", "dmesg", "sort", "wc", "awk", "tr", "uniq", "PYTHONPATH=/usr/local/lib/homelab-maint",
                "sudo", "nice", "ionice", "echo", "cut"}
MUTATING = re.compile(r"\b(rm|mv|cp|kill|pkill|killall|restart|start|stop|reload|enable|disable|mask|reset-failed|prune|remove|rmi|rm -f|"
                      r"update|edit|chmod|chown|umount|mount|truncate|tee|dd|mkfs|fsck|badblocks|apt|apt-get|dpkg|install|reboot|poweroff|"
                      r"pause|resume|approve|unload|free|-X|--data|-d|-T|-F|--upload-file|--delete|-delete|-exec|-fprint|--output|>)\b")
DOCKER_READ = {"ps", "inspect", "logs", "top", "stats", "info", "images", "image", "system", "buildx", "container", "compose"}
SYSTEMCTL_READ = {"status", "show", "list-timers", "--failed", "list-units", "is-active", "is-enabled", "cat"}
# `run` and `gate` are NOT read-only, not even with --dry-run: a run appends a history sample, feeds the pager's debounce (it can open an
# incident, page, or close one) and rewrites tier_runs; a gate rewrites gates.json and exits 0 after 12 h of deferral even while busy.
HOMELAB_READ = {"status", "plan", "doctor"}
ROUTINE_READ = {"status", "plan", "due", "check"}                  # `homelab-maint routine run|ack|note|clear-halt|canary-reset` all write
SNAP_READ = {"list", "changes", "get", "services", "refresh"}


def split_command(line):
    """Pipeline segments of a `$ ` line with their words (quotes respected)."""
    import shlex
    body = line[2:]
    segs, cur = [], []
    for tok in shlex.split(body, posix=True):
        if tok in ("|", "&&", ";"):
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    return [s for s in segs if s]


WRAPPERS = ("sudo", "nice", "ionice", "-n", "-c3", "19", "PYTHONPATH=/usr/local/lib/homelab-maint")


def operands(words, with_value=()):
    """The non-option words of a command line (`with_value` options swallow the word after them)."""
    ops, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w in with_value:
            skip = True
        elif not w.startswith("-"):
            ops.append(w)
    return ops


def lint_check_command(c, name=""):
    """Raise AssertionError unless the `$ ` line only READS: every pipeline segment starts with a known read-only program, and the
    sub-command / flags of the programs that can also write (docker, systemctl, curl, find, snap, homelab-maint, ...) are read-only."""
    assert c.startswith("$ "), c
    for seg in split_command(c):
        words = list(seg)
        while words and words[0] in WRAPPERS:
            words.pop(0)
        assert words, c
        b, rest = words[0], words[1:]
        assert b in READONLY_BIN, f"{name}: {c!r}: {b} is not on the read-only list"
        if b == "docker":
            sub = next((w for w in rest if not w.startswith("-")), "")
            assert sub in DOCKER_READ, f"{name}: {c!r}"
            assert not (sub in ("system", "buildx", "builder") and "prune" in rest), c
            assert not (sub == "image" and set(rest) & {"rm", "prune", "pull", "load"}), c
            assert not (sub == "container" and set(rest) & {"rm", "prune", "stop", "start", "restart", "kill", "update"}), c
        elif b == "systemctl":
            assert next((w for w in rest if not w.startswith("-") or w == "--failed"), "") in SYSTEMCTL_READ, c
        elif b == "homelab-maint":
            assert rest and (rest[0] in HOMELAB_READ or rest[:1] == ["routine"] and rest[1:2] and rest[1] in ROUTINE_READ
                             or rest[:1] == ["swap"] and rest[1:2] in ([], ["status"], ["relieve"]) and "--apply" not in rest), \
                f"{name}: {c!r}: only `homelab-maint status|plan|doctor|routine status|plan|due|check|swap [status|relieve]` (never --apply) read without recording anything"
        elif b == "snap":
            assert rest and rest[0] in SNAP_READ and (rest[0] != "refresh" or rest[1:] == ["--time"]), c
        elif b == "curl":
            assert not (set(rest) & {"-X", "--request", "-d", "--data", "--data-raw", "-T", "-F", "--form", "--upload-file", "-K"}), c
        elif b == "find":
            assert not (set(rest) & {"-delete", "-exec", "-execdir", "-ok", "-fprint"}), c
        elif b == "crontab":
            assert rest[:1] == ["-l"], c
        elif b == "python3":
            assert rest[:1] in (["-m"], ["-c"]) and ("json.tool" in c or "homelab_maint" in c or "json.load" in c), c
        elif b == "git":
            assert "status" in rest or "log" in rest, c
            assert not (set(rest) & {"reset", "checkout", "clean", "push", "commit", "rm", "stash", "rebase", "merge"}), c
        elif b == "smartctl":
            assert not (set(rest) & {"-t", "-X", "--abort", "-s", "-S"}), c
        elif b == "tune2fs":
            assert rest[:1] == ["-l"], c
        elif b == "findmnt":
            # `findmnt A B` means "source A mounted on target B", not "both": two bare operands match nothing (found by running the
            # shipped commands on this host). Several paths need several lines, or `-T` per path.
            assert len(operands(rest, {"-o", "-T", "-t", "-S", "-M", "-O", "-x", "-N", "-w"})) <= 1, f"{name}: {c!r}: findmnt takes ONE path"
        elif b in ("tail", "head"):
            # The obsolete `-3` form is rejected by tail/head when there are several files (a glob usually expands to several):
            # "tail: option used in invalid context -- 3". `-n 3` always works.
            ops = operands(rest, {"-n", "-c"})
            assert not (any(re.fullmatch(r"-\d+", w) for w in rest) and (len(ops) > 1 or any("*" in o or "?" in o for o in ops))), \
                f"{name}: {c!r}: use -n N, not -N, with several files or a glob"
    # (a program that is not on the list was already rejected above, wherever it sits in the pipeline: `| xargs rm`, `| tee f`, ...)
    assert ">" not in c.replace("2>/dev/null", "").replace("> /dev/null", ""), f"{name}: {c!r}: redirection writes a file"


# (task, wording the task really writes into Result.summary): taken from the check's code, not invented
HINT_CASES = [
    ("disk_forecast", "warn: /media/SandiskSSD 9% free (170.0 GiB), full in 12d"),
    ("disk_forecast", "crit: / 4% free (70.0 GiB); /mnt/backup/system 7% free (400.0 GiB)"),
    ("disk_forecast", "warn: /media/Immich 8% free (100.0 GiB)"),
    ("failed_units", "warn: 2 failed unit(s): nginx.service, plexmediaserver.service"),
    ("failed_units", "warn: 1 unexpected exited: foo; 2 unhealthy: a, b; 1 restarting: c"),
    ("backup_freshness", "crit: backup-system FAILED (1d3h ago)"),
    ("backup_freshness", "warn: stack-backup 27h old (limit 26h)"),
    ("backup_freshness", "warn: stack-backup MISSING (? ago)"),
    ("backup_freshness", "warn: backup-immich UNREADABLE (? ago)"),
    ("memory_health", "warn: memory stall 6.1%, 9.0 GiB available, swap-in 3000 pages/s, 1 OOM kill(s) in last 6h (x)"),
    ("plex_media_mount_check", "crit: Media dir is not mounted (no bind mount); Plex would regenerate Media on the root disk"),
    ("plex_media_mount_check", "crit: Plex Media mounted from /dev/sdb1, expected under /media/SandiskSSD/plex"),
    ("smart_trend", "SMART: sdf CRC +3; sdc realloc +4, pending +2; sdb offline-unc +1; nvme0 72C"),
    ("alert_path_health", "alert path: bridge missing; smartd hook broken; 2 smart hook sends failed rc=1: smtp error"),
    ("growth_watch", "growth over limit: config/logs 1.2 GiB/d, docker/containers 0.9 GiB/d; 1/4 paths unmeasured (missing/no access/cut off)"),
    ("growth_watch", "growth blind >12h: 1/4 paths unmeasured (missing/no access/cut off): log/journal"),
    ("probes", "2 down: Plex, Immich; 1 flapping: Kavita; 1 bad probe defs; 20/25 up"),
    ("surrealdb_health", "SurrealDB: WAL is 2048 MB; container hit its memory cap 1x; container has restarted 5x; only 12 GB free on the store's filesystem"),
    ("comfyui_idle_reclaim", "restart of comfyui failed: rc=1"),
    ("comfyui_idle_reclaim", "report: would restart comfyui (idle 2 checks, 5200 MB VRAM)"),
    ("immich_recycle", "immich_server restarted but is exited"),
    ("immich_recycle", "recycle deferred: busy: immich jobs active"),
    ("os_jobs", "OS jobs: 3 of 13 need attention: logrotate failed (last run failed: exit-code); fstrim overdue; fwupd-refresh inactive"),
]


class TestPlaybooks:
    def test_every_registered_task_has_its_own_playbook(self):
        missing = sorted(registered_tasks() - set(PLAYBOOKS))
        assert not missing, f"add [playbook.X] to homelab_maint/data/playbooks.toml for: {missing}"
        assert len(registered_tasks()) >= 37

    def test_no_playbook_for_a_task_that_does_not_exist(self):
        assert not sorted(set(PLAYBOOKS) - registered_tasks()), "stale playbook(s): rename or delete them"

    @pytest.mark.parametrize("name", sorted(PLAYBOOKS))
    def test_playbook_is_complete(self, name):
        p = PLAYBOOKS[name]
        for k in ("title", "class", "meaning", "impact", "ask"):
            assert isinstance(p.get(k), str) and p[k].strip(), f"{name}.{k}"
        assert p["class"] in ("P0", "P1", "P2", "P3")
        assert len(p["meaning"]) >= 60 and len(p["impact"]) >= 25
        for k in ("checks", "fixes", "avoid"):
            assert isinstance(p.get(k), list) and p[k] and all(isinstance(x, str) and x.strip() for x in p[k]), f"{name}.{k}"
        assert any(c.startswith("$ ") for c in p["checks"]), f"{name}: no command in checks"
        assert len(p["avoid"]) >= 1 and len(p["fixes"]) >= 1
        assert isinstance(p.get("followups"), list) and p["followups"]
        assert set(p.get("related_tasks", [])) <= registered_tasks(), f"{name}.related_tasks"
        assert name not in p.get("related_tasks", [])
        seen = set()
        for h in p.get("hint", []):
            assert set(h) == {"match", "text"} and h["text"].strip()
            re.compile(h["match"])
            assert h["match"] not in seen
            seen.add(h["match"])

    @pytest.mark.parametrize("name", sorted(PLAYBOOKS))
    def test_check_commands_are_read_only(self, name):
        for c in (x for x in PLAYBOOKS[name]["checks"] if x.startswith("$ ")):
            lint_check_command(c, name)

    def test_the_lint_accepts_reads_and_rejects_every_kind_of_write(self):
        for good in ("$ docker ps --format '{{.Names}}'", "$ systemctl status x.service --no-pager", "$ systemctl --failed --no-pager",
                     "$ curl -s http://127.0.0.1:8188/queue", "$ sudo nice -n 19 ionice -c3 du -xh --max-depth=1 / | sort -h | tail",
                     "$ homelab-maint status | grep disk_forecast", "$ homelab-maint plan c2_candidates", "$ homelab-maint routine plan", "$ homelab-maint routine status", "$ snap refresh --time", "$ sudo tune2fs -l /dev/nvme0n1p2 | grep -i reserved",
                     "$ docker buildx du --builder b | tail -4", "$ PYTHONPATH=/usr/local/lib/homelab-maint python3 -m homelab_maint.probes list",
                     "$ findmnt -T /mnt/backup/system", "$ findmnt -o TARGET,SOURCE | grep backup", "$ tail -n 3 /var/lib/smartmontools/attrlog.*.csv",
                     "$ tail -20 /var/log/smart-alert.log", "$ ls -t /var/log | head -5", "$ homelab-maint swap", "$ homelab-maint swap relieve"):
            lint_check_command(good, "self-test")
        for bad in ("$ docker restart x", "$ docker system prune -a", "$ docker buildx prune -f", "$ docker image rm x", "$ docker container rm x",
                    "$ systemctl restart docker", "$ systemctl stop x", "$ rm -rf /tmp/x", "$ curl -X POST http://127.0.0.1:8188/free",
                    "$ curl -d x http://h", "$ homelab-maint run --task x", "$ homelab-maint run --task x --dry-run",
                    "$ homelab-maint gate ollama; echo rc=$?", "$ homelab-maint gate immich-recycle", "$ homelab-maint approve c2_candidates abc", "$ homelab-maint pause", "$ sudo homelab-maint swap relieve --apply", "$ homelab-maint swap relieve --apply", "$ homelab-maint swap bogus",
                    "$ homelab-maint routine run --apply", "$ homelab-maint routine clear-halt", "$ homelab-maint routine ack restore_drill x", "$ homelab-maint routine",
                    "$ sudo snap remove foo", "$ snap refresh plexmediaserver", "$ find / -delete", "$ find / -exec rm {} ;",
                    "$ sudo smartctl -t short /dev/sda", "$ sudo tune2fs -m 1 /dev/sda", "$ crontab -r", "$ git -C x reset --hard", "$ kill 5",
                    "$ python3 setup.py install", "$ sudo apt-get clean", "$ echo x | tee /etc/foo",
                    "$ findmnt /mnt/backup/system /mnt/backup/immich", "$ tail -3 /var/lib/smartmontools/attrlog.*.csv", "$ tail -3 /a /b",
                    "$ head -5 /var/log/*.log"):
            with pytest.raises(AssertionError):
                lint_check_command(bad, "self-test")

    @pytest.mark.parametrize("name", sorted(PLAYBOOKS) + ["_default"])
    def test_playbook_text_holds_no_secret_and_is_stable_under_redaction(self, name):
        p = PB["playbook"][name]
        strings = [p.get(k, "") for k in ("title", "meaning", "impact", "ask")] + p.get("fixes", []) + p.get("avoid", []) + p.get("followups", []) \
            + [h["text"] for h in p.get("hint", [])] + list(p.get("checks", []))        # commands are exported too
        for s in strings:
            assert incidents.redact(s, paths=False) == s, f"{name}: redaction would change: {s[:90]!r}"
            assert not re.search(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]", s), s
        for c in p.get("checks", []):
            assert not re.search(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S", c), c
            assert "alert_transports.env" not in c and ".hermes" not in c                      # never point at the credential file

    def test_the_credential_database_commands_never_print_container_config(self):
        for name in ("surrealdb_health",):
            for c in PLAYBOOKS[name]["checks"]:
                if "docker inspect" in c:
                    assert " -f " in c and "Config" not in c and "Env" not in c, c
        assert "password" in " ".join(PLAYBOOKS["surrealdb_health"]["avoid"]).lower()

    def test_ask_and_avoid_use_the_real_names_on_this_host(self):
        text = PB["playbook"]["failed_units"]["avoid"][0] + PB["playbook"]["surrealdb_health"]["meaning"] + PB["playbook"]["immich_recycle"]["fixes"][0]
        for n in ("immich_postgres", "open-notebook-surrealdb-1", "immich_redis"):
            assert n in text

    def test_exported_playbook_keeps_its_paths_and_commands_intact(self, day):
        pb = incidents._public_pb(incidents.playbook_for("surrealdb_health"))
        assert "$ ls -lS /home/ohmz/docker-container-data/open-notebook/surreal_data/mydatabase.db | head -5" in pb["checks"]
        assert pb["checks"] == incidents.playbook_for("surrealdb_health")["checks"]
        for name in PLAYBOOKS:
            full = incidents.playbook_for(name)
            exp = incidents._public_pb(full)
            for k in ("checks", "fixes", "avoid"):
                assert exp[k] == full[k], (name, k)

    def test_playbook_for_known_task(self):
        pb = incidents.playbook_for("disk_forecast")
        assert pb["title"] == "Disk space" and pb["class"] == "P0" and "docker_cache" in pb["related_tasks"]
        assert pb["checks"][0].startswith("$ df -h") and len(pb["hint"]) == 5

    def test_playbook_for_unknown_task_falls_back_to_the_generic_one(self):
        pb = incidents.playbook_for("brand_new_check")
        assert pb["title"] == "Check failed" and pb["class"] == "P2" and pb["hint"] == []
        assert "$ homelab-maint status | grep brand_new_check" in pb["checks"]
        assert not any("TASK_NAME" in x for x in pb["checks"] + pb["fixes"])
        assert "homelab_maint/data/playbooks.toml" in " ".join(pb["followups"])

    def test_override_file_merges_per_playbook_and_per_key(self, w):
        (w.conf / "playbooks.toml").write_text('[playbook.disk_forecast]\nask = "Call the plumber."\n[playbook.custom_check]\ntitle = "Mine"\nmeaning = "m"\nchecks = ["$ true"]\n'
                                               '[incidents]\nrecent_max = 1\n')
        pb = incidents.playbook_for("disk_forecast")
        assert pb["ask"] == "Call the plumber." and pb["meaning"].startswith("A watched filesystem")      # other keys kept
        assert incidents.playbook_for("custom_check")["title"] == "Mine"
        assert incidents.playbook_for("failed_units")["title"] == "Services & containers"
        assert incidents.load_config()["incidents"]["recent_max"] == 1 and incidents.load_config()["incidents"]["group_window_s"] == 600

    def test_override_slo_is_merged_by_name_not_replacing_the_shipped_list(self, w):
        shipped = {o["name"]: o for o in incidents.load_config()["slo"]}
        (w.conf / "playbooks.toml").write_text('[[slo]]\nname = "Only"\ntarget_pct = 95\nchecks = ["x"]\n'
                                               '[[slo]]\nname = "Backups fresh"\ntarget_pct = 97.5\n'
                                               '[[slo]]\nname = "Services up"\nenabled = false\n')
        got = {o["name"]: o for o in incidents.load_config()["slo"]}
        assert got["Only"]["checks"] == ["x"]                                                # added
        assert got["Backups fresh"]["target_pct"] == 97.5 and got["Backups fresh"]["checks"] == shipped["Backups fresh"]["checks"]   # per key
        assert "Services up" not in got and set(got) == (set(shipped) - {"Services up"}) | {"Only"}                             # dropped
        assert got["Platform mounts"] == shipped["Platform mounts"]                          # everything else untouched

    def test_every_hint_in_the_file_fires_on_the_real_wording_of_its_task(self):
        """A hint whose regex never matches what its task really writes is dead weight (the first draft's `kavita` hint was: the check
        shortens the path to config/logs). Every shipped hint must be exercised by at least one real-wording case below."""
        fired = {(t, h["match"]) for t, summary in HINT_CASES for h in PLAYBOOKS[t].get("hint", []) if re.search(h["match"], summary)}
        every = {(t, h["match"]) for t, p in PLAYBOOKS.items() for h in p.get("hint", [])}
        assert not every - fired, f"hints no real-wording case triggers: {sorted(every - fired)}"

    def test_cause_hint_uses_the_matching_texts_and_at_most_three(self):
        cfg = incidents.load_config()
        inc = {"task": "memory_health", "id": "INC-x-001", "started_at": 1.0, "word": "warn"}
        summary = "warn: memory stall 6.1%, 9.0 GiB available, swap-in 3000 pages/s, 1 OOM kill(s) in last 6h (x)"
        hint = incidents._cause_hint(inc, incidents._Ledger([]), cfg, summary)
        texts = [h["text"] for h in cfg["playbook"]["memory_health"]["hint"] if re.search(h["match"], summary)]
        assert len(texts) == 4 and all(t[:30] in hint for t in texts[:3]) and texts[3][:30] not in hint
        assert incidents._cause_hint({"task": "failed_units", "id": "x", "started_at": 1.0, "word": "warn"}, incidents._Ledger([]), cfg,
                                     "warn: 2 failed unit(s): nginx.service, plexmediaserver.service").startswith("nginx.service has been failing")

    def test_format_playbook_is_a_complete_runbook(self):
        txt = incidents.format_playbook("backup_freshness")
        for frag in ("Backups (backup_freshness), service class P2", "What it means:", "Impact:", "First checks:", "Safe fixes:", "Do NOT:",
                     "Who or what to ask:", "  $ ls -lt /var/log/backup | head"):
            assert frag in txt
        assert "umount -l" in txt                                         # the avoid list is part of it


# ====================================================================================================== review fixes: commands that record
def all_strings(node):
    """Every string in a parsed TOML document, at any depth."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from all_strings(v)


class TestPlaybookCommandsDoNotRecord:
    """`homelab-maint run --task X --dry-run` on a check task still appends a history sample, runs Notifier.evaluate (it can page) and
    overwrites tier_runs; `homelab-maint gate NAME` rewrites gates.json and exits 0 after 12 h of deferral EVEN WHILE BUSY. An operator
    following a playbook would bypass the pager's debounce (one blip plus one manual run opens an incident and sends an SMS; two manual
    runs close a flapping one) and feed the SLO a sample per run. The checks therefore only use `status | grep` and the real probes."""

    RECORDING = re.compile(r"\$ (?:sudo )?homelab-maint (?:run|gate|approve|pause|resume)\b")

    def test_no_check_line_of_any_playbook_runs_or_gates(self):
        bad = [(n, c) for n, p in PB["playbook"].items() for c in p.get("checks", []) if self.RECORDING.match(c)]
        assert not bad, bad

    def test_the_lint_itself_rejects_run_and_gate_with_any_flags(self):
        for cmd in ("$ homelab-maint run --task disk_forecast --dry-run", "$ homelab-maint run --tier check --dry-run",
                    "$ homelab-maint gate immich-recycle", "$ homelab-maint gate ollama; echo rc=$?", "$ sudo homelab-maint gate comfyui"):
            with pytest.raises(AssertionError):
                lint_check_command(cmd, "t")
        for cmd in ("$ homelab-maint status", "$ homelab-maint status | grep -E 'disk_forecast|docker_df'", "$ homelab-maint plan c2_candidates"):
            lint_check_command(cmd, "t")

    def test_no_text_anywhere_advises_a_dry_run_or_an_exit_code_as_the_idle_test(self):
        for sx in all_strings(PB):
            assert "--dry-run" not in sx, sx
            assert "rc=$?" not in sx and "echo rc" not in sx, sx
            assert not re.search(r"homelab-maint gate \S+ (?:says|prints|exits)", sx), sx

    def test_every_playbook_that_used_run_or_gate_still_has_a_read_only_substitute(self):
        """Replaced, not deleted: the substitute of `run --task X --dry-run` is the last result of X, and of a gate the real probe."""
        for name in ("docker_df", "spike_sampler", "stuck_detector", "image_ledger", "docker_images", "retention", "qos_classes",
                     "openwebui_media_prune", "docker_containers_prune", "docker_prune_parity", "report_daily", "report_weekly"):
            assert any(c.startswith("$ homelab-maint status | grep") for c in PB["playbook"][name]["checks"]), name
        assert any("api/ps" in c or "journalctl -u ollama" in c for c in PB["playbook"]["bulkhead_check"]["checks"])
        assert any("/queue" in c for c in PB["playbook"]["comfyui_idle_reclaim"]["checks"])
        im = PB["playbook"]["immich_recycle"]["checks"]
        assert any("docker stats" in c and "immich" in c for c in im) and any("is-active backup-system.service" in c for c in im)

    def test_the_generic_playbook_is_what_every_alert_without_a_playbook_shows(self):
        """notify.playbook_lines puts the first three checks into the page: the generic ones are shown at EVERY unknown alert."""
        pb = incidents.playbook_for("brand_new_check")
        for c in pb["checks"][:3]:
            lint_check_command(c, "_default")
        assert pb["checks"][0] == "$ homelab-maint status | grep brand_new_check"
        assert pb["checks"][1].endswith("-m homelab_maint.incidents list")

    def test_the_gate_advice_no_longer_trusts_the_exit_code(self):
        fix = " ".join(PB["playbook"]["backup_freshness"]["fixes"])
        assert "exit code" in fix and "12 hours" in fix and "docker stats" in fix

    def test_the_commands_that_replaced_them_parse_and_exist(self):
        """The substitutes are real commands of THIS tool: `incidents list` prints the open incidents without touching anything."""
        assert incidents.main(["list"]) == 0


# ====================================================================================================== review fixes: clock steps
YEAR = 365 * 86400


class TestClockSkew:
    """Observations were consumed by `t > cursor` on the wall clock. A cursor stamped in the future (RTC ahead, then an NTP step back)
    made every later observation look old: the check could be genuinely failing for hours with no incident, and an open one never
    saw its healthy samples. The Notifier has no such cursor and kept paging."""

    def test_rtc_a_year_ahead_then_corrected_does_not_freeze_tracking(self, w):
        for i in range(4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        for i in (4, 5):                                           # bad RTC: the clock, and so `now`, is a year ahead (consumed normally)
            w.tick(tk(i) + YEAR, {"disk_forecast": "ok"}, now=tk(i) + YEAR + 5)
        assert json.loads((w.state / "incidents-state.json").read_text())["tasks"]["disk_forecast"]["cursor"] > tk(5) + YEAR - 1
        outs = [w.tick(tk(i), {"disk_forecast": "warn"}) for i in range(6, 12)]       # corrected: and the check is genuinely failing
        pub = w.pub(tk(11) + 5)
        assert len(pub["open"]) == 1, "tracking was frozen by the future cursor"
        assert pub["open"][0]["since"] == tk(6) and pub["open"][0]["detected_at"] == tk(7)
        assert [len(o["opened"]) for o in outs] == [0, 1, 0, 0, 0, 0]                  # the incident opens live, on the second confirming run
        assert "disk_forecast" in outs[0]["clamped"]
        assert json.loads((w.state / "incidents-state.json").read_text())["tasks"]["disk_forecast"]["cursor"] <= tk(11) + 5

    def test_plus_four_hours_error_does_not_freeze_for_four_hours(self, w):
        h4 = 4 * 3600
        for i in range(2):
            w.tick(tk(i) + h4, {"disk_forecast": "warn"}, now=tk(i) + h4 + 5)        # the clock ran 4 h ahead ...
        assert len(w.pub(tk(1) + h4 + 5)["open"]) == 1
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})                                   # ... then NTP stepped it back: healthy samples
        pub = w.pub(tk(3) + 5)
        assert pub["open"] == [] and len(pub["recent"]) == 1, "the open incident never saw its healthy samples"
        r = pub["recent"][0]
        assert r["mttr_s"] >= 0 and r["duration_s"] >= 0                             # stamped by the bad clock: never negative

    def test_the_same_with_the_state_file_lost(self, w):
        """After a lost state file the floor is the newest ledger event: a future one must not become the floor."""
        h4 = 4 * 3600
        for i in range(2):
            w.tick(tk(i) + h4, {"disk_forecast": "warn"}, now=tk(i) + h4 + 5)
        (w.state / "incidents-state.json").unlink()
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        assert w.pub(tk(3) + 5)["open"] == []

    def test_the_future_ledger_event_does_not_pin_the_watermark(self, w):
        """State older than the ledger resets a tracker to the watermark; a future watermark would freeze it again."""
        h4 = 4 * 3600
        for i in range(2):
            w.tick(tk(i) + h4, {"disk_forecast": "warn"}, now=tk(i) + h4 + 5)
        (w.state / "incidents-state.json").write_text(json.dumps({"v": 1, "tasks": {"disk_forecast": {"cursor": tk(0)}}, "live": {}}))
        for i in range(2, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        assert w.pub(tk(3) + 5)["open"] == []

    def test_a_future_stamped_record_is_ignored_and_reported(self, w):
        for i in range(4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        for k in (1, 2):                                           # two junk 'warn' samples a year ahead would confirm a bogus incident
            w.hist(tk(3) + YEAR + k * STEP, "disk_forecast", "warn")
        out = incidents.update(w.status, None, tk(3) + 10)
        assert out["ok"] and out["future"] == {"disk_forecast": 2} and w.pub(tk(3) + 10)["open"] == []
        st = json.loads((w.state / "incidents-state.json").read_text())
        assert st["tasks"]["disk_forecast"]["cursor"] == tk(3) and st["clock"]["future"] == {"disk_forecast": 2}
        for i in range(4, 7):                                      # the real clock goes on: tracking is intact
            w.tick(tk(i), {"disk_forecast": "warn" if i >= 5 else "ok"})
        assert len(w.pub(tk(6) + 5)["open"]) == 1

    def test_a_future_stamped_status_entry_is_ignored_too(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "ok"})
        w.entry(tk(2) + YEAR, "disk_forecast", "crit")             # status.json from the bad-clock era, not yet overwritten
        out = incidents.update(w.status, None, tk(2) + 5)
        assert out["future"] == {"disk_forecast": 1} and w.pub(tk(2) + 5)["open"] == []

    def test_samples_within_the_slack_are_still_trusted(self, w):
        """A few minutes of clock skew is not a jump: the sample counts."""
        w.tick(tk(0), {"disk_forecast": "warn"}, now=tk(0) - 600)
        out = w.tick(tk(1), {"disk_forecast": "warn"}, now=tk(1) - 600)
        assert out["future"] == {} and len(out["opened"]) == 1

    def test_an_observation_is_never_consumed_twice_after_a_step_back(self, w):
        """The cursor moves behind `now`; records between it and `now` that were consumed already must not be counted again. One run
        of 'warn' (consumed) re-read next to ONE new 'warn' would confirm a 2-run debounce and open a bogus incident."""
        w.tick(tk(0), {"disk_forecast": "warn"})                                      # ONE failing run, consumed (streak 1)
        for i in range(1, 4):
            w.tick(tk(i), {"disk_forecast": "ok"})
        now = tk(0) - 450                                                             # the clock steps back by 1 h 15 min
        out = w.tick(now - 10, {"disk_forecast": "warn"}, now=now)                    # ONE real new failing run
        assert out["ok"] and out["future"] == {"disk_forecast": 3} and "disk_forecast" in out["clamped"]
        assert w.pub(now)["open"] == [] and w.pub(now)["stats"]["incidents_30d"] == 0
        assert incidents.update(w.status, None, now + 1)["opened"] == []              # and the same inputs again change nothing

    def test_the_open_incident_says_so_once_and_list_shows_a_clock_note(self, w, capsys):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})
        for k in range(3):
            w.hist(tk(2) + YEAR + k * STEP, "disk_forecast", "warn")
            incidents.update(w.status, None, tk(2) + 20 + k)
        notes = [e for e in w.events() if e["ev"] == "note"]
        assert len(notes) == 1 and "ahead of the clock" in notes[0]["text"]
        assert any(x["kind"] == "note" for x in w.pub(tk(2) + 30)["open"][0]["timeline"])
        w.mp.setattr(incidents, "time", types.SimpleNamespace(**{**vars(time), "time": lambda: tk(2) + 60}))     # `list` shows the note for 24 h of the REAL clock: freeze it
        assert incidents.main(["list"]) == 0
        assert "NOTE: clock trouble" in capsys.readouterr().out

    def test_future_samples_do_not_count_as_slo_slots(self, w):
        (w.conf / "playbooks.toml").write_text('[[slo]]\nname = "T"\ntarget_pct = 99\nchecks = ["a"]\n')
        now = 2000 * STEP * 30
        hist = [{"t": now - 5, "kind": "task", "task": "a", "status": "ok"}] + \
               [{"t": now + YEAR + k * STEP, "kind": "task", "task": "a", "status": "crit"} for k in range(50)]
        o = next(x for x in incidents.export_slo(hist, now)["objectives"] if x["name"] == "T")
        assert o["samples"] == 1 and o["bad_minutes"] == 0


# ====================================================================================================== review fixes: replay is not news
class TestReplayIsNotNews:
    """update() returned every transition it folded, including those replayed from 31 days of history on a first install: wiring the
    returned ids to incident_open / incident_resolved notifications would page about yesterday (5 pages against a daily budget of 8)."""

    KINDS = ("opened", "escalated", "improved", "resolved")

    def _history(self, w, seq, start=0, task="disk_forecast"):
        for i, st in enumerate(seq):
            w.hist(tk(start + i), task, st)

    def test_first_install_replay_reports_nothing_as_live(self, w):
        self._history(w, ["warn", "warn", "ok", "ok", "warn", "warn", "crit", "crit", "ok", "ok"])        # all of it yesterday
        out = incidents.update({"tasks": {}}, None, tk(10) + 86400)
        assert out["ok"]
        for k in self.KINDS:
            assert out[k] == [], k
        assert len(out["backfilled"]["opened"]) == 2 and len(out["backfilled"]["resolved"]) == 2
        assert len(out["backfilled"]["escalated"]) == 1 and out["backfilled"]["improved"] == []
        assert len(w.pub(tk(10) + 86400)["recent"]) == 2                                                  # the history itself is still recorded

    def test_the_real_data_shape_five_pages_would_have_gone_out(self, w):
        """Three incidents opened and two resolved yesterday (the review's replay of this host's history): none may be live."""
        for k, task in enumerate(("disk_forecast", "memory_health", "failed_units")):
            self._history(w, ["warn", "warn"] + (["ok", "ok"] if k < 2 else []), start=10 * k, task=task)
        out = incidents.update({"tasks": {}}, None, tk(40) + 86400)
        assert total(out, "opened") == 3 and total(out, "resolved") == 2
        assert out["opened"] == [] and out["resolved"] == []

    def test_a_recovery_of_an_incident_whose_opening_was_backfilled_is_history_too(self, w):
        """The real host: opened 40 min ago (never announced), recovered on the very last run. Announcing 'resolved' for it would be a page
        about something the owner never heard of; in steady state (opened live, resolved a day later) the resolve IS live."""
        self._history(w, ["ok", "warn", "warn", "ok", "ok"])                           # open at tk(2), first healthy run tk(3), confirmed tk(4)
        now = tk(4) + 5
        out = incidents.update({"tasks": {}}, None, now)
        assert incidents.LIVE_S < now - tk(2)                                         # the opening is too old to be news ...
        assert tk(4) >= now - incidents.LIVE_S                                        # ... the recovery is not
        assert out["opened"] == [] and out["resolved"] == []
        assert len(out["backfilled"]["opened"]) == 1 and out["backfilled"]["resolved"] == out["backfilled"]["opened"]

    def test_a_recovery_is_live_when_its_opening_was_announced_earlier(self, w):
        for i in range(2):
            w.tick(tk(i), {"disk_forecast": "warn"})                                   # opened live in an earlier call
        outs = [w.tick(tk(i), {"disk_forecast": "ok"}) for i in range(2, 6)]
        assert sum(len(o["resolved"]) for o in outs) == 1 and all(o["backfilled"]["resolved"] == [] for o in outs)

    def test_a_tick_after_the_replay_reports_its_own_transitions_as_live(self, w):
        self._history(w, ["warn", "warn", "ok", "ok"])
        incidents.update({"tasks": {}}, None, tk(4) + 86400)
        t = tk(0) + 86400
        w.tick(t, {"memory_health": "warn"})
        out = w.tick(t + STEP, {"memory_health": "warn"})
        assert len(out["opened"]) == 1 and out["backfilled"]["opened"] == []
        out = w.tick(t + 2 * STEP, {"memory_health": "crit"})
        out = w.tick(t + 3 * STEP, {"memory_health": "crit"})
        assert len(out["escalated"]) == 1
        w.tick(t + 4 * STEP, {"memory_health": "ok"})
        out = w.tick(t + 5 * STEP, {"memory_health": "ok"})
        assert len(out["resolved"]) == 1 and out["backfilled"]["resolved"] == []

    def test_one_call_can_hold_both_old_and_new_transitions(self, w):
        now = tk(60)
        self._history(w, ["warn", "warn", "ok", "ok"], start=0)                                           # long ago
        self._history(w, ["warn", "warn"], start=58, task="memory_health")                                # the last two runs
        out = incidents.update({"tasks": {}}, None, now)
        assert len(out["backfilled"]["opened"]) == 1 and len(out["backfilled"]["resolved"]) == 1
        assert len(out["opened"]) == 1 and out["resolved"] == []
        assert w.pub(now)["open"][0]["task"] == "memory_health"

    def test_the_live_window_boundary_is_two_sampling_intervals(self, w):
        w.hist(tk(0), "disk_forecast", "warn")
        w.hist(tk(1), "disk_forecast", "warn")                                                            # opens at tk(1)
        now = tk(1) + incidents.LIVE_S
        assert incidents.LIVE_S == 2 * incidents.SLOT_S
        out = incidents.update({"tasks": {}}, None, now)                                                  # exactly LIVE_S old: still news
        assert len(out["opened"]) == 1
        for f in ("incidents.jsonl", "incidents-state.json"):
            (w.state / f).unlink()
        out = incidents.update({"tasks": {}}, None, now + 1)                                              # one second older: history
        assert out["opened"] == [] and len(out["backfilled"]["opened"]) == 1

    def test_the_result_stays_small_and_json_serialisable(self, w):
        self._history(w, ["warn", "warn", "ok", "ok"] * 6)
        out = incidents.update({"tasks": {}}, None, tk(24) + 86400)
        assert len(json.dumps(out)) < 2000 and set(out["backfilled"]) == set(self.KINDS)


# ====================================================================================================== review fixes: baseline in the package
class TestBaselineShipsInsideThePackage:
    """The shipped playbooks were etc/playbooks.toml, which install.sh copies to /etc/homelab-maint when absent. load_config merged that
    copy over the packaged one per key (and replaced [[slo]] / [[correlation]] wholesale), so a later text fix, including the avoid /
    fixes SAFETY text, was shadowed by the stale installed copy, and the path <lib>/../etc/playbooks.toml is not installed at all."""

    def test_the_baseline_is_a_data_file_next_to_the_module(self):
        assert incidents._PKG_PLAYBOOKS == Path(incidents.__file__).resolve().parent / "data" / "playbooks.toml"
        assert incidents._PKG_PLAYBOOKS.is_file() and incidents._PKG_PLAYBOOKS == BASELINE.resolve()
        assert len(incidents.load_config()["playbook"]) >= 38

    def test_etc_playbooks_is_an_overrides_only_template(self):
        """What install.sh copies into /etc/homelab-maint must not carry a single active key: a copy there shadows nothing."""
        etc = (ROOT / "etc" / "playbooks.toml").read_text()
        assert tomllib.loads(etc) == {}
        assert "homelab_maint/data/playbooks.toml" in etc and "overrides" in etc.lower() and "enabled = false" in etc
        assert len(etc) < 4000
        # the commented examples are valid TOML once uncommented, and say what they claim
        ex = [re.sub(r"^# ", "", ln) for ln in etc.splitlines() if re.match(r"^# (\[|[a-z_]+ = )", ln)]
        doc = tomllib.loads("\n".join(ex))
        assert doc["playbook"]["my_new_check"]["title"] == "My check" and [x["name"] for x in doc["slo"]] == ["Backups fresh", "Monitoring probes"]
        assert doc["slo"][1]["enabled"] is False and doc["incidents"]["confirm_runs"] == 3

    def test_the_installed_tree_finds_its_baseline_without_the_source_tree(self, tmp_path):
        """What install.sh does: tar the `homelab_maint` directory to <lib>. A fresh interpreter on that COPY (nothing else around) must
        load every playbook from it."""
        lib = tmp_path / "lib"
        shutil.copytree(ROOT / "homelab_maint", lib / "homelab_maint", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = {**os.environ, "PYTHONPATH": str(lib), "HOMELAB_MAINT_STATE": str(tmp_path / "s"), "HOMELAB_MAINT_LOG": str(tmp_path / "l"),
               "HOMELAB_MAINT_RUN": str(tmp_path / "r"), "HOMELAB_MAINT_CONF": str(tmp_path / "c")}
        proc = _REAL_POPEN(["python3", "-B", "-c", "from homelab_maint import incidents as i; c = i.load_config(); "
                            "print(i._PKG_PLAYBOOKS.parent.parent.parent.name, len(c['playbook']), len(c['slo']), len(c['correlation']), "
                            "i.playbook_for('disk_forecast')['title'])"],
                           cwd=tmp_path, env=env, stdout=-1, stderr=-1, text=True)
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        parts = out.split()
        assert parts[0] == "lib" and int(parts[1]) >= 38 and int(parts[2]) >= 6 and int(parts[3]) >= 5 and parts[4:] == ["Disk", "space"]

    def test_a_text_fix_in_the_baseline_reaches_a_host_that_has_an_override_file(self, w, monkeypatch, tmp_path):
        """The regression itself: ship a new baseline, keep the owner's small override: untouched playbooks and keys get the fix."""
        text = BASELINE.read_text()
        fixed = text.replace('"Do not delete state files (/var/lib/homelab-maint) to make an alert go away', '"NEW SAFETY TEXT: do not delete state files (/var/lib/homelab-maint) to make an alert go away')
        assert fixed != text
        newb = tmp_path / "playbooks.toml"
        newb.write_text(fixed)
        (w.conf / "playbooks.toml").write_text('[playbook.disk_forecast]\nask = "Call the plumber."\n')           # a deliberate override
        assert not any("NEW SAFETY" in x for x in incidents.playbook_for("anything")["avoid"])
        monkeypatch.setattr(incidents, "_PKG_PLAYBOOKS", newb)
        assert any("NEW SAFETY TEXT" in x for x in incidents.playbook_for("anything")["avoid"])
        assert incidents.playbook_for("disk_forecast")["ask"] == "Call the plumber."
        assert incidents.override_notes() == []                                                                    # a real override is not a warning

    def test_a_pasted_full_copy_shadows_later_fixes_and_is_reported(self, w, monkeypatch, tmp_path, capsys):
        """Documented limit of per-key overrides: a copy of the old text keeps winning. So `incidents list` says so."""
        (w.conf / "playbooks.toml").write_text(BASELINE.read_text())                                               # what the old install did
        notes = incidents.override_notes()
        assert len(notes) == 1 and "repeats" in notes[0] and "hide every later fix" in notes[0]
        n = int(re.search(r"repeats (\d+) shipped", notes[0]).group(1))
        assert n >= sum(len(v) for v in PB["playbook"].values())                                                   # all of it, not a few keys
        assert incidents.main(["list"]) == 0
        assert "NOTE: " in capsys.readouterr().out

    def test_only_identical_values_are_reported(self, w):
        (w.conf / "playbooks.toml").write_text('[playbook.disk_forecast]\nask = "Call the plumber."\ntitle = "Disk space"\n'
                                               '[incidents]\nrecent_max = 7\ngroup_window_s = 600\n'
                                               '[[slo]]\nname = "Backups fresh"\ntarget_pct = 98.5\nclass = "P2"\n')
        (note,) = incidents.override_notes()
        assert "repeats 3 shipped" in note and "disk_forecast.title" in note
        assert "disk_forecast.ask" not in note and "recent_max" not in note

    def test_correlation_overrides_merge_by_cause_and_effect(self, w):
        base = incidents.load_config()["correlation"]
        assert len(base) >= 5
        first = base[0]
        (w.conf / "playbooks.toml").write_text(f'[[correlation]]\ncause = "{first["cause"]}"\neffect = "{first["effect"]}"\ntext = "mine"\n'
                                               '[[correlation]]\ncause = "a"\neffect = "b"\ntext = "new"\n'
                                               f'[[correlation]]\ncause = "{base[1]["cause"]}"\neffect = "{base[1]["effect"]}"\nenabled = false\n')
        got = {(c["cause"], c["effect"]): c for c in incidents.load_config()["correlation"]}
        assert got[(first["cause"], first["effect"])]["text"] == "mine" and got[("a", "b")]["text"] == "new"
        assert (base[1]["cause"], base[1]["effect"]) not in got and len(got) == len(base)

    def test_shipped_slo_and_correlation_keys_are_unique(self):
        """Merging by key would silently collapse a duplicate in the baseline itself."""
        names = [t["name"] for t in tomllib.loads(BASELINE.read_text())["slo"]]
        pairs = [(t["cause"], t["effect"]) for t in tomllib.loads(BASELINE.read_text())["correlation"]]
        assert len(names) == len(set(names)) == len(incidents.load_config()["slo"])
        assert len(pairs) == len(set(pairs)) == len(incidents.load_config()["correlation"])

    def test_junk_override_tables_are_skipped_not_fatal(self, w):
        (w.conf / "playbooks.toml").write_text('slo = "x"\ncorrelation = [1, 2]\n[[slo]]\ntarget_pct = 5\n[[correlation]]\ncause = 1\n')
        cfg = incidents.load_config()
        assert len(cfg["slo"]) >= 6 and len(cfg["correlation"]) >= 5 and incidents.override_notes() == []


# ====================================================================================================== command line
class TestCli:
    def test_list_show_playbook_slo_export_update(self, day, capsys):
        assert incidents.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "1 open, 7 in 30 days" in out and "INC-20261002-007" in out
        assert incidents.main(["show", "INC-20261002-003"]) == 0
        shown = capsys.readouterr().out
        assert "Memory pressure" in shown and "# Postmortem" in shown
        assert incidents.main(["show", "INC-nope"]) == 1
        assert incidents.main(["playbook", "disk_forecast"]) == 0 and "First checks:" in capsys.readouterr().out
        assert incidents.main(["slo"]) == 0 and "Services up" in capsys.readouterr().out
        assert incidents.main(["export"]) == 0 and json.loads(capsys.readouterr().out)["stats"]["open_count"] == 1
        (day.state / "status.json").write_text(json.dumps(day.status))
        assert incidents.main(["update"]) == 0 and json.loads(capsys.readouterr().out)["ok"] is True
        assert incidents.main(["bogus"]) == 2 and "usage" in capsys.readouterr().err

    def test_python_dash_m_entry_point(self):
        src = (ROOT / "homelab_maint" / "incidents.py").read_text()
        assert 'if __name__ == "__main__":' in src and "sys.exit(main())" in src


# ====================================================================================================== acknowledged issues (SPEC5)
# The owner's "I understand this error and I'm okay with it": a failing status entry carries `acked` (acks.apply_to_status). The incident
# stays OPEN and listed, in state `acknowledged`; it is not paged or escalated; the tracker keeps following the TRUE status; the SLO is not
# adjusted. Everything below runs update() on tmp dirs with hand-written entries; nothing here needs acks.py.
FPA = "0123456789abcdef"
DAYS90 = 90 * 86400


def ackd(until, by="email", note="", sev="warn", since=None, fp=FPA):
    return {"fp": fp, "until": until, "by": by, "note": note, "severity": sev, "since": since}


def step(w, i, status, task="disk_forecast", ack=None, summary=None, now=None, fp=FPA):
    """One run of one check at tick i; the entry carries the issue id when failing and `acked` when given."""
    t = tk(i)
    w.hist(t, task, status)
    extra = {"fp": fp} if status in ("warn", "crit", "error") else {}
    if ack is not None:
        extra["acked"] = ack
    w.entry(t, task, status, summary, None, **extra)
    return incidents.update(w.status, None, t + 5 if now is None else now)


def inc_of(w, task="disk_forecast", now=None):
    p = w.pub(now if now is not None else tk(30))
    return next(i for i in p["open"] + p["recent"] if i["task"] == task)


class TestAcknowledged:
    def test_an_acknowledgement_keeps_the_incident_open_and_listed_as_acknowledged(self, w):
        step(w, 0, "warn")
        step(w, 1, "warn")
        assert w.pub(tk(1) + 5)["open"][0]["status"] == "open" and "ack" not in w.pub(tk(1) + 5)["open"][0]
        since = tk(2) - 100
        out = step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=since, note="known flap", by="web"))
        assert out["acked"] == [out["acked"][0]] and len(out["acked"]) == 1 and out["opened"] == []
        p = w.pub(tk(2) + 10)
        (i,) = p["open"]
        assert i["status"] == "acknowledged" and i["fp"] == FPA and i["level"] == 1
        assert i["ack"] == {"fp": FPA, "until": tk(2) + DAYS90, "at": since, "by": "web", "note": "known flap", "severity": "warn"}
        assert i["acknowledged_at"] == since and p["stats"]["open_count"] == 1 and p["stats"]["acknowledged_count"] == 1
        assert p["stats"]["mtta_s_30d"] == since - tk(0)                                   # time to acknowledge, from the first failing sample
        assert [x["kind"] for x in i["timeline"]].count("acked") == 1 and p["recent"] == []
        assert [e["ev"] for e in w.events()].count("acked") == 1

    def test_the_owners_acknowledgement_is_the_mtta_only_when_nothing_paged_first(self, w):
        w.queue_audit([(tk(1) + 60, "notify", "send", "disk_forecast: WARN Disk space", 0, "sent")])
        step(w, 0, "warn")
        step(w, 1, "warn")
        w.flush_audit(tk(1) + 120)
        incidents.update(w.status, None, tk(1) + 120)                                      # the delivered page is found in the audit trail
        assert inc_of(w, now=tk(1) + 130)["acknowledged_at"] == tk(1) + 60
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        i = inc_of(w, now=tk(2) + 10)
        assert i["status"] == "acknowledged" and i["acknowledged_at"] == tk(1) + 60        # the page was first: its time stays the MTTA
        assert [e["ev"] for e in w.events() if e["ev"] in ("ack", "acked")] == ["ack", "acked"]

    def test_a_recurrence_inside_the_window_opens_acknowledged_and_is_not_in_the_lists_glue_pages_from(self, w):
        step(w, 0, "warn", ack=ackd(tk(0) + DAYS90, since=tk(0) - 5 * 86400))             # acknowledged days ago, failing again
        out = step(w, 1, "warn", ack=ackd(tk(1) + DAYS90, since=tk(0) - 5 * 86400))
        (i,) = w.pub(tk(1) + 10)["open"]
        assert i["status"] == "acknowledged" and out["opened"] == [] and out["held"] == [i["id"]] and out["acked"] == [i["id"]]
        assert i["acknowledged_at"] == i["detected_at"]                                    # an ack older than the incident is clamped to its opening

    def test_escalation_and_improvement_of_an_acknowledged_incident_are_held_not_paged(self, w):
        for k in (0, 1):
            step(w, k, "warn", ack=ackd(tk(k) + DAYS90, since=tk(0)))
        out = step(w, 2, "crit", ack=ackd(tk(2) + DAYS90, sev="crit", since=tk(0)))        # (a crit ack, pre-accepted by the owner)
        out = step(w, 3, "crit", ack=ackd(tk(3) + DAYS90, sev="crit", since=tk(0)))
        assert out["escalated"] == [] and len(out["held"]) == 1 and inc_of(w, now=tk(3) + 9)["status"] == "acknowledged"
        assert inc_of(w, now=tk(3) + 9)["severity"] == "sev2"                              # the ledger still records the true level

    def test_removing_the_ack_while_still_failing_puts_it_back_to_open(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        step(w, 3, "warn")                                                                 # acks.remove(): the entry has no `acked` any more
        i = inc_of(w, now=tk(3) + 9)
        assert i["status"] == "open" and "ack" not in i and i["acknowledged_at"] == tk(2)  # MTTA is history, it stays
        ev = [e for e in w.events() if e["ev"] in ("acked", "unacked")]
        assert [e["ev"] for e in ev] == ["acked", "unacked"] and "removed, expired" in ev[1]["text"]
        assert w.pub(tk(3) + 9)["stats"].get("acknowledged_count") is None

    def test_a_worse_severity_breaks_the_ack_and_escalates_normally(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        step(w, 3, "crit")                                                                 # past the ceiling: acks.apply_to_status drops `acked`
        out = step(w, 4, "crit")
        i = inc_of(w, now=tk(4) + 9)
        assert i["status"] == "open" and i["severity"] == "sev2" and i["level"] == 2 and len(out["escalated"]) == 1 and out["held"] == []
        assert [e["text"] for e in w.events() if e["ev"] == "unacked"] == ["Acknowledgement ended: it got worse than what was acknowledged, so it alerts again"]

    def test_a_recovery_in_progress_keeps_the_state_and_the_incident_resolves_as_always(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        step(w, 3, "ok")                                                                   # the entry is healthy: no `acked`, no failing status
        assert inc_of(w, now=tk(3) + 9)["status"] == "acknowledged" and not any(e["ev"] == "unacked" for e in w.events())
        out = step(w, 4, "ok")
        p = w.pub(tk(4) + 9)
        assert out["resolved"] and p["open"] == [] and p["recent"][0]["status"] == "resolved" and "ack" not in p["recent"][0]
        assert p["stats"].get("acknowledged_count") is None

    def test_the_ack_covers_the_recurrence_after_a_resolved_incident(self, w):
        for k in (0, 1):
            step(w, k, "warn", ack=ackd(tk(0) + DAYS90, since=tk(0)))
        for k in (2, 3):
            step(w, k, "ok")
        for k in (4, 5):
            out = step(w, k, "warn", ack=ackd(tk(4) + DAYS90, since=tk(0)))
        p = w.pub(tk(5) + 9)
        assert len(p["open"]) == 1 and p["open"][0]["status"] == "acknowledged" and len(p["recent"]) == 1 and out["opened"] == []

    def test_an_acknowledged_incident_neither_escalates_nor_counts_towards_sev1(self, w):
        """Three correlated crit failures are sev1 (each has 2 related); with two of them acknowledged from the start, the third is plain sev2."""
        ack = lambda k: ackd(tk(k) + DAYS90, sev="crit", since=tk(0))                      # noqa: E731
        for k in (0, 1):
            for task in ("disk_forecast", "memory_health", "failed_units"):
                step(w, k, "crit", task=task, ack=ack(k) if task != "disk_forecast" else None, fp=FPA)
        by = {i["task"]: i for i in w.pub(tk(1) + 9)["open"]}
        assert {t: i["severity"] for t, i in by.items()} == {"disk_forecast": "sev2", "memory_health": "sev2", "failed_units": "sev2"}
        assert by["memory_health"]["status"] == "acknowledged" and by["disk_forecast"]["status"] == "open"
        # control: nobody acknowledged -> sev1
        w2 = type(w)(w.root / "control", w.mp)
        for k in (0, 1):
            for task in ("disk_forecast", "memory_health", "failed_units"):
                step(w2, k, "crit", task=task)
        assert {i["severity"] for i in w2.pub(tk(1) + 9)["open"]} == {"sev1"}

    def test_update_is_idempotent_and_crash_safe(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        n, raw = len(w.events()), w.ledger_bytes()
        again = incidents.update(w.status, None, tk(2) + 20)
        assert again["ok"] and again["acked"] == [] and len(w.events()) == n and w.ledger_bytes() == raw
        lines = raw.split(b"\n")                                                          # tear the acked line in half: a crash while writing it
        torn = b"\n".join(lines[:-2]) + b"\n" + lines[-2][: len(lines[-2]) // 2]
        (w.state / "incidents.jsonl").write_bytes(torn)
        assert inc_of(w, now=tk(2) + 30)["status"] == "open"                              # the torn line is skipped on read
        incidents.update(w.status, None, tk(2) + 40)
        assert inc_of(w, now=tk(2) + 50)["status"] == "acknowledged" and [e["ev"] for e in w.events()].count("open") == 1

    @pytest.mark.parametrize("bad", [5, "x", [], {}, {"fp": FPA}, {"fp": "short", "until": 2e9}, {"fp": FPA, "until": "never"},
                                     {"fp": FPA, "until": float("nan")}, {"fp": FPA, "until": 1.0}, {"fp": 5, "until": 2e9}])
    def test_a_malformed_or_ended_ack_is_ignored(self, w, bad):
        for k in (0, 1, 2):
            out = step(w, k, "warn", ack=bad)
        i = inc_of(w, now=tk(2) + 9)
        assert i["status"] == "open" and "ack" not in i and out["acked"] == [] and not any(e["ev"] == "acked" for e in w.events())

    def test_only_a_failing_entry_can_hold_an_incident(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "ok", ack=ackd(tk(2) + DAYS90, since=tk(2)))                           # an `acked` on a healthy entry means nothing
        assert not any(e["ev"] == "acked" for e in w.events())

    def test_no_news_about_a_check_leaves_the_state_alone(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2)))
        w.status["tasks"]["failed_units"] = w.entry(tk(3), "failed_units", "ok", None, None)
        del w.status["tasks"]["disk_forecast"]                                            # status.json without that task (e.g. a partial write)
        incidents.update(w.status, None, tk(3) + 5)
        assert inc_of(w, now=tk(3) + 9)["status"] == "acknowledged"

    def test_the_note_is_redacted_in_the_ledger_and_the_export(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(tk(2) + DAYS90, since=tk(2), note="password=hunter2 mail me@example.com " + "x" * 400))
        blob = w.ledger_bytes().decode() + json.dumps(w.pub(tk(2) + 9))
        assert "hunter2" not in blob and "example.com" not in blob
        assert len(inc_of(w, now=tk(2) + 9)["ack"]["note"]) <= 200

    def test_the_issue_id_is_published_for_open_incidents_so_the_buttons_can_post_it(self, w):
        for k in (0, 1):
            step(w, k, "warn")
        (i,) = w.pub(tk(1) + 9)["open"]
        assert i["fp"] == FPA
        step(w, 2, "ok", fp="zz")
        for k in (3, 4):
            step(w, k, "warn", fp="not-a-fingerprint")
        assert "fp" not in next(x for x in w.pub(tk(4) + 9)["open"])

    def test_the_slo_is_not_adjusted_by_acknowledgements(self, w):
        """Availability stays honest: history records flagged acked count as bad exactly like the others."""
        def history(flag):
            return [{"t": tk(i), "kind": "task", "task": "disk_forecast", "status": "warn", **({"acked": True} if flag else {})} for i in range(40)]
        a, b = incidents.export_slo(history(False), tk(40)), incidents.export_slo(history(True), tk(40))
        assert a == b and any(o["bad_minutes"] for o in a["objectives"])

    def test_result_shape_always_has_the_new_lists(self, w):
        out = step(w, 0, "ok")
        assert out["acked"] == [] and out["held"] == [] and out["ok"] is True

    def test_the_cli_shows_the_state(self, w, capsys):
        for k in (0, 1):
            step(w, k, "warn")
        step(w, 2, "warn", ack=ackd(2_000_000_000, since=tk(2)))
        (w.state / "status.json").write_text(json.dumps(w.status))
        assert incidents.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "acknowledged" in out and "1 open" in out
        assert incidents.main(["show", w.pub(tk(2) + 9)["open"][0]["id"]]) == 0 and " acked " in capsys.readouterr().out


    def test_acknowledgement_audit_rows_never_count_as_a_mitigation(self, w):
        """acks.py audits under the task name "acks" with the fingerprint as target; an entity name inside it must not look like a fix."""
        w.queue_audit([(tk(2) + 30, "acks", "ack", "kavita immich_postgres", 0, "done")])
        for k in (0, 1, 2):
            w.tick(tk(k), {"failed_units": "crit"})
        i = inc_of(w, task="failed_units", now=tk(2) + 60)
        assert i["mitigated_at"] is None and i["related_actions"] == []
        w.queue_audit([(tk(2) + 30, "docker_cache", "buildx-prune", "kavita", 0, "done")])       # control: a real action on that entity does
        w.audit_written = 0
        w.flush_audit(tk(2) + 60)
        incidents.update(w.status, None, tk(2) + 90)
        assert inc_of(w, task="failed_units", now=tk(2) + 100)["mitigated_at"] is not None
