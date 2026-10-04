"""Tests for homelab_maint/reports.py: periods and DST, the health score, every report section on a realistic week of
fixtures (history, audit, incidents, spikes, changes, journal, metrics ring, samples), new installs, missing and damaged
inputs, redaction, retention, concurrency and the two tasks. Everything runs on tmp dirs; the module never runs a command
(the autouse fixture makes any subprocess call fail the test)."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import copy
import dataclasses
import hashlib
import itertools
import json
import os
import stat
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from homelab_maint import core, reports
from homelab_maint.core import GIB, Ctx

TZ = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")


def at(y, m, d, h=0, mi=0, tz=TZ):
    return datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


NOW_WEEKLY = at(2026, 10, 7, 7, 45)      # Wed: the weekly report covers Wed 30 Sep 00:00 -> Wed 7 Oct 00:00 (EDT)
NOW_DAILY = at(2026, 10, 7, 7, 30)       # the daily report covers Tue 6 Oct
SEP30, OCT7 = at(2026, 9, 30), at(2026, 10, 7)

# --------------------------------------------------------------------------- the host's real task set (names, tiers, titles)
CHECKS = [("alert_path_health", "Alert path"), ("backup_freshness", "Backups"), ("disk_forecast", "Disk space"),
          ("docker_df", "Docker disk"), ("failed_units", "Services & containers"), ("growth_watch", "Growth watch"),
          ("image_ledger", "Image usage ledger"), ("memory_health", "Memory pressure"), ("orphan_report", "Orphaned processes"),
          ("plex_media_mount_check", "Plex Media mount"), ("smart_trend", "SMART trend"),
          ("spike_sampler", "Memory spike sampler"), ("stuck_detector", "Stuck containers")]
ALERT_OFF = {"docker_df", "orphan_report", "stuck_detector", "config_drift"}
CLEANERS = [("docker_cache", "Docker build cache"), ("docker_images", "Unused Docker images"), ("snap_revisions", "Old snap revisions")]
MOUNTS = ["/", "/media/SandiskSSD", "/mnt/backup/system"]
SIZES = {"/": 1800 * GIB, "/media/SandiskSSD": 1800 * GIB, "/mnt/backup/system": 5400 * GIB}


class World:
    """A tmp STATE/LOG/CONF tree plus helpers that write realistic inputs into it."""

    def __init__(self, root: Path, monkeypatch=None):
        self.root = root
        self.state, self.log, self.conf = root / "state", root / "log", root / "conf"
        for d in (self.state, self.log, self.conf):
            d.mkdir(parents=True, exist_ok=True)
        self.mp = monkeypatch or pytest.MonkeyPatch()
        for name, d in (("STATE_DIR", self.state), ("LOG_DIR", self.log), ("CONF_DIR", self.conf)):
            self.mp.setattr(core, name, d)
        self.status: dict = {}
        self.eps: list[tuple] = []                  # (task, t0, t1, status)
        self.metric_fns: dict = {}                  # task -> fn(t) -> dict
        self.hist: list[dict] = []

    # ---- files
    def jl(self, name, rows, where="state", mode="w"):
        p = (self.state if where == "state" else self.log) / name
        with open(p, mode) as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r, separators=(",", ":"))) + "\n")
        return p

    def js(self, rel, obj, where="state"):
        p = (self.state if where == "state" else self.log) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))
        return p

    # ---- history
    def stat(self, task, t):
        for tk, a, b, st in self.eps:
            if tk == task and a <= t < b:
                return st
        return "ok"

    def history(self, t_from, t_to, disk_fn=None, tasks=CHECKS, step=900, jitter=20.0):
        """One check-tier run every `step` seconds: a task record per check plus a disk record per watched mount."""
        t = t_from
        while t < t_to:
            for i, (name, _title) in enumerate(tasks):
                m = {}
                if name in self.metric_fns:
                    m = self.metric_fns[name](t)
                self.hist.append({"t": t + jitter + i * 0.3, "kind": "task", "task": name, "status": self.stat(name, t),
                                  "reclaimed": 0, "dur": 0.1, "metrics": m})
            for mount in MOUNTS:
                free = disk_fn(mount, t) if disk_fn else SIZES[mount] * 0.4
                self.hist.append({"t": t + jitter - 1, "kind": "disk", "mount": mount, "free": int(free)})
            t += step
        return self

    def daily_tier(self, t_from, t_to):
        """Daily cleaners at 07:30 local and the weekly task at Wed 07:45 (records only; audit is separate)."""
        d = datetime.fromtimestamp(t_from, TZ).date()
        while at(d.year, d.month, d.day, 7, 30) < t_to:
            t = at(d.year, d.month, d.day, 7, 30)
            if t >= t_from:
                for name, _ in CLEANERS:
                    self.hist.append({"t": t + 60, "kind": "task", "task": name, "status": "ok", "reclaimed": 0, "dur": 1.0, "metrics": {}})
            d = date.fromordinal(d.toordinal() + 1)
        return self

    def flush_history(self, extra=()):
        rows = sorted(self.hist + list(extra), key=lambda r: r["t"])
        self.jl("history.jsonl", rows)

    # ---- audit
    def audit(self, rows):
        out = []
        for t, task_, action, target, nbytes, outcome in rows:
            out.append({"ts": datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "task": task_, "action": action,
                        "target": target, "bytes": nbytes, "outcome": outcome})
        self.jl("audit.jsonl", out, where="log")

    # ---- status.json (what the runner wrote last)
    def make_status(self, now, modes="report", overrides=None):
        tasks = {}
        for name, title in CHECKS:
            tasks[name] = {"title": title, "klass": "C0", "tier": "check", "status": self.stat(name, now - 900), "summary": "ok",
                           "last_run": now - 300, "alert": name not in ALERT_OFF, "metrics": {}, "items": [], "mode": "check"}
        tasks["config_drift"] = {"title": "Config drift", "klass": "C0", "tier": "weekly", "status": "info", "alert": False,
                                 "summary": "2 settings differ from policy", "last_run": now - 3600, "metrics": {}, "items": [], "mode": "check"}
        for name, title in CLEANERS:
            tasks[name] = {"title": title, "klass": "C1", "tier": "daily", "status": "ok", "alert": True, "summary": "report",
                           "last_run": now - 3600, "mode": "dry-run", "items": [],
                           "metrics": {"mode": modes, "selected": 3, "selected_h": "10.0 GiB"}}
        tasks["disk_forecast"]["metrics"] = {"mounts": [
            {"mount": m, "free": int(SIZES[m] * 0.4), "free_h": reports.human(SIZES[m] * 0.4), "used_pct": 60.0, "days": None,
             "level": "ok", "info": False} for m in MOUNTS]}
        tasks["backup_freshness"]["metrics"] = {"backups": [
            {"name": "backup-system", "age": "5d19h", "age_h": 139.0, "result": "ok", "level": "ok"},
            {"name": "backup-immich", "age": "4d20h", "age_h": 116.0, "result": "ok", "level": "ok"},
            {"name": "stack-backup", "age": "18h13m", "age_h": 18.2, "result": "ok", "level": "ok"}], "failed": 0}
        tasks["docker_df"].update(status="warn", summary="warn: build cache 28.6 GiB (>= 25 GiB)",
                                  metrics={"build_cache_h": "28.6 GiB", "images_reclaim_h": "14.0 GiB", "safe_reclaim_h": "42.6 GiB"})
        for k, v in (overrides or {}).items():
            tasks.setdefault(k, {}).update(v)
        self.status = {"schema": 1, "generated_at": now - 300, "host": "ohmz-homelab", "overall": "ok", "paused": False,
                       "tasks": tasks, "tier_runs": {}, "reclaimed_log": []}
        return self.status

    def write_status(self):
        self.js("status.json", self.status)

    # ---- metrics ring (SPEC2 raw layout)
    def ring(self, t_from, t_to, fn):
        hours = [{"h": 0, "n": 0, "sum": {}, "cnt": {}, "max": {}, "min": {}} for _ in range(168)]
        for h in range(int(t_from // 3600), int(t_to // 3600) + 1):
            vals = fn(h)
            hours[h % 168] = {"h": h, "n": 60, "sum": {k: v * 60 for k, v in vals.items()}, "cnt": {k: 60 for k in vals},
                              "max": {k: v + 5 for k, v in vals.items()}, "min": {k: v - 5 for k, v in vals.items()}}
        self.js("metrics-ring.json", {"v": 1, "slots": 168, "hours": hours, "last": {}})

    def samples(self, t_list, fn):
        rows = [{"t": t, "kind": "sample", "host": {"mem_avail": 70 * GIB}, "c": fn(t), "p": []} for t in t_list]
        self.jl("samples.jsonl", rows)


@pytest.fixture(autouse=True)
def no_commands(monkeypatch):
    """reports.py is pure file reading: any attempt to run a command fails the test."""
    def boom(*a, **k):
        raise AssertionError("reports must not run commands: %r" % (a[:1],))
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(core, "sh", boom)


def spike_ledger(t0, dur, peak, dims, psi, contrib, outcome, nothing_killed=True, restarted=(), oom=0):
    """spikes.jsonl exactly as pressure_state writes it: the same id/t on every line, `open` each 15-minute tick, then `closed`."""
    base = {"t": t0, "kind": "spike", "id": int(t0), "peak_level": peak, "dims": dims, "psi": psi, "contributors": contrib, "oom_kills": oom}
    rows = [{**base, "state": "open", "end": None, "duration_s": d, "outcome": "in progress", "nothing_killed": True, "restarted": [],
             "stopped": [], "reclaimed": 0, "throttled": 0} for d in range(0, dur, 900)]
    rows.append({**base, "state": "closed", "end": t0 + dur, "duration_s": dur, "outcome": outcome, "nothing_killed": nothing_killed,
                 "restarted": list(restarted), "stopped": [], "reclaimed": 1, "throttled": 0})
    return rows


def ctr(anon_gib_fn):
    return lambda t: {"anon_total_gib": round(anon_gib_fn(t), 2)}


def build_week(root, mp, *, with_ring=True, with_incidents=True, with_spikes=True, with_audit=True, with_samples=True):
    """A realistic week (Wed 30 Sep - Tue 6 Oct) plus a week of warm-up history before it, up to Wed 7 Oct 07:45."""
    w = World(root, mp)
    day = 86400
    # problems: disk warn Fri 14:15 -> Sat 06:30, backups crit Sun 01:00 -> 03:00, memory warn during Tue's spike, services blips
    w.eps = [("disk_forecast", at(2026, 10, 2, 14, 15), at(2026, 10, 3, 6, 30), "warn"),
             ("backup_freshness", at(2026, 10, 4, 1, 0), at(2026, 10, 4, 3, 0), "crit"),
             ("memory_health", at(2026, 10, 6, 14, 15), at(2026, 10, 6, 14, 45), "warn"),
             ("memory_health", at(2026, 10, 2, 14, 15), at(2026, 10, 2, 14, 30), "warn"),
             ("failed_units", at(2026, 10, 1, 9, 0), at(2026, 10, 1, 9, 30), "warn"),
             ("failed_units", at(2026, 10, 5, 22, 0), at(2026, 10, 5, 22, 15), "warn")]
    w.metric_fns["spike_sampler"] = ctr(lambda t: 10.0 + 2.5 * (t - (SEP30 - 3 * day)) / (10 * day) * 2 if t > SEP30 else 10.0)
    w.metric_fns["memory_health"] = lambda t: {"psi_mem_full60": 12.0 if w.stat("memory_health", t) == "warn" else 0.2,
                                               "psi_io_some60": 8.0, "mem_available_gib": 61.0, "oom_kills_delta": 0}
    w.metric_fns["backup_freshness"] = lambda t: {"failed": 1 if w.stat("backup_freshness", t) == "crit" else 0, "worst_age_h": 100.0}
    free0 = 230 * GIB
    w.history(SEP30 - 7 * day, NOW_WEEKLY, disk_fn=lambda m, t: (free0 - 2.4 * GIB * (t - (SEP30 - 7 * day)) / day) if m == "/" else SIZES[m] * 0.4)
    w.daily_tier(SEP30 - 7 * day, NOW_WEEKLY)
    w.flush_history()
    w.make_status(NOW_WEEKLY)
    w.status["tasks"]["disk_forecast"]["metrics"]["mounts"][0].update(free=int(212 * GIB), free_h="212.0 GiB", used_pct=88.3, level="warn")
    w.status["tasks"]["disk_forecast"].update(status="warn", summary="warn: / 12% free (212.0 GiB)")
    w.write_status()
    if with_audit:
        w.audit([(at(2026, 10, 2, 7, 35), "docker_cache", "docker builder prune", "default", 18 * GIB, "done"),
                 (at(2026, 10, 2, 7, 36), "snap_revisions", "snap remove", "lxd 31333", 200 * 2 ** 20, "done"),
                 (at(2026, 10, 2, 7, 36), "snap_revisions", "snap remove", "core22 1722", 218 * 2 ** 20, "done"),
                 (at(2026, 10, 3, 7, 35), "docker_images", "docker image rm", "sha256:abc", 0, "failed: image in use"),
                 (at(2026, 10, 3, 7, 35), "docker_images", "docker image rm", "ghcr.io/x/y:1", 4 * GIB, "dry-run"),
                 (at(2026, 10, 3, 7, 36), "retention", "rm", "/var/log/old.log", 10, "refused-protected"),
                 (at(2026, 10, 2, 14, 12), "pressure_response", "ollama-unload", "ollama:qwen3:32b", 0, "done"),
                 (at(2026, 10, 2, 14, 30), "notify", "send", "disk_forecast: WARN Disk space", 0, "sent"),
                 (at(2026, 10, 3, 6, 40), "notify", "send", "OK Disk space: recovered", 0, "sent"),
                 (at(2026, 10, 4, 1, 40), "notify", "send", "CRIT Backups", 0, "failed rc=1 Username and Password not accepted"),
                 (at(2026, 10, 7, 7, 35), "docker_cache", "docker builder prune", "default", 5 * GIB, "done")])   # after the period
    if with_incidents:
        w.js("incidents.json", {"generated_at": NOW_WEEKLY, "open": [
            {"id": "inc-0007", "task": "disk_forecast", "title": "Disk space low on /", "severity": "sev3", "since": at(2026, 10, 2, 14, 30),
             "duration_s": 4 * day, "summary": "root 12% free"}],
            "recent": [{"id": "inc-0006", "task": "backup_freshness", "title": "Backup immich failed", "severity": "sev2",
                        "since": at(2026, 10, 4, 1, 15), "resolved_at": at(2026, 10, 4, 3, 15), "duration_s": 7200, "mttr_s": 7200,
                        "postmortem_md": "# Postmortem\n- token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
                       {"id": "inc-0001", "task": "x", "title": "Old one", "severity": "sev3", "since": at(2026, 9, 1), "resolved_at": at(2026, 9, 1, 2), "mttr_s": 7200}],
            "stats": {}}, )
    if with_spikes:
        w.jl("spikes.jsonl", spike_ledger(at(2026, 10, 2, 14, 10), 540, 3, {"mem": 3, "io": 1}, {"mem_full60": 12.0, "io_some60": 30.0},
                                          [{"name": "tunarr-host-net", "class": "P2", "anon_gib": 5.1, "cpu_pct": 240.0}],
                                          "handled: reclaimed 1 idle item(s), nothing killed") +
              spike_ledger(at(2026, 10, 6, 14, 10), 1800, 2, {"mem": 2}, {"mem_full60": 9.0},
                           [{"name": "tunarr-host-net", "class": "P2", "anon_gib": 4.2, "cpu_pct": 180.0}],
                           "resolved by itself, nothing killed"))
        w.jl("pressure-log.jsonl", [{"ts": at(2026, 10, 2, 14, 12), "level": 3, "rung": "L2", "action": "unload idle Ollama model",
                                     "target": "ollama:qwen3:32b", "class": "P1", "outcome": "done"},
                                    {"ts": at(2026, 10, 2, 14, 13), "level": 3, "rung": "L3", "action": "slow batch container",
                                     "target": "tunarr-host-net", "class": "P2", "outcome": "would"}])
    w.jl("changes.jsonl", [{"ts": at(2026, 10, 1, 10, 0), "task": "caps", "kind": "config", "detail": "applied ceiling to kavita",
                            "bytes": 0, "outcome": "done", "verified": True}])
    w.jl("maintenance-journal.jsonl", [{"ts": datetime.fromtimestamp(at(2026, 10, 3, 20, 0), TZ).isoformat(),
                                        "title": "Replaced the CPU cooler fan", "detail": "owner, 20 min downtime"}])
    if with_ring:
        w.ring(SEP30 + 31 * 3600, NOW_WEEKLY, lambda h: {"cpu_temp": 52.0 + (h % 24) / 4, "gpu_temp": 41.0, "ram_temp": 38.0, "nvme_temp": 47.0,
                                                           "cpu_fan_rpm": 1180.0, "case_fan_rpm": 800.0, "gpu_fan_pct": 20.0})
    if with_samples:
        first = [SEP30 + i * 900 for i in range(12)]
        last = [OCT7 - 86400 + i * 900 for i in range(12)]
        w.samples(first + last, lambda t: {"tunarr-host-net": {"anon": int((2.0 if t < SEP30 + 86400 else 3.4) * GIB)},
                                           "kavita": {"anon": int(1.0 * GIB)}})
    w.js("slo.json", {"objectives": [{"name": "Host health", "availability_pct": 99.1, "status": "ok"},
                                      {"name": "Backups", "availability_pct": 97.0, "status": "at_risk"}]})
    w.js("public/routine.json", {"calendar": [{"date": "2026-10-07", "items": [{"time": "07:45", "title": "Weekly review", "kind": "weekly"}]},
                                              {"date": "2026-10-08", "items": [{"time": "07:30", "title": "Daily cleanup", "kind": "daily"}]}]})
    return w


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    """(world, weekly doc) built once; tests only read the doc."""
    root = tmp_path_factory.mktemp("week")
    mp = pytest.MonkeyPatch()
    try:
        w = build_week(root, mp)
        doc = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
        inp = reports.gather(reports.period_for("weekly", NOW_WEEKLY, TZ), TZ)
    finally:
        mp.undo()
    return w, doc, inp


@pytest.fixture
def doc(week):
    return copy.deepcopy(week[1])


@pytest.fixture
def inputs(week):
    return copy.deepcopy(week[2])


def rebuild(inp, kind="weekly", now=NOW_WEEKLY):
    return reports._scrub(reports.build_report(kind, now, inp, TZ), reports._cleaner())


# =========================================================================== periods, time zones, DST
def test_daily_period_is_yesterday_in_local_midnights():
    p = reports.period_for("daily", NOW_DAILY, TZ)
    assert p.id == "2026-10-06" and p.label == "Tue 6 Oct 2026"
    assert p.start == at(2026, 10, 6) and p.end == at(2026, 10, 7)
    assert p.days == (date(2026, 10, 6),) and p.tz == "America/Toronto"


def test_weekly_period_is_the_last_seven_complete_days_and_names_the_iso_week_of_the_last_day():
    p = reports.period_for("weekly", NOW_WEEKLY, TZ)
    assert (p.start, p.end) == (SEP30, OCT7)
    assert p.days[0] == date(2026, 9, 30) and p.days[-1] == date(2026, 10, 6) and len(p.days) == 7
    assert p.id == "2026-W41" and p.label == "Wed 30 Sep - Tue 6 Oct 2026"


def test_period_depends_only_on_the_local_date_of_now():
    a = reports.period_for("daily", at(2026, 10, 7, 0, 0), TZ)
    b = reports.period_for("daily", at(2026, 10, 7, 23, 59), TZ)
    assert a == b and a.id == "2026-10-06"


def test_dst_fall_back_day_is_25_hours_and_spring_forward_day_is_23():
    fall = reports.period_for("daily", at(2026, 11, 2, 7, 30), TZ)         # Sun 1 Nov 2026: clocks go back
    assert fall.id == "2026-11-01" and fall.end - fall.start == 25 * 3600
    spring = reports.period_for("daily", at(2026, 3, 9, 7, 30), TZ)         # Sun 8 Mar 2026: clocks go forward
    assert spring.id == "2026-03-08" and spring.end - spring.start == 23 * 3600
    week = reports.period_for("weekly", at(2026, 11, 2, 7, 45), TZ)
    assert week.end - week.start == (7 * 24 + 1) * 3600 and len(week.days) == 7


def test_iso_week_id_uses_the_iso_year_across_new_year():
    # 2026 has 53 ISO weeks: Sun 3 Jan 2027 still belongs to 2026-W53, and 2027-W01 starts Mon 4 Jan.
    assert reports.period_for("weekly", at(2027, 1, 4, 7, 45), TZ).id == "2026-W53"
    assert reports.period_for("weekly", at(2027, 1, 11, 7, 45), TZ).id == "2027-W01"
    assert reports.period_for("daily", at(2027, 1, 1, 7, 30), TZ).id == "2026-12-31"


def test_same_instant_gives_different_periods_in_different_zones():
    now = at(2026, 10, 7, 1, 0, UTC)              # 21:00 Tue in Toronto, 01:00 Wed in UTC
    assert reports.period_for("daily", now, UTC).id == "2026-10-06"
    assert reports.period_for("daily", now, TZ).id == "2026-10-05"


def test_unknown_period_kind_is_rejected():
    with pytest.raises(ValueError):
        reports.period_for("monthly", NOW_DAILY, TZ)


def test_resolve_tz_prefers_the_configured_name_and_survives_garbage(monkeypatch):
    assert reports.resolve_tz("America/Toronto").key == "America/Toronto"
    monkeypatch.setenv("HOMELAB_MAINT_TZ", "Europe/Paris")
    assert reports.resolve_tz("not/a-zone").key == "Europe/Paris"           # bad config falls through to the next source
    monkeypatch.delenv("HOMELAB_MAINT_TZ")
    assert reports.resolve_tz("../../etc/passwd") is not None               # path-like names never raise
    assert reports.tz_name(reports.resolve_tz("UTC")) == "UTC"


def test_parse_ts_accepts_epoch_iso_offsets_and_naive_local_strings():
    assert reports.parse_ts(1790904804.5, TZ) == 1790904804.5
    assert reports.parse_ts("2026-10-01T21:45:19-0400", TZ) == at(2026, 10, 1, 21, 45, TZ) + 19
    assert reports.parse_ts("2026-10-01 21:45:19", TZ) == at(2026, 10, 1, 21, 45) + 19      # naive: in the report zone
    assert reports.parse_ts("2026-10-01T01:45:19Z", TZ) == reports.parse_ts("2026-09-30T21:45:19-04:00", TZ)
    assert reports.parse_ts("garbage", TZ) is None and reports.parse_ts(True, TZ) is None and reports.parse_ts(None, TZ) is None


# =========================================================================== the health score
P_DAY = 96 * 15


def score(**kw):
    return reports.health_score(period_min=kw.pop("period_min", P_DAY), **kw)


def test_score_documented_examples():
    assert score()["score"] == 100 and score()["grade"] == "A"
    assert score(warn_min=P_DAY)["score"] == 75 and score(warn_min=P_DAY)["grade"] == "C"
    assert score(crit_min=360)["score"] == 85 and score(crit_min=360)["grade"] == "B"        # 6 h of crit in a day
    assert score(crit_min=P_DAY)["score"] == 40 and score(crit_min=P_DAY)["grade"] == "F"
    assert score(blind_min=P_DAY)["score"] == 65


def test_score_terms_and_caps():
    d = score(open_incidents=[1, 2, 3], backups_failed=1, backups_stale=1, stale_checks=2)
    assert d["deductions"] == {"time": 0.0, "incidents": 17.0, "backups": 20.0, "stale_checks": 6.0, "open": 0.0}
    assert d["score"] == 57
    assert score(open_incidents=[1] * 9)["deductions"]["incidents"] == 20.0           # capped
    assert score(backups_failed=9)["deductions"]["backups"] == 30.0
    assert score(stale_checks=99)["deductions"]["stale_checks"] == 15.0
    assert score(open_incidents=["bad", None])["deductions"]["incidents"] == 4.0       # unknown severity = sev3


def test_score_is_bounded_and_integer_for_any_input():
    nasty = [0, 1, 15, 1440, 10 ** 9, -5, float("nan"), float("inf"), None, "x", True]
    for c, w, b in itertools.product(nasty, repeat=3):
        for p in (0, 1, 1440, -3, 10 ** 6):
            s = reports.health_score(period_min=p, crit_min=c, warn_min=w, blind_min=b, backups_failed=9, stale_checks=9,
                                     open_incidents=[1, 1, 1])
            assert isinstance(s["score"], int) and 0 <= s["score"] <= 100
            assert s["grade"] in "ABCDF"


def test_score_never_increases_when_anything_gets_worse():
    steps = [0, 15, 60, 240, 720, 1440, 3000]
    base = dict(open_incidents=[], backups_failed=0, backups_stale=0, stale_checks=0)
    for field in ("crit_min", "warn_min", "blind_min"):
        prev = 101
        for v in steps:
            s = reports.health_score(period_min=P_DAY, **{field: v}, **base)["score"]
            assert s <= prev
            prev = s
    for field, vals in (("backups_failed", range(0, 5)), ("backups_stale", range(0, 5)), ("stale_checks", range(0, 9))):
        prev = 101
        for v in vals:
            s = reports.health_score(period_min=P_DAY, **{field: v})["score"]
            assert s <= prev
            prev = s
    prev = 101
    for incs in ([], [3], [3, 3], [2, 3], [2, 2, 3], [1, 2, 3], [1, 1, 1]):
        s = reports.health_score(period_min=P_DAY, open_incidents=incs)["score"]
        assert s <= prev
        prev = s


def test_moving_time_from_ok_to_warn_to_blind_to_crit_lowers_the_score_step_by_step():
    for n in (360, 600, 1440):
        ok, warn, blind, crit = score()["score"], score(warn_min=n)["score"], score(blind_min=n)["score"], score(crit_min=n)["score"]
        assert ok > warn > blind > crit     # a critical minute costs more than a blind one, which costs more than a warning one


def test_any_problem_at_all_costs_at_least_a_point_so_100_means_nothing_was_wrong():
    assert score()["score"] == 100
    for kw in ({"warn_min": 15}, {"crit_min": 15}, {"blind_min": 15}, {"stale_checks": 1}, {"backups_stale": 1}, {"open_incidents": [3]}):
        assert score(**kw)["score"] < 100, kw
    assert score(warn_min=15)["grade"] == "A"                     # ... but a quarter of an hour of warning is still an A


def test_score_is_deterministic_and_order_independent():
    a = reports.health_score(period_min=P_DAY, crit_min=45, warn_min=120, open_incidents=[3, 2], stale_checks=1)
    b = reports.health_score(period_min=P_DAY, crit_min=45, warn_min=120, open_incidents=[2, 3], stale_checks=1)
    assert a == b == reports.health_score(period_min=P_DAY, crit_min=45, warn_min=120, open_incidents=(3, 2), stale_checks=1)


def test_grade_thresholds():
    assert [reports.grade_for(s) for s in (100, 90, 89, 80, 79, 65, 64, 50, 49, 0)] == list("AABBCCDDFF")
    assert reports.grade_for(None) == "n/a"


# =========================================================================== analysis units on small histories
def rec(t, task_, status="ok", **m):
    return reports.Rec(t, task_, status, reports.LEVEL[status], m)


def inputs_of(recs, first_t=None, status=None, **kw):
    return reports.Inputs(status=status, recs=recs, first_t=first_t if first_t is not None else (recs[0].t if recs else None), **kw)


def day_period(d=date(2026, 10, 6)):
    return reports.period_for("daily", at(d.year, d.month, d.day + 1, 7, 30), TZ)


def full_day(per, fn, step=900, tasks=("disk_forecast", "failed_units")):
    out, t = [], per.start + 20
    while t < per.end:
        for n in tasks:
            out.append(rec(t, n, fn(n, t)))
        t += step
    return out


def test_minutes_come_from_15_minute_slots_of_the_worst_alerting_check():
    per = day_period()
    recs = full_day(per, lambda n, t: "warn" if n == "disk_forecast" and per.start + 3600 <= t < per.start + 7200 else
                    "crit" if n == "failed_units" and per.start + 7200 <= t < per.start + 7200 + 1800 else "ok")
    c = reports.analyse_checks(inputs_of(recs), per, TZ, set())
    assert (c["warn_min"], c["crit_min"], c["blind_min"], c["ok_min"]) == (60, 30, 0, 1440 - 90)
    assert c["worst"] == "crit" and c["observed_pct"] == 100.0 and c["time_ok_pct"] == pytest.approx(100 * 1350 / 1440, abs=0.1)
    assert c["ok_pct_by_task"]["disk_forecast"] == pytest.approx(100 * 92 / 96, abs=0.1)


def test_informational_checks_never_colour_a_slot_but_keep_their_own_ok_percentage():
    per = day_period()
    recs = full_day(per, lambda n, t: "warn" if n == "docker_df" else "ok", tasks=("docker_df", "disk_forecast"))
    c = reports.analyse_checks(inputs_of(recs), per, TZ, {"docker_df"})
    assert c["warn_min"] == 0 and c["worst"] == "ok" and c["episodes"] == {}
    assert c["ok_pct_by_task"]["docker_df"] == 0.0 and c["ok_pct_by_task"]["disk_forecast"] == 100.0


def test_missing_stretch_is_blind_not_ok():
    per = day_period()
    recs = [r for r in full_day(per, lambda n, t: "ok") if not per.start + 6 * 3600 <= r.t < per.start + 9 * 3600]
    c = reports.analyse_checks(inputs_of(recs, first_t=per.start - 5000), per, TZ, set())
    assert c["blind_min"] == 180 and c["observed_pct"] == pytest.approx(100 * (96 - 12) / 96, abs=0.1)
    assert reports.health_score(period_min=c["n_slots"] * 15, blind_min=c["blind_min"])["score"] < 100


def test_new_install_is_clipped_to_the_first_record_and_marked_provisional():
    per = day_period()
    start = per.start + 18 * 3600                     # history starts 18:00: 6 h of a 24 h day
    recs = [r for r in full_day(per, lambda n, t: "ok") if r.t >= start]
    c = reports.analyse_checks(inputs_of(recs, first_t=start + 20), per, TZ, set())
    assert c["n_slots"] == 24 and c["blind_min"] == 0 and c["observed_pct"] == 25.0
    inp = inputs_of(recs, first_t=start + 20, status={"tasks": {}})
    doc_ = reports.build_report("daily", NOW_DAILY, inp, TZ)
    assert doc_["health"]["provisional"] is True and doc_["health"]["score"] == 100
    assert any("Provisional" in n for n in doc_["notes"]) and "history starts" in doc_["highlights"][0]
    assert doc_["headline"].startswith("A (100, early data)")


def test_under_one_hour_of_data_gives_no_score_and_says_so():
    per = day_period()
    recs = [r for r in full_day(per, lambda n, t: "ok") if r.t >= per.end - 1800]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}), TZ)
    assert d["health"]["score"] is None and d["health"]["grade"] == "n/a" and not d["health"]["provisional"]
    assert d["headline"] == "Collecting data: 30 min of history for Tue 6 Oct 2026"
    assert d["highlights"][0] == ("Only about 30 min of check history exists for the day (2% of it): "
                                  "too little to give a score, still collecting data.")
    assert any(n.startswith("Too little check history to score the day") for n in d["notes"])
    assert d["digest_text"].startswith("homelab-maint daily 2026-10-06 (Tue 6 Oct 2026): health not scored yet (collecting data).")


def test_under_a_tenth_of_the_period_is_not_scored_but_over_it_is_provisional():
    """A new install's first week: 12 h of data must not be graded as 'the week'; 2 days of it can be, flagged provisional."""
    now = NOW_WEEKLY
    per = reports.period_for("weekly", now, TZ)

    def week_with(hours):
        recs, t = [], per.end - hours * 3600
        while t < per.end:
            recs.append(rec(t + 20, "disk_forecast", "ok"))
            t += 900
        return reports.build_report("weekly", now, inputs_of(recs, status={"tasks": {}}), TZ)
    short = week_with(12)                                          # 7.1 % of the week
    assert short["health"]["score"] is None and short["health"]["coverage_pct"] == 7.1
    assert short["headline"].startswith("Collecting data: 12 h of history") and "10%" in " ".join(short["notes"])
    longer = week_with(48)                                         # 28.6 %
    assert longer["health"]["score"] == 100 and longer["health"]["provisional"] is True and longer["headline"].startswith("A (100, early data)")


def test_no_history_at_all_means_no_score_and_an_explicit_statement():
    d = reports.build_report("daily", NOW_DAILY, reports.Inputs(), TZ)
    assert d["health"]["score"] is None and d["health"]["worst_status"] == "unknown"
    assert d["headline"] == "No data recorded for Tue 6 Oct 2026"
    assert d["highlights"][0].startswith("No health-check results were recorded")
    for n in ("history.jsonl", "status.json", "audit log", "incident ledger"):
        assert any(n in x for x in d["notes"])
    assert d["spikes"]["handled_without_harm"] is None and d["incidents"]["available"] is False


def test_episodes_are_clipped_to_the_period_and_flag_carry_over_and_open_ends():
    per = day_period()
    t = per.start - 3 * 3600
    recs = []
    while t < per.end:                                 # warn from 3 h BEFORE the period until 02:00, then ok, then warn from 22:00 to the end
        warn = t < per.start + 2 * 3600 or t >= per.start + 22 * 3600
        recs.append(rec(t + 20, "disk_forecast", "warn" if warn else "ok"))
        t += 900
    eps = reports._episodes(recs, per)
    assert len(eps) == 2
    assert eps[0]["before"] is True and eps[0]["start"] == per.start and eps[0]["open"] is False and eps[0]["end"] == pytest.approx(per.start + 2 * 3600 + 20, abs=1)
    assert eps[1]["open"] is True and eps[1]["before"] is False and eps[1]["end"] == per.end


def test_stale_checks_are_judged_at_the_period_end():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast", "failed_units"))
    recs = [r for r in recs if not (r.task == "failed_units" and r.t > per.end - 3 * 3600)]       # silent for the last 3 h
    status = {"tasks": {"disk_forecast": {"tier": "check", "last_run": per.end + 100}, "failed_units": {"tier": "check", "last_run": per.end + 100},
                        "docker_cache": {"tier": "daily", "last_run": per.end - 20 * 3600},
                        "never_ran": {"tier": "check"}, "unknown_tier": {"last_run": 1}}}
    c = reports.analyse_checks(inputs_of(recs, status=status), per, TZ, set())
    assert c["stale"] == ["failed_units"]              # daily task 20 h old is fine; no-record tasks are not guessed at


def test_a_check_that_stopped_reporting_is_said_to_be_unknown_not_healthy():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast", "failed_units"))
    recs = [r for r in recs if not (r.task == "failed_units" and r.t > per.end - 3 * 3600)]
    status = {"tasks": {"disk_forecast": {"tier": "check", "last_run": per.end + 100}, "failed_units": {"tier": "check", "last_run": per.end + 100}}}
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), status=status), TZ)
    assert "1 check had stopped reporting by the end of the day (failed_units): its state is unknown, not healthy." in d["highlights"]
    assert d["health"]["deductions"]["stale_checks"] == 3.0 and any("stopped reporting (failed_units)" in r for r in d["capacity"]["recommendations"])


def test_failed_backup_is_taken_from_the_last_result_in_the_period():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [rec(per.end - 600, "backup_freshness", "crit", failed=2), rec(per.start + 600, "backup_freshness", "ok", failed=0)]
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t)), per, TZ, set())
    assert c["backups"] == {"failed": 2, "stale": 0, "seen": True, "peak_failed": 2}
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), status={"tasks": {}}), TZ)
    assert d["health"]["deductions"]["backups"] == 30.0


def test_per_day_buckets_follow_local_dates_and_a_dst_day_has_25_hours_of_slots():
    now = at(2026, 11, 2, 7, 45)
    per = reports.period_for("weekly", now, TZ)
    recs, t = [], per.start + 20
    while t < per.end:
        recs.append(rec(t, "disk_forecast", "ok"))
        t += 900
    c = reports.analyse_checks(inputs_of(recs), per, TZ, set())
    assert c["by_day"][date(2026, 11, 1)]["ok"] == 25 * 60 and c["by_day"][date(2026, 10, 31)]["ok"] == 24 * 60
    assert c["blind_min"] == 0 and c["ok_min"] == 169 * 60
    d = reports.build_report("weekly", now, inputs_of(recs, status={"tasks": {}}), TZ)
    by = {x["day"]: x for x in d["days"]}
    assert by["2026-11-01"]["hours"] == 25.0 and by["2026-10-31"]["hours"] == 24.0 and by["2026-11-01"]["worst"] == "ok"


def test_the_informational_flag_recorded_with_a_run_beats_the_tasks_current_default():
    per = day_period()
    recs = full_day(per, lambda n, t: "warn" if n == "orphan_report" else "ok", tasks=("orphan_report", "disk_forecast"))
    for r in recs:
        if r.task == "orphan_report":
            r.info = True                                  # this run said alert=False
    assert reports.analyse_checks(inputs_of(recs), per, TZ, set())["warn_min"] == 0
    for r in recs:
        if r.task == "orphan_report":
            r.info = False                                 # this run alerted, although the task is informational today
    c = reports.analyse_checks(inputs_of(recs), per, TZ, {"orphan_report"})
    assert c["warn_min"] == 1440 and "orphan_report" in c["episodes"]
    for r in recs:
        if r.task == "orphan_report":
            r.info = None                                  # not recorded: fall back to the status.json flag
    assert reports.analyse_checks(inputs_of(recs), per, TZ, {"orphan_report"})["warn_min"] == 0


def test_history_alert_flag_is_read_when_present(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.jl("history.jsonl", [{"t": SEP30 + 60, "kind": "task", "task": "a", "status": "warn", "alert": False, "metrics": {}},
                           {"t": SEP30 + 120, "kind": "task", "task": "b", "status": "warn", "alert": True, "metrics": {}},
                           {"t": SEP30 + 180, "kind": "task", "task": "c", "status": "warn", "metrics": {}}])
    recs, _disk, first = reports._scan_history(w.state / "history.jsonl", SEP30, OCT7)
    assert [(r.task, r.info) for r in recs] == [("a", True), ("b", False), ("c", None)] and first == SEP30 + 60


def test_a_daily_tasks_result_stands_until_its_next_run_not_for_one_slot():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs.append(rec(per.start - 16.5 * 3600, "docker_images", "warn"))            # yesterday's 07:30 run warned too: confirmed (2 runs in a row)
    recs.append(rec(per.start + 7.5 * 3600, "docker_images", "warn"))             # the 07:30 daily run warned; the next one is tomorrow
    status = {"tasks": {"disk_forecast": {"tier": "check"}, "docker_images": {"tier": "daily"}}}
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())
    assert c["warn_min"] == 990                                                    # 07:30 -> midnight, not 15 minutes
    ep = c["episodes"]["docker_images"][0]
    assert ep["start"] == per.start + 7.5 * 3600 and ep["end"] == per.end and ep["open"] is True
    # the same record from a check-tier task (or a task of unknown tier) is one slot
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status={"tasks": {}}), per, TZ, set())
    assert c["warn_min"] == 15


def test_a_daily_result_is_replaced_by_the_next_run_of_the_same_task():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [rec(per.start - 22 * 3600, "docker_images", "crit"),                 # the run before also failed: confirmed
             rec(per.start + 2 * 3600, "docker_images", "crit"), rec(per.start + 5 * 3600, "docker_images", "ok")]
    status = {"tasks": {"docker_images": {"tier": "daily"}}}
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())
    assert c["crit_min"] == 180 and c["episodes"]["docker_images"][0]["open"] is False


@pytest.mark.parametrize("zone", ["America/Toronto", "Europe/London", "Asia/Beirut", "America/Havana", "Australia/Lord_Howe", "Pacific/Chatham",
                                  "Asia/Kolkata", "UTC"])
def test_consecutive_periods_tile_exactly_in_every_kind_of_zone(zone):
    """No gap and no overlap between the report of one day and the next, DST changes at midnight and 30-minute shifts included."""
    tz = ZoneInfo(zone)
    t = datetime(2026, 1, 1, 12, tzinfo=tz).timestamp()
    prev_end, lengths = None, set()
    for i in range(366):
        p = reports.period_for("daily", t + i * 86400, tz)
        assert prev_end is None or p.start == prev_end
        assert 22 * 3600 <= p.end - p.start <= 26 * 3600
        lengths.add(p.end - p.start)
        prev_end = p.end
        w = reports.period_for("weekly", t + i * 86400, tz)
        assert len(w.days) == 7 and w.end == p.end and w.start == reports.period_for("daily", t + (i - 6) * 86400, tz).start
    assert (len(lengths) > 1) == (zone != "UTC" and zone != "Asia/Kolkata")


# =========================================================================== the full weekly report
TOP = ["schema", "id", "kind", "generated_at", "period", "headline", "health", "highlights", "actions", "incidents", "spikes",
       "capacity", "temperature", "slo", "upcoming", "notes", "pressure", "days", "digest_text"]


def test_weekly_report_has_the_specified_shape(doc):
    assert set(TOP) <= set(doc)
    assert doc["id"] == "2026-W41" and doc["kind"] == "weekly" and doc["generated_at"] == NOW_WEEKLY
    assert doc["period"]["start"] == SEP30 and doc["period"]["end"] == OCT7 and doc["period"]["tz"] == "America/Toronto"
    h = doc["health"]
    assert {"score", "grade", "worst_status", "time_ok_pct", "checks_ok_pct_by_task"} <= set(h)
    assert isinstance(h["score"], int) and h["grade"] in "ABCDF" and h["worst_status"] == "crit"
    assert set(doc["actions"]) >= {"freed_bytes", "count", "by_task", "notable"}
    assert set(doc["incidents"]) >= {"opened", "resolved", "open_now", "mttr_s", "list"}
    assert set(doc["spikes"]) >= {"count", "worst_level", "list", "handled_without_harm"}
    assert set(doc["capacity"]) >= {"mounts", "memory_baseline_gib", "recommendations"}
    assert set(doc["capacity"]["memory_baseline_gib"]) >= {"start", "end", "drift"}
    assert set(doc["temperature"]) >= {"cpu_avg", "cpu_max", "gpu_avg", "gpu_max", "ram_avg", "fan_note"}
    assert len(doc["days"]) == 7 and [d["dow"] for d in doc["days"]] == ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"]
    json.dumps(doc, allow_nan=False)


def test_text_fields_are_bounded_ascii_single_line(doc):
    assert len(doc["headline"]) <= 80 and len(doc["highlights"]) <= 8 and len(doc["digest_text"]) <= 600

    def walk(o):
        if isinstance(o, str):
            assert o.isascii() and "\n" not in o, o
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc)


def test_health_matches_the_scenario(doc):
    h = doc["health"]
    # disk warn Fri 14:15-Sat 06:30 = 16.25 h, memory warn 2x, services warn 2x, backups crit 2 h; slots are 15 minutes
    assert h["minutes"]["crit"] == 120 and h["minutes"]["blind"] == 0
    assert h["minutes"]["warn"] == 1050                                # 16.25 h disk + 30 min memory + 30 min + 15 min services (memory Fri is inside the disk warn)
    assert h["coverage_pct"] == 100.0 and h["provisional"] is False
    assert h["time_ok_pct"] == pytest.approx(100 * h["minutes"]["ok"] / (h["minutes"]["ok"] + h["minutes"]["warn"] + h["minutes"]["crit"]), abs=0.1)
    d = h["deductions"]
    assert d["time"] == pytest.approx((60 * 120 + 25 * h["minutes"]["warn"]) / (7 * 96 * 15), abs=0.1)
    assert d["incidents"] == 2.0                                     # the open sev3 incident
    assert d["backups"] == 0.0                                       # backups were back to ok at the end of the period
    assert 0 <= 100 - sum(d.values()) - h["score"] < 1.2                # rounded down; the deductions themselves are shown to 0.1
    assert h["checks_ok_pct_by_task"]["backup_freshness"] == pytest.approx(100 * (672 - 8) / 672, abs=0.2)
    assert h["checks_ok_pct_by_task"]["alert_path_health"] == 100.0


def test_highlights_read_like_a_sysadmin_note(doc):
    """The week's note, word for word: specific, ordered by importance, and always covering incidents, spikes and maintenance."""
    assert doc["highlights"] == [
        "Checks were healthy 88% of the observed week (warning 10%, critical 1%).",
        "Backups: critical for 2 h, from Sun 01:00, ok again at Sun 03:00.",
        "2 incidents opened, 1 resolved; mean time to resolve 2 h; still open: sev3 Disk space low on / (4 d 9 h).",
        "1 alert could not be delivered, so you may not have been paged.",
        "Disk space: warning for 16 h 15 min, from Fri 14:15, ok again at Sat 06:30.",
        "Services & containers: warning in 2 episodes, 45 min in total (longest 30 min, from Thu 09:00).",
        "2 load spikes; worst: level 3 (slow batch) at Fri 14:10 for 9 min, memory pressure, mostly from tunarr-host-net. "
        "Nothing was killed or restarted.",
        "Maintenance freed 18.4 GiB in 4 actions (docker build cache pruned 18.0 GiB, old snap revisions removed 418.0 MiB)."]


def test_headline_names_the_biggest_problem_and_the_freed_space(doc):
    assert doc["headline"] == "A (94): open incident: Disk space low on /; 2 spikes handled, 18.4 GiB freed"
    assert len(doc["headline"]) <= 80


def test_actions_totals_match_the_audit_trail_for_the_period_only(doc):
    a = doc["actions"]
    done = 18 * GIB + 200 * 2 ** 20 + 218 * 2 ** 20 + 0                      # the 5 GiB prune after the period must not count
    assert a["freed_bytes"] == done and a["count"] == 4
    by = {e["task"]: e for e in a["by_task"]}
    assert by["docker_cache"] == {"task": "docker_cache", "count": 1, "freed": 18 * GIB}
    assert by["snap_revisions"]["count"] == 2 and by["snap_revisions"]["freed"] == 418 * 2 ** 20
    assert a["failed"] == 1 and a["refused"] == 1 and a["dry_run"] == 1
    assert a["alerts_sent"] == 2 and a["alerts_failed"] == 1
    assert [e["task"] for e in a["by_task"]][:2] == ["docker_cache", "snap_revisions"]       # biggest first
    assert a["would_free"] and a["would_free"][0]["human"] == "10.0 GiB"
    assert a["audit_available"] is True


def test_notable_merges_journal_changes_audit_and_failures(doc):
    titles = [n["title"] for n in doc["actions"]["notable"]]
    assert "Replaced the CPU cooler fan" in titles
    assert any(t.startswith("Container memory ceilings applied: config") for t in titles)
    assert "Docker build cache pruned" in titles and "Old snap revisions removed" in titles
    assert any(t.startswith("FAILED: docker_images") for t in titles)
    assert all(set(n) >= {"ts", "title", "detail"} for n in doc["actions"]["notable"])
    assert [n["ts"] for n in doc["actions"]["notable"]] == sorted((n["ts"] for n in doc["actions"]["notable"]), reverse=True)
    ch = next(n for n in doc["actions"]["notable"] if n["title"].startswith("Container memory ceilings"))
    assert "(verified)" in ch["detail"]
    assert not any("notify" in n["title"].lower() for n in doc["actions"]["notable"])        # alerts are counted, not listed
    assert not any("Username and Password" in json.dumps(n) for n in doc["actions"]["notable"])


def test_incidents_section(doc):
    i = doc["incidents"]
    assert i["available"] is True and i["opened"] == 2 and i["resolved"] == 1 and i["open_now"] == 1
    assert i["mttr_s"] == 7200
    rows = {r["id"]: r for r in i["list"]}
    assert set(rows) == {"inc-0007", "inc-0006"}                                       # the September incident is out of period
    assert rows["inc-0007"]["resolved"] is False and rows["inc-0007"]["severity"] == "sev3"
    assert rows["inc-0006"]["resolved"] is True and rows["inc-0006"]["duration_s"] == 7200
    assert i["list"][0]["id"] == "inc-0007"                                            # open ones first
    assert "ghp_" not in json.dumps(doc)


def test_correlated_incidents_are_counted_once_under_their_parent(inputs):
    t = at(2026, 10, 3, 10, 0)
    snap = {"open": [{"id": "inc-1", "task": "failed_units", "title": "Services down", "severity": "sev1", "since": t, "parent": None},
                     {"id": "inc-2", "task": "disk_forecast", "title": "Disk space low", "severity": "sev2", "since": t + 300, "parent": "inc-1"},
                     {"id": "inc-3", "task": "backup_freshness", "title": "Backups late", "severity": "sev3", "since": t + 600, "parent": "inc-1"},
                     {"id": "inc-9", "task": "x", "title": "Orphaned child", "severity": "sev3", "since": t, "parent": "inc-gone"}],
            "recent": []}
    d = rebuild(dataclasses.replace(inputs, incidents=snap))
    i = d["incidents"]
    assert i["opened"] == 2 and i["open_now"] == 4                      # the group, plus a child whose parent is out of the snapshot
    row = next(r for r in i["list"] if r["id"] == "inc-1")
    assert row["related"] == 2 and row["severity"] == "sev1"
    assert d["health"]["deductions"]["incidents"] == 12.0                # sev1 10 + sev3 2: not 10 + 5 + 2 + 2
    assert any("sev1 Services down (3 d 14 h, 2 related check(s))" in h for h in d["highlights"])


def test_incident_durations_are_as_of_the_end_of_the_period(inputs):
    snap = {"open": [{"id": "inc-1", "task": "x", "title": "Old and open", "severity": "sev3", "since": SEP30 + 3600, "duration_s": 99 * 86400}],
            "recent": [{"id": "inc-2", "task": "y", "title": "Closed later", "severity": "sev3", "since": OCT7 - 7200, "resolved_at": OCT7 + 3600,
                        "mttr_s": 10800}]}
    i = rebuild(dataclasses.replace(inputs, incidents=snap))["incidents"]
    by = {r["id"]: r for r in i["list"]}
    assert by["inc-1"]["duration_s"] == OCT7 - (SEP30 + 3600)            # not the snapshot's 99 days
    assert by["inc-2"]["duration_s"] == 7200 and by["inc-2"]["resolved"] is False       # resolved after the period ended
    assert i["opened"] == 2 and i["resolved"] == 0


def test_spikes_section_folds_the_ledger_to_one_event_per_spike(doc):
    """pressure_state appends a line per tick while a spike is open plus a closing line: 2 spikes = 2 events, not 5."""
    s = doc["spikes"]
    assert s["count"] == 2 and s["worst_level"] == 3 and s["data"] == "ok" and s["handled_without_harm"] is True
    first = next(e for e in s["list"] if e["level"] == 3)
    assert first["duration_s"] == 540 and first["contributors"] == ["tunarr-host-net (P2, 5.1 GiB, 240% cpu)"]
    assert first["outcome"] == "handled: reclaimed 1 idle item(s), nothing killed"          # the ledger's own sentence
    assert first["resource"] == "memory" and first["psi_mem"] == 12.0 and first["ongoing"] is False
    second = next(e for e in s["list"] if e["level"] == 2)
    assert second["outcome"] == "resolved by itself, nothing killed" and second["duration_s"] == 1800
    assert [e["t"] for e in s["list"]] == sorted((e["t"] for e in s["list"]), reverse=True)


def test_spike_ledger_fold_keeps_the_closed_line_and_survives_odd_rows():
    rows = reports._spike_ledger([{"id": 1, "state": "open", "x": 1}, {"id": 1, "state": "closed", "x": 2}, {"id": 1, "state": "open", "x": 3},
                                  {"id": 2, "state": "open", "x": 4}, {"x": 5}, {"x": 6}, {"id": None, "x": 7}])
    assert sorted(r["x"] for r in rows) == [2, 4, 5, 6, 7]        # spike 1 -> its closed line, spike 2 -> open, three id-less rows stay separate


def test_a_spike_still_open_at_the_end_of_the_period_says_so(tmp_path, monkeypatch):
    per = day_period()
    t0 = per.end - 1500
    ledger = spike_ledger(t0, 900, 3, {"io": 3}, {"io_some60": 70.0}, [{"name": "immich_server", "class": "P1", "anon_gib": 2.0, "cpu_pct": 300.0}], "x")[:-1]
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}, spikes=[{**r, "_t": r["t"]} for r in ledger], audit=[]), TZ)
    ev = d["spikes"]["list"][0]
    assert d["spikes"]["count"] == 1 and ev["ongoing"] is True and ev["resource"] == "disk I/O"
    assert ev["outcome"] == "still in progress at the end of the period"
    assert any("still going" in h for h in d["highlights"])


def test_a_restart_recorded_in_the_ledger_is_harm_even_without_an_audit_row():
    t0 = at(2026, 10, 6, 14, 10)
    ledger = spike_ledger(t0, 600, 4, {"mem": 4}, {"mem_full60": 20.0}, [{"name": "tunarr-host-net", "class": "P2", "anon_gib": 8.0, "cpu_pct": 0.0}],
                          "handled: restarted tunarr-host-net", nothing_killed=False, restarted=["tunarr-host-net"])
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}, audit=[], spikes=[{**r, "_t": r["t"]} for r in ledger]), TZ)
    assert d["spikes"]["handled_without_harm"] is False
    assert d["spikes"]["list"][0]["outcome"] == "handled: restarted tunarr-host-net"
    assert any(h.endswith("A workload was affected: handled: restarted tunarr-host-net.") for h in d["highlights"])


def test_only_restart_stop_and_kill_actions_count_as_harm():
    mk = lambda action, **k: {"task": "x", "action": action, **k}                    # noqa: E731
    assert all(reports._harm_action(mk(a)) for a in ("docker restart", "docker stop", "kill gradle daemon", "reboot",
                                                     "docker-restart", "sigterm-daemon", "sigkill", "terminate process", "docker rm", "docker-rm"))
    assert not any(reports._harm_action(mk(a)) for a in ("ollama-unload", "comfyui-free", "docker-update-throttle", "snap remove",
                                                         "docker image rm", "docker-image-rm", "docker builder prune", "rm"))
    assert reports._harm_action(mk("anything", rung="L4")) and reports._harm_action(mk("anything", rung="L5"))
    assert not reports._harm_action(mk("anything", rung="L2"))


def test_capacity_trend_and_forecast_for_the_shrinking_root_disk(doc):
    root = next(m for m in doc["capacity"]["mounts"] if m["mount"] == "/")
    assert root["trend_gib_per_day"] == pytest.approx(2.4, abs=0.05)
    assert root["free_h"] == "196.4 GiB" and root["note"] == "filling"                # the free space at the END of the period (history), not the latest status
    assert root["days_to_full"] == pytest.approx(196.4 / 2.4, abs=0.5)
    assert doc["capacity"]["mounts"][0]["mount"] == "/"                              # soonest-to-full first
    stable = next(m for m in doc["capacity"]["mounts"] if m["mount"] == "/media/SandiskSSD")
    assert stable["days_to_full"] is None and stable["note"] == "free space is stable or growing" and stable["trend_gib_per_day"] == 0.0


def test_memory_baseline_drift_and_growers(doc):
    b = doc["capacity"]["memory_baseline_gib"]
    assert b["metric"] == "container_anon" and b["start"] is not None and b["drift"] == pytest.approx(b["end"] - b["start"], abs=0.11)
    assert b["drift"] > 1.0
    g = doc["capacity"]["memory_growers"]
    assert g[0]["name"] == "tunarr-host-net" and g[0]["start_gib"] == 2.0 and g[0]["end_gib"] == 3.4 and g[0]["delta_gib"] == 1.4
    assert all(x["name"] != "kavita" for x in g)


def test_temperature_section_covers_only_the_hours_in_the_ring(doc):
    t = doc["temperature"]
    assert t["gpu_avg"] == 41.0 and t["gpu_max"] == 46.0 and t["ram_avg"] == 38.0 and t["nvme_avg"] == 47.0
    assert 52 <= t["cpu_avg"] <= 58 and t["cpu_max"] > t["cpu_avg"]
    assert 100 < t["hours_covered"] < t["hours_expected"] == 168
    assert "CPU fan 1180 rpm avg" in t["fan_note"] and "case fans 800 rpm avg" in t["fan_note"] and "GPU fan up to 25%" in t["fan_note"]
    assert any("Temperatures cover" in n for n in doc["notes"])
    assert not any(h.startswith("Temperatures") for h in doc["highlights"])            # a crowded week: 8 lines, thermals are in their own section


def test_slo_upcoming_and_previous_week_comparison(doc, week):
    assert doc["slo"] == [{"name": "Host health", "availability_pct": 99.1, "status": "ok"},
                          {"name": "Backups", "availability_pct": 97.0, "status": "at_risk"}]
    assert doc["upcoming"][0] == {"when": "Wed 7 Oct 07:45", "what": "Weekly review"}
    assert {"when": "Thu 8 Oct 07:30", "what": "Daily cleanup"} in doc["upcoming"]
    assert "previous" not in doc["health"]


def test_recommendations_are_specific_and_actionable(doc):
    rec = doc["capacity"]["recommendations"]
    assert 1 <= len(rec) <= 8
    joined = "\n".join(rec)
    assert rec == [
        "Root disk (/): 196.4 GiB free (88% used), growing 2.4 GiB/day, about 82 days to full. Docker holds 28.6 GiB build cache and "
        "14.0 GiB reclaimable images (docker_cache is report-only; set mode = apply to prune).",
        "Container memory baseline rose from 11.7 to 14.7 GiB (+3.0); biggest growers: tunarr-host-net +1.4 GiB. Check for a leak; "
        "the caps task can bound a container.",
        "1 alert failed to send: fix the alert bridge or you will not be paged."]
    assert all(len(r) <= 260 for r in rec)


def test_digest_is_short_ascii_and_has_the_essentials(doc):
    d = doc["digest_text"]
    assert d.startswith("homelab-maint weekly 2026-W41") and "health " in d and len(d) <= 600 and d.isascii()
    assert "Incidents: 2 opened, 1 open now." in d and "Spikes: 2, nothing killed." in d


def test_pressure_block_summarises_the_15_minute_checks(doc):
    p = doc["pressure"]
    assert p["mem_full60_max"] == 12.0 and p["io_some60_avg"] == 8.0 and p["mem_available_min_gib"] == 61.0 and p["oom_kills"] == 0
    assert p["pressure_state_seen"] is False


def test_pick_reserves_a_line_for_the_overall_incident_spike_and_maintenance_topics_in_a_crowded_week():
    crowd = [(0.0 + i / 10, "episode", f"problem {i}") for i in range(12)]
    H = crowd + [(-1, "overall", "overall"), (3.4, "incident", "incidents"), (3.5, "spike", "spikes"), (4, "maint", "maintenance"),
                 (5.5, "cap", "capacity")]
    picked = [t for _p, _c, t in reports._pick(H)]
    assert len(picked) == reports.MAX_HIGHLIGHTS == 8
    assert {"overall", "incidents", "spikes", "maintenance"} <= set(picked)
    assert picked == ["overall", "problem 0", "problem 1", "problem 2", "problem 3", "incidents", "spikes", "maintenance"]
    assert [t for _p, _c, t in reports._pick(H[-5:])] == ["overall", "incidents", "spikes", "maintenance", "capacity"]


def mem_io(io, mem_full):
    per = day_period()
    recs = [rec(t, "memory_health", "ok", psi_io_some60=io, psi_mem_full60=mem_full, mem_available_gib=60.0)
            for t in (per.start + 3600 * k for k in range(1, 20))]
    recs += full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    return reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), status={"tasks": {}}), TZ)


def test_high_disk_pressure_is_called_disk_saturation_only_when_memory_is_fine():
    fine = mem_io(80.0, 0.4)
    line = next(h for h in fine["highlights"] if h.startswith("Disk I/O pressure was high"))
    assert line == ("Disk I/O pressure was high: tasks waited on disk 80% of the time on average (peak 80%) while memory stall peaked at 0.4%; "
                    "this is disk saturation, not a RAM shortage.")
    assert any(r.startswith("Disk I/O pressure averaged 80% (peak 80%): something keeps the disks busy; start with `iotop -oPa`") for r in fine["capacity"]["recommendations"])
    both = mem_io(80.0, 12.0)
    line = next(h for h in both["highlights"] if h.startswith("Disk I/O pressure was high"))
    assert "while memory stall peaked at 12.0%." in line and "not a RAM shortage" not in line
    assert not any(h.startswith("Disk I/O pressure") for h in mem_io(10.0, 0.4)["highlights"])


def test_a_few_missing_ring_hours_are_normal_and_not_a_caveat(tmp_path, monkeypatch):
    """The 168-hour ring cannot hold the first hours of a week that ended this morning, so 160 of 168 h is full coverage in practice."""
    w = World(tmp_path, monkeypatch)
    w.history(SEP30, OCT7)
    w.flush_history()
    w.ring(SEP30 + 8 * 3600, OCT7, lambda h: {"cpu_temp": 50.0})
    t = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert t["temperature"]["hours_covered"] == 160 and not any("Temperatures cover" in n for n in t["notes"])
    w.ring(SEP30 + 48 * 3600, OCT7, lambda h: {"cpu_temp": 50.0})
    t = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert t["temperature"]["hours_covered"] == 120 and "Temperatures cover 120 of 168 h (the sampler ring keeps 7 days)." in t["notes"]


def test_the_public_incident_snapshot_wins_over_the_state_copy(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.history(SEP30, OCT7)
    w.flush_history()
    w.js("incidents.json", {"open": [{"id": "old", "title": "state copy", "severity": "sev3", "since": SEP30 + 3600}], "recent": []})
    assert reports.generate("weekly", NOW_WEEKLY, TZ, write=False)["incidents"]["list"][0]["title"] == "state copy"
    w.js("public/incidents.json", {"open": [{"id": "new", "title": "public copy", "severity": "sev2", "since": SEP30 + 3600}], "recent": []})
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert [r["title"] for r in d["incidents"]["list"]] == ["public copy"]


def test_a_finished_bulk_copy_is_not_forecast_as_a_filling_disk(tmp_path, monkeypatch):
    """A 600 GiB step three days ago followed by flat free space is an event, not a trend (same rule as the disk_forecast check)."""
    w = World(tmp_path, monkeypatch)
    step_at = SEP30 + 3 * 86400
    w.history(SEP30 - 3 * 86400, NOW_WEEKLY, disk_fn=lambda m, t: SIZES[m] * 0.6 - (600 * GIB if t > step_at else 0) if m == "/" else SIZES[m] * 0.4)
    w.flush_history()
    w.make_status(NOW_WEEKLY)
    w.status["tasks"]["disk_forecast"]["metrics"]["mounts"][0].update(used_pct=63.0)
    w.write_status()
    root = next(m for m in reports.generate("weekly", NOW_WEEKLY, TZ, write=False)["capacity"]["mounts"] if m["mount"] == "/")
    assert root["days_to_full"] is None and root["note"] == "free space is stable or growing" and root["trend_gib_per_day"] == 0.0


def test_a_disk_that_is_filling_slowly_is_reported_without_a_date(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.history(SEP30 - 3 * 86400, NOW_WEEKLY, disk_fn=lambda m, t: SIZES[m] * 0.6 - 0.01 * GIB * (t - SEP30) / 86400 if m == "/" else SIZES[m] * 0.4)
    w.flush_history()
    w.make_status(NOW_WEEKLY)
    w.write_status()
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    root = next(m for m in d["capacity"]["mounts"] if m["mount"] == "/")
    assert root["days_to_full"] is None and root["note"] == "filling, no full-disk date within 10 years" and root["trend_gib_per_day"] > 0


# =========================================================================== honesty when inputs are missing
MISSING = [("audit", "audit log"), ("incidents", "incident ledger"), ("ring", "metrics ring"), ("spikes", None), ("status", "status.json")]


@pytest.mark.parametrize("field,label", MISSING)
def test_each_optional_input_can_be_missing(inputs, field, label):
    d = rebuild(dataclasses.replace(inputs, **{field: None}))
    assert d["health"]["score"] is not None
    if label:
        assert any(label in n for n in d["notes"]), d["notes"]
    assert 1 <= len(d["highlights"]) <= 8 and d["headline"] and d["digest_text"]


def test_missing_incident_ledger_is_never_reported_as_no_incidents(inputs):
    d = rebuild(dataclasses.replace(inputs, incidents=None))
    txt = " ".join(d["highlights"])
    assert "No incident ledger was found" in txt and "No incidents were opened" not in txt
    assert d["incidents"]["available"] is False and d["incidents"]["opened"] == 0
    assert "Incidents:" not in d["digest_text"]


def test_empty_incident_ledger_is_a_real_zero(inputs):
    d = rebuild(dataclasses.replace(inputs, incidents={"open": [], "recent": []}))
    assert "No incidents were opened during the week." in d["highlights"]
    assert d["incidents"] == {"opened": 0, "resolved": 0, "open_now": 0, "mttr_s": None, "list": [], "available": True}


def test_without_spike_data_nothing_is_claimed_about_spikes(inputs):
    inp = dataclasses.replace(inputs, spikes=None)
    d = rebuild(inp)
    s = d["spikes"]
    assert s["data"] == "missing" and s["handled_without_harm"] is None and s["count"] == 0
    assert any("Spike tracking has no data" in h and "memory stall peaked at 12.0%" in h for h in d["highlights"])
    assert not any("Nothing was killed" in h or "No load spikes" in h for h in d["highlights"])


def test_with_spike_tracking_running_and_no_spikes_that_is_a_real_zero():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)) + \
        [rec(t, "pressure_state", "ok", level=1) for t in (per.start + 3600, per.start + 7200)]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), spikes=[], audit=[], status={"tasks": {}}), TZ)
    assert d["spikes"]["data"] == "ok" and d["spikes"]["count"] == 0 and d["spikes"]["handled_without_harm"] is True
    assert d["spikes"]["worst_level"] == 1 and any("No load spikes were recorded (peak pressure level 1)" in h for h in d["highlights"])


def test_no_spikes_is_not_claimed_for_hours_pressure_state_did_not_watch():
    per = day_period()
    start = per.start + 9 * 3600 + 20
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)) + \
        [rec(start + 900 * i, "pressure_state", "ok", level=0) for i in range(30)]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), spikes=[], audit=[], status={"tasks": {}}), TZ)
    assert d["pressure"]["spike_tracking_since"] == start
    assert "No load spikes were recorded, but spike tracking only started Tue 09:00." in d["highlights"]
    # tracking that ran all along (records before the period) gets no caveat
    older = [rec(per.start - 3600, "pressure_state", "ok", level=0)] + recs
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(older, key=lambda r: r.t), spikes=[], audit=[], status={"tasks": {}}), TZ)
    assert d["pressure"]["spike_tracking_since"] is None and "No load spikes were recorded." in d["highlights"]


def test_without_an_audit_trail_a_spike_outcome_is_unverified(inputs):
    d = rebuild(dataclasses.replace(inputs, audit=None))
    assert d["spikes"]["handled_without_harm"] is None
    assert [e["outcome"] for e in d["spikes"]["list"]] == [
        "unverified: resolved by itself, nothing killed (no audit trail to check it against)",
        "unverified: handled: reclaimed 1 idle item(s), nothing killed (no audit trail to check it against)"]
    assert any(h.endswith("The outcome could not be verified (no audit trail).") for h in d["highlights"])
    assert any("cannot be listed" in h for h in d["highlights"])
    assert not any("Nothing was killed" in h for h in d["highlights"])


def test_without_an_audit_trail_and_without_a_ledger_verdict_the_outcome_is_unknown(inputs):
    sp = [{k: v for k, v in r.items() if k not in ("outcome", "nothing_killed")} for r in inputs.spikes]
    d = rebuild(dataclasses.replace(inputs, audit=None, spikes=sp))
    assert {e["outcome"] for e in d["spikes"]["list"]} == {"outcome unknown (no audit trail)"}


def test_a_restart_during_a_spike_is_harm_and_is_named(inputs):
    audit = inputs.audit + [{"t": at(2026, 10, 6, 14, 20), "task": "stuck_detector", "action": "docker restart", "target": "tunarr-host-net",
                             "bytes": 0, "o": "done", "raw": "done"}]
    d = rebuild(dataclasses.replace(inputs, audit=audit))
    assert d["spikes"]["handled_without_harm"] is False
    assert any("docker restart ran on tunarr-host-net" in e["outcome"] for e in d["spikes"]["list"])
    assert any(h.endswith("A workload was affected: docker restart ran on tunarr-host-net.") for h in d["highlights"])
    assert "nothing killed" not in d["digest_text"] and "Spikes: 2." in d["digest_text"]
    assert [e["harm"] for e in d["spikes"]["list"]] == [True, False]


def test_an_oom_kill_is_never_handled_without_harm(inputs):
    recs = [r for r in inputs.recs]
    recs.append(reports.Rec(at(2026, 10, 3, 3, 0), "memory_health", "warn", 1, {"oom_kills_delta": 2, "psi_mem_full60": 30.0}))
    recs.sort(key=lambda r: r.t)
    d = rebuild(dataclasses.replace(inputs, recs=recs))
    assert d["pressure"]["oom_kills"] == 2 and d["spikes"]["handled_without_harm"] is False
    assert any("OOM-killed 2 processes" in h for h in d["highlights"])


def test_missing_ring_says_temperatures_were_not_checked(inputs):
    d = rebuild(dataclasses.replace(inputs, ring=None))
    t = d["temperature"]
    assert t["hours_covered"] == 0 and t["cpu_avg"] is None and t["fan_note"] == "no temperature data in this period"
    assert "Missing input: metrics ring (temperatures)." in d["notes"]


def test_cleanup_that_did_not_run_is_not_reported_as_nothing_to_clean(inputs):
    cleaners = {n for n, _ in __import__("test_reports").CLEANERS}
    recs = [r for r in inputs.recs if r.task not in cleaners]
    d = rebuild(dataclasses.replace(inputs, recs=recs, audit=[]))
    assert any("cleanup tasks did not run" in h for h in d["highlights"])


def test_report_mode_cleaners_are_described_honestly(inputs):
    d = rebuild(dataclasses.replace(inputs, audit=[]))
    assert any(h.startswith("Cleanup is in report mode: nothing was deleted; 30.0 GiB is reclaimable right now.") for h in d["highlights"])
    assert any("All cleanup tasks are in report mode" in n for n in d["notes"])


def test_apply_mode_cleaners_that_found_nothing_say_so(inputs):
    st = copy.deepcopy(inputs.status)
    for n in ("docker_cache", "docker_images", "snap_revisions"):
        st["tasks"][n]["metrics"] = {"mode": "apply", "selected": 0}
    d = rebuild(dataclasses.replace(inputs, status=st, audit=[]))
    assert any("ran and found nothing to remove" in h for h in d["highlights"])
    assert not any("report mode" in n for n in d["notes"])


def test_kill_switch_is_mentioned(inputs):
    d = rebuild(dataclasses.replace(inputs, paused=True))
    assert any("PAUSE" in n for n in d["notes"])


def test_informational_check_warnings_are_noted_not_scored(doc):
    assert any(n.startswith("Docker disk is informational (never pages): build cache 28.6 GiB") for n in doc["notes"])
    assert "docker_df" not in {e for e in doc["health"]["checks_ok_pct_by_task"] if doc["health"]["checks_ok_pct_by_task"][e] is None}


# =========================================================================== damaged inputs
def test_garbage_in_the_optional_files_never_breaks_the_report(tmp_path, monkeypatch):
    w = build_week(tmp_path, monkeypatch)
    (w.state / "spikes.jsonl").write_text('not json\n[1,2]\n{"t": "x"}\n{"t": 1}\n\x00\n')
    (w.state / "changes.jsonl").write_text("{broken\n")
    (w.state / "maintenance-journal.jsonl").write_text('{"ts": "yesterday", "title": 5}\n')
    (w.state / "incidents.json").write_text("[]")
    (w.state / "metrics-ring.json").write_text('{"hours": [1, {"h": "x"}, {"h": 5, "n": 1, "sum": 3, "cnt": 1}]}')
    (w.state / "slo.json").write_text('{"objectives": [1, {"name": null}, {"name": "ok", "availability_pct": "n/a", "status": null}]}')
    (w.state / "public" / "routine.json").write_text('{"calendar": [1, {"date": "nope"}, {"date": "2026-10-08", "items": [3, {"title": "x"}]}]}')
    (w.log / "audit.jsonl").write_text('garbage\n{"ts": "bad"}\n{"ts": "2026-10-02T07:35:00-0400", "task": 5, "bytes": "many", "outcome": null}\n')
    st = json.loads((w.state / "status.json").read_text())
    st["tasks"]["disk_forecast"]["metrics"] = {"mounts": [None, {"mount": "/", "used_pct": "lots", "level": 5}]}
    st["reclaimed_log"] = [None, {"t": "x"}, {"t": SEP30 + 5, "task": "docker_cache", "bytes": "NaN"}]
    (w.state / "status.json").write_text(json.dumps(st))
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert d["health"]["score"] is not None and d["incidents"]["available"] is False
    assert not [n for n in d["notes"] if n.startswith("Section ")], d["notes"]
    assert d["actions"]["freed_bytes"] >= 0 and d["slo"] == [{"name": "ok", "availability_pct": None, "status": "unknown"}]


def test_randomly_damaged_inputs_never_break_the_report(tmp_path, monkeypatch):
    """Fuzz: truncate, overwrite and bit-flip every input file (seeded), 25 rounds. generate() must always return a valid report."""
    import random
    w = build_week(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in list(w.state.rglob("*")) + list(w.log.rglob("*")) if p.is_file()}
    rnd = random.Random(20261002)
    for _round in range(25):
        for p, raw in originals.items():
            data = bytearray(raw)
            kind = rnd.choice(("keep", "keep", "truncate", "garbage", "flip", "empty", "delete"))
            if kind == "truncate" and data:
                data = data[: rnd.randrange(len(data))]
            elif kind == "garbage":
                at_ = rnd.randrange(len(data) + 1)
                data[at_:at_] = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 60)))
            elif kind == "flip" and data:
                for _ in range(rnd.randrange(1, 20)):
                    data[rnd.randrange(len(data))] = rnd.randrange(256)
            elif kind == "empty":
                data = bytearray()
            if kind == "delete":
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(bytes(data))
        d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
        assert set(TOP) <= set(d) and len(d["highlights"]) <= 8 and len(d["digest_text"]) <= 600
        assert d["health"]["score"] is None or 0 <= d["health"]["score"] <= 100
        json.dumps(d, allow_nan=False)


JUNK = [None, 0, -1, 1.5, "x", "", [], {}, [1], {"a": 1}, True, 10 ** 30, "2026-10-01T00:00:00", "nope"]


def mutate(o, rnd, rate):
    """Replace random nodes of a decoded JSON document by random other JSON types."""
    if isinstance(o, dict):
        return {k: (rnd.choice(JUNK) if rnd.random() < rate else mutate(v, rnd, rate)) for k, v in o.items()}
    if isinstance(o, list):
        return [(rnd.choice(JUNK) if rnd.random() < rate else mutate(v, rnd, rate)) for v in o]
    return o


def test_wrong_json_types_inside_the_inputs_are_tolerated_by_every_section(tmp_path, monkeypatch):
    """Fuzz the structure, not the bytes: valid JSON with strings where lists belong etc. No section may fail (no 'Section ... unavailable')."""
    import random
    w = build_week(tmp_path, monkeypatch)
    files = [p for p in list(w.state.rglob("*.json")) + list(w.state.rglob("*.jsonl")) + list(w.log.rglob("*.jsonl"))
             if p.name not in ("history.jsonl", "samples.jsonl")]
    originals = {p: p.read_text() for p in files}
    assert len(files) >= 8
    for seed in range(20):
        rnd = random.Random(seed)
        for p, txt in originals.items():
            if p.suffix == ".json":
                p.write_text(json.dumps(mutate(json.loads(txt), rnd, 0.08)))
            else:
                p.write_text("\n".join(json.dumps(mutate(json.loads(ln), rnd, 0.15)) for ln in txt.splitlines() if ln.strip()) + "\n")
        for kind, now in (("weekly", NOW_WEEKLY), ("daily", NOW_DAILY)):
            d = reports.generate(kind, now, TZ, write=False)
            assert not [n for n in d["notes"] if n.startswith("Section ")], (seed, kind, d["notes"])
            json.dumps(d, allow_nan=False)


def test_truncated_and_binary_history_lines_are_skipped(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.history(SEP30 - 2 * 86400, NOW_WEEKLY)
    w.flush_history()
    with open(w.state / "history.jsonl", "ab") as f:
        f.write(b'{"t":1790904804.3,"kind":"task","task":"x","stat\n\xff\xfe\n{"t":')
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert d["health"]["score"] == 100 and d["health"]["coverage_pct"] == 100.0


SECTIONS = [("analyse_checks", "checks"), ("pressure_summary", "pressure"), ("summarize_actions", "actions"), ("summarize_incidents", "incidents"),
            ("summarize_spikes", "spikes"), ("capacity_section", "capacity"), ("temperature_section", "temperature"), ("slo_section", "slo"),
            ("upcoming_section", "upcoming"), ("recommendations", "recommendations"), ("build_highlights", "highlights")]


@pytest.mark.parametrize("fn,label", SECTIONS)
def test_a_failing_section_degrades_to_a_note_not_a_crash(inputs, monkeypatch, fn, label):
    """Each analyser breaking on its own leaves a complete, valid report with a note that names the section."""
    monkeypatch.setattr(reports, fn, lambda *a, **k: 1 / 0)
    d = rebuild(inputs)
    assert f"Section {label} unavailable (ZeroDivisionError)." in d["notes"]
    assert set(TOP) <= set(d) and d["headline"] and d["digest_text"]
    json.dumps(d, allow_nan=False)


def test_every_section_failing_at_once_still_gives_a_report(inputs, monkeypatch):
    for fn, _label in SECTIONS:
        monkeypatch.setattr(reports, fn, lambda *a, **k: 1 / 0)
    d = rebuild(inputs)
    assert d["health"]["score"] is None and d["highlights"] == [] and d["headline"] == "No data recorded for Wed 30 Sep - Tue 6 Oct 2026"
    assert len([n for n in d["notes"] if n.startswith("Section ")]) == len(SECTIONS)


# =========================================================================== secrets and redaction
SECRETS = ["ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter2hunter2", "AKIAABCDEFGHIJKLMNOP", "s3cr3t-t0ken-value-1234567890abcdef"]


def planted_week(tmp_path, monkeypatch):
    w = build_week(tmp_path, monkeypatch)
    w.audit([(at(2026, 10, 2, 7, 35), "docker_cache", "docker login", "https://user:hunter2hunter2@reg.example.com/v2/?token=" + SECRETS[3], 1, "done"),
             (at(2026, 10, 2, 7, 36), "retention", "rm", "/home/ohmz/StudioProjects/tunarr/.docker-data/tunarr/cache/subtitles/a/b.srt", 10, "done"),
             (at(2026, 10, 2, 7, 37), "notify", "send", "CRIT x password=hunter2hunter2", 0, "failed rc=1 AKIAABCDEFGHIJKLMNOP"),
             (at(2026, 10, 3, 7, 37), "docker_images", "docker image rm", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", 0, "failed: api_key=hunter2hunter2")])
    w.jl("maintenance-journal.jsonl", [{"ts": at(2026, 10, 3, 20), "title": "Rotated key AKIAABCDEFGHIJKLMNOP", "detail": "password: hunter2hunter2 for owner@example.com"}])
    st = json.loads((w.state / "status.json").read_text())
    st["tasks"]["failed_units"].update(status="warn", summary="warn: 1 unhealthy: app (Authorization: Bearer " + SECRETS[3] + ")")
    st["tasks"]["failed_units"]["tier"] = "check"
    w.js("status.json", st)
    w.eps.append(("failed_units", at(2026, 10, 6, 23, 0), OCT7 + 99999, "warn"))
    return w


@pytest.mark.parametrize("cleaner", ["publish", "fallback"])
def test_no_secret_reaches_the_report(tmp_path, monkeypatch, cleaner):
    """Both the shared publish.clean and the module's own conservative copy keep credentials, e-mail addresses and deep paths out."""
    w = planted_week(tmp_path, monkeypatch)
    if cleaner == "fallback":
        monkeypatch.setattr(reports, "_cleaner", lambda: reports._fallback_clean)
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    blob = json.dumps(d)
    for secret in SECRETS + ["owner@example.com", "user:hunter2", "Username and Password", "reg.example.com/v2/?"]:
        assert secret not in blob, (cleaner, secret)
    assert "cache/subtitles/a/b.srt" not in blob                              # /home/<user>/ paths are cut after the third component
    assert "/home/ohmz/StudioProjects/tunarr/.docker-data/..." in blob
    assert "[redacted]" in blob


def test_the_whole_document_is_scrubbed_even_when_a_section_forgets(doc):
    """_scrub is the safety net over everything build_report returns, whatever an analyser let through."""
    leaky = {"a": ["token=abcdefghijklmnop1234567890", {"b": "x" * 5000}], "c": float("nan"), "d": 1.23456789, "e": b"bytes", "f": "caf\u00e9"}
    out = reports._scrub(leaky, reports._cleaner())
    assert out["a"][0].endswith("[redacted]") and len(out["a"][1]["b"]) <= 700
    assert out["c"] is None and out["d"] == 1.2346 and out["e"].startswith("b'") and out["f"] == "caf?"


def test_pages_are_counted_in_a_plain_sentence():
    per = day_period()
    n = lambda o: {"t": per.start + 3600, "task": "notify", "action": "alert", "target": "", "bytes": 0, "o": o, "raw": ""}      # noqa: E731
    recs = full_day(per, lambda n_, t: "ok", tasks=("disk_forecast",))
    for sent, line in ((1, "1 alert was sent to you."), (3, "3 alerts were sent to you.")):
        d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}, audit=[n("sent")] * sent), TZ)
        assert line in d["highlights"] and d["actions"]["alerts_sent"] == sent
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}, audit=[n("sent"), n("failed")]), TZ)
    assert "1 alert could not be delivered, so you may not have been paged." in d["highlights"]
    assert d["actions"]["alerts_sent"] == 1 and d["actions"]["alerts_failed"] == 1 and d["actions"]["failed"] == 0      # a failed page is not a failed maintenance action


def test_notify_entries_contribute_counts_only(inputs):
    sent = [a for a in inputs.audit if a["task"] == "notify"]
    assert sent and all(a["target"] == "" and a["raw"] == "" and a["action"] == "alert" for a in sent)
    assert {a["o"] for a in sent} == {"sent", "failed"}


# =========================================================================== files: write, index, retention
def tiny_world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.history(at(2026, 9, 20), at(2026, 10, 9), tasks=CHECKS[:4])        # 19 quiet days of four checks: enough for every period used below
    w.flush_history()
    return w


def rdir(w):
    return w.state / "public" / "reports"


def test_generate_writes_report_and_index_with_the_right_modes(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    d = reports.generate("daily", NOW_DAILY, TZ)
    f = rdir(w) / "2026-10-06.json"
    assert json.loads(f.read_text()) == d
    assert stat.S_IMODE(f.stat().st_mode) == 0o644 and stat.S_IMODE(rdir(w).stat().st_mode) == 0o755
    assert stat.S_IMODE((rdir(w) / "index.json").stat().st_mode) == 0o644
    idx = json.loads((rdir(w) / "index.json").read_text())
    assert idx == [{"id": "2026-10-06", "kind": "daily", "period_start": at(2026, 10, 6), "period_end": at(2026, 10, 7),
                    "generated_at": NOW_DAILY, "headline": d["headline"],
                    "health": {"score": 100, "grade": "A", "worst_status": "ok"}}]
    assert sorted(p.name for p in rdir(w).iterdir()) == ["2026-10-06.json", "index.json"]
    assert (w.state / "reports.lock").exists()


def test_regenerating_is_idempotent_apart_from_generated_at(tmp_path, monkeypatch):
    tiny_world(tmp_path, monkeypatch)
    a = reports.generate("weekly", NOW_WEEKLY, TZ)
    b = reports.generate("weekly", NOW_WEEKLY + 600, TZ)
    assert {**a, "generated_at": 0, "digest_text": ""} == {**b, "generated_at": 0, "digest_text": ""}
    assert len(json.loads((tmp_path / "state" / "public" / "reports" / "index.json").read_text())) == 1       # one id, one entry, however often it is rebuilt


def test_index_is_newest_first_and_lists_both_kinds(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    for day in (3, 4, 5):
        reports.generate("daily", at(2026, 10, day, 7, 30), TZ)
    reports.generate("weekly", at(2026, 10, 7, 7, 45), TZ)
    idx = json.loads((rdir(w) / "index.json").read_text())
    assert [e["id"] for e in idx] == ["2026-W41", "2026-10-04", "2026-10-03", "2026-10-02"]       # the weekly ends Wed 00:00, the newest daily Tue 00:00
    ends = [e["period_end"] for e in idx]
    assert ends == sorted(ends, reverse=True)


def test_weekly_report_links_to_the_previous_weeks_score(tmp_path, monkeypatch):
    tiny_world(tmp_path, monkeypatch)
    reports.generate("weekly", at(2026, 9, 30, 7, 45), TZ)
    d = reports.generate("weekly", at(2026, 10, 7, 7, 45), TZ)
    assert d["health"]["previous"] == {"id": "2026-W40", "score": 100, "grade": "A"}


def entry(kind, i, base=at(2026, 1, 1)):
    day = 86400
    if kind == "weekly":
        end = base + i * 7 * day
        return {"id": f"w{i:04d}", "kind": "weekly", "period_start": end - 7 * day, "period_end": end}
    end = base + i * day
    return {"id": f"d{i:04d}", "kind": "daily", "period_start": end - day, "period_end": end}


def test_trim_index_drops_the_oldest_dailies_first_and_keeps_every_weekly():
    entries = [entry("daily", i) for i in range(70)] + [entry("weekly", i) for i in range(12)]
    kept, dropped = reports.trim_index(entries, 60)
    assert len(kept) == 60 and len(dropped) == 22
    assert {e["id"] for e in kept if e["kind"] == "weekly"} == {f"w{i:04d}" for i in range(12)}
    assert all(e["kind"] == "daily" for e in dropped) and max(e["period_end"] for e in dropped) < min(e["period_end"] for e in kept if e["kind"] == "daily")


def test_trim_index_protects_the_newest_dailies_and_then_drops_old_weeklies():
    entries = [entry("daily", i) for i in range(20)] + [entry("weekly", i) for i in range(60)]
    kept, dropped = reports.trim_index(entries, 60)
    assert len(kept) == 60
    assert sum(1 for e in kept if e["kind"] == "daily") == reports.KEEP_MIN_DAILY
    assert {e["id"] for e in kept if e["kind"] == "weekly"} == {f"w{i:04d}" for i in range(14, 60)}
    assert reports.trim_index(entries[:10], 60) == (sorted(entries[:10], key=lambda e: (-e["period_end"], e["id"])), [])


def test_retention_deletes_evicted_files_and_nothing_else(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    rd = rdir(w)
    rd.mkdir(parents=True)
    (rd / "notes.txt").write_text("mine")
    (rd / "2026-01-01.json.bak").write_text("mine too")
    for i in range(8):
        reports.generate("daily", at(2026, 10, 1 + i, 7, 30), TZ, keep=5)
    names = sorted(p.name for p in rd.iterdir())
    assert names == ["2026-10-04.json", "2026-10-05.json", "2026-10-06.json", "2026-10-07.json", "2026-10-08.json",
                     "2026-01-01.json.bak", "index.json", "notes.txt"] or len([n for n in names if n[:2] == "20" and n.endswith(".json") and n != "index.json"]) == 5
    assert (rd / "notes.txt").read_text() == "mine" and (rd / "2026-01-01.json.bak").exists()
    assert len(json.loads((rd / "index.json").read_text())) == 5


def test_a_lost_or_corrupt_index_is_rebuilt_from_the_files(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    for i in range(3):
        reports.generate("daily", at(2026, 10, 3 + i, 7, 30), TZ)               # reports 10-02, 10-03, 10-04
    (rdir(w) / "index.json").write_text("{corrupt")
    (rdir(w) / "2026-10-03.json").write_text("{corrupt")           # a damaged report drops out instead of breaking the index
    reports.generate("daily", at(2026, 10, 6, 7, 30), TZ)         # report 10-05
    idx = json.loads((rdir(w) / "index.json").read_text())
    assert [e["id"] for e in idx] == ["2026-10-05", "2026-10-04", "2026-10-02"]


def test_index_command_rebuilds_without_generating(tmp_path, monkeypatch, capsys):
    w = tiny_world(tmp_path, monkeypatch)
    reports.generate("daily", NOW_DAILY, TZ)
    (rdir(w) / "index.json").unlink()
    assert reports.main(["index"]) == 0
    assert [e["id"] for e in json.loads((rdir(w) / "index.json").read_text())] == ["2026-10-06"]
    assert "index: 1 reports" in capsys.readouterr().out


def test_write_is_atomic_when_the_replace_fails(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    first = reports.generate("daily", NOW_DAILY, TZ)
    before = (rdir(w) / "2026-10-06.json").read_bytes()
    real = os.replace

    def flaky(a, b):
        if str(b).endswith("2026-10-06.json"):
            raise OSError("disk full")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        reports.generate("daily", NOW_DAILY + 60, TZ)
    monkeypatch.undo()
    assert (rdir(w) / "2026-10-06.json").read_bytes() == before
    assert not list(rdir(w).glob(".rep-*.tmp")) and first["id"] == "2026-10-06"


def test_concurrent_generators_keep_one_consistent_index(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    errors = []

    def go(i):
        try:
            reports.generate("daily", at(2026, 10, 1 + i, 7, 30), TZ)
        except Exception as exc:                                    # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    idx = json.loads((rdir(w) / "index.json").read_text())
    expected = {reports.period_for("daily", at(2026, 10, 1 + i, 7, 30), TZ).id for i in range(8)}      # 2026-09-30 .. 2026-10-07
    assert len(idx) == 8 and {e["id"] for e in idx} == expected and [e["id"] for e in idx] == sorted(expected, reverse=True)
    assert all(json.loads((rdir(w) / f"{i}.json").read_text())["id"] == i for i in expected)
    assert not list(rdir(w).glob(".rep-*.tmp"))


def test_oversized_reports_are_trimmed_to_fit_the_cap(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    d = reports.generate("daily", NOW_DAILY, TZ, write=False)
    d["actions"]["notable"] = [{"ts": i, "title": "x" * 200, "detail": "y" * 300} for i in range(2000)]
    data = reports._encode(d)
    assert len(data) <= reports.MAX_FILE and json.loads(data)["id"] == "2026-10-06"
    d["notes"] = ["z" * 500] * 1000
    d["notable_blob"] = "q" * (reports.MAX_FILE * 2)
    with pytest.raises(ValueError):
        reports._encode(d)


def test_generate_only_writes_inside_public_reports_and_the_lock(tmp_path, monkeypatch):
    w = build_week(tmp_path, monkeypatch)

    def snapshot():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    before = snapshot()
    reports.generate("weekly", NOW_WEEKLY, TZ)
    after = snapshot()
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {"state/public/reports/2026-W41.json", "state/public/reports/index.json", "state/reports.lock"}
    assert not [k for k in before if k not in after]


# =========================================================================== tasks and CLI
def cfg(**tc):
    return {"global": {}, "tasks": {"report_daily": dict(tc), "report_weekly": dict(tc)}, "caps": {},
            "protected": {"patterns": ["x"]}}


def test_tasks_are_registered_as_c0_in_the_right_tiers():
    t = core.REGISTRY
    assert (t["report_daily"].klass, t["report_daily"].tier) == ("C0", "daily")
    assert (t["report_weekly"].klass, t["report_weekly"].tier) == ("C0", "weekly")
    assert t["report_daily"].title == "Daily report" and t["report_weekly"].timeout >= 120


def test_daily_task_result_is_small_ascii_and_never_pages(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    ctx = Ctx(cfg(timezone="America/Toronto"), "report_daily", apply=True, now=NOW_DAILY)
    res = reports.report_daily(ctx)
    assert res.status == "ok" and res.alert is False and ctx.apply is False
    assert len(res.summary) <= 140 and res.summary.isascii() and res.summary == "daily report 2026-10-06: A (100): all checks ok"
    assert res.metrics["id"] == "2026-10-06" and res.metrics["score"] == 100 and "digest_text" not in res.metrics
    assert len(res.items) <= 12 and all(set(i) == {"highlight"} for i in res.items)
    assert (rdir(w) / "2026-10-06.json").exists()
    assert not (w.log / "audit.jsonl").exists()                      # no ctx.act, no audit record: nothing mutated


def test_weekly_task_exposes_the_digest_for_the_lead_to_send(tmp_path, monkeypatch):
    w = build_week(tmp_path, monkeypatch)
    res = reports.report_weekly(Ctx(cfg(timezone="America/Toronto"), "report_weekly", apply=False, now=NOW_WEEKLY))
    assert res.metrics["digest_text"].startswith("homelab-maint weekly 2026-W41") and len(res.metrics["digest_text"]) <= 600
    assert json.loads((rdir(w) / "2026-W41.json").read_text())["digest_text"] == res.metrics["digest_text"]
    assert res.metrics["freed_bytes"] > 0 and len(res.summary) <= 140


def test_task_runs_through_the_runner_wrapper(tmp_path, monkeypatch):
    tiny_world(tmp_path, monkeypatch)
    res, dur = core.run_task(core.REGISTRY["report_daily"], cfg(timezone="America/Toronto"), apply=True)
    assert res.status == "ok" and res.alert is False and res.summary.startswith("daily report ") and res.reclaimed_bytes == 0
    assert dur < 5


def test_task_honours_the_keep_option(tmp_path, monkeypatch):
    w = tiny_world(tmp_path, monkeypatch)
    for i in range(4):
        reports.report_daily(Ctx(cfg(keep=2, timezone="America/Toronto"), "report_daily", False, now=at(2026, 10, 3 + i, 7, 30)))
    assert len(json.loads((rdir(w) / "index.json").read_text())) == 2


def test_task_ignore_tasks_option_removes_a_noisy_check_from_the_score(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.eps = [("failed_units", at(2026, 10, 6), at(2026, 10, 7), "warn")]
    w.history(at(2026, 10, 5), at(2026, 10, 8))
    w.flush_history()
    w.make_status(NOW_DAILY)
    w.write_status()
    noisy = reports.generate("daily", NOW_DAILY, TZ, write=False)
    quiet = reports.generate("daily", NOW_DAILY, TZ, opts={"ignore_tasks": ["failed_units"]}, write=False)
    assert noisy["health"]["score"] == 72 and quiet["health"]["score"] == 100 and quiet["health"]["worst_status"] == "ok"   # 75, -3: still open at midnight


def test_cli_prints_the_report_without_writing(tmp_path, monkeypatch, capsys):
    w = tiny_world(tmp_path, monkeypatch)
    assert reports.main(["daily", "--now", str(NOW_DAILY), "--tz", "America/Toronto", "--print"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "2026-10-06" and not rdir(w).exists()
    assert reports.main(["weekly", "--now", str(NOW_WEEKLY), "--tz", "America/Toronto"]) == 0
    assert "wrote 2026-W41" in capsys.readouterr().out and (rdir(w) / "2026-W41.json").exists()


# =========================================================================== a quiet week, and the real host's shape
def test_a_quiet_week_reads_clean(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.history(SEP30 - 3 * 86400, NOW_WEEKLY)
    w.daily_tier(SEP30 - 3 * 86400, NOW_WEEKLY)
    w.flush_history()
    w.make_status(NOW_WEEKLY, modes="apply")
    w.status["tasks"]["docker_df"].update(status="ok", summary="ok")
    w.write_status()
    w.audit([(at(2026, 10, 2, 7, 35), "docker_cache", "docker builder prune", "default", 12 * GIB, "done")])
    w.js("incidents.json", {"open": [], "recent": []})
    w.jl("spikes.jsonl", [])
    w.ring(SEP30, NOW_WEEKLY, lambda h: {"cpu_temp": 50.0, "gpu_temp": 40.0, "nvme_temp": 45.0})
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    assert d["health"]["score"] == 100 and d["health"]["grade"] == "A" and d["health"]["worst_status"] == "ok"
    assert d["headline"] == "A (100): all checks ok; 12.0 GiB freed"
    hl = d["highlights"]
    assert hl[0] == "All checks were ok for 100% of the observed week; no warnings or critical results."
    assert "No incidents were opened during the week." in hl and "No load spikes were recorded." in hl
    assert "Maintenance freed 12.0 GiB in 1 action (docker build cache pruned 12.0 GiB)." in hl
    assert any(h.startswith("Backups are current at the latest check (age: system 5d19h, immich 4d20h, stack-backup 18h13m)") for h in hl)
    assert "Temperatures: CPU 50 C avg / 55 C max, GPU 40 C avg / 45 C max, NVMe 45 C avg / 50 C max." in hl
    assert d["notes"] == []                                  # nothing missing, nothing provisional, nothing informational to explain


def test_real_status_shape_from_this_host_is_understood(tmp_path, monkeypatch):
    """A copy of the live status.json task set (kept small): every check's metrics key that reports.py reads exists in the real format."""
    w = World(tmp_path, monkeypatch)
    w.history(SEP30, OCT7)
    w.flush_history()
    w.make_status(NOW_WEEKLY)
    st = w.status["tasks"]
    st["growth_watch"]["metrics"] = {"n_over": 1, "paths": [], "worst_gib_day": 1.6, "worst_path": "/var/log/journal"}
    st["smart_trend"]["metrics"] = {"devices": [{"dev": "sdd", "level": "warn", "pending": 8, "realloc": 0, "temp_c": 43}]}
    st["plan_task"] = {"klass": "C2", "tier": "weekly", "title": "Cleanup candidates", "status": "ok", "plan_hash": "abc123def456",
                       "plan": {"items": [{"name": "a", "bytes": 5 * GIB}, {"name": "b", "bytes": GIB}], "total_bytes": 6 * GIB}, "metrics": {}}
    w.write_status()
    d = reports.generate("weekly", NOW_WEEKLY, TZ, write=False)
    rec = "\n".join(d["capacity"]["recommendations"])
    assert "/var/log/journal is growing 1.6 GiB/day" in rec
    assert "SMART shows trouble on sdd" in rec
    assert "2 cleanup candidates (6.0 GiB) await approval: homelab-maint plan plan_task." in rec
    assert d["upcoming"][-1]["when"] == "needs your approval" and "homelab-maint plan plan_task" in d["upcoming"][-1]["what"]


# =========================================================================== review round: redaction order, monitoring gaps,
# failed restarts, standing results, run-dependent alert flags, problems still open at the end of the period
ALERT_SUMMARY = ("warn: alert path: 1 notifier sends failed rc=1: cannot load /etc/hermes/gmail-app-credentials.json "
                 "for ohmz.homelab@gmail.com (sms 4165550123@txt.bell.ca)")
ALERT_LEAKS = ["gmail-app-credentials", "/etc/hermes", "ohmz.homela", "4165550", "txt.bell", "cannot load"]
STRADDLE = [("github token", "ghp_" + "AbCdEfGh12" * 4), ("40 hex", "3f786850e387550fdab836ed7e6dc881de23001b"),
            ("sms gateway address", "4165550123@txt.bell.ca"), ("sk key", "sk-" + "Zq8wXr3TnB" * 3), ("aws key id", "AKIAABCDEFGHIJKLMNOP")]


def failing_now(tmp_path, monkeypatch, task_name, summary, metrics=None, status="warn"):
    """Tue 6 Oct in which `task_name` turns `status` at 10:00 and is still failing at the latest check, with `summary`."""
    w = World(tmp_path, monkeypatch)
    per = reports.period_for("daily", NOW_DAILY, TZ)
    w.eps = [(task_name, per.start + 10 * 3600, per.end + 99999, status)]
    w.history(per.start - 86400, per.end)
    w.flush_history()
    w.make_status(NOW_DAILY)
    w.status["tasks"][task_name].update(status=status, summary=summary, metrics=metrics or {})
    w.write_status()
    return w


@pytest.mark.parametrize("cleaner", ["publish", "fallback"])
def test_an_open_alert_path_episode_never_quotes_the_bridge_error(tmp_path, monkeypatch, cleaner):
    """alert_path_health's summary quotes the bridge's stderr (credential file, provider reply, phone gateway): like publish.py,
    the report builds its wording from counters and never quotes it, and cuts everything after "rc=<n>" everywhere else."""
    if cleaner == "fallback":
        monkeypatch.setattr(reports, "_cleaner", lambda: reports._fallback_clean)
    failing_now(tmp_path, monkeypatch, "alert_path_health", ALERT_SUMMARY, {"notify_broken": True, "notify_fail_24h": 1, "bridge_ok": True})
    d = reports.generate("daily", NOW_DAILY, TZ, write=False)
    blob = json.dumps(d)
    for leak in ALERT_LEAKS:
        assert leak not in blob, leak
    ep = next(h for h in d["highlights"] if h.startswith("Alert path:"))
    assert "warning for" in ep and "rc=1" not in ep


def test_an_informational_alert_path_check_is_not_quoted_in_the_notes_either(tmp_path, monkeypatch):
    failing_now(tmp_path, monkeypatch, "alert_path_health", ALERT_SUMMARY, {"notify_broken": True, "notify_fail_24h": 1})
    d = reports.generate("daily", NOW_DAILY, TZ, opts={"ignore_tasks": ["alert_path_health"]}, write=False)
    assert any("is informational (never pages)" in n for n in d["notes"])
    blob = json.dumps(d)
    for leak in ALERT_LEAKS:
        assert leak not in blob, leak


@pytest.mark.parametrize("cleaner", ["publish", "fallback"])
@pytest.mark.parametrize("label,token", STRADDLE, ids=[x[0] for x in STRADDLE])
def test_a_secret_straddling_the_cut_leaves_no_prefix(tmp_path, monkeypatch, cleaner, label, token):
    """Redact first, shorten after: the old order cut the summary at 110 characters and only then redacted, so the first
    characters of a token that began at character 100 (too short for any pattern to match) reached the public page."""
    if cleaner == "fallback":
        monkeypatch.setattr(reports, "_cleaner", lambda: reports._fallback_clean)
    pad = ("unhealthy: " + "c1 " * 100)[:99] + " "
    assert len(pad) == 100
    failing_now(tmp_path, monkeypatch, "failed_units", "warn: " + pad + token)
    d = reports.generate("daily", NOW_DAILY, TZ, write=False)
    blob = json.dumps(d)
    assert token[:6] not in blob, label
    ep = next(h for h in d["highlights"] if h.startswith("Services & containers:"))
    assert "at the latest check: unhealthy: c1 c1" in ep and ep.isascii()


def test_asc_redacts_before_it_shortens_for_every_caller():
    for _label, token in STRADDLE:
        cut = reports._asc("x " * 49 + token, 100)               # the token starts at character 98 of a 100-character budget
        assert token[:6] not in cut, cut
    assert reports._asc("a b\nc", 20) == "a b"                    # first line only, like publish.clean (stderr tails stay out)


def test_text_after_rc_is_cut_in_check_summaries_and_audit_failures(tmp_path, monkeypatch):
    w = failing_now(tmp_path, monkeypatch, "failed_units", "warn: 2 unhealthy rc=1: cannot read /srv/private-stuff/token.json")
    per = reports.period_for("daily", NOW_DAILY, TZ)
    w.audit([(per.start + 3600, "docker_images", "docker image rm", "sha256:abc", 0,
              "failed: docker image rm rc=1: conflict /srv/private-stuff/x is used by the host")])
    d = reports.generate("daily", NOW_DAILY, TZ, write=False)
    blob = json.dumps(d)
    assert "private-stuff" not in blob and "cannot read" not in blob and "is used by the host" not in blob
    assert any("rc=1" in h for h in d["highlights"]) and any(n["title"].startswith("FAILED: docker_images") for n in d["actions"]["notable"])


# --------------------------------------------------------------------------- monitoring gaps are not new installs
def silent_after(tmp_path, monkeypatch, stop_at, kind="daily", now=NOW_DAILY):
    w = World(tmp_path, monkeypatch)
    per = reports.period_for(kind, now, TZ)
    w.history(per.start - 21 * 86400, stop_at)                    # three weeks of history, then the check timer stops
    w.flush_history()
    w.make_status(now)
    w.write_status()
    return reports.generate(kind, now, TZ, write=False), per


@pytest.mark.parametrize("stop_h,blind_h,score,grade", [(4, 20, 55, "D"), (16, 8, 73, "C")])
def test_a_check_timer_that_stopped_mid_period_is_a_monitoring_gap_not_all_ok(tmp_path, monkeypatch, stop_h, blind_h, score, grade):
    per = reports.period_for("daily", NOW_DAILY, TZ)
    d, _ = silent_after(tmp_path, monkeypatch, per.start + stop_h * 3600)
    h = d["health"]
    assert h["score"] == score and h["grade"] == grade                     # scored (blind time), never null / n/a
    assert d["headline"] == f"{grade} ({score}): monitoring gap: {blind_h} h without results"
    assert "early data" not in d["headline"] and "all checks ok" not in d["headline"]
    assert h["monitoring_gap"] is True and h["provisional"] is False and h["minutes"]["blind"] == blind_h * 60
    assert d["highlights"][0].startswith(f"Monitoring gap: {blind_h} h without results, so that time is unknown, not healthy.")
    assert f"MONITORING GAP: {blind_h} h without results." in d["digest_text"]
    assert any(n.startswith("Monitoring gap:") for n in d["notes"]) and not any("Provisional" in n for n in d["notes"])


def test_a_week_with_a_dead_check_tier_is_scored_and_called_a_gap_not_collecting_data(tmp_path, monkeypatch):
    per = reports.period_for("weekly", NOW_WEEKLY, TZ)
    d, _ = silent_after(tmp_path, monkeypatch, per.start + 12 * 3600, kind="weekly", now=NOW_WEEKLY)
    assert d["health"]["score"] == 52 and d["health"]["grade"] == "D" and d["health"]["coverage_pct"] == 7.1
    assert d["headline"] == "D (52): monitoring gap: 6 d 12 h without results" and "Collecting data" not in d["headline"]
    assert d["health"]["monitoring_gap"] is True and d["health"]["provisional"] is False


def test_a_whole_period_without_results_is_a_gap_when_history_exists_from_before_it(tmp_path, monkeypatch):
    per = reports.period_for("daily", NOW_DAILY, TZ)
    d, _ = silent_after(tmp_path, monkeypatch, per.start - 3600)
    assert d["health"]["score"] == 50 and d["health"]["worst_status"] == "unknown" and d["health"]["minutes"]["blind"] == 1440
    assert d["headline"] == "D (50): monitoring gap: 24 h without results" and "No data recorded" not in d["headline"]
    assert d["highlights"][0].startswith("Monitoring gap: no check reported anything during the day although history exists from before it")
    assert "MONITORING GAP: 24 h without results." in d["digest_text"] and "No problem episodes" not in d["digest_text"]


def test_the_same_thin_data_is_collecting_data_for_a_new_install_and_a_gap_for_old_history():
    per = reports.period_for("weekly", NOW_WEEKLY, TZ)
    recs = [rec(t, "disk_forecast", "ok") for t in (per.end - 12 * 3600 + 900 * i + 20 for i in range(48))]
    new = reports.build_report("weekly", NOW_WEEKLY, inputs_of(recs, first_t=recs[0].t, status={"tasks": {}}), TZ)
    old = reports.build_report("weekly", NOW_WEEKLY, inputs_of(recs, first_t=per.start - 5 * 86400, status={"tasks": {}}), TZ)
    assert new["health"]["score"] is None and new["headline"].startswith("Collecting data") and new["health"]["monitoring_gap"] is False
    assert old["health"]["score"] is not None and old["health"]["grade"] != "n/a" and old["headline"].startswith(f"{old['health']['grade']} (")
    assert "monitoring gap" in old["headline"] and "early data" not in old["headline"] and not old["health"]["provisional"]


def test_a_new_install_with_partial_coverage_is_still_provisional_not_a_gap():
    per = day_period()
    recs = [r for r in full_day(per, lambda n, t: "ok") if r.t >= per.start + 18 * 3600]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, first_t=recs[0].t, status={"tasks": {}}), TZ)
    assert d["health"]["provisional"] is True and d["health"]["monitoring_gap"] is False and d["headline"].startswith("A (100, early data)")


def test_a_check_that_stopped_reporting_makes_the_headline_a_monitoring_gap():
    per = day_period()
    recs = [r for r in full_day(per, lambda n, t: "ok") if not (r.task == "failed_units" and r.t > per.end - 3 * 3600)]
    status = {"tasks": {n: {"tier": "check", "last_run": per.end + 100} for n in ("disk_forecast", "failed_units")}}
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), status=status), TZ)
    assert d["health"]["minutes"]["blind"] == 0 and d["health"]["monitoring_gap"] is True
    assert d["headline"] == "A (97): monitoring gap: 1 check stopped reporting" and "all checks ok" not in d["headline"]


def test_blind_time_under_5_percent_is_not_called_a_gap_and_over_it_is():
    per = day_period()

    def day_without(minutes):
        recs = [r for r in full_day(per, lambda n, t: "ok") if not per.start + 6 * 3600 <= r.t < per.start + 6 * 3600 + minutes * 60]
        return reports.analyse_checks(inputs_of(recs, first_t=per.start - 5000), per, TZ, set())
    assert day_without(60)["gap"] is False and day_without(60)["blind_min"] == 60          # 4.2% of the day
    assert day_without(90)["gap"] is True and day_without(90)["blind_min"] == 90           # 6.3%


def test_the_task_result_flags_a_monitoring_gap_for_the_glue(tmp_path, monkeypatch):
    per = reports.period_for("weekly", NOW_WEEKLY, TZ)
    silent_after(tmp_path, monkeypatch, per.start + 12 * 3600, kind="weekly", now=NOW_WEEKLY)
    res = reports.report_weekly(Ctx(cfg(timezone="America/Toronto"), "report_weekly", apply=False, now=NOW_WEEKLY))
    assert res.metrics["monitoring_gap"] is True and res.metrics["score"] == 52 and "monitoring gap" in res.metrics["digest_text"].lower()
    tiny_world(tmp_path / "q", monkeypatch)                                         # a healthy host: no gap
    assert reports.report_daily(Ctx(cfg(timezone="America/Toronto"), "report_daily", False, now=NOW_DAILY)).metrics["monitoring_gap"] is False


# --------------------------------------------------------------------------- a failed restart/stop is not evidence of no harm
def spike_day(audit=(), plog=()):
    """A level-4 spike Tue 14:10 (10 min) with the given audit rows (task, action, outcome class, raw) and pressure-log rows
    (rung, action, outcome), each 2 minutes in."""
    t0 = at(2026, 10, 6, 14, 10)
    ledger = spike_ledger(t0, 600, 4, {"mem": 4}, {"mem_full60": 20.0}, [{"name": "tunarr", "class": "P2", "anon_gib": 8.0, "cpu_pct": 0.0}],
                          "resolved by itself, nothing killed")
    arows = [{"t": t0 + 120, "task": tk, "action": act, "target": "tunarr", "bytes": 0, "o": o, "raw": raw} for tk, act, o, raw in audit]
    prows = [{"_t": t0 + 120, "ts": t0 + 120, "level": 4, "rung": rung, "action": act, "target": "tunarr", "class": "P2", "outcome": out}
             for rung, act, out in plog]
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    inp = inputs_of(recs, status={"tasks": {}}, audit=arows, spikes=[{**r, "_t": r["t"]} for r in ledger], pressure_log=prows)
    return reports.build_report("daily", NOW_DAILY, inp, TZ)


FAILED_HARM = [
    ("audit: docker stop timed out (rc 124)", dict(audit=[("pressure_response", "docker stop", "failed", "failed: docker stop tunarr rc=124")]), "docker stop"),
    ("audit: docker restart failed", dict(audit=[("stuck_detector", "docker restart", "failed", "failed: docker restart tunarr rc=1")]), "docker restart"),
    ("audit: native docker-restart failed", dict(audit=[("stuck_detector", "docker-restart", "failed", "failed: boom")]), "docker-restart"),
    ("audit: sigterm of a daemon failed", dict(audit=[("gradle_reaper", "sigterm-daemon", "failed", "failed: no such process")]), "sigterm-daemon"),
    ("pressure-log: L4 restart failed", dict(plog=[("L4", "restart stuck container (growth)", "failed: timed out")]), "restart"),
    ("pressure-log: L5 stop failed", dict(plog=[("L5", "emergency stop best-effort container", "failed: docker stop tunarr rc=124")]), "stop"),
]


@pytest.mark.parametrize("label,kw,verb", FAILED_HARM, ids=[x[0] for x in FAILED_HARM])
def test_a_failed_restart_or_stop_during_a_spike_is_unknown_not_handled(label, kw, verb):
    d = spike_day(**kw)
    sp = d["spikes"]
    ev = sp["list"][0]
    assert sp["handled_without_harm"] is None and ev["harm"] is None and ev["attempted"] is True
    assert ev["outcome"] == f"{verb} attempted on tunarr, result unknown (the command failed)"
    assert "handled" not in d["headline"] and "Nothing was killed or restarted" not in " ".join(d["highlights"])
    assert any("A restart or stop was attempted and failed" in h for h in d["highlights"])
    assert "a restart/stop failed" in d["digest_text"] and "nothing killed" not in d["digest_text"]


def test_a_sigterm_that_worked_is_harm_and_a_known_harm_beats_an_unknown_one():
    d = spike_day(audit=[("gradle_reaper", "sigterm-daemon", "done", "done")])
    assert d["spikes"]["handled_without_harm"] is False and d["spikes"]["list"][0]["outcome"] == "sigterm-daemon ran on tunarr"
    d = spike_day(audit=[("stuck_detector", "docker restart", "failed", "failed: rc=1"), ("stuck_detector", "docker restart", "done", "done")])
    assert d["spikes"]["handled_without_harm"] is False and d["spikes"]["list"][0]["harm"] is True


def test_failed_soft_actions_and_failures_outside_the_spike_are_not_harm_evidence():
    soft = spike_day(audit=[("pressure_response", "docker update", "failed", "failed: boom"), ("docker_images", "docker image rm", "failed", "failed: busy")],
                     plog=[("L2", "unload idle Ollama model", "failed: timeout"), ("L3", "slow batch container", "failed: inspect")])
    assert soft["spikes"]["handled_without_harm"] is True and soft["spikes"]["list"][0]["attempted"] is False
    t0 = at(2026, 10, 6, 14, 10)
    per = day_period()
    late = {"t": t0 + 3 * 3600, "task": "stuck_detector", "action": "docker restart", "target": "x", "bytes": 0, "o": "failed", "raw": "failed"}
    ledger = spike_ledger(t0, 600, 3, {"mem": 3}, {"mem_full60": 9.0}, [], "resolved by itself, nothing killed")
    inp = inputs_of(full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)), status={"tasks": {}}, audit=[late], spikes=[{**r, "_t": r["t"]} for r in ledger])
    assert reports.build_report("daily", NOW_DAILY, inp, TZ)["spikes"]["handled_without_harm"] is True


# --------------------------------------------------------------------------- standing results: one transient failure is not a week of F
def span_recs(t0, t1, task_name="disk_forecast", status="ok"):
    return [rec(t, task_name, status) for t in (t0 + 20 + 900 * i for i in range(int((t1 - t0) // 900)))]


def test_the_report_generators_never_grade_themselves():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [rec(per.start - 17 * 3600, "report_weekly", "error"), rec(per.start + 7.5 * 3600, "report_daily", "error"),
             rec(per.start + 7.6 * 3600, "report_daily", "crit")]
    for r in recs:
        if r.task.startswith("report_"):
            r.info = False                                    # run_task's own error result carries alert=True: still never counted
    status = {"tasks": {"disk_forecast": {"tier": "check"}, "report_daily": {"tier": "daily"}, "report_weekly": {"tier": "weekly"}}}
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())
    assert (c["crit_min"], c["warn_min"]) == (0, 0) and c["episodes"] == {} and c["open"] == {}


def test_one_transient_weekly_error_is_one_warn_slot_and_the_next_days_are_clean():
    wed = at(2026, 10, 7, 7, 45)
    status = {"tasks": {"disk_forecast": {"tier": "check"}, "c2_candidates": {"tier": "weekly"}}}
    base = [rec(wed - 7 * 86400, "c2_candidates", "ok"), rec(wed, "c2_candidates", "error")]
    wed_per = reports.period_for("daily", at(2026, 10, 8, 7, 30), TZ)             # the day of the failure
    c = reports.analyse_checks(inputs_of(sorted(base + span_recs(wed_per.start, wed_per.end), key=lambda r: r.t), status=status), wed_per, TZ, set())
    assert (c["crit_min"], c["warn_min"]) == (0, 15)                                # an error of a weekly task is a warning, for its own slot
    thu_per = reports.period_for("daily", at(2026, 10, 9, 7, 30), TZ)
    c = reports.analyse_checks(inputs_of(sorted(base + span_recs(thu_per.start, thu_per.end), key=lambda r: r.t), status=status), thu_per, TZ, set())
    assert (c["crit_min"], c["warn_min"], c["open"]) == (0, 0, {})                  # the old rule held a crit for the whole week: F


def test_a_weekly_error_that_repeats_stands_as_a_warning_for_two_days_not_a_week():
    status = {"tasks": {"disk_forecast": {"tier": "check"}, "c2_candidates": {"tier": "weekly"}}}
    per = reports.period_for("weekly", NOW_WEEKLY, TZ)
    runs = [rec(per.start - 7 * 86400 + 27900, "c2_candidates", "error"), rec(per.start + 27900, "c2_candidates", "error")]      # Wed 07:45 twice
    c = reports.analyse_checks(inputs_of(sorted(runs + span_recs(per.start - 86400, per.end), key=lambda r: r.t), status=status), per, TZ, set())
    assert c["crit_min"] == 0 and 2880 - 15 <= c["warn_min"] <= 2880 + 15            # Wed 07:45 + 48 h (HOLD_CAP_S), not 7 days
    assert reports.HOLD_CAP_S == 2 * 86400


def test_a_check_tier_error_is_still_crit_and_a_confirmed_daily_crit_still_stands_as_crit():
    per = day_period()
    recs = full_day(per, lambda n, t: "error" if t - per.start == 3 * 3600 + 20 else "ok", tasks=("disk_forecast",))
    assert reports.analyse_checks(inputs_of(recs, status={"tasks": {"disk_forecast": {"tier": "check"}}}), per, TZ, set())["crit_min"] == 15
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [rec(per.start - 16.5 * 3600, "docker_images", "crit"), rec(per.start + 7.5 * 3600, "docker_images", "crit")]
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status={"tasks": {"docker_images": {"tier": "daily"}}}), per, TZ, set())
    assert c["crit_min"] == 990 and c["warn_min"] == 0 and c["open"] == {"docker_images": 2}


def test_a_daily_result_is_confirmed_only_by_the_run_right_before_it():
    per = day_period()
    status = {"tasks": {"docker_images": {"tier": "daily"}}}
    base = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    for prev_t, expect in ((per.start - 16.5 * 3600, 990), (per.start - 3 * 86400, 15)):          # yesterday's run / a run three days ago
        recs = base + [rec(prev_t, "docker_images", "warn"), rec(per.start + 7.5 * 3600, "docker_images", "warn")]
        c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())
        assert c["warn_min"] == expect, prev_t
    recs = base + [rec(per.start - 16.5 * 3600, "docker_images", "ok"), rec(per.start + 7.5 * 3600, "docker_images", "warn")]
    assert reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())["warn_min"] == 15


def test_stands_rule_directly():
    tiers_daily = 86400.0
    mk = lambda *sts: [rec(100000 + i * 86400, "t", s) for i, s in enumerate(sts)]                # noqa: E731
    rs = mk("ok", "warn", "warn", "error")
    assert reports._stands(rs, 1, set(), tiers_daily) == (reports.SLOT_S, 1)                       # first non-ok run: a blip
    assert reports._stands(rs, 2, set(), tiers_daily) == (86400, 1)                                # second in a row: stands a day
    assert reports._stands(rs, 3, set(), tiers_daily) == (86400, 1)                                # ... and an error stands as warn
    assert reports._stands(rs, 2, set(), 7 * 86400.0) == (reports.HOLD_CAP_S, 1)                   # a weekly result: capped at 2 days
    assert reports._stands(mk("warn", "warn"), 1, set(), reports.SLOT_S) == (reports.SLOT_S, 1)    # the check tier: one slot always
    assert reports._stands(mk("warn", "warn"), 1, {"t"}, tiers_daily)[1] == 0                       # informational results never count


def test_episodes_cover_the_same_time_the_score_counts():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [rec(per.start - 16.5 * 3600, "docker_images", "warn"), rec(per.start + 7.5 * 3600, "docker_images", "warn")]
    recs.sort(key=lambda r: r.t)
    inp = inputs_of(recs, status={"tasks": {"docker_images": {"tier": "daily"}}})
    c = reports.analyse_checks(inp, per, TZ, set())
    ep = c["episodes"]["docker_images"]
    assert len(ep) == 1 and ep[0]["end"] == per.end and ep[0]["open"] is True
    assert ep[0]["end"] - ep[0]["start"] == pytest.approx(c["warn_min"] * 60, abs=20)


def test_a_weekly_failure_is_confirmed_across_the_15_day_lookback_the_daily_report_reads(tmp_path, monkeypatch):
    """The previous weekly run is 7 days older than the failure: outside the old 7-day read window, inside the new one."""
    def thursday(prior_error):
        w = World(tmp_path / ("p" if prior_error else "n"), monkeypatch)
        thu_now = at(2026, 10, 9, 7, 30)                                                         # the report for Thu 8 Oct
        w.history(at(2026, 9, 25), thu_now)
        for t, st in ((at(2026, 9, 30, 7, 45), "error" if prior_error else "ok"), (at(2026, 10, 7, 7, 45), "error")):
            w.hist.append({"t": t, "kind": "task", "task": "c2_candidates", "status": st, "reclaimed": 0, "dur": 120, "metrics": {}})
        w.flush_history()
        w.make_status(thu_now)
        w.status["tasks"]["c2_candidates"] = {"title": "Cleanup candidates", "klass": "C2", "tier": "weekly", "status": "error", "alert": True,
                                              "summary": "timeout", "last_run": at(2026, 10, 7, 7, 45), "metrics": {}}
        w.write_status()
        return reports.generate("daily", thu_now, TZ, write=False)
    once, twice = thursday(False), thursday(True)
    assert once["health"]["score"] == 100 and once["headline"] == "A (100): all checks ok"
    assert twice["health"]["minutes"]["warn"] == 1440 and twice["health"]["minutes"]["crit"] == 0 and twice["health"]["score"] == 72


# --------------------------------------------------------------------------- run-dependent alert flags (pressure_state)
PRESSURE_TASKS = list(CHECKS) + [("pressure_state", "Load pressure level")]


def pressure_day(tmp_path, monkeypatch, status="crit", full60=18.0, extra=None, flag=None, opts=None, hours=6):
    """A day with `hours` of pressure_state `status` from 06:00, history WITHOUT the per-run alert flag (unless `flag`), and
    status.json saying alert=False for it, as the live runner writes it."""
    w = World(tmp_path, monkeypatch)
    per = reports.period_for("daily", NOW_DAILY, TZ)
    w.eps = [("pressure_state", per.start + 6 * 3600, per.start + (6 + hours) * 3600, status)]
    w.metric_fns["pressure_state"] = lambda t: ({"psi_mem_full60": full60, **(extra or {})} if w.stat("pressure_state", t) != "ok"
                                                else {"psi_mem_full60": 0.1})
    w.history(per.start - 86400, per.end, tasks=PRESSURE_TASKS)
    if flag is not None:
        for r in w.hist:
            if r.get("task") == "pressure_state":
                r["alert"] = flag
    w.flush_history()
    w.make_status(NOW_DAILY)
    w.status["tasks"]["pressure_state"] = {"title": "Load pressure level", "klass": "C0", "tier": "check", "status": "ok", "alert": False,
                                           "summary": "L0 normal", "last_run": NOW_DAILY - 300, "metrics": {}}
    w.write_status()
    return reports.generate("daily", NOW_DAILY, TZ, opts=opts, write=False)


def test_memory_pressure_counts_even_though_status_json_says_alert_false_and_history_has_no_flag(tmp_path, monkeypatch):
    d = pressure_day(tmp_path, monkeypatch)
    assert d["health"]["minutes"]["crit"] == 360 and d["health"]["score"] == 85 and d["health"]["grade"] == "B"
    assert d["headline"] == "B (85): Load pressure level critical 6 h"


def test_io_only_pressure_without_the_flag_stays_informational(tmp_path, monkeypatch):
    d = pressure_day(tmp_path, monkeypatch, status="warn", full60=0.2, extra={"psi_io_some60": 60.0, "psi_mem_some60": 4.0, "mem_avail_gib": 70.0})
    assert d["health"]["minutes"]["warn"] == 0 and d["headline"] == "A (100): all checks ok"


def test_the_per_run_flag_beats_the_metrics_in_both_directions(tmp_path, monkeypatch):
    assert pressure_day(tmp_path / "a", monkeypatch, flag=False)["health"]["minutes"]["crit"] == 0              # the run said informational
    d = pressure_day(tmp_path / "b", monkeypatch, status="warn", full60=0.2, flag=True)                          # the run alerted
    assert d["health"]["minutes"]["warn"] == 360


def test_ignore_tasks_still_silences_pressure_state(tmp_path, monkeypatch):
    d = pressure_day(tmp_path, monkeypatch, opts={"ignore_tasks": ["pressure_state"]})
    assert d["health"]["minutes"]["crit"] == 0 and d["headline"] == "A (100): all checks ok"


@pytest.mark.parametrize("status,metrics,expect", [
    ("warn", {"psi_mem_full60": 3.0}, True), ("warn", {"psi_mem_full60": 2.9}, False),
    ("warn", {"psi_mem_full60": 0.1, "psi_mem_some60": 20.0}, True), ("warn", {"psi_mem_full60": 0.1, "psi_mem_some60": 19.0}, False),
    ("warn", {"psi_mem_full60": 0.1, "mem_avail_gib": 12.0}, True), ("warn", {"psi_mem_full60": 0.1, "mem_avail_gib": 12.5}, False),
    ("warn", {"psi_mem_full60": 0.1, "swap_in_pps": 1000}, True), ("warn", {"psi_mem_full60": 0.1, "swap_in_pps": 999}, False),
    ("warn", {"psi_mem_full60": 0.1, "psi_io_some60": 80.0}, False), ("warn", {}, True),   # io only / nothing to judge by: do not hide it
    ("warn", {"psi_io_some60": 80.0}, True),
    ("crit", {"psi_mem_full60": 0.0}, True),                                         # io and cpu are capped at level 3: a crit is memory
    ("error", {}, True)])
def test_run_alerts_reads_the_memory_signals_at_the_ladders_level_2_thresholds(status, metrics, expect):
    r = reports.Rec(1.0, "pressure_state", status, reports.LEVEL[status], metrics)
    assert reports._run_alerts(r) is expect
    assert (reports._lvl(r, set()) > 0) is expect


def test_pressure_response_without_a_flag_is_not_hidden_but_with_one_it_follows_it():
    r = reports.Rec(1.0, "pressure_response", "warn", 1, {})
    assert reports._lvl(r, set()) == 1 and reports._lvl(r, {"pressure_response"}) == 0          # only an explicit ignore silences it
    r.info = True
    assert reports._lvl(r, set()) == 0


# --------------------------------------------------------------------------- a problem still open at the end of the period
def open_day(tmp_path, monkeypatch, start_offset, status="crit", task_name="backup_freshness", fixed_at=None):
    w = World(tmp_path, monkeypatch)
    per = reports.period_for("daily", NOW_DAILY, TZ)
    w.eps = [(task_name, per.end - start_offset, fixed_at if fixed_at else per.end + 99999, status)]
    w.history(per.start - 86400, per.end)
    w.flush_history()
    w.make_status(NOW_DAILY)
    w.write_status()
    return reports.generate("daily", NOW_DAILY, TZ, write=False)


@pytest.mark.parametrize("offset,minutes_text", [(900, "14 min"), (7200, "1 h 59 min")])
def test_a_crit_still_open_at_midnight_never_scores_an_a_and_says_so(tmp_path, monkeypatch, offset, minutes_text):
    d = open_day(tmp_path, monkeypatch, offset)
    assert d["health"]["score"] == 89 and d["health"]["grade"] == "B" and d["health"]["deductions"]["open"] == 3.0
    assert d["headline"] == f"B (89): Backups critical {minutes_text}, still open"
    assert any("still open at the end of the day" in h for h in d["highlights"])
    assert "still open" in d["digest_text"]


def test_a_crit_fixed_before_the_period_ends_is_not_open(tmp_path, monkeypatch):
    per = reports.period_for("daily", NOW_DAILY, TZ)
    d = open_day(tmp_path, monkeypatch, 7200, fixed_at=per.end - 3600)
    assert d["health"]["deductions"]["open"] == 0.0 and d["health"]["grade"] == "A" and "still open" not in d["headline"]
    assert d["headline"] == "A (97): Backups critical 60 min"


def test_an_open_warn_costs_three_points_and_is_not_capped(tmp_path, monkeypatch):
    d = open_day(tmp_path, monkeypatch, 900, status="warn", task_name="disk_forecast")
    assert d["health"]["deductions"]["open"] == 3.0 and d["health"]["score"] == 96 and d["health"]["grade"] == "A"
    assert d["headline"] == "A (96): Disk space warning 14 min, still open"


def test_a_check_that_went_silent_after_a_crit_is_stale_not_open():
    per = day_period()
    recs = [r for r in full_day(per, lambda n, t: "ok", tasks=("disk_forecast", "failed_units")) if not (r.task == "failed_units" and r.t > per.end - 3 * 3600)]
    recs = [reports.Rec(r.t, r.task, "crit", 2, {}) if r.task == "failed_units" and r.t > per.end - 3 * 3600 - 900 else r for r in recs]
    status = {"tasks": {n: {"tier": "check", "last_run": per.end + 100} for n in ("disk_forecast", "failed_units")}}
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t), status=status), per, TZ, set())
    assert c["open"] == {} and c["stale"] == ["failed_units"]


def test_a_confirmed_daily_failure_is_open_but_an_unconfirmed_one_is_not():
    per = day_period()
    status = {"tasks": {"docker_images": {"tier": "daily"}, "disk_forecast": {"tier": "check"}}}
    base = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    confirmed = base + [rec(per.start - 16.5 * 3600, "docker_images", "warn"), rec(per.start + 7.5 * 3600, "docker_images", "warn")]
    single = base + [rec(per.start + 7.5 * 3600, "docker_images", "warn")]
    assert reports.analyse_checks(inputs_of(sorted(confirmed, key=lambda r: r.t), status=status), per, TZ, set())["open"] == {"docker_images": 1}
    assert reports.analyse_checks(inputs_of(sorted(single, key=lambda r: r.t), status=status), per, TZ, set())["open"] == {}


def test_open_problems_come_first_in_the_headline_among_equals():
    per = day_period()
    recs = full_day(per, lambda n, t: ("crit" if n == "disk_forecast" and per.start + 2 * 3600 <= t < per.start + 10 * 3600 else
                                       "crit" if n == "failed_units" and t >= per.end - 3600 else "ok"))
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}), TZ)
    assert d["headline"].split(": ", 1)[1].startswith("failed_units critical") or "still open" in d["headline"]
    assert d["headline"].endswith("still open") and "Failed units" in d["headline"]


def test_health_score_open_level_is_monotone_capped_and_documented():
    assert score(open_level=1)["score"] == 97 and score(open_level=1)["deductions"]["open"] == 3.0
    assert score(open_level=2)["score"] == 89 and score(open_level=2)["grade"] == "B"
    assert score(crit_min=P_DAY, open_level=2)["score"] == 37 and score(open_level=0)["deductions"]["open"] == 0.0
    for kw in ({}, {"warn_min": 300}, {"crit_min": 60}, {"blind_min": 600, "stale_checks": 2}, {"backups_failed": 1}):
        scores = [score(open_level=lv, **kw)["score"] for lv in (0, 1, 2)]
        assert scores[0] >= scores[1] >= scores[2], kw
    assert score(open_level=9)["score"] == score(open_level=2)["score"] and score(open_level=-3)["score"] == 100


# =========================================================================== acknowledged issues (SPEC5)
# History records written while the owner had acknowledged that exact error carry `acked: true`. For the SCORE they count like alert=false:
# no slot colour, no episode, no D_open / D_incidents / D_backups, no grade cap. The report lists them (an `acknowledged` section and a
# highlight), the SLO is NOT adjusted. Nothing changes for a report without any acknowledgement.
def arec(t, task_, status="warn", **m):
    return reports.Rec(t, task_, status, reports.LEVEL[status], m, True, True)


def test_an_acknowledged_result_colours_no_slot_and_opens_no_episode():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok")
    recs = [arec(r.t, r.task, "crit") if r.task == "failed_units" else r for r in recs]            # failing and acknowledged all day
    c = reports.analyse_checks(inputs_of(recs), per, TZ, set())
    assert (c["warn_min"], c["crit_min"], c["ok_min"]) == (0, 0, 1440) and c["worst"] == "ok" and c["episodes"] == {} and c["open"] == {}
    assert c["ok_pct_by_task"]["failed_units"] == 0.0                                              # it keeps its own, honest, per-task percentage
    plain = [rec(r.t, r.task, "crit") if r.task == "failed_units" else r for r in full_day(per, lambda n, t: "ok")]
    c2 = reports.analyse_checks(inputs_of(plain), per, TZ, set())
    assert c2["crit_min"] == 1440 and c2["open"] == {"failed_units": 2}                            # the control: the same results unacknowledged


def test_the_grade_is_not_capped_nor_charged_for_an_acknowledged_open_crit():
    per = day_period()
    recs = [arec(r.t, r.task, "crit") if r.task == "failed_units" else r for r in full_day(per, lambda n, t: "ok")]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(recs, status={"tasks": {}}), TZ)
    assert d["health"]["score"] == 100 and d["health"]["grade"] == "A" and d["health"]["deductions"]["open"] == 0.0
    d2 = reports.build_report("daily", NOW_DAILY, inputs_of([rec(r.t, r.task, r.status) for r in recs], status={"tasks": {}}), TZ)
    assert d2["health"]["score"] <= 89 and d2["health"]["score"] < d["health"]["score"]


def test_acknowledgement_ending_mid_day_counts_only_from_that_moment():
    per = day_period()
    cut = per.start + 12 * 3600
    recs = [(arec if r.t < cut else rec)(r.t, r.task, "warn") if r.task == "failed_units" else r for r in full_day(per, lambda n, t: "ok")]
    c = reports.analyse_checks(inputs_of(recs), per, TZ, set())
    assert c["warn_min"] == 12 * 60 and c["open"] == {"failed_units": 1}                           # warning again since noon, still open


def test_an_acknowledged_backup_problem_costs_no_backup_points_but_the_peak_is_kept():
    per = day_period()
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",))
    recs += [arec(per.end - 600, "backup_freshness", "crit", failed=2)]
    c = reports.analyse_checks(inputs_of(sorted(recs, key=lambda r: r.t)), per, TZ, set())
    assert c["backups"] == {"failed": 0, "stale": 0, "seen": True, "peak_failed": 2}
    d = reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), status={"tasks": {}}), TZ)
    assert d["health"]["deductions"]["backups"] == 0.0


def test_history_files_flag_is_read_and_a_non_true_value_is_not_an_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    t = at(2026, 10, 6, 12)
    rows = [{"t": t + i, "kind": "task", "task": "failed_units", "status": "warn", "alert": True, **extra}
            for i, extra in enumerate(({"acked": True}, {"acked": False}, {"acked": 1}, {"acked": "yes"}, {}))]
    (tmp_path / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    recs, _disk, _first = reports._scan_history(tmp_path / "history.jsonl", t - 10, t + 100)
    assert [r.acked for r in recs] == [True, False, False, False, False] and [r.info for r in recs] == [True, False, False, False, False]
    assert [reports._lvl(r, set()) for r in recs] == [0, 1, 1, 1, 1]


def test_acknowledged_incidents_are_listed_but_not_counted_as_open(inputs):
    t = at(2026, 10, 3, 10, 0)
    snap = {"open": [{"id": "inc-1", "task": "failed_units", "title": "Services down", "severity": "sev2", "since": t, "status": "acknowledged",
                      "ack": {"until": t + 90 * 86400}},
                     {"id": "inc-2", "task": "disk_forecast", "title": "Disk space low", "severity": "sev3", "since": t, "status": "open"}],
            "recent": []}
    d = rebuild(dataclasses.replace(inputs, incidents=snap))
    i = d["incidents"]
    assert i["open_now"] == 1 and i["acknowledged_now"] == 1 and i["opened"] == 2
    rows = {r["id"]: r for r in i["list"]}
    assert rows["inc-1"]["acknowledged"] is True and "acknowledged" not in rows["inc-2"]
    assert d["health"]["deductions"]["incidents"] == 2.0                                          # only the sev3: the acknowledged sev2 costs nothing


def test_the_incidents_shape_is_unchanged_without_acknowledged_incidents(inputs):
    i = rebuild(inputs)["incidents"]
    assert "acknowledged_now" not in i and all("acknowledged" not in r for r in i["list"])


ACKS = {"generated_at": 1.0, "stats": {"active": 3, "expired_30d": 2},
        "acks": [{"id": "0123456789abcdef", "task": "failed_units", "title": "Services & containers", "severity": "warn", "until": at(2026, 12, 20, 12),
                  "by": "email", "suppressed": 14, "active": True, "summary": "1 unhealthy: kavita", "note": "known flap"},
                 {"id": "fedcba9876543210", "task": "disk_forecast", "title": "Disk space", "severity": "crit", "until": at(2026, 10, 20, 12),
                  "by": "web", "suppressed": 0, "active": False, "summary": "/ 3% free", "note": ""}]}


def test_the_acknowledged_section_lists_every_ack_with_its_expiry(inputs):
    d = rebuild(dataclasses.replace(inputs, acks=ACKS))
    a = d["acknowledged"]
    assert a["available"] is True and a["count"] == 2 and a["failing"] == 1 and a["expired_30d"] == 2
    assert [r["id"] for r in a["list"]] == ["fedcba9876543210", "0123456789abcdef"]                # soonest expiry first
    r = a["list"][1]
    assert (r["task"], r["severity"], r["suppressed"], r["by"], r["note"], r["active"]) == ("failed_units", "warn", 14, "email", "known flap", True)
    assert r["until_label"] == "Sun 20 Dec 2026" and r["days_left"] == 75
    assert any("2 acknowledged issues you accepted are not counted as problems" in h and "Disk space until Tue 20 Oct 2026" in h
               and "1 still failing" in h for h in d["highlights"])
    json.dumps(d, allow_nan=False)


def test_without_an_acks_file_the_section_says_unavailable_and_nothing_else_changes(inputs):
    a = rebuild(inputs)["acknowledged"]
    assert a == {"available": False, "count": 0, "failing": 0, "list": [], "expired_30d": 0}
    assert not any("acknowledged" in h for h in rebuild(inputs)["highlights"])


def test_an_empty_acks_file_adds_no_highlight(inputs):
    d = rebuild(dataclasses.replace(inputs, acks={"acks": [], "stats": {"active": 0, "expired_30d": 0}}))
    assert d["acknowledged"]["count"] == 0 and d["acknowledged"]["available"] is True
    assert not any("acknowledged" in h for h in d["highlights"])


@pytest.mark.parametrize("acks", [5, "x", [], {"acks": 5}, {"acks": [1, "x", None, {}, {"until": "no"}, {"until": float("nan")}]},
                                  {"acks": [{"until": 1e12 * 1e12}]}, {"acks": [{"until": at(2027, 1, 1), "title": ["x"], "suppressed": "many", "by": 5}]}])
def test_a_damaged_acks_file_degrades_the_section_not_the_report(inputs, acks):
    d = rebuild(dataclasses.replace(inputs, acks=acks))
    assert d["health"]["score"] is not None and "acknowledged" in d
    json.dumps(d, allow_nan=False)


def test_acknowledgement_text_is_redacted_and_ascii(inputs):
    acks = {"acks": [{"id": "0123456789abcdef", "task": "t", "title": "Mail ops@example.com", "severity": "warn", "until": at(2026, 12, 1),
                      "summary": "failed https://x.example/p?token=SECRET password=hunter2", "note": "call +1 416 555 0199 café", "by": "cli"}]}
    d = rebuild(dataclasses.replace(inputs, acks=acks))
    text = json.dumps(d["acknowledged"]) + json.dumps(d["highlights"])
    assert text.isascii() and "\n" not in text
    d2 = reports._scrub(d, reports._cleaner())
    blob = json.dumps(d2)
    for leak in ("example.com", "SECRET", "hunter2", "555"):
        assert leak not in blob


def test_gather_reads_the_public_acks_file(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "acks.json").write_text(json.dumps(ACKS))
    per = reports.period_for("daily", NOW_DAILY, TZ)
    assert reports.gather(per, TZ).acks == ACKS
    (tmp_path / "public" / "acks.json").write_text("{ torn")
    assert reports.gather(per, TZ).acks is None
    (tmp_path / "public" / "acks.json").write_text("[1]")
    assert reports.gather(per, TZ).acks is None


def test_the_slo_section_is_not_adjusted_by_acknowledgements(inputs):
    slo = [{"name": "Services up", "status": "at_risk", "availability_pct": 97.0, "budget_remaining_pct": 10.0}]
    a = rebuild(dataclasses.replace(inputs, acks=ACKS, slo={"objectives": slo}))["slo"]
    b = rebuild(dataclasses.replace(inputs, slo={"objectives": slo}))["slo"]
    assert a == b and a and a[0]["status"] == "at_risk"


def test_acknowledgement_audit_rows_are_not_maintenance_actions(inputs):
    t = at(2026, 10, 3, 12)
    rows = [{"t": t + i, "task": "acks", "action": a, "target": "0123456789abcdef", "bytes": 0, "o": o, "raw": ""}
            for i, (a, o) in enumerate((("ack", "done"), ("expire", "done"), ("refresh-failed", "failed"), ("unack", "refused")))]
    rows.append({"t": t + 9, "task": "trash", "action": "trash-empty", "target": "/x", "bytes": 5, "o": "done", "raw": ""})
    d = rebuild(dataclasses.replace(inputs, audit=rows))
    a = d["actions"]
    assert a["count"] == 1 and a["failed"] == 0 and a["refused"] == 0 and [e["task"] for e in a["by_task"]] == ["trash"]
    assert "acks" not in json.dumps(a["notable"])


# =========================================================================== gate_level: an io-only plateau is not memory pressure
def gate_day(*levels, **extra):
    """A quiet day plus one pressure_state record per entry of `levels` ((level, gate_level | None) pairs, one per hour)."""
    per = day_period()
    ps = [rec(per.start + 3600 * (i + 1), "pressure_state", "info" if g == 0 else "ok", level=lv, **({} if g is None else {"gate_level": g}))
          for i, (lv, g) in enumerate(levels)]
    recs = full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)) + ps
    return reports.build_report("daily", NOW_DAILY, inputs_of(sorted(recs, key=lambda r: r.t), spikes=[], audit=[], status={"tasks": {}}, **extra), TZ)


def test_level_max_reads_gate_level_so_an_io_only_night_is_not_reported_as_level_3():
    d = gate_day((3, 0), (3, 0), (3, 0))                       # this host holds io PSI at level 3 for hours every night
    assert d["pressure"]["level_max"] == 0 and d["spikes"]["worst_level"] == 0 and d["pressure"]["pressure_state_seen"] is True
    assert any(h.startswith("No load spikes were recorded") and "peak pressure level" not in h for h in d["highlights"])      # not "(peak pressure level 3)"
    assert gate_day((3, 0), (3, 2), (1, 1))["pressure"]["level_max"] == 2                          # memory/cpu pressure still counts
    assert gate_day((2, None), (1, None))["pressure"]["level_max"] == 2                            # records written before gate_level: the level
    assert gate_day((4, 0), (1, None))["pressure"]["level_max"] == 1                                # mixed: each record by its own best word
    assert gate_day((3, "x"), (2, None))["pressure"]["level_max"] == 3                              # junk gate_level falls back to the level, never raises


def test_a_spike_with_a_gate_peak_of_zero_is_an_io_or_gpu_only_spike_and_not_a_memory_one():
    per = day_period()
    t0 = per.start + 7200
    io = spike_ledger(t0, 900, 3, {"io": 3}, {"io_some60": 70.0}, [], "ended without intervention")
    mem = spike_ledger(t0 + 7200, 900, 2, {"mem": 2}, {"mem_full60": 8.0}, [], "ended without intervention")
    ledger = [{**r, "gate_peak": 0 if r["id"] == int(t0) else 2, "_t": r["t"]} for r in io + mem]
    d = reports.build_report("daily", NOW_DAILY, inputs_of(full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)), status={"tasks": {}},
                                                           spikes=ledger, audit=[]), TZ)
    s = d["spikes"]
    by = {e["level"]: e for e in s["list"]}
    assert s["count"] == 2 and s["worst_level"] == 2                 # the io spike (host level 3) did not make "worst" 3
    assert by[3]["gate_level"] == 0 and by[3]["resource"] == "disk I/O" and by[2]["gate_level"] == 2
    old = [{k: v for k, v in r.items() if k != "gate_peak"} for r in ledger]
    d2 = reports.build_report("daily", NOW_DAILY, inputs_of(full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)), status={"tasks": {}},
                                                            spikes=old, audit=[]), TZ)
    assert d2["spikes"]["worst_level"] == 3 and {e["gate_level"] for e in d2["spikes"]["list"]} == {None}      # a ledger from before gate_peak


# =========================================================================== aggregated report-mode audit rows
def test_one_aggregate_dry_run_row_counts_for_all_the_decisions_it_stands_for(tmp_path):
    """core.Ctx.flush_dry writes `n` on the row that stands for many would-do decisions: the report counts n, not 1."""
    per = day_period()
    p = tmp_path / "audit.jsonl"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(per.start + 3600))
    rows = [{"ts": ts, "task": "retention", "action": "delete", "target": f"/x/{i}", "bytes": 5, "outcome": "dry-run"} for i in range(20)]
    rows.append({"ts": ts, "task": "retention", "action": "delete", "target": "(+181 more, not listed)", "bytes": 905, "outcome": "dry-run", "n": 181})
    rows.append({"ts": ts, "task": "retention", "action": "delete", "target": "/y", "bytes": 5, "outcome": "dry-run", "n": "junk"})      # a bad n is 1
    rows.append({"ts": ts, "task": "retention", "action": "delete", "target": "/z", "bytes": 5, "outcome": "dry-run", "n": -4})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    got = reports._read_audit(p, per.start, per.end, TZ)
    assert [r["n"] for r in got][-3:] == [181, 1, 1] and len(got) == 23
    inp = inputs_of(full_day(per, lambda n, t: "ok", tasks=("disk_forecast",)), status={"tasks": {}}, audit=got, spikes=[])
    d = reports.build_report("daily", NOW_DAILY, inp, TZ)
    assert d["actions"]["dry_run"] == 20 + 181 + 1 + 1 and d["actions"]["count"] == 0          # nothing was done, 203 things would have been


def test_every_cleaner_title_reads_as_what_was_done():
    assert reports.ACTION_TITLES["stale_driver_packages"] == "Stale NVIDIA driver packages purged"
    assert reports.ACTION_TITLES["tool_caches"] == "Tool and app caches cleaned"
    assert "restarted" in reports.ACTION_TITLES["comfyui_idle_reclaim"] and "recycled" in reports.ACTION_TITLES["immich_recycle"]
    for t in ("stale_driver_packages", "apt_autoremove_unused", "flatpak_unused", "tool_caches", "stale_build_output", "unused_venvs", "large_cold_files",
              "app_cache_trim", "log_compress", "dangling_images", "crash_dumps", "apt_cache"):
        assert reports.ACTION_TITLES[t] and not reports.HARM_RX.search(reports.ACTION_TITLES[t]), t     # a title never makes a cleanup look like harm
