"""Tests for homelab_maint/publish.py: the public JSON export behind the maintenance web UI (SPEC2 section 2, SPEC3 section 3,
SPEC4 S12).

Security properties (no secrets/notify text/tracebacks, bounded sizes, hostile input) are tested as hard requirements, for the
SPEC2 files and for every passed-through file (routine, incidents, slo, pressure, jobs, monitors, notifications, migration).
Everything runs against tmp dirs with a fixed clock and TZ=UTC; systemctl, statvfs, the metrics ring and the modules that own the
passed-through files are stubbed (the `real_env` tests run the real modules against tmp dirs and the repo's etc/*.toml).
"""
import conftest  # noqa: F401  (points homelab_maint at throw-away dirs before it is imported)

import fcntl
import importlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from homelab_maint import core, publish as P

ROOT = Path(__file__).resolve().parent.parent
NOW = float(timegm((2026, 10, 1, 12, 0, 0)))          # noon UTC
GIB = 1024 ** 3
FILES = ["overview.json", "checks.json", "actions.json", "storage.json", "metrics.json", "schedule.json",
         "health-history.json"]
NEW_FILES = ["routine.json", "incidents.json", "slo.json", "pressure.json", "jobs.json", "monitors.json", "notifications.json",
             "migration.json"]
SELF_FILES = ["self.json"]                            # SPEC6 S6: the pipeline's own health (tasks.self_health.export), after migration.json
ALL_FILES = FILES + NEW_FILES + SELF_FILES            # what publish() returns when no report has been generated yet (the `env` fixture has no registry)
REGISTRY_FILES = ["rules.json", "rules-history.json", "manifest.json"]      # SPEC6 S5: written LAST, by _publish_registry (the `env` fixture stubs it)
OPTIONAL_FILES = ["jobs.json", "monitors.json", "notifications.json", "migration.json", "self.json"]     # their module may be absent


# =========================================================================== SPEC2 key lists as a tiny schema language
class Enum:
    def __init__(self, *v): self.v = v


class Opt:
    def __init__(self, s): self.s = s


class ListOf:
    def __init__(self, s, max=None): self.s, self.max = s, max


class Map:
    def __init__(self, s): self.s = s


class Sub:
    """Object spec: these keys must exist and match, other keys are allowed (the owning module may add fields)."""
    def __init__(self, s): self.s = s


NUM, SCALAR = "num", "scalar"
STATUS5 = Enum("ok", "info", "warn", "crit", "error")
DAY = "day"


def check(v, spec, path="$"):
    """Raise AssertionError naming the path when `v` does not match `spec` (dict spec = EXACT key set)."""
    if isinstance(spec, dict):
        assert isinstance(v, dict), f"{path}: not an object"
        assert set(v) == set(spec), f"{path}: keys {sorted(v)} != {sorted(spec)}"
        for k, s in spec.items():
            check(v[k], s, f"{path}.{k}")
    elif isinstance(spec, Enum):
        assert v in spec.v, f"{path}: {v!r} not in {spec.v}"
    elif isinstance(spec, Opt):
        if v is not None:
            check(v, spec.s, path)
    elif isinstance(spec, ListOf):
        assert isinstance(v, list), f"{path}: not a list"
        assert spec.max is None or len(v) <= spec.max, f"{path}: {len(v)} > {spec.max}"
        for i, x in enumerate(v):
            check(x, spec.s, f"{path}[{i}]")
    elif isinstance(spec, Map):
        assert isinstance(v, dict), f"{path}: not an object"
        for k, x in v.items():
            check(x, spec.s, f"{path}.{k}")
    elif isinstance(spec, Sub):
        assert isinstance(v, dict), f"{path}: not an object"
        for k, s in spec.s.items():
            assert k in v, f"{path}: missing key {k!r} (has {sorted(v)})"
            check(v[k], s, f"{path}.{k}")
    elif spec is int:
        assert isinstance(v, int) and not isinstance(v, bool), f"{path}: {v!r} not int"
    elif spec == NUM:
        assert isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v), f"{path}: {v!r} not a number"
    elif spec == SCALAR:
        assert v is None or isinstance(v, (bool, int, float, str)), f"{path}: {v!r} not a scalar"
        assert not isinstance(v, float) or math.isfinite(v), f"{path}: non-finite"
    elif spec == DAY:
        assert isinstance(v, str) and len(v) == 10 and v[4] == v[7] == "-", f"{path}: {v!r} not YYYY-MM-DD"
    else:
        assert isinstance(v, spec), f"{path}: {v!r} not {spec}"


CHECK_ROW = {"name": str, "title": str, "klass": Enum("C0", "C1", "C2"), "tier": Enum("check", "daily", "weekly", "monthly"),
             "status": STATUS5, "summary": str, "last_run": Opt(NUM), "duration_s": NUM,
             "mode": Enum("check", "report", "apply", "dry-run"), "metrics": Map(SCALAR),
             "items": ListOf(Map(SCALAR), 8)}
TIER = {"last_run": Opt(NUM), "next_run": Opt(NUM), "mode": Enum("check", "report", "apply")}
SCHEMAS = {
    "overview.json": {"schema": int, "generated_at": NUM, "host": str, "overall": Enum("ok", "warn", "crit"), "paused": bool,
                      "counts": {k: int for k in ("ok", "warn", "crit", "info", "error")}, "headline": str,
                      "tiers": {"check": TIER, "daily": TIER, "weekly": TIER, "monthly": TIER},
                      "cleanup_mode": Enum("report", "apply", "mixed"), "uptime_s": int, "kernel": str,
                      "live_age_s": Opt(NUM), "export_errors": ListOf(str)},
    "checks.json": {"generated_at": NUM, "checks": ListOf(CHECK_ROW)},
    "actions.json": {"generated_at": NUM,
                     "recent": ListOf({"ts": NUM, "task": str, "action": str, "target": str, "bytes": int, "outcome": str}, 300),
                     "by_task": Map({"last_run": Opt(NUM), "last_action": Opt(NUM), "last_outcome": str, "freed_24h": int,
                                     "freed_7d": int, "freed_30d": int, "freed_total": int, "runs_30d": int}),
                     "totals": {"freed_24h": int, "freed_7d": int, "freed_30d": int, "actions_24h": int, "actions_7d": int,
                                "actions_30d": int},
                     "journal": ListOf({"ts": NUM, "title": str, "detail": str}, 100)},
    "storage.json": {"generated_at": NUM,
                     "mounts": ListOf({"mount": str, "free_b": int, "size_b": int, "used_pct": NUM, "free_h": str,
                                       "days": Opt(int), "level": Enum("ok", "info", "warn", "crit"), "info": bool}),
                     "series": Map({"t": ListOf(int, 720), "free_gib": ListOf(NUM, 720)}),
                     "freed_by_day": ListOf({"day": DAY, "bytes": int}, 30)},
    "schedule.json": {"generated_at": NUM, "timers": ListOf({"unit": str, "title": str, "last": Opt(NUM), "next": Opt(NUM),
                                                              "schedule": str})},
    "health-history.json": {"generated_at": NUM,
                            "days": ListOf({"day": DAY, "worst": Enum("ok", "warn", "crit", "unknown"),
                                            "warn_minutes": int, "crit_minutes": int}, 30)},
}
# The passed-through files (SPEC3 section 2/3, SPEC4 S12): the keys the web tabs rely on must exist, the owning module may add more.
STEP = Sub({"task": str, "class": str, "mode": str, "next_due": Opt(NUM), "last_run": Opt(NUM), "last_outcome": str})
TIMELINE = Sub({"t": NUM, "kind": str, "text": str})
NEW_SCHEMAS = {
    "routine.json": Sub({"generated_at": NUM, "windows": dict, "freeze": dict,
                         "routine": ListOf(Sub({"name": str, "cadence": Enum("daily", "weekly", "monthly"), "window": str,
                                                "steps": ListOf(STEP), "next_run": Opt(NUM)})),
                         "changes": ListOf(Sub({"ts": NUM, "task": str, "kind": str, "detail": str, "bytes": int, "outcome": str,
                                                "verified": bool}), 100),
                         "calendar": ListOf(Sub({"date": DAY, "items": ListOf(Sub({"time": str, "title": str, "kind": str}))}), 14)}),
    "incidents.json": Sub({"generated_at": NUM,
                           "open": ListOf(Sub({"id": str, "task": str, "title": str, "severity": str, "since": NUM, "duration_s": int,
                                               "summary": str, "playbook": dict, "timeline": ListOf(TIMELINE, 20),
                                               "related_actions": ListOf(dict, 10)})),
                           "recent": ListOf(Sub({"id": str, "task": str, "mttr_s": int, "postmortem_md": str, "timeline": ListOf(TIMELINE)}), 50),
                           "stats": Sub({"mttd_s_30d": Opt(int), "mttr_s_30d": Opt(int), "incidents_30d": int, "open_count": int})}),
    "slo.json": Sub({"generated_at": NUM, "window_days": int,
                     "objectives": ListOf(Sub({"name": str, "target_pct": NUM, "class": str, "checks": ListOf(str),
                                               "availability_pct": Opt(NUM), "budget_remaining_pct": Opt(NUM),
                                               "burn_rate_1d": Opt(NUM), "status": Enum("ok", "at_risk", "breached")}))}),
    "pressure.json": Sub({"generated_at": NUM, "level": int, "since": Opt(NUM),
                          "history": ListOf(Sub({"t": NUM, "level": int}), 96), "spikes": ListOf(dict, 30), "actions": ListOf(dict, 50),
                          "classes": ListOf(Sub({"class": str, "members": ListOf(str), "policy": str}))}),
    "jobs.json": Sub({"generated_at": NUM,
                      "jobs": ListOf(Sub({"job": str, "title": str, "source": Enum("native", "adapter", "os", "external", "task"), "mode": str,
                                          "class": str, "schedule": str, "next_due": Opt(NUM), "last_start": Opt(NUM),
                                          "last_end": Opt(NUM), "last_status": Opt(str)}))}),
    "monitors.json": Sub({"generated_at": NUM,
                          "probes": ListOf(Sub({"name": str, "title": str, "type": str, "class": str, "source": str, "state": str,
                                                "interval_s": Opt(NUM), "avail_30d": Opt(NUM)}))}),
    "notifications.json": Sub({"generated_at": NUM,
                               "recent": ListOf(Sub({"ts": NUM, "kind": str, "severity": str, "title": str, "ok": bool,
                                                     "channels": ListOf(str), "note": str}), 100),
                               "counts": Sub({"24h": Sub({"total": int, "failed": int}), "7d": Sub({"total": int, "failed": int})}),
                               "failures": Sub({"24h": int, "7d": int})}),
    "migration.json": Sub({"generated_at": NUM, "total": int, "retirable": int, "retired": int, "remaining": ListOf(str),
                           "complete": bool, "items": ListOf(Sub({"name": str, "title": str, "mode": str, "state": str,
                                                                 "replaced_by": str}))}),
}
SCHEMAS.update(NEW_SCHEMAS)
SCHEMAS["self.json"] = Sub({"schema": int, "generated_at": NUM, "level": Enum("ok", "degraded", "down"), "headline": str,
                            "verdict": Sub({"level": Enum("ok", "degraded", "down"), "reasons": ListOf(str), "since": NUM}),
                            "ttl": Sub({"degraded_after_s": NUM, "down_after_s": NUM}),
                            "checks": ListOf(Sub({"id": str, "title": str, "state": Enum("ok", "degraded", "down", "info", "unknown"), "detail": str}))})
RING_KEYS = {"generated_at", "interval_s", "slots", "current", "hour_avg", "prev_hour_avg", "avg_24h", "avg_7d", "max_7d",
             "loop", "series"}


# =========================================================================== fixtures
def ring_export(now=None, **over):
    ex = {"generated_at": NOW, "interval_s": 3600, "slots": 168, "current": {"cpu_temp": 47.0, "sampled_at": NOW},
          "hour_avg": {"cpu_temp": 46.0}, "prev_hour_avg": {"cpu_temp": 45.0}, "avg_24h": {"cpu_temp": 44.0},
          "avg_7d": {"cpu_temp": 43.0}, "max_7d": {"cpu_temp": 71.0},
          "loop": {"pos": 167, "overwrites_next_at": NOW + 3600, "oldest_hour": NOW - 167 * 3600, "complete": True},
          "series": {"t": [NOW - (167 - i) * 3600 for i in range(168)], "cpu_temp": [40.0 + i % 9 for i in range(168)]}}
    ex.update(over)
    return ex


# --- stand-ins for the modules that own the passed-through files; each returns the SPEC shape (fresh dicts every call)
def fake_routine(now=NOW, timers=None):
    step = {"task": "docker_cache", "name": "docker_cache", "title": "Docker cache", "class": "C1", "mode": "report", "state": "done",
            "reason": "", "disruptive": False, "next_due": NOW + 3600, "last_run": NOW - 3600, "last_outcome": "ok"}
    return {"schema": 1, "generated_at": now, "timezone": "UTC", "valid": True, "enforce": False, "errors": [],
            "windows": {"daily": "07:30-09:30", "weekly": "Wed 07:45-10:00", "monthly": "1st Sat 04:30-07:00"},
            "freeze": {"evenings": "18:00-23:30"}, "state": {"paused": False},
            "routine": [{"name": "daily", "cadence": "daily", "window": "07:30-09:30", "steps": [dict(step)], "next_run": NOW + 19 * 3600},
                        {"name": "weekly", "cadence": "weekly", "window": "Wed 07:45-10:00", "steps": [dict(step)], "next_run": NOW + 5 * 86400},
                        {"name": "monthly", "cadence": "monthly", "window": "1st Sat 04:30-07:00",
                         "steps": [dict(step, task="routine_trends")], "next_run": NOW + 10 * 86400}],
            "changes": [{"ts": NOW - 100 * i, "task": "docker_cache", "kind": "cleanup", "detail": f"pruned {i}", "bytes": i,
                         "outcome": "done", "verified": True} for i in range(5)],
            "calendar": [{"date": f"2026-10-{d:02d}", "items": [{"time": "07:30", "title": "Daily routine", "kind": "daily",
                                                                  "source": "routine"}]} for d in range(1, 15)]}


def fake_incidents(now=NOW):
    inc = {"id": "INC-20261001-001", "task": "backup_freshness", "title": "Backups", "severity": "sev2", "level": 2, "status": "open",
           "since": NOW - 7200, "duration_s": 7200, "summary": "backup-system 9d old", "cause_hint": "check the timer",
           "timeline": [{"t": NOW - 7200, "kind": "detect", "text": "confirmed crit"}], "related_actions": [],
           "playbook": {"task": "backup_freshness", "title": "Backups", "class": "P1", "meaning": "a backup is overdue",
                        "checks": ["$ systemctl list-timers"], "fixes": ["run it by hand"], "avoid": ["do not delete the old copy"],
                        "ask": "the owner"}}
    done = {"id": "INC-20260928-001", "task": "failed_units", "title": "Services", "severity": "sev3", "status": "resolved",
            "since": NOW - 4 * 86400, "duration_s": 600, "summary": "nginx failed", "resolved_at": NOW - 4 * 86400 + 600, "mttr_s": 600,
            "timeline": [{"t": NOW - 4 * 86400, "kind": "detect", "text": "confirmed warn"}], "related_actions": [],
            "postmortem_md": "# Postmortem: Services (INC-20260928-001)\n\n## Summary\n\n- one\n- two"}
    return {"generated_at": now, "open": [inc], "recent": [done],
            "stats": {"mttd_s_30d": 900, "mttr_s_30d": 600, "incidents_30d": 2, "open_count": 1}}


def fake_slo(now=NOW):
    return {"generated_at": now, "window_days": 30,
            "objectives": [{"name": "host health", "target_pct": 99.0, "class": "P1", "checks": ["disk_forecast", "failed_units"],
                            "availability_pct": 99.5, "budget_remaining_pct": 50.0, "burn_rate_1d": 0.5, "status": "ok"},
                           {"name": "backups", "target_pct": 99.0, "class": "P1", "checks": ["backup_freshness"],
                            "availability_pct": None, "budget_remaining_pct": None, "burn_rate_1d": None, "status": "ok"}]}


def fake_pressure(now=NOW):
    return {"generated_at": now, "level": 1, "gate_level": 0, "since": NOW - 600, "level_name": "elevated",
            "history": [{"t": NOW - 900 * i, "level": i % 3} for i in range(95, -1, -1)],
            "spikes": [{"t": NOW - 3600 * i, "peak": 2, "duration_s": 540, "contributors": ["tunarr"], "outcome": "resolved"} for i in range(5)],
            "actions": [{"ts": NOW - 60 * i, "level": 2, "rung": "reclaim", "action": "ollama-unload", "target": "llama3", "class": "P2",
                         "outcome": "would"} for i in range(5)],
            "classes": [{"class": c, "members": ["plex", "immich_server"], "policy": "never throttled"} for c in ("P0", "P1", "P2", "P3")]}


def fake_jobs(now=NOW):
    return {"generated_at": now, "tick": {"status": "ok", "summary": "ticking"}, "counts": {"managed": 1, "external": 1},
            "jobs": [{"job": "backup-system", "title": "System backup", "source": "adapter", "mode": "observe", "class": "P2", "heavy": True,
                      "monitor": True, "schedule": "weekly sat 01:00", "next_due": NOW + 86400, "last_start": NOW - 5 * 86400,
                      "last_end": NOW - 5 * 86400 + 900, "last_status": "ok", "last_summary": "ok", "why": "waiting for its schedule"},
                     {"job": "fstrim", "title": "SSD trim", "source": "os", "mode": "external", "class": "P3", "heavy": False,
                      "monitor": False, "schedule": "weekly mon", "next_due": None, "last_start": None, "last_end": None,
                      "last_status": None, "last_summary": "", "why": "managed externally: distro"}]}


def fake_monitors(now=NOW):
    return {"schema": 1, "generated_at": now, "last_run": NOW - 30, "stale": False, "summary": {"total": 2, "up": 1, "down": 1},
            "sources": {"native": 2}, "errors": 0, "kuma": {"ported": 0},
            "probes": [{"name": "plex", "title": "Plex", "type": "http", "group": "media", "class": "P1", "source": "native", "state": "down",
                        "since": NOW - 300, "last_run": NOW - 30, "interval_s": 60, "ms": None, "detail": "connection refused",
                        "avail_7d": 99.1, "avail_30d": 99.5, "slo": 99.0},
                       {"name": "immich", "title": "Immich", "type": "http", "group": "media", "class": "P1", "source": "native",
                        "state": "up", "since": NOW - 9000, "last_run": NOW - 30, "interval_s": 60, "ms": 12, "detail": "",
                        "avail_7d": 100.0, "avail_30d": 100.0, "slo": 99.0}]}


def fake_notifications(now=NOW):
    return {"schema": 1, "generated_at": now,
            "recent": [{"ts": NOW - 60, "kind": "alert", "severity": "crit", "title": "Backups: backup-system 9d old", "ok": True,
                        "channels": ["sms", "email"], "note": "sms sent; email sent"}],
            "counts": {"24h": {"total": 1, "sent": 1, "failed": 0}, "7d": {"total": 3, "sent": 2, "failed": 1}},
            "failures": {"24h": 0, "7d": 1, "last": None}, "by_kind_24h": {"alert": 1}, "legs": {"sms": {"ok": 1, "fails": 0}}}


def fake_migration(now=NOW):
    return {"generated_at": now, "total": 2, "retirable": 2, "retired": 1, "remaining": ["mem-guard"], "next": "mem-guard", "complete": False,
            "items": [{"name": "mem-guard", "title": "mem-guard timer", "mode": "retire", "kind": "timer", "state": "pending",
                       "replaced_by": "pressure_state", "retirable": True, "blocked_by": []},
                      {"name": "thermal-log", "title": "thermal logger", "mode": "retire", "kind": "unit", "state": "retired",
                       "replaced_by": "metrics-sample", "retirable": True, "blocked_by": []}]}


def fake_self(now=NOW):
    ok = lambda i, t, d: {"id": i, "title": t, "state": "ok", "detail": d}      # noqa: E731
    return {"schema": 2, "generated_at": now, "valid_until": now + 180, "level": "ok", "headline": "Monitoring pipeline: healthy",
            "verdict": {"level": "ok", "reasons": [], "since": now - 86400},
            "ttl": {"refresh_s": 60, "degraded_after_s": 180, "down_after_s": 600}, "limits": {"runner_late_s": 1470, "runner_down_s": 2700},
            "checks": [ok("runner", "Runner (check tier)", "last check run 4 min ago"), ok("publish", "Publish to website", "published 1 min ago"),
                       {"id": "website", "title": "Website", "state": "info", "detail": "the website is not deployed yet"}],
            "metrics": {"errors_24h": 0, "runs_24h": 96, "error_rate_pct": 0.0, "web": "not deployed"}}


FAKE_SOURCES = {"jobs.json": fake_jobs, "monitors.json": fake_monitors, "notifications.json": fake_notifications,
                "migration.json": fake_migration, "self.json": fake_self}
# the five always-present exports, by the module-level seam publish.py provides
SEAMS = {"routine.json": ("_routine_export", lambda now, timers: fake_routine(now)),
         "incidents.json": ("_incidents_export", fake_incidents), "slo.json": ("_slo_export", lambda now, history=None: fake_slo(now)),
         "pressure.json": ("_pressure_export", fake_pressure)}

REAL_PUBLISH_REGISTRY = P._publish_registry
REAL_LIST_TIMERS = P._list_timers
REAL_TIMER_SPECS = P._timer_specs
TIMERS = [  # `systemctl list-timers --all --output=json` shape: epoch MICROseconds; never-run timers have last 0/null
    {"unit": "comfyui-idle-vram.timer", "next": int((NOW + 100) * 1e6), "last": int((NOW - 100) * 1e6)},
    {"unit": "homelab-maint-weekly.timer", "next": int((NOW + 5 * 86400) * 1e6), "last": 0},
    {"unit": "homelab-maint-check.timer", "next": int((NOW + 600) * 1e6), "last": int((NOW - 300) * 1e6)},
    {"unit": "homelab-maint-daily.timer", "next": int((NOW + 19 * 3600) * 1e6), "last": None},
    {"unit": "backup-system.timer", "next": int((NOW + 86400) * 1e6), "last": int((NOW - 5 * 86400) * 1e6)},
    {"unit": "fstrim.timer", "next": None, "last": int((NOW - 3 * 86400) * 1e6)},
    {"unit": "motd-news.timer", "next": int((NOW + 5) * 1e6), "last": int((NOW - 5) * 1e6)},
]
SPECS = {"homelab-maint-check.timer": "*-*-* *:00/15:00", "homelab-maint-daily.timer": "*-*-* 07:30:00",
         "homelab-maint-weekly.timer": "Wed *-*-* 07:45:00", "backup-system.timer": "Sat *-*-* 01:00:00",
         "fstrim.timer": "Mon *-*-* 00:00:00"}


def entry(klass="C0", tier="check", status="ok", summary="ok: all good", **kw):
    e = {"title": kw.pop("title", "Check"), "klass": klass, "tier": tier, "status": status, "summary": summary,
         "last_run": NOW - 300, "duration_s": 0.5, "reclaimed_bytes": 0, "metrics": {}, "items": [], "alert": True,
         "mode": "check" if klass == "C0" else "dry-run"}
    e.update(kw)
    return e


def make_status(**over):
    mounts = [
        {"mount": "/", "free": 100 * GIB, "free_h": "100.0 GiB", "used_pct": 88.3, "days": 12.4, "level": "warn", "info": False,
         "size": 855 * GIB},
        {"mount": "/media/SandiskSSD", "free": 400 * GIB, "free_h": "400.0 GiB", "used_pct": 60.0, "days": None, "level": "ok",
         "info": False},
        {"mount": "/media/WD22TB", "free": 20 * GIB, "free_h": "20.0 GiB", "used_pct": 99.9, "days": None, "level": "info",
         "info": True},
    ]
    st = {"schema": 1, "generated_at": NOW - 120, "host": "ohmz-homelab", "overall": "crit", "paused": False,
          "reclaimed_log": [],
          "tier_runs": {"check": {"last_run": NOW - 120, "dry_run": False}, "daily": {"last_run": NOW - 5 * 3600, "dry_run": False},
                        "weekly": {"last_run": NOW - 3 * 86400, "dry_run": True}},
          "tasks": {
              "disk_forecast": entry(status="warn", summary="warn: / 100.0 GiB free", title="Disk space",
                                     metrics={"mounts": mounts, "root_free_h": "100.0 GiB", "root_days": 12.4},
                                     items=[{"mount": "/", "free_h": "100.0 GiB", "level": "warn"}]),
              "failed_units": entry(status="warn", summary="warn: 2 failed units", title="Services"),
              "backup_freshness": entry(status="crit", summary="crit: backup-system 9d old", title="Backups"),
              "memory_health": entry(status="error", summary="error: timed out after 120s", title="Memory"),
              "orphan_report": entry(status="info", summary="info: 2 zombies", title="Orphans", alert=False),
              "docker_df": entry(status="warn", summary="warn: build cache 30 GiB", title="Docker disk", alert=False),
              "stuck_detector": entry(status="skipped", summary="skipped: no samples", title="Stuck containers"),
              "image_ledger": entry(summary="ok: 69 tracked", title="Image ledger"),
              "docker_cache": entry("C1", "daily", summary="ok: would prune 1 GiB", title="Docker cache"),
              "snap_revisions": entry("C1", "daily", summary="ok: removed 2", title="Snap revisions", mode="apply",
                                      reclaimed_bytes=5 * GIB),
              "c2_candidates": entry("C2", "weekly", summary="ok: 3 candidates", title="Big old files",
                                     plan={"items": [{"name": "/home/ohmz/x", "bytes": 1}], "total_bytes": 1}, plan_hash="abc"),
          }}
    st.update(over)
    return st


class Env:
    def __init__(self, tmp: Path):
        self.state, self.log, self.out = tmp / "state", tmp / "log", tmp / "state" / "public"
        self.state.mkdir()
        self.log.mkdir()

    def run(self, status="auto", now=NOW):
        return P.publish(make_status() if status == "auto" else status, now)

    def load(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def raw(self) -> bytes:
        """Every byte published, reports/ included."""
        return b"".join(p.read_bytes() for p in sorted(self.out.rglob("*")) if p.is_file())

    def audit(self, recs):
        with open(self.log / "audit.jsonl", "a") as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def history(self, recs):
        with open(self.state / "history.jsonl", "a") as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(r, separators=(",", ":"))) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(P, "_list_timers", lambda: [dict(t) for t in TIMERS])
    monkeypatch.setattr(P, "_timer_specs", lambda units: {u: SPECS[u] for u in units if u in SPECS})
    monkeypatch.setattr(P, "_statvfs_sizes", lambda mounts, budget=0.5: {})
    monkeypatch.setattr(P, "_metrics_export", lambda now: ring_export())
    for seam, fn in SEAMS.values():
        monkeypatch.setattr(P, seam, fn)
    monkeypatch.setattr(P, "_find_source", lambda name: FAKE_SOURCES.get(name))
    monkeypatch.setattr(P, "_publish_registry", lambda pub, status, now: [])        # the registry files have their own tests (REAL_PUBLISH_REGISTRY)
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield Env(tmp_path)
    monkeypatch.undo()
    time.tzset()


def iso(t, tz_h=-4):
    return datetime.fromtimestamp(t, timezone(timedelta(hours=tz_h))).strftime("%Y-%m-%dT%H:%M:%S%z")


def rec(t, task="retention", action="retention-delete", target="/var/log/old.log", nbytes=GIB, outcome="done"):
    return {"ts": iso(t), "task": task, "action": action, "target": target, "bytes": nbytes, "outcome": outcome}


# =========================================================================== structure, modes, atomicity
def test_all_files_written_valid_and_world_readable(env):
    assert env.run() == ALL_FILES
    assert oct(env.out.stat().st_mode & 0o777) == "0o755"
    for n in ALL_FILES:
        p = env.out / n
        assert oct(p.stat().st_mode & 0o777) == "0o644", n
        assert p.stat().st_size < 200_000
        doc = json.loads(p.read_bytes().decode("utf-8"))             # strict UTF-8, strict JSON
        if n in SCHEMAS:
            check(doc, SCHEMAS[n])
    assert RING_KEYS <= set(env.load("metrics.json"))
    assert not [p for p in os.listdir(env.out) if p.startswith(".")]  # no temp files left behind


def test_schema_holds_with_rich_inputs(env):
    env.audit([rec(NOW - 3600), rec(NOW - 10, task="notify", action="send", target="disk: WARN", outcome="sent"),
               rec(NOW - 20, outcome="dry-run"), rec(NOW - 30, outcome="refused-protected"),
               rec(NOW - 40, task="docker_images", outcome="failed: boom")])
    env.history([{"t": NOW - 900 * i, "kind": "disk", "mount": "/", "free": 100 * GIB + i} for i in range(300)]
                + [{"t": NOW - 900 * i, "kind": "task", "task": "failed_units", "status": "warn"} for i in range(50)])
    (env.state / "maintenance-journal.jsonl").write_text(json.dumps({"ts": NOW - 5, "title": "Swapped fan", "detail": "case fan"}) + "\n")
    env.run(make_status(reclaimed_log=[{"t": NOW - 3600, "task": "snap_revisions", "bytes": 5 * GIB}]))
    for n, spec in SCHEMAS.items():
        check(env.load(n), spec)


def test_dir_created_with_0755_even_under_strict_umask(env):
    old = os.umask(0o077)
    try:
        env.run()
    finally:
        os.umask(old)
    assert oct(env.out.stat().st_mode & 0o777) == "0o755"
    assert oct((env.out / "overview.json").stat().st_mode & 0o777) == "0o644"


def test_reads_status_file_when_no_status_given(env):
    (env.state / "status.json").write_text(json.dumps(make_status()))
    assert P.publish(None, NOW) == ALL_FILES
    assert env.load("overview.json")["host"] == "ohmz-homelab"
    assert P.publish("not a dict", NOW) == ALL_FILES                      # junk argument falls back to the file


def test_no_status_keeps_previous_status_files(env):
    env.run()
    before = {n: (env.out / n).read_bytes() for n in ("overview.json", "checks.json", "storage.json")}
    (env.state / "status.json").unlink(missing_ok=True)
    written = P.publish(None, NOW + 60)
    assert "overview.json" not in written and "checks.json" not in written and "storage.json" not in written
    assert {"actions.json", "schedule.json", "health-history.json"} <= set(written)
    for n, b in before.items():
        assert (env.out / n).read_bytes() == b                       # previous file untouched, it will visibly age


def test_never_raises_and_isolates_failing_builders(env, monkeypatch):
    env.run()
    old = (env.out / "checks.json").read_bytes()

    def boom(run):
        raise RuntimeError("secret=hunter2")
    monkeypatch.setattr(P, "BUILDERS", [("checks.json", boom)] + [b for b in P.BUILDERS if b[0] != "checks.json"])
    written = env.run(now=NOW + 60)
    assert "checks.json" not in written and "overview.json" in written
    assert (env.out / "checks.json").read_bytes() == old


@pytest.mark.parametrize("now", ["abc", object(), float("nan")])
def test_hostile_now_does_not_raise(env, now):
    assert isinstance(P.publish(make_status(), now), list)


def test_unwritable_public_dir_returns_empty(tmp_path, monkeypatch):
    blocker = tmp_path / "state"
    blocker.write_text("i am a file")                                 # STATE_DIR/public cannot be created
    monkeypatch.setattr(core, "STATE_DIR", blocker)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    assert P.publish(make_status(), NOW) == []


def test_atomic_replace_failure_keeps_old_file_and_cleans_tmp(env, monkeypatch):
    env.run()
    old = (env.out / "checks.json").read_bytes()
    real = os.replace

    def flaky(src, dst):
        if str(dst).endswith("checks.json"):
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(P.os, "replace", flaky)
    written = env.run(make_status(overall="warn"), NOW + 60)
    assert "checks.json" not in written and "overview.json" in written
    assert (env.out / "checks.json").read_bytes() == old
    assert not [p for p in os.listdir(env.out) if p.startswith(".")]


def test_garbage_status_still_produces_valid_files(env):
    st = {"generated_at": NOW, "overall": 7, "paused": "yes", "host": None, "tier_runs": "x", "reclaimed_log": "x",
          "tasks": {"a": None, "b": {"status": 5, "metrics": "x", "items": "y", "last_run": "never", "klass": 3, "tier": []},
                    "c": {"status": "warn", "metrics": {"k": [1], "n": float("nan"), "i": float("inf")}, "items": [1, None, {"a": [1]}]},
                    5: {"status": "ok"},
                    "disk_forecast": {"status": "ok", "metrics": {"mounts": [{"mount": "/", "free": "lots", "used_pct": "x", "days": [1],
                                                                              "level": []}]}}}}
    assert env.run(st) == ALL_FILES
    for n, spec in SCHEMAS.items():
        check(env.load(n), spec)


def test_unhashable_and_odd_field_types_do_not_sink_a_file(env):
    st = make_status()
    st["tasks"]["weird"] = {"status": ["warn"], "tier": ["daily"], "klass": {"a": 1}, "mode": [], "title": ["t"], "summary": {"s": 1},
                            "alert": [], "last_run": [1], "duration_s": "fast"}
    st["tasks"]["failed_units"]["tier"] = {"x": 1}
    assert env.run(st) == ALL_FILES
    for n, spec in SCHEMAS.items():
        check(env.load(n), spec)


def test_surrogates_nan_and_odd_types_are_strict_json(env):
    st = make_status()
    st["tasks"]["failed_units"]["summary"] = "warn: bad\udcffname 漢字"
    st["tasks"]["failed_units"]["metrics"] = {"x": float("nan"), "y": float("-inf"), "z": "a\udcb0"}
    env.run(st)
    for n in ALL_FILES:
        text = (env.out / n).read_bytes().decode("utf-8")             # raises on lone surrogates
        json.loads(text, parse_constant=lambda c: pytest.fail(f"{n} contains {c}"))


def test_runs_fast_on_a_realistic_install(env):
    env.history([{"t": NOW - 900 * k, "kind": "task", "task": f"t{i}", "status": "ok", "reclaimed": 0, "dur": 1.0, "metrics": {"a": 1}}
                 for k in range(2 * 96) for i in range(26)])
    t0 = time.time()
    env.run()
    assert time.time() - t0 < 1.0


# =========================================================================== overview.json
def test_overview_counts_overall_headline_and_alert_false(env):
    env.run()
    o = env.load("overview.json")
    # warn/crit findings flagged alert=False and skipped checks are informational, exactly as cli.overall treats them
    assert o["counts"] == {"ok": 4, "warn": 2, "crit": 1, "info": 3, "error": 1}
    assert o["overall"] == "crit" and o["generated_at"] == NOW - 120 and o["paused"] is False
    assert len(o["headline"]) <= 80 and o["headline"] == "1 critical, 1 failed, 2 warnings: Backups, Memory, Disk space +1"
    assert o["host"] == "ohmz-homelab" and o["uptime_s"] >= 0 and o["kernel"]


def test_headline_examples(env):
    st = make_status()
    st["tasks"] = {k: v for k, v in st["tasks"].items() if k in ("disk_forecast", "failed_units", "image_ledger")}
    env.run(st)
    assert env.load("overview.json")["headline"] == "2 warnings: Disk space, Services"
    st["tasks"].pop("disk_forecast"), st["tasks"].pop("failed_units")
    env.run(st)
    assert env.load("overview.json")["headline"] == "All checks healthy"
    env.run(make_status(tasks={}))
    assert env.load("overview.json")["headline"] == "No data yet"
    many = {f"t{i}": entry(status="warn", title=f"A rather long check title number {i}") for i in range(9)}
    env.run(make_status(tasks=many))
    assert len(env.load("overview.json")["headline"]) <= 80


def test_overall_computed_when_missing_and_paused_passthrough(env):
    st = make_status(paused=True)
    del st["overall"]
    env.run(st)
    o = env.load("overview.json")
    assert o["overall"] == "crit" and o["paused"] is True            # error/crit => crit (core.LEVELS), alert=False ignored


def test_overview_tiers_and_cleanup_mode(env):
    env.run()
    o = env.load("overview.json")
    assert o["tiers"]["check"] == {"last_run": NOW - 120, "next_run": NOW + 600, "mode": "check"}
    assert o["tiers"]["daily"] == {"last_run": NOW - 5 * 3600, "next_run": NOW + 19 * 3600, "mode": "apply"}   # snap_revisions applies
    assert o["tiers"]["weekly"]["mode"] == "report" and o["tiers"]["weekly"]["next_run"] == NOW + 5 * 86400
    assert o["cleanup_mode"] == "mixed"
    st = make_status()
    for e in st["tasks"].values():
        if e["klass"] != "C0":
            e["mode"] = "apply"
    env.run(st)
    assert env.load("overview.json")["cleanup_mode"] == "apply"
    for e in st["tasks"].values():
        if e["klass"] != "C0":
            e["mode"] = "dry-run"
    env.run(st)
    o = env.load("overview.json")
    assert o["cleanup_mode"] == "report" and o["tiers"]["daily"]["mode"] == "report"


def test_tier_last_run_falls_back_to_task_entries(env):
    st = make_status()
    del st["tier_runs"]
    env.run(st)
    t = env.load("overview.json")["tiers"]
    assert t["check"]["last_run"] == NOW - 300 and t["weekly"]["last_run"] == NOW - 300


def test_overview_generated_at_is_status_time_not_publish_time(env):
    env.run(now=NOW + 999)
    assert env.load("overview.json")["generated_at"] == NOW - 120
    assert env.load("checks.json")["generated_at"] == NOW + 999
    st = make_status()
    del st["generated_at"]
    old = (env.out / "overview.json").read_bytes()
    assert "overview.json" not in env.run(st)                         # a status without a timestamp must not look fresh
    assert (env.out / "overview.json").read_bytes() == old


# =========================================================================== checks.json
def test_checks_sorted_by_severity_and_status_words(env):
    env.run()
    rows = env.load("checks.json")["checks"]
    order = [r["status"] for r in rows]
    assert order == sorted(order, key=["crit", "warn", "error", "info", "ok"].index)
    by = {r["name"]: r for r in rows}
    assert by["backup_freshness"]["status"] == "crit" and by["memory_health"]["status"] == "error"
    assert by["docker_df"]["status"] == "info" and by["stuck_detector"]["status"] == "info"       # alert=False / skipped
    assert by["failed_units"]["summary"] == "2 failed units"                                       # level prefix dropped
    assert by["disk_forecast"]["title"] == "Disk space" and by["disk_forecast"]["last_run"] == NOW - 300


def test_checks_mode_words(env):
    env.run()
    by = {r["name"]: r["mode"] for r in env.load("checks.json")["checks"]}
    assert by["disk_forecast"] == "check" and by["snap_revisions"] == "apply"
    assert by["docker_cache"] == "report"            # daily tier really ran with --apply, task is configured report
    assert by["c2_candidates"] == "dry-run"          # weekly tier last ran with --dry-run


def test_checks_metrics_and_items_are_scalar_and_capped(env):
    st = make_status()
    st["tasks"]["failed_units"]["metrics"] = {"nested": {"a": 1}, "lst": [1, 2], "traceback": "x" * 600, "last_error": "smtp said no",
                                              "api_token": "abcd1234"} | {f"m{i}": i for i in range(40)}
    st["tasks"]["failed_units"]["items"] = [{"a": i, "nest": {"x": 1}, "l": [1], "s": "ok"} for i in range(30)] + ["junk", None]
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "failed_units")
    assert len(row["metrics"]) == 20 and list(row["metrics"])[0] == "m0" and all(not isinstance(v, (dict, list)) for v in row["metrics"].values())
    assert "traceback" not in row["metrics"] and "last_error" not in row["metrics"] and "api_token" not in row["metrics"]
    assert len(row["items"]) == 8 and all(set(i) == {"a", "s"} for i in row["items"])


def test_task_error_traceback_never_published(env):
    tb = ('Traceback (most recent call last):\n  File "/usr/local/lib/homelab-maint/tasks/x.py", line 12, in run\n'
          '    boom()\nValueError: no usable mounts')
    st = make_status()
    st["tasks"]["memory_health"].update(status="error", summary="error: " + tb,
                                        metrics={"traceback": tb, "note": tb, "n": 1}, items=[{"detail": tb}])
    env.run(st)
    raw = env.raw().decode()
    assert "Traceback" not in raw and 'File "' not in raw and "/usr/local/lib" not in raw and "boom()" not in raw
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "memory_health")
    assert row["summary"].endswith("ValueError: no usable mounts") and row["metrics"]["note"] == "ValueError: no usable mounts"


def test_checks_size_cap_trims_to_fit_default_limit(env):
    st = make_status()
    st["tasks"] = {f"t{i:03d}": entry(status="warn", summary="warn: " + "word " * 40,
                                      metrics={f"k{j}": "v" * 100 for j in range(20)},
                                      items=[{f"c{j}": "x" * 100 for j in range(10)} for _ in range(8)]) for i in range(300)}
    assert "checks.json" in env.run(st)
    p = env.out / "checks.json"
    assert 0 < p.stat().st_size <= 200_000
    check(env.load("checks.json"), SCHEMAS["checks.json"])


# =========================================================================== redaction
LEAKY = [
    "password=hunter2", "PASSWORD: hunter2", '{"password": "hunter2"}', "db_passwd = hunter2", "x-fan-token: hunter2",
    "api_key=hunter2", "--password hunter2 --verbose", "Authorization: Bearer hunter2hunter2", "Bearer hunter2hunter2",
    "token hunter2hunter2", "cookie=hunter2;", "secret='hunter2'",
    "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz", "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "xoxb-1234567890-abcdefghij", "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1rwWQ",
    "mbYRtt4s0RW5hsAMD4gdsLXBS8TuhE9h",                                 # Uptime Kuma push token shape
    "d41d8cd98f00b204e9800998ecf8427e" * 2,                             # 64 hex
    "5551234567@vtext.com", "ohmz@example.org",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END OPENSSH PRIVATE KEY-----",
    "https://user:hunter2@example.com/path",
    "http://127.0.0.1:3011/api/push/mbYRtt4s0RW5hsAMD4gdsLXBS8TuhE9h?status=up&msg=OK",
    "https://example.com/dl?token=hunter2&x=1", "https://example.com/dl#access_token=hunter2",
]
SECRET_FRAGMENTS = ["hunter2", "ghp_1A2b", "github_pat_", "sk-proj", "xoxb-", "AKIAIOSFODNN", "AIzaSy", "AAHdqTcv", "eyJhbGci", "dBjftJeZ",
                    "mbYRtt4s0", "d41d8cd9", "5551234567", "ohmz@", "b3BlbnNz", "BEGIN OPENSSH", "?status=up", "?token", "#access"]


@pytest.mark.parametrize("text", LEAKY)
def test_clean_removes_secrets(text):
    for wrapped in (text, f"warn: sending failed ({text}) rc=1", f"line one\n{text}"):
        out = P.clean(wrapped)
        assert not [f for f in SECRET_FRAGMENTS if f in out], (wrapped, out)
        assert "\n" not in out


@pytest.mark.parametrize("text", [
    "19 unhealthy: tday_backend, saved-vault, friendarr", "some-very-long-file-name-2026-09-27", "kotlin-compiler-embeddable-1.9.22",
    "ghcr.io/immich-app/immich-server:v1.119.0", "immich_machine_learning_model_cache", "/var/lib/docker/volumes/immich_pgdata/_data",
    "backup-system 5d19h", "free 212.0 GiB, +0.08 GiB/d", "https://example.com/downloads/file.iso", "token expired",
    "/home/ohmz/Downloads/old.iso", "192.168.1.10:8096",
])
def test_clean_leaves_ordinary_text_alone(text):
    assert P.clean(text) == text


def test_clean_urls_traceback_paths_and_limits():
    assert P.clean("https://u:p@h.example/a/b?x=1#f") == "https://h.example/a/b"
    assert P.clean("see http://h.example/hook/AbC123dEf456GhI789 now") == "see http://h.example/hook/[redacted] now"
    assert P.clean("a\x00b\x1b[31m\tc") == "a b [31m c"
    assert P.clean("first\nsecond secret") == "first"
    assert P.clean("") == "" and P.clean(None) == "" and P.clean(12) == "12"
    assert P.clean("bad\udcffname") == "bad?name"
    assert P.clean("x" * 5000, 40) == "x" * 37 + "..."
    long_path = "/srv/" + "/".join(["segment"] * 80)
    assert len(P.clean(long_path, 100)) <= 100 and P.clean(long_path, 100).startswith("/srv/segment") and "..." in P.clean(long_path, 100)
    assert P.clean('Traceback (most recent call last):\n  File "/a/b.py", line 1, in f\nKeyError: k') == "KeyError: k"
    assert P.clean('Traceback (most recent call last):\n  File "/a/b.py", line 1, in f') == "error (details withheld)"
    assert P.clean('oops File "/a/b.py", line 3') == "error (details withheld)"
    assert P.clean("sha256:" + "ab" * 32) == "sha256:" + "ab" * 6


@pytest.mark.parametrize("path, want", [
    ("/home/ohmz/.gradle/caches/modules-2/files-2.1/foo/bar.jar", "/home/ohmz/.gradle/caches/modules-2/..."),
    ("/home/ohmz/a/b/c", "/home/ohmz/a/b/c"), ("/home/ohmz/a/b/c/d", "/home/ohmz/a/b/c/..."), ("/home/ohmz", "/home/ohmz"),
    ("/home/ohmz/Downloads/old.iso", "/home/ohmz/Downloads/old.iso"), ("/root/a/b/c/d/e", "/root/a/b/c/..."),
    ("removed /home/bob/x/y/z/w and /home/amy/q/r/s/t ok", "removed /home/bob/x/y/z/... and /home/amy/q/r/s/... ok"),
    ("/var/log/a/b/c/d/e", "/var/log/a/b/c/d/e"),
])
def test_home_paths_truncated_after_third_component(path, want):
    assert P.clean(path) == want


def test_clean_is_fast_on_pathological_input():
    t0 = time.time()
    for s in ("a." * 3000, "word-" * 900, "password" * 500, "pass.word-token_" * 300, "http://a/" + "b/" * 2000, "x=y " * 2000):
        P.clean(s)
    assert time.time() - t0 < 0.5


def test_no_secret_reaches_any_file(env):
    """Plant secrets in every free-text spot a task or the audit log can reach and grep the whole public dir."""
    leaks = "password=hunter2 ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz https://h.example/x?token=hunter2"
    st = make_status(host="host password=hunter2")
    t = st["tasks"]["failed_units"]
    t.update(title="Services " + leaks, summary="warn: " + leaks, metrics={"msg": leaks, "last_error": "rc=1 hunter2", "stderr": "hunter2",
                                                                         "my_password": "hunter2", "secret": "hunter2", "ok": 1},
             items=[{"detail": leaks, "token": "hunter2", "note": "ok"}])
    st["tasks"]["disk_forecast"]["metrics"]["mounts"][0]["mount"] = "/mnt/x?token=hunter2"
    env.audit([rec(NOW - 5, target=f"/home/ohmz/a/b/c/d/e/{leaks}"), rec(NOW - 6, action="rm " + leaks, target=leaks),
               rec(NOW - 7, outcome="failed: " + leaks),
               rec(NOW - 8, task="notify", action="send", target="disk_forecast: WARN password=hunter2", nbytes=0, outcome="sent"),
               rec(NOW - 9, task="notify", action="send", target="x: CRIT hunter2", nbytes=0,
                   outcome="failed rc=1 smtp 535 auth failed password=hunter2 user 5551234567@vtext.com"),
               rec(NOW - 10, task="gate", action="defer-limit", target="immich", nbytes=0,
                   outcome="proceeding after 3.2 h of deferral: busy password=hunter2")])
    (env.state / "maintenance-journal.jsonl").write_text(json.dumps({"ts": NOW - 5, "title": "Rotated " + leaks, "detail": leaks}) + "\n")
    env.history([{"t": NOW - 60, "kind": "sample", "host": {}, "c": {"leaky-container hunter2": {"anon": 1}}, "tok": "hunter2"}])
    env.run(st)
    raw = env.raw().decode()
    frags = [f for f in SECRET_FRAGMENTS if f != "?token"]       # "/mnt/x?token=[redacted]": the key stays, its value is gone
    assert not [f for f in frags if f in raw], [f for f in frags if f in raw]
    assert "leaky-container" not in raw and "?token=[redacted]" in raw


# =========================================================================== actions.json
def test_notify_entries_are_reduced_and_blanked(env):
    env.audit([rec(NOW - 5, task="notify", action="send", target="disk_forecast: WARN Disk space", nbytes=0, outcome="sent"),
               rec(NOW - 6, task="notify", action="send", target="x: CRIT Backups: kuma token xyz", nbytes=0,
                   outcome="failed rc=2 no reason logged"),
               rec(NOW - 7, task="notify", action="budget-exhausted", target="disk_forecast", nbytes=0, outcome="dropped")])
    env.run()
    rows = env.load("actions.json")["recent"]
    assert [(r["task"], r["action"], r["target"], r["bytes"], r["outcome"]) for r in rows] == [
        ("notify", "alert", "", 0, "sent"), ("notify", "alert", "", 0, "failed"), ("notify", "alert", "", 0, "dropped")]
    raw = env.raw().decode()
    assert "WARN Disk space" not in raw and "kuma token" not in raw and "no reason logged" not in raw
    assert "notify" not in env.load("actions.json")["by_task"]


def test_audit_records_parsed_sorted_and_normalised(env):
    env.audit([rec(NOW - 7200, nbytes=GIB), rec(NOW - 60, task="docker_images", action="docker-image-rm", target="ghcr.io/x/y:1",
                                                 nbytes=2 * GIB, outcome="failed: image is in use password=hunter2"),
               rec(NOW - 30, outcome="refused-protected"), rec(NOW - 40, outcome="refused-cap!!"), rec(NOW - 50, outcome="approved"),
               rec(NOW - 80, nbytes=-5), rec(NOW - 90, nbytes="12"), rec(NOW - 100, nbytes=True), rec(NOW - 110, nbytes=None),
               {"ts": NOW - 20, "task": "caps", "action": "x", "target": "y", "bytes": 1, "outcome": "done"}])    # numeric ts
    env.run()
    rows = env.load("actions.json")["recent"]
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    by_out = {r["outcome"] for r in rows}
    assert {"done", "approved", "refused-protected", "refused-cap", "failed: image is in use password=[redacted]"} <= by_out
    assert rows[-1]["ts"] == NOW - 7200 and rows[-1]["target"] == "/var/log/old.log"        # "-0400" offsets converted
    assert {r["bytes"] for r in rows if r["task"] == "retention"} == {GIB, 0, 12}
    assert any(r["ts"] == NOW - 20 and r["task"] == "caps" for r in rows)


def test_by_task_windows_and_totals(env):
    env.audit([rec(NOW - 3600, nbytes=1 * GIB), rec(NOW - 3 * 86400, nbytes=2 * GIB), rec(NOW - 20 * 86400, nbytes=4 * GIB),
               rec(NOW - 40 * 86400, nbytes=8 * GIB), rec(NOW - 60, nbytes=100 * GIB, outcome="dry-run"),
               rec(NOW - 120, task="docker_cache", action="buildx-prune", nbytes=0, outcome="done")])
    env.history([{"t": NOW - 900 * i, "kind": "task", "task": "retention", "status": "ok"} for i in range(10)]
                + [{"t": NOW - 40 * 86400, "kind": "task", "task": "retention", "status": "ok"}])
    env.run()
    a = env.load("actions.json")
    r = a["by_task"]["retention"]
    assert (r["freed_24h"], r["freed_7d"], r["freed_30d"], r["freed_total"]) == (GIB, 3 * GIB, 7 * GIB, 15 * GIB)
    assert r["last_action"] == NOW - 3600 and r["last_outcome"] == "dry-run" and r["runs_30d"] == 10
    assert a["by_task"]["docker_cache"]["last_action"] == NOW - 120 and a["by_task"]["docker_cache"]["freed_total"] == 0
    assert a["by_task"]["snap_revisions"]["last_action"] is None and a["by_task"]["snap_revisions"]["last_outcome"] == "none"
    assert a["by_task"]["snap_revisions"]["last_run"] == NOW - 300
    assert "disk_forecast" not in a["by_task"]                                    # C0 checks do no maintenance
    # "done" records only: 1 h + 2 min ago inside 24 h, + 3 d inside 7 d, + 20 d inside 30 d; the 40 d one and the dry-run are out
    assert a["totals"] == {"freed_24h": GIB, "freed_7d": 3 * GIB, "freed_30d": 7 * GIB, "actions_24h": 2, "actions_7d": 3,
                           "actions_30d": 4}


def test_reclaimed_log_and_audit_are_not_double_counted(env):
    env.audit([rec(NOW - 3600, task="snap_revisions", nbytes=5 * GIB)])
    log = [{"t": NOW - 3600, "task": "snap_revisions", "bytes": 5 * GIB},                 # same bytes as the audit record
           {"t": NOW - 2 * 86400, "task": "snap_revisions", "bytes": 3 * GIB},             # only the log knows this one
           {"t": NOW - 100, "task": "trash", "bytes": GIB}, {"t": "bad"}, "junk", {"t": NOW, "task": "x", "bytes": -3}]
    env.run(make_status(reclaimed_log=log))
    a = env.load("actions.json")
    s = a["by_task"]["snap_revisions"]
    assert (s["freed_24h"], s["freed_7d"]) == (5 * GIB, 8 * GIB) and s["freed_total"] == 8 * GIB
    assert a["by_task"]["trash"]["freed_24h"] == GIB and a["totals"]["freed_24h"] == 6 * GIB
    days = {d["day"]: d["bytes"] for d in env.load("storage.json")["freed_by_day"]}
    assert days["2026-10-01"] == 6 * GIB and days["2026-09-29"] == 3 * GIB


def test_recent_prefers_real_actions_over_dry_run_noise(env):
    env.audit([rec(NOW - 5000 - i, target=f"/real/{i}") for i in range(5)]
              + [rec(NOW - 100 - i / 10, target=f"/would/{i}", outcome="dry-run") for i in range(500)]
              + [rec(NOW - 50 - i / 10, target=f"/prot/{i}", outcome="refused-protected") for i in range(200)])
    env.run()
    rows = env.load("actions.json")["recent"]
    assert sum(r["outcome"] == "done" for r in rows) == 5 and len(rows) == 5 + P.RECENT_NOISE_MAX


def test_recent_capped_newest_first_and_trims_oldest(env, monkeypatch):
    env.audit([rec(NOW - 1000 + i, target=f"/f/{i}") for i in range(450)])
    env.run()
    rows = env.load("actions.json")["recent"]
    assert len(rows) == 300 and rows[0]["target"] == "/f/449" and rows[-1]["target"] == "/f/150"
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 12_000)
    env.run()
    small = env.load("actions.json")["recent"]
    assert 0 < len(small) < 300 and small[0]["target"] == "/f/449"                 # the oldest rows were the ones dropped
    assert (env.out / "actions.json").stat().st_size <= 12_000


def test_audit_targets_trim_home_paths_and_urls(env):
    env.audit([rec(NOW - 5, target="/home/ohmz/.gradle/caches/modules-2/files-2.1/org/x.jar"),
               rec(NOW - 6, target="https://example.com/dl?token=abc&x=1"), rec(NOW - 7, target="/home/ohmz/Downloads/a.iso"),
               rec(NOW - 8, target="/x/" + "y" * 900)])
    env.run()
    t = [r["target"] for r in env.load("actions.json")["recent"]]
    assert t[0] == "/home/ohmz/.gradle/caches/modules-2/..." and t[1] == "https://example.com/dl"
    assert t[2] == "/home/ohmz/Downloads/a.iso" and len(t[3]) <= 160


def test_malformed_audit_lines_are_skipped(env):
    junk = ["{not json", "", "null", "[1,2]", '"str"', "42", '{"ts": "yesterday", "task": "a"}', '{"task": "a"}',
            '{"ts": "2026-10-01T10:00:00+0000"', "\x00\x00\x00", "{" * 500, "[" * 60_000, '{"ts": %f, "task": "far-future", "outcome": "done"}' % (NOW + 99999),
            json.dumps({"ts": NOW - 5, "task": ["list"], "action": {"a": 1}, "target": [1], "bytes": "lots", "outcome": 5})]
    env.audit(junk + [rec(NOW - 10, target="/good")])
    with open(env.log / "audit.jsonl", "ab") as f:
        f.write(b"\xff\xfe\xfd not utf8 \n" + b'{"ts": "2026-10-01T11:00:00+0000", "task": "trunc')           # binary and a cut-off last line
    assert "actions.json" in env.run()
    rows = env.load("actions.json")["recent"]
    assert any(r["target"] == "/good" for r in rows) and not any(r["task"] == "far-future" for r in rows)
    check(env.load("actions.json"), SCHEMAS["actions.json"])


def test_huge_audit_file_is_bounded_and_fast(env):
    lines = []
    base = NOW - 40 * 86400
    for i in range(120_000):                                                     # ~19 MB, far more than the 6 MiB window
        lines.append(json.dumps({"ts": iso(base + i * 28), "task": "retention", "action": "retention-delete",
                                 "target": f"/home/ohmz/a/b/c/d/file{i}.log", "bytes": i % 5000,
                                 "outcome": ["done", "dry-run", "refused-protected"][i % 3]}))
    lines.insert(60_000, "y" * 3_000_000)                                       # one 3 MB line in the middle
    (env.log / "audit.jsonl").write_text("\n".join(lines) + "\n" + "z" * 2_000_000)   # and an unterminated 2 MB tail
    t0 = time.time()
    assert "actions.json" in env.run()
    assert time.time() - t0 < 2.0
    a = env.load("actions.json")
    check(a, SCHEMAS["actions.json"])
    assert len(a["recent"]) <= 300 and (env.out / "actions.json").stat().st_size < 200_000
    assert a["recent"][0]["target"].startswith("/home/ohmz/a/b/c/...")


def test_audit_read_is_windowed_so_a_64mb_log_is_still_fast(env):
    line = (json.dumps(rec(NOW - 100, target="/home/ohmz/a/b/c/d/e.log", nbytes=1000)) + "\n").encode()
    (env.log / "audit.jsonl").write_bytes(line * 400_000)                          # ~64 MB, all inside the 30-day window
    t0 = time.time()
    env.run()
    assert time.time() - t0 < 1.0                                                  # unbounded this takes several seconds
    a = env.load("actions.json")
    # only the newest AUDIT_WINDOW bytes are read, so totals are a lower bound on huge logs, never an error
    assert 0 < a["by_task"]["retention"]["freed_total"] <= 1000 * (P.AUDIT_WINDOW // len(line) + 1)


def test_journal_entries(env):
    j = [{"ts": "2026-09-28T09:30:00-0400", "title": "Replaced case fan", "detail": "Noctua NF-A14, cpu_fan token=hunter2"},
         {"ts": NOW - 100, "title": "Cleaned dust filters", "detail": "x" * 2000}, {"ts": "bad", "title": "no"}, {"ts": NOW, "title": ""},
         "junk", {"ts": NOW - 5000, "title": "Multi\nline", "detail": "d1\nd2"}]
    (env.state / "maintenance-journal.jsonl").write_text("\n".join(json.dumps(x) for x in j) + "\n{broken\n")
    env.run()
    rows = env.load("actions.json")["journal"]
    assert [r["title"] for r in rows] == ["Cleaned dust filters", "Multi", "Replaced case fan"]
    assert len(rows[0]["detail"]) <= 600 and "hunter2" not in rows[2]["detail"] and rows[2]["ts"] == timegm((2026, 9, 28, 13, 30, 0))
    (env.state / "maintenance-journal.jsonl").write_text("".join(json.dumps({"ts": NOW - i, "title": f"t{i}"}) + "\n" for i in range(250)))
    env.run()
    rows = env.load("actions.json")["journal"]
    assert len(rows) == 100 and rows[0]["title"] == "t0"


# =========================================================================== storage.json / health-history.json
def test_storage_mounts(env, monkeypatch):
    st = make_status()
    st["tasks"]["disk_forecast"]["metrics"]["mounts"] += [
        {"mount": "/mnt/x", "free": 50 * GIB, "used_pct": 50.0, "level": "ok"},                      # size derived from used_pct
        {"mount": "/mnt/y", "free": 50 * GIB, "used_pct": 99.99, "level": "crit", "days": "n/a"},    # unknowable: 0
        {"mount": "/mnt/z", "free": 10 * GIB, "used_pct": 10.0, "level": "weird", "days": -3},
        {"mount": "/mnt/v", "free": 10 * GIB, "used_pct": 10.0}, "junk", {"nomount": 1}]
    monkeypatch.setattr(P, "_statvfs_sizes", lambda m, budget=0.5: {"/mnt/v": 100 * GIB, "/mnt/x": 1})   # 1 B < free: implausible, ignored
    env.run(st)
    ms = {m["mount"]: m for m in env.load("storage.json")["mounts"]}
    assert ms["/"]["size_b"] == 855 * GIB and ms["/"]["days"] == 12 and ms["/"]["free_h"] == "100.0 GiB" and ms["/"]["level"] == "warn"
    assert ms["/media/SandiskSSD"]["size_b"] == pytest.approx(1000 * GIB, rel=1e-6) and ms["/media/SandiskSSD"]["days"] is None
    assert ms["/mnt/x"]["size_b"] == pytest.approx(100 * GIB, rel=1e-6) and ms["/mnt/y"]["size_b"] == 0
    assert ms["/mnt/v"]["size_b"] == 100 * GIB                                                    # statvfs when plausible
    assert ms["/mnt/z"]["level"] == "ok" and ms["/mnt/z"]["days"] is None and ms["/mnt/y"]["days"] is None
    assert ms["/media/WD22TB"]["info"] is True and ms["/"]["info"] is False
    order = [m["mount"] for m in env.load("storage.json")["mounts"]]
    assert order[0] == "/mnt/y" and order[-1] == "/media/WD22TB"                                  # crit first, info mounts last


def test_storage_series_hourly_mean_watch_mounts_and_30_day_window(env):
    h = int(NOW // 3600) * 3600 - 5 * 3600
    env.history([{"t": h + 60 * i, "kind": "disk", "mount": "/", "free": (100 + 10 * i) * GIB} for i in range(4)]          # one hour
                + [{"t": h + 3600 + 5, "kind": "disk", "mount": "/", "free": 90 * GIB}]                                      # next hour
                + [{"t": NOW - 40 * 86400, "kind": "disk", "mount": "/", "free": 1 * GIB}]                                   # too old
                + [{"t": h, "kind": "disk", "mount": "/media/WD22TB", "free": 20 * GIB}]                                     # info mount
                + [{"t": h, "kind": "disk", "mount": "/gone", "free": 20 * GIB}]                                             # not in status
                + [{"t": h, "kind": "size", "path": "/var/log", "bytes": 5}, "{broken", {"t": h, "kind": "disk"}])
    env.run()
    s = env.load("storage.json")["series"]
    assert set(s) == {"/"} and s["/"]["t"] == [h, h + 3600] and s["/"]["free_gib"] == [115.0, 90.0]


def test_storage_series_30_day_hourly_cap_and_sizes(env):
    t0 = int(NOW // 3600) * 3600 - 29 * 86400
    env.history([{"t": t0 + 900 * k, "kind": "disk", "mount": m, "free": (500 - k // 100) * GIB}
                 for k in range(29 * 96) for m in ("/", "/media/SandiskSSD")])
    env.run()
    doc = env.load("storage.json")
    assert all(len(v["t"]) <= 720 and len(v["t"]) == len(v["free_gib"]) for v in doc["series"].values())
    assert len(doc["series"]["/"]["t"]) >= 600 and (env.out / "storage.json").stat().st_size < 200_000
    assert doc["series"]["/"]["t"] == sorted(doc["series"]["/"]["t"])
    assert len(doc["freed_by_day"]) == 30 and doc["freed_by_day"][-1]["day"] == "2026-10-01"
    assert doc["freed_by_day"][0]["day"] == "2026-09-02"


def test_storage_trims_oldest_points_when_over_cap(env, monkeypatch):
    t0 = int(NOW // 3600) * 3600 - 29 * 86400
    env.history([{"t": t0 + 3600 * k, "kind": "disk", "mount": "/", "free": (500 + k) * GIB} for k in range(500)])
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 6_000)
    env.run()
    doc = env.load("storage.json")
    assert (env.out / "storage.json").stat().st_size <= 6_000 and 0 < len(doc["series"]["/"]["t"]) < 500
    assert doc["series"]["/"]["t"][-1] == t0 + 3600 * 499                                          # newest points survive


def test_storage_kept_when_disk_forecast_missing(env):
    env.run()
    old = (env.out / "storage.json").read_bytes()
    st = make_status()
    del st["tasks"]["disk_forecast"]
    assert "storage.json" not in env.run(st) and (env.out / "storage.json").read_bytes() == old


def test_sample_records_are_never_published_or_parsed(env, monkeypatch):
    h = int(NOW // 3600) * 3600 - 3600
    big = {"t": h, "kind": "sample", "host": {"mem_avail": 1}, "c": {f"zz-container-{i}": {"anon": i, "disk": 1} for i in range(100)}}
    env.history([big, json.dumps(big, separators=(",", ":")), json.dumps({"t": h, "kind": "sample", "x": "y" * 70_000}),
                 {"t": h, "kind": "disk", "mount": "/", "free": 7 * GIB}])
    seen = []
    real = json.loads
    monkeypatch.setattr(P.json, "loads", lambda s, *a, **k: seen.append(len(s)) or real(s, *a, **k))
    env.run()
    monkeypatch.setattr(P.json, "loads", real)
    assert seen == []                                                   # sample lines are skipped before any parsing
    assert "zz-container" not in env.raw().decode()
    assert env.load("storage.json")["series"]["/"]["free_gib"] == [7.0]


def test_health_calendar_slots_minutes_and_unknown_days(env):
    d1 = timegm((2026, 9, 30, 0, 0, 0))
    d2 = d1 - 86400

    def run(t, **status):
        return [{"t": t + i, "kind": "task", "task": n, "status": status.get(n, "ok"), "reclaimed": 0, "dur": 0.1, "metrics": {}}
                for i, n in enumerate(("disk_forecast", "failed_units", "image_ledger", "docker_df", "backup_freshness"))]
    recs = (run(d1 + 10 * 3600, disk_forecast="warn", failed_units="warn", docker_df="crit")     # two warns in ONE run = 15 min
            + run(d1 + 10 * 3600 + 900, disk_forecast="warn")                                      # next slot = 30 min total
            + run(d1 + 11 * 3600, backup_freshness="crit", failed_units="crit")                    # crit slot
            + run(d1 + 11 * 3600 + 900, failed_units="error")                                      # error counts as crit
            + run(d1 + 12 * 3600)                                                                  # healthy slot
            + run(d2 + 3600) + run(d2 + 7200, docker_df="warn"))                                   # alert=False warn: day stays ok
    random.Random(1).shuffle(recs)                                                                 # order must not matter
    env.history(recs + [{"t": NOW - 45 * 86400, "kind": "task", "task": "x", "status": "crit"}, "{oops",
                        {"t": d1, "kind": "task", "task": "x", "status": "weird"}])
    env.run()
    days = {d["day"]: d for d in env.load("health-history.json")["days"]}
    assert days["2026-09-30"] == {"day": "2026-09-30", "worst": "crit", "warn_minutes": 30, "crit_minutes": 30}
    assert days["2026-09-29"] == {"day": "2026-09-29", "worst": "ok", "warn_minutes": 0, "crit_minutes": 0}
    assert days["2026-09-28"]["worst"] == "unknown" and days["2026-10-01"]["worst"] == "unknown"
    all_days = env.load("health-history.json")["days"]
    assert len(all_days) == 30 and all_days[-1]["day"] == "2026-10-01" and all_days[0]["day"] == "2026-09-02"
    assert all_days == sorted(all_days, key=lambda d: d["day"])


def test_health_calendar_only_alert_off_task_still_counts_as_data(env):
    env.history([{"t": NOW - 86400 + i, "kind": "task", "task": "docker_df", "status": "warn"} for i in range(3)])
    env.run()
    assert next(d for d in env.load("health-history.json")["days"] if d["day"] == "2026-09-30")["worst"] == "ok"


def test_garbled_numbers_and_absurd_timestamps_never_break_the_scans(env):
    d = NOW - 3600
    env.history(['{"t":1.2.3,"kind":"task","task":"a","status":"crit"}', '{"t":e,"kind":"task","task":"a","status":"crit"}',
                 '{"t":%s,"kind":"disk","mount":"/","free":1e999}' % d, '{"t":%s,"kind":"disk","mount":"/","free":-}' % d,
                 '{"t":1e999,"kind":"task","task":"a","status":"crit"}', '{"t":1e18,"kind":"task","task":"a","status":"crit"}',
                 '{"t":%s,"kind":"task","task":"a","status":"crit"}' % (NOW + 5 * 86400),                          # future
                 '{"t":"x","kind":"task","task":"a","status":"crit"}', '{"t":%s,"kind":"task","task":["a"],"status":{"b":1}}' % d,
                 '["task", "disk", 1]', '{"t":%s,"kind":"disk","mount":"/","free":%d}' % (d, 9 * GIB)])
    env.audit([json.dumps({"ts": "0001-01-01T00:00:00", "task": "x", "outcome": "done", "bytes": 5}),
               json.dumps({"ts": "9999-12-31T23:59:59+0000", "task": "x", "outcome": "done", "bytes": 5}),
               json.dumps({"ts": NOW - 5, "task": "big", "action": "a", "target": "t", "bytes": 10 ** 40, "outcome": "done"})])
    log = [{"t": 1e18, "task": "x", "bytes": GIB}, {"t": 1e999, "task": "x", "bytes": GIB}, {"t": NOW + 86400, "task": "x", "bytes": GIB},
           {"t": NOW - 5, "task": "big", "bytes": 10 ** 40}]
    assert env.run(make_status(reclaimed_log=log)) == ALL_FILES
    for n, spec in SCHEMAS.items():
        check(env.load(n), spec)
    assert env.load("storage.json")["series"]["/"]["free_gib"] == [9.0]
    assert next(d for d in env.load("health-history.json")["days"] if d["day"] == "2026-10-01")["worst"] == "ok"   # no crit leaked in
    assert env.load("actions.json")["by_task"]["big"]["freed_total"] == 2 ** 53 and "x" not in env.load("actions.json")["by_task"]


def test_history_regex_and_json_fallback_agree(env):
    t = NOW - 600
    env.history([f'{{"t": {t}, "kind": "task", "task": "a", "status": "warn"}}',                      # spaced json
                 f'{{"kind":"task","t":{t + 1},"task":"b","status":"crit"}}',                         # key order differs
                 '{"t":%s,"kind":"disk","mount":"/media/we\\"ird","free":%d}' % (t, 5 * GIB)])      # escaped quote
    env.run()
    assert next(d for d in env.load("health-history.json")["days"] if d["day"] == "2026-10-01")["worst"] == "crit"


# =========================================================================== schedule.json
def test_schedule_converts_filters_and_orders(env):
    env.run()
    tm = env.load("schedule.json")["timers"]
    assert [t["unit"] for t in tm] == ["homelab-maint-check.timer", "homelab-maint-daily.timer", "homelab-maint-weekly.timer",
                                       "backup-system.timer", "fstrim.timer"]                          # no comfyui/motd timers
    by = {t["unit"]: t for t in tm}
    c = by["homelab-maint-check.timer"]
    assert c == {"unit": "homelab-maint-check.timer", "title": "Health checks", "last": NOW - 300, "next": NOW + 600, "schedule": "every 15 min"}
    assert by["homelab-maint-daily.timer"]["last"] is None and by["homelab-maint-weekly.timer"]["last"] is None     # 0 / null => never
    assert by["homelab-maint-daily.timer"]["schedule"] == "daily at 07:30" and by["homelab-maint-weekly.timer"]["schedule"] == "Wednesdays at 07:45"
    assert by["fstrim.timer"]["next"] is None and by["backup-system.timer"]["schedule"] == "Saturdays at 01:00"
    assert by["backup-system.timer"]["title"] == "System backup"


@pytest.mark.parametrize("result", [CompletedProcess([], 127, "", "not found"), CompletedProcess([], 1, "", "boom"),
                                    CompletedProcess([], 0, "not json", ""), CompletedProcess([], 0, '{"a": 1}', "")])
def test_schedule_degrades_to_empty_list_without_systemctl(env, monkeypatch, result):
    monkeypatch.setattr(P, "_list_timers", REAL_LIST_TIMERS)                                # real reader, stubbed `sh`
    monkeypatch.setattr(P, "sh", lambda *a, **k: result)
    assert "schedule.json" in P.publish(make_status(), NOW)
    assert env.load("schedule.json")["timers"] == []


def test_timer_specs_and_friendly_text(monkeypatch):
    text = ("Id=homelab-maint-check.timer\nTimersMonotonic={ OnBootUSec=3min ; next_elapse=3min }\n"
            "TimersCalendar={ OnCalendar=*-*-* *:00/15:00 ; next_elapse=Thu 2026-10-01 22:00:00 EDT }\n\n"
            "Id=homelab-maint-metrics.timer\nTimersMonotonic={ OnBootUSec=1min ; OnUnitActiveUSec=1min ; next_elapse=1min }\n\n"
            "Id=odd.timer\nTimersCalendar={ OnCalendar=Mon,Fri *-*-1..7 03:15:00 ; next_elapse=x }\n\nId=none.timer\n")
    monkeypatch.setattr(P, "sh", lambda *a, **k: CompletedProcess([], 0, text, ""))
    specs = P._timer_specs(["homelab-maint-check.timer", "homelab-maint-metrics.timer", "odd.timer", "none.timer"])
    assert specs == {"homelab-maint-check.timer": "*-*-* *:00/15:00", "homelab-maint-metrics.timer": "every 1min",
                     "odd.timer": "Mon,Fri *-*-1..7 03:15:00", "none.timer": ""}
    assert [P._friendly(s) for s in ("every 1min", "every 5min", "every 30s", "*-*-* *:00/10:00", "*-*-* 00:00:00", "Mon *-*-* 00:00:00")] == [
        "every minute", "every 5 min", "every 30 s", "every 10 min", "daily at 00:00", "Mondays at 00:00"]
    assert P._friendly("Mon,Fri *-*-1..7 03:15:00") == "Mon,Fri *-*-1..7 03:15:00"
    monkeypatch.setattr(P, "sh", lambda *a, **k: CompletedProcess([], 1, "", ""))
    assert P._timer_specs(["a.timer"]) == {} and P._timer_specs([]) == {}


# =========================================================================== metrics.json
def test_metrics_passthrough_nan_and_failures(env, monkeypatch):
    monkeypatch.setattr(P, "_metrics_export", lambda now: ring_export(avg_7d={"cpu_temp": float("nan")}, extra=("a", "b")))
    env.run()
    m = env.load("metrics.json")
    assert m["avg_7d"] == {"cpu_temp": None} and m["extra"] == ["a", "b"] and len(m["series"]["t"]) == 168
    old = (env.out / "metrics.json").read_bytes()

    def broken(now):
        raise ImportError("no module metrics_ring")
    for bad in (broken, lambda now: None, lambda now: [1, 2]):
        monkeypatch.setattr(P, "_metrics_export", bad)
        assert "metrics.json" not in env.run(now=NOW + 60)
        assert (env.out / "metrics.json").read_bytes() == old
    monkeypatch.setattr(P, "_metrics_export", lambda now: {"series": {}})
    env.run()
    assert env.load("metrics.json")["generated_at"] == NOW


def test_metrics_too_large_is_skipped_not_truncated(env, monkeypatch):
    env.run()
    old = (env.out / "metrics.json").read_bytes()
    monkeypatch.setattr(P, "_metrics_export", lambda now: ring_export(series={"t": list(range(60_000)), "x": [1.5] * 60_000}))
    assert "metrics.json" not in env.run(now=NOW + 60) and (env.out / "metrics.json").read_bytes() == old


# =========================================================================== review fixes: redaction blind spots
# every (text, fragment that must NOT survive) pair is a shape that leaked before the fix
BLIND_SPOTS = [
    ("key=abc123def", "abc123def"), ("sig=Zm9vYmFy", "Zm9vYmFy"), ("signature: Zm9vYmFy", "Zm9vYmFy"), ("pw=hunter2", "hunter2"),
    ("psk=hunter2", "hunter2"), ("jwt=hunter2", "hunter2"), ("pin=482913", "482913"), ("otp=482913", "482913"),
    ("dsn=hunter2", "hunter2"), ("KEY: hunter2", "hunter2"), ('{"key": "hunter2"}', "hunter2"), ("passphrase=hunter2", "hunter2"),
    ("sshpass -p hunter2 ssh host", "hunter2"), ("docker login -u me -p S3cr3tPw reg.example", "S3cr3tPw"),
    ("mysql -uroot -pTopSecret99 db", "TopSecret99"), ("psql -h x -p hunter2 db", "hunter2"), ("redis-cli -a x -p hunter2", "hunter2"),
    ("POST https://hc-ping.com/6f1c2e3a-1b2c-4d5e-8f90-a1b2c3d4e5f6 failed", "6f1c2e3a"),         # UUID path token
    ("GET https://hooks.example/services/a1b2c3d4e5f60718 -> 404", "a1b2c3d4e5f60718"),            # 16 hex
    ("GET https://hooks.example/v1/0123456789abcdef0123456789abcd failed", "0123456789abcdef0123456789abcd"),
    ("GET https://x.example/p/Abcdefghij1234567890Zz/run failed", "Abcdefghij1234567890Zz"),      # 22 chars, digit+letter, low entropy
    ("http://127.0.0.1:3011/api/push/aB3dE5fG7h", "aB3dE5fG7h"),                                    # short token after a "push" path word
    ("https://h.example/hook/x1y2z3w4v5", "x1y2z3w4v5"),
    ("http://admin:p@ssword@nas/x", "ssword"), ("http://admin:p@ss@word@nas/x", "word@"), ("ftp://u:a@b@c@host:21/f", "a@b"),
    ("sent to +14165551234 failed", "4165551234"), ("sms +1 (416) 555-1234 failed", "555-1234"),
    ("sms 416-555-1234 failed", "555-1234"), ("sms (416) 555-1234 failed", "555-1234"), ("sms 416.555.1234 failed", "555.1234"),
    ("password = my secret phrase", "secret phrase"), ("db password=correct horse battery staple, retrying", "horse"),
    ("passwd=two words rc=1", "two words"),
]


@pytest.mark.parametrize("text, frag", BLIND_SPOTS)
def test_clean_closes_redaction_blind_spots(text, frag):
    for wrapped in (text, f"warn: step failed ({text}) rc=1", f"first line\n{text}", text + " trailing words"):
        out = P.clean(wrapped)
        assert frag not in out, (wrapped, out)
    assert P.clean(text) != text                                       # and the clean text is never the input unchanged


def test_redaction_keeps_the_harmless_neighbours():
    assert P.clean("mysql -uroot -pTopSecret99 db") == "mysql -uroot -p [redacted] db"            # the db name survives
    assert P.clean("docker login -u me -p S3cr3tPw reg.example") == "docker login -u me -p [redacted] reg.example"
    assert P.clean("http://admin:p@ssword@nas/x") == "http://nas/x"
    assert P.clean("password=hunter2 user=bob") == "password=[redacted] user=bob"
    assert P.clean("password = my secret phrase user=bob") == "password = [redacted] user=bob"
    assert P.clean("db password=correct horse battery staple, retrying") == "db password=[redacted], retrying"


@pytest.mark.parametrize("text", [
    "KeyError: k", "monkey=1", "keyboard: ok", "pinned: 3", "design=x", "signal=15", "turnkey=yes", "mysql -uroot db", "psql -h localhost db",
    "docker run -p 8080:80 nginx", "ssh -p 2222 host", "pytest -p no:cacheprovider", "docker login --password-stdin",
    "2026-09-27", "2026-10-01 12:00:00 +0200", "built 2026-09-27T12:00:00+0000", "free +0.08 GiB/d", "grew by 104857600 bytes",
    "https://example.com/api/push/notifications", "https://example.com/hook/summary", "https://example.com/v1/images/12345678",
    "https://registry.example/v2/library/nginx/manifests/latest", "192.168.1.10:8096", "ver 1.119.0", "build 1759320000.123",
    "password policy ok", "Username and Password not accepted",
])
def test_new_redaction_rules_do_not_eat_ordinary_text(text):
    assert P.clean(text) == text


def test_url_userinfo_is_dropped_up_to_the_last_at_sign():
    assert P.clean("see ftp://u:p@a@b@host:21/f.txt now") == "see ftp://host:21/f.txt now"
    assert P.clean("mail me@x.example about https://example.com/a@b") == "mail [redacted] about https://example.com/a@b"
    assert P.clean("https://example.com") == "https://example.com"


def test_redaction_stays_fast_on_the_new_patterns():
    t0 = time.time()
    for s in ("http://" + "@" * 700, "a://" * 200, "password=" + "w " * 400, "mysql " + "-p " * 250, "key=" * 190, "+1 " * 260,
              "http://a/" + "0123456789abcdef" * 40, "(555) " * 130):
        P.clean(s)
    assert time.time() - t0 < 0.5


# =========================================================================== review fixes: alert-path text never published
BRIDGE_ERRORS = [
    "send_sms failed: 550 5.1.1 <+14165551234>... User unknown",
    "SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted. For more information, go to https://support.google.com')",
    "no transport configured (~/.hermes/alert_transports.env missing)",
    "cannot import alert_transports from /home/ohmz/StudioProjects/ai-stack/scripts (No module named 'requests')",
]
BRIDGE_FRAGMENTS = ["4165551234", "User unknown", "550 5.1.1", "(535", "5.7.8", "Username and Password", "support.google", "alert_transports", ".hermes",
                    "ai-stack", "requests", "transport", "smtp", "SMTP", "send_sms"]


def alert_status_from_real_task(env, monkeypatch, tmp_path, err):
    """Run the REAL alert_path_health over a failed notify audit row (the reviewer's reproduction) and wrap the Result like cli does."""
    from homelab_maint.tasks import checks_health as ch
    bridge, hook = tmp_path / "bridge.py", tmp_path / "smart-alert.sh"
    for f in (bridge, hook):
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    conf = tmp_path / "smartd.conf"
    conf.write_text(f"DEVICESCAN -a -M exec {hook}\n")
    monkeypatch.setattr(ch, "SMARTD_CONF", conf)
    monkeypatch.setattr(ch, "AUDIT_LOG", env.log / "audit.jsonl")
    env.audit([{"ts": iso(NOW - 600), "task": "notify", "action": "send", "target": "disk_forecast: WARN Disk space", "bytes": 0,
                "outcome": "failed rc=1 " + err}])
    cfg = {"global": {"bridge": str(bridge)}, "tasks": {"alert_path_health": {"smart_log": str(tmp_path / "none.log")}}, "caps": {}, "protected": {}}
    res = ch.alert_path_health(core.Ctx(cfg, "alert_path_health", apply=False, now=NOW))
    return res, entry(status=res.status, summary=res.summary, title="Alert path", metrics=res.metrics, items=res.items)


@pytest.mark.parametrize("err", BRIDGE_ERRORS)
def test_alert_path_bridge_stderr_is_not_published(env, monkeypatch, tmp_path, err):
    res, e = alert_status_from_real_task(env, monkeypatch, tmp_path, err)
    assert res.status == "warn" and "rc=1" in res.summary                    # the task itself still reports the raw reason locally
    st = make_status()
    st["tasks"]["alert_path_health"] = e
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "alert_path_health")
    assert row["status"] == "warn" and row["summary"] == "alert path: 1 notifier sends failed in 24h"
    assert {i["what"] for i in row["items"]} <= {"bridge", "smartd hook", "smart hook", "notifier", "notifier budget"}
    assert all(not ({"rc=", "/"} & {c for c in ("rc=", "/") if c in str(i.get("detail", ""))}) for i in row["items"])
    assert all(set(i) <= {"what", "level", "detail"} for i in row["items"])
    raw = env.raw().decode()
    assert not [f for f in BRIDGE_FRAGMENTS if f in raw], [f for f in BRIDGE_FRAGMENTS if f in raw]


def test_alert_path_summary_is_built_from_counters_only(env):
    leak = "rc=1: smtp 535 password=hunter2 to +14165551234 via /home/ohmz/.hermes/alert_transports.env"
    st = make_status()
    st["tasks"]["alert_path_health"] = entry(
        status="crit", title="Alert path", summary="alert path: bridge missing; 3 smart hook sends failed " + leak,
        metrics={"bridge_ok": False, "hook_ok": False, "smart_fail_24h": 3, "smart_broken": True, "notify_fail_24h": 2,
                 "notify_broken": True, "last_error": leak, "note": leak},
        items=[{"what": "bridge", "level": "crit", "detail": "/usr/local/sbin/backup-notify-hermes.py missing or not executable"},
               {"what": "smartd hook", "level": "warn", "detail": "/usr/local/sbin/smart-alert.sh missing or not executable"},
               {"what": "smart hook", "level": "warn", "detail": "3 sends failed in 24h, last 10-01 19:08 " + leak},
               {"what": "notifier", "level": "warn", "detail": "cannot read log: Permission denied", "stderr": leak},
               {"what": "notifier budget", "level": "info", "detail": "2 alerts dropped in 24h (daily budget exhausted)"}])
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "alert_path_health")
    assert row["summary"] == "alert path: bridge missing; smartd hook broken; 3 SMART alert sends failed in 24h; 2 notifier sends failed in 24h"
    assert row["items"] == [{"what": "bridge", "level": "crit"}, {"what": "smartd hook", "level": "warn"},
                            {"what": "smart hook", "level": "warn"},
                            {"what": "notifier", "level": "warn", "detail": "cannot read log: Permission denied"},
                            {"what": "notifier budget", "level": "info", "detail": "2 alerts dropped in 24h (daily budget exhausted)"}]
    assert "last_error" not in row["metrics"]
    raw = env.raw().decode()
    assert not [f for f in ("hunter2", "4165551234", "alert_transports", ".hermes", "backup-notify", "smart-alert.sh", "smtp 535") if f in raw]


def test_alert_path_without_countable_problem_gets_a_fixed_summary(env):
    leak = "TypeError: cannot import alert_transports from /home/ohmz/StudioProjects/ai-stack/scripts"
    st = make_status()
    st["tasks"]["alert_path_health"] = entry(status="error", title="Alert path", summary="error: " + leak)
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "alert_path_health")
    assert row["summary"] == "alert path: check failed" and "alert_transports" not in env.raw().decode()
    st["tasks"]["alert_path_health"] = entry(status="warn", title="Alert path", summary="alert path: notifier log unreadable",
                                             items=[{"what": "notifier", "level": "warn", "detail": "cannot read log: Permission denied"},
                                                    {"what": "bridge", "level": "ok", "detail": "/x/y executable"}])
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "alert_path_health")
    assert row["summary"] == "alert path: 1 problem(s)"
    st["tasks"]["alert_path_health"] = entry(summary="ok: alert path ok; last SMART send 10-01 19:04", title="Alert path",
                                             items=[{"what": "bridge", "level": "ok", "detail": "/usr/local/sbin/x executable"}])
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "alert_path_health")
    assert row["summary"] == "alert path ok; last SMART send 10-01 19:04" and row["items"] == [{"what": "bridge", "level": "ok"}]


def test_text_after_an_rc_marker_is_cut_from_every_check(env):
    st = make_status()
    st["tasks"]["failed_units"].update(summary="warn: backup failed rc=2: smtp said 535 go away", metrics={"note": "x rc=1: raw stderr", "n": 1},
                                       items=[{"a": "docker exited RC=137 raw tail here", "b": "no marker here", "c": "src=1 is not a marker"}])
    env.audit([rec(NOW - 5, task="docker_images", outcome="failed: docker rm rc=1: Error response from daemon: secret detail")])
    env.run(st)
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "failed_units")
    assert row["summary"] == "backup failed rc=2" and row["metrics"] == {"note": "x rc=1", "n": 1}
    assert row["items"] == [{"a": "docker exited RC=137", "b": "no marker here", "c": "src=1 is not a marker"}]
    assert env.load("actions.json")["recent"][0]["outcome"] == "failed: docker rm rc=1"
    assert "smtp said" not in env.raw().decode() and "raw tail" not in env.raw().decode() and "secret detail" not in env.raw().decode()


# =========================================================================== review fixes: one malformed number must not poison files
HUGE = "1" + "0" * 400                                                      # a 401 digit integer: float() raises OverflowError


def test_huge_integers_are_not_numbers_helpers():
    big = int(HUGE)
    assert P._num(big) is None and P._num(big, 7) == 7 and P._int(big) == 0 and P._int(big, 5) == 5
    assert P._parse_ts(big) is None and P._parse_ts(-big) is None and P._parse_ts(float("inf")) is None
    assert P._scalar(big) is None and P._scalar(P.MAX_INT) == P.MAX_INT and P._scalar(-big) is None


@pytest.mark.parametrize("poison", [
    '{"ts":%s,"task":"a","action":"x","target":"y","bytes":1,"outcome":"done"}' % HUGE,
    '{"ts":-%s,"task":"a","action":"x","target":"y","bytes":1,"outcome":"done"}' % HUGE,
    '{"ts":"%s","task":"a","action":"x","target":"y","bytes":1,"outcome":"done"}' % HUGE,
    '{"ts":"2026-10-01T10:00:00+0000","task":"a","action":"x","target":"y","bytes":%s,"outcome":"done"}' % HUGE,
    '{"ts":"2026-10-01T10:00:00+0000","task":"a","action":"x","target":"y","bytes":-%s,"outcome":"done"}' % HUGE,
])
def test_one_poison_audit_line_does_not_stop_actions_and_storage(env, poison):
    env.audit([rec(NOW - 100, nbytes=2 * GIB), poison, rec(NOW - 50, task="trash", nbytes=GIB)])
    assert env.run(make_status()) == ALL_FILES                                             # nothing failed, nothing left stale
    a = env.load("actions.json")
    assert a["by_task"]["retention"]["freed_total"] == 2 * GIB and a["by_task"]["trash"]["freed_24h"] == GIB
    days = {d["day"]: d["bytes"] for d in env.load("storage.json")["freed_by_day"]}
    assert days["2026-10-01"] >= 3 * GIB
    assert env.load("overview.json")["export_errors"] == []
    for n, spec in SCHEMAS.items():
        check(env.load(n), spec)


def test_poison_reclaimed_log_record_does_not_stop_storage(env):
    log = [{"t": int(HUGE), "task": "x", "bytes": GIB}, {"t": NOW - 60, "task": "snap_revisions", "bytes": int(HUGE)},
           {"t": -int(HUGE), "task": "x", "bytes": 5}, {"t": NOW - 90, "task": "trash", "bytes": GIB}]
    assert env.run(make_status(reclaimed_log=log)) == ALL_FILES
    a = env.load("actions.json")
    assert a["by_task"]["trash"]["freed_24h"] == GIB and "x" not in a["by_task"]
    assert {d["day"]: d["bytes"] for d in env.load("storage.json")["freed_by_day"]}["2026-10-01"] >= GIB
    assert env.load("overview.json")["export_errors"] == []


def test_per_record_guard_survives_an_unforeseen_exception(env, monkeypatch):
    """Belt and braces: even a bug in the record handling skips ONE record instead of failing the whole file."""
    real = P._outcome

    def flaky(task, raw):
        if raw == "explode":
            raise RuntimeError("boom")
        return real(task, raw)
    monkeypatch.setattr(P, "_outcome", flaky)
    env.audit([rec(NOW - 100, outcome="explode"), rec(NOW - 50, nbytes=GIB)])
    assert env.run(make_status(reclaimed_log=[{"t": NOW - 5, "task": "ok", "bytes": 1}] + [{"t": "x", "bytes": None}])) == ALL_FILES
    assert [r["bytes"] for r in env.load("actions.json")["recent"]] == [GIB]
    monkeypatch.setattr(P, "_num", lambda v, default=None: (_ for _ in ()).throw(RuntimeError("x")) if v == "bomb" else default)
    P_reclaimed = P._Run(make_status(reclaimed_log=[{"t": "bomb", "task": "x", "bytes": 5}, "junk"]), NOW).reclaimed
    assert P_reclaimed == []


def test_poison_numbers_in_metrics_become_null_not_a_failure(env):
    st = make_status()
    st["tasks"]["failed_units"]["metrics"] = {"big": int(HUGE), "ok": 3, "edge": P.MAX_INT, "neg": -int(HUGE)}
    st["tasks"]["disk_forecast"]["metrics"]["mounts"][0].update(free=int(HUGE), used_pct=int(HUGE), days=int(HUGE))
    assert env.run(st) == ALL_FILES
    row = next(r for r in env.load("checks.json")["checks"] if r["name"] == "failed_units")
    assert row["metrics"] == {"big": None, "ok": 3, "edge": P.MAX_INT, "neg": None}


# =========================================================================== review fixes: failed files are announced, not hidden
def test_overview_reports_files_that_failed_to_build(env, monkeypatch):
    assert env.run() == ALL_FILES
    assert env.load("overview.json")["export_errors"] == []
    old = (env.out / "actions.json").read_bytes()

    def boom(run):
        raise RuntimeError("secret=hunter2")
    with monkeypatch.context() as m:
        m.setattr(P, "BUILDERS", [(n, boom if n in ("actions.json", "health-history.json") else b) for n, b in P.BUILDERS])
        written = env.run(now=NOW + 60)
    assert written == [n for n in ALL_FILES if n not in ("actions.json", "health-history.json")]          # order of ALL_FILES is kept
    assert env.load("overview.json")["export_errors"] == ["actions.json", "health-history.json"]
    assert (env.out / "actions.json").read_bytes() == old                                              # stale, but now announced
    assert "hunter2" not in env.raw().decode()
    check(env.load("overview.json"), SCHEMAS["overview.json"])
    assert env.run(now=NOW + 120) == ALL_FILES and env.load("overview.json")["export_errors"] == []       # recovers on the next run


def test_overview_reports_files_that_cannot_be_written_or_fit(env, monkeypatch):
    env.run()
    real = os.replace

    def flaky(src, dst):
        if str(dst).endswith("schedule.json"):
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(P.os, "replace", flaky)
    monkeypatch.setattr(P, "_metrics_export", lambda now: ring_export(series={"t": list(range(60_000)), "x": [1.5] * 60_000}))
    written = env.run(now=NOW + 60)
    assert "overview.json" in written and "schedule.json" not in written and "metrics.json" not in written
    assert env.load("overview.json")["export_errors"] == ["metrics.json", "schedule.json"]


def test_no_data_yet_is_not_an_export_error(env):
    env.run()
    st = make_status()
    del st["tasks"]["disk_forecast"]
    assert "storage.json" not in env.run(st)                                     # kept as it was: "no data", not "failed"
    assert env.load("overview.json")["export_errors"] == []


def test_poisoned_audit_line_makes_no_stale_banner_and_no_missing_file(env):
    """The reviewer's scenario end to end: before the fix actions.json and storage.json were silently left stale."""
    env.run()
    before = (env.out / "actions.json").read_bytes()
    env.audit(['{"ts":%s,"task":"a","bytes":1,"outcome":"done"}' % HUGE, rec(NOW + 10, task="trash", nbytes=GIB)])
    assert env.run(now=NOW + 60) == ALL_FILES
    assert (env.out / "actions.json").read_bytes() != before and env.load("overview.json")["export_errors"] == []



# =========================================================================== publish v2: SPEC3 / SPEC4 files
REAL_SEAMS = {seam: getattr(P, seam) for seam, _ in SEAMS.values()}
REAL_FIND_SOURCE = P._find_source
FAKES = {"routine.json": fake_routine, "incidents.json": fake_incidents, "slo.json": fake_slo, "pressure.json": fake_pressure,
         "jobs.json": fake_jobs, "monitors.json": fake_monitors, "notifications.json": fake_notifications,
         "migration.json": fake_migration}


def set_source(monkeypatch, name, fn):
    """Replace the export behind one passed-through file (fn takes the same arguments as the real export)."""
    if name in SEAMS:
        monkeypatch.setattr(P, SEAMS[name][0], fn)
    else:
        monkeypatch.setattr(P, "_find_source", lambda n, _o=P._find_source: fn if n == name else _o(n))


def boom(*a, **k):
    raise RuntimeError("secret=hunter2 /home/ohmz/a/b/c/d/e")


@pytest.fixture
def real_env(env, monkeypatch, tmp_path):
    """The real modules behind the passed-through files, over tmp dirs and the repo's shipped etc/*.toml."""
    conf = tmp_path / "conf"
    conf.mkdir()
    conf.chmod(0o755)
    for f in (ROOT / "etc").glob("*.toml"):
        shutil.copy(f, conf / f.name)
        (conf / f.name).chmod(0o644)                                  # legacy.py refuses a group/world-writable inventory
    monkeypatch.setattr(core, "CONF_DIR", conf)
    monkeypatch.setattr(core, "RUN_DIR", tmp_path / "run")
    for seam, fn in REAL_SEAMS.items():
        monkeypatch.setattr(P, seam, fn)
    monkeypatch.setattr(P, "_find_source", REAL_FIND_SOURCE)
    from homelab_maint.tasks import self_health                       # the real self.json, but it may not ask this host's systemd or website
    monkeypatch.setattr(self_health, "service_state", lambda *a, **k: ("unknown", False))
    monkeypatch.setattr(self_health, "http_get", lambda *a, **k: (None, b"", "refused", 1))
    return env


# --------------------------------------------------------------------------- shape, fidelity, modes
def test_new_files_are_written_valid_world_readable_and_spec_shaped(env):
    assert env.run() == ALL_FILES
    for n in NEW_FILES:
        p = env.out / n
        assert oct(p.stat().st_mode & 0o777) == "0o644" and 0 < p.stat().st_size < 200_000, n
        check(json.loads(p.read_bytes().decode("utf-8")), SCHEMAS[n], n)
    assert not [p for p in os.listdir(env.out) if p.startswith(".")]


@pytest.mark.parametrize("name", NEW_FILES)
def test_clean_documents_pass_through_unchanged(env, name):
    """The scrubber must not mangle ordinary names, times, class words, Markdown or numbers."""
    env.run()
    assert env.load(name) == json.loads(json.dumps(FAKES[name]()))


@pytest.mark.parametrize("name", NEW_FILES)
def test_a_source_that_failed_keeps_the_old_file_and_is_announced(env, monkeypatch, name):
    env.run()
    old = {n: (env.out / n).read_bytes() for n in ALL_FILES}
    set_source(monkeypatch, name, boom)
    written = env.run(now=NOW + 60)
    assert written == [n for n in ALL_FILES if n != name]                    # every other file is still written
    assert (env.out / name).read_bytes() == old[name]                        # the failed one keeps its content
    o = env.load("overview.json")
    assert o["export_errors"] == [name]
    assert "hunter2" not in env.raw().decode() and "/home/ohmz" not in env.raw().decode()
    check(o, SCHEMAS["overview.json"])
    set_source(monkeypatch, name, lambda *a, **k: FAKES[name](NOW + 120))     # and recovers by itself
    assert env.run(now=NOW + 120) == ALL_FILES and env.load("overview.json")["export_errors"] == []


@pytest.mark.parametrize("name", NEW_FILES)
@pytest.mark.parametrize("bad", [[1, 2], "text", 7, ("a",)], ids=["list", "str", "int", "tuple"])
def test_a_source_that_returns_a_non_object_is_an_export_error(env, monkeypatch, name, bad):
    env.run()
    old = (env.out / name).read_bytes()
    set_source(monkeypatch, name, lambda *a, **k: bad)
    assert name not in env.run(now=NOW + 60)
    assert (env.out / name).read_bytes() == old and env.load("overview.json")["export_errors"] == [name]


@pytest.mark.parametrize("name", ["routine.json", "incidents.json", "slo.json", "pressure.json"])
def test_the_core_exports_have_no_unavailable_fallback(env, monkeypatch, name):
    """routine/incidents/slo/pressure always exist in this build: None from them is a bug, not 'module absent'."""
    env.run()
    old = (env.out / name).read_bytes()
    set_source(monkeypatch, name, lambda *a, **k: None)
    assert name not in env.run(now=NOW + 60) and (env.out / name).read_bytes() == old
    assert env.load("overview.json")["export_errors"] == [name]


def test_publish_does_not_break_the_rest_when_every_new_source_fails(env, monkeypatch):
    for n in NEW_FILES:
        set_source(monkeypatch, n, boom)
    written = env.run()
    assert written == FILES + SELF_FILES                                     # the SPEC2 files (and the pipeline's self.json) are unaffected
    assert env.load("overview.json")["export_errors"] == sorted(NEW_FILES)
    assert not [n for n in NEW_FILES if (env.out / n).exists()]


def test_a_new_file_that_cannot_be_replaced_keeps_its_old_content_and_leaves_no_temp_file(env, monkeypatch):
    env.run()
    old = (env.out / "pressure.json").read_bytes()
    real = os.replace
    monkeypatch.setattr(P.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("disk full")) if str(d).endswith("pressure.json")
                        else real(s, d))
    written = env.run(now=NOW + 60)
    assert "pressure.json" not in written and "slo.json" in written
    assert (env.out / "pressure.json").read_bytes() == old
    assert env.load("overview.json")["export_errors"] == ["pressure.json"]
    assert not [p for p in os.listdir(env.out) if p.startswith(".")]


def test_generated_at_is_the_publish_time_and_defaults_when_missing_or_garbled(env, monkeypatch):
    env.run(now=NOW + 5)
    for n in NEW_FILES:
        assert env.load(n)["generated_at"] == NOW + 5, n
    for bad in (None, "yesterday", float("nan"), True):
        set_source(monkeypatch, "slo.json", lambda now, history=None, _b=bad: {**fake_slo(now), "generated_at": _b})
        env.run(now=NOW + 9)
        assert env.load("slo.json")["generated_at"] == NOW + 9, bad
    set_source(monkeypatch, "slo.json", lambda now, history=None: {k: v for k, v in fake_slo(now).items() if k != "generated_at"})
    env.run(now=NOW + 11)
    assert env.load("slo.json")["generated_at"] == NOW + 11


def test_routine_json_gets_the_timers_publish_already_read(env, monkeypatch):
    """One `systemctl list-timers` call serves schedule.json and the routine calendar (epoch SECONDS, 0 and null -> None)."""
    seen = []
    monkeypatch.setattr(P, "_routine_export", lambda now, timers: seen.append(timers) or fake_routine(now))
    calls = []
    monkeypatch.setattr(P, "_list_timers", lambda: calls.append(1) or [dict(t) for t in TIMERS])
    env.run()
    assert len(calls) == 1 and len(seen) == 1
    t = seen[0]
    assert t["homelab-maint-check.timer"] == {"next": NOW + 600, "last": NOW - 300}
    assert t["homelab-maint-weekly.timer"] == {"next": NOW + 5 * 86400, "last": None}
    assert t["fstrim.timer"] == {"next": None, "last": NOW - 3 * 86400} and "motd-news.timer" in t     # ALL timers, not just notable ones
    monkeypatch.setattr(P, "_list_timers", lambda: None)                      # no systemctl: an empty map, the calendar shows nominal times
    seen.clear()
    env.run()
    assert seen == [{}]


def test_routine_failure_does_not_cost_a_second_attempt(env, monkeypatch):
    n = []
    monkeypatch.setattr(P, "_routine_export", lambda now, timers: n.append(1) or boom())
    env.run()
    assert len(n) == 1                                                       # routine.json and the overview share ONE attempt
    assert env.load("overview.json")["export_errors"] == ["routine.json"]


# --------------------------------------------------------------------------- SPEC4 modules may be absent
def test_absent_spec4_modules_publish_unavailable_markers_without_an_error(env, monkeypatch):
    monkeypatch.setattr(P, "_find_source", lambda name: None)
    assert env.run() == ALL_FILES
    for n in OPTIONAL_FILES:
        assert env.load(n) == {"unavailable": True, "generated_at": NOW}, n
    assert env.load("overview.json")["export_errors"] == []                  # "not installed yet" is not a failure
    assert env.raw().count(b"unavailable") == len(OPTIONAL_FILES)
    for n in ("routine.json", "incidents.json", "slo.json", "pressure.json"):
        check(env.load(n), SCHEMAS[n])                                       # the SPEC3 files are unaffected


@pytest.mark.parametrize("name, modules", [("jobs.json", ["scheduler"]), ("monitors.json", ["tasks.monitors", "probes"]),
                                           ("notifications.json", ["notify"]), ("migration.json", ["legacy"])])
def test_find_source_survives_an_unimportable_or_incomplete_module(monkeypatch, name, modules):
    for m in modules:
        monkeypatch.setitem(sys.modules, f"homelab_maint.{m}", None)          # `import` raises ImportError, like a missing file
    assert REAL_FIND_SOURCE(name) is None


def test_find_source_prefers_the_first_candidate_that_imports_and_has_the_function(monkeypatch):
    import types
    first = types.ModuleType("homelab_maint.zz_first")
    second = types.ModuleType("homelab_maint.zz_second")
    second.export = lambda now: {"from": "second"}
    broken = types.ModuleType("homelab_maint.zz_broken")                      # importable, but the function is missing
    monkeypatch.setitem(sys.modules, "homelab_maint.zz_first", first)
    monkeypatch.setitem(sys.modules, "homelab_maint.zz_second", second)
    monkeypatch.setitem(sys.modules, "homelab_maint.zz_broken", broken)
    monkeypatch.setattr(P, "OPTIONAL_SOURCES", {"jobs.json": (("zz_nope", "export"), ("zz_broken", "export"), ("zz_second", "export"))})
    assert REAL_FIND_SOURCE("jobs.json")(NOW) == {"from": "second"}
    assert REAL_FIND_SOURCE("unknown.json") is None


def test_a_syntax_error_in_a_spec4_module_means_unavailable_not_a_crash(tmp_path, monkeypatch):
    import homelab_maint
    d = tmp_path / "pkgdir"
    d.mkdir()
    (d / "half_written.py").write_text("def export(now:\n")                      # another team's file, mid-edit
    monkeypatch.setattr(homelab_maint, "__path__", [*homelab_maint.__path__, str(d)])
    monkeypatch.setattr(P, "OPTIONAL_SOURCES", {"jobs.json": (("half_written", "export"),)})
    importlib.invalidate_caches()
    assert REAL_FIND_SOURCE("jobs.json") is None


def test_a_source_that_returns_none_is_published_as_unavailable(env, monkeypatch):
    set_source(monkeypatch, "migration.json", lambda now: None)               # legacy.export_public: inventory unusable -> hide the card
    assert env.run() == ALL_FILES
    assert env.load("migration.json") == {"unavailable": True, "generated_at": NOW}
    assert env.load("overview.json")["export_errors"] == []


def test_a_module_that_appears_or_disappears_changes_the_file_at_the_next_run(env, monkeypatch):
    monkeypatch.setattr(P, "_find_source", lambda name: None)
    env.run()
    assert env.load("jobs.json")["unavailable"] is True
    monkeypatch.setattr(P, "_find_source", lambda name: FAKE_SOURCES.get(name))
    env.run(now=NOW + 60)
    check(env.load("jobs.json"), SCHEMAS["jobs.json"])
    monkeypatch.setattr(P, "_find_source", lambda name: None)                 # removed again: the page must not keep showing old jobs
    env.run(now=NOW + 120)
    assert env.load("jobs.json") == {"unavailable": True, "generated_at": NOW + 120}


@pytest.mark.parametrize("name", OPTIONAL_FILES)
def test_a_present_source_that_raises_is_an_error_not_unavailable(env, monkeypatch, name):
    set_source(monkeypatch, name, boom)
    written = env.run()
    assert name not in written and not (env.out / name).exists()              # nothing old to keep: the web API answers "unavailable"
    assert env.load("overview.json")["export_errors"] == [name]


def test_find_source_is_called_once_per_file_per_run(env, monkeypatch):
    seen = []
    monkeypatch.setattr(P, "_find_source", lambda name: seen.append(name) or FAKE_SOURCES.get(name))
    env.run()
    assert sorted(seen) == sorted(OPTIONAL_FILES)


# --------------------------------------------------------------------------- redaction, identical to the SPEC2 files
LEAK = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz https://h.example/x?token=hunter2 ohmz@example.org 416-555-1234 password=hunter2"


def plant(o, leak=LEAK):
    """Deep copy of a document with a leak appended to every string and credential / raw-output fields added to every object."""
    if isinstance(o, dict):
        out = {k: plant(v, leak) for k, v in o.items()}
        out.update({"api_token": "hunter2", "stderr": "hunter2 " + leak, "last_error": "rc=1 hunter2", "traceback": "hunter2",
                    "my_password": "hunter2", "secrets": ["hunter2"], "note_rc": "backup failed rc=2: smtp said 535 go away hunter2"})
        return out
    if isinstance(o, list):
        return [plant(v, leak) for v in o]
    return o + " " + leak if isinstance(o, str) else o


GONE_KEYS = ("api_token", "stderr", "last_error", "traceback", "my_password", '"secrets"')


@pytest.mark.parametrize("name", NEW_FILES)
def test_secrets_are_scrubbed_from_every_string_of_every_new_file(env, monkeypatch, name):
    set_source(monkeypatch, name, lambda *a, **k: plant(FAKES[name]()))
    assert name in env.run()
    raw = (env.out / name).read_bytes().decode()
    frags = [f for f in SECRET_FRAGMENTS if f != "?token"]
    assert not [f for f in frags if f in raw], [f for f in frags if f in raw]
    assert "password=[redacted]" in raw and "https://h.example/x" in raw and "?token" not in raw      # the URL keeps host and path only
    assert "555-1234" not in raw and "416-555" not in raw
    assert not [k for k in GONE_KEYS if k in raw], [k for k in GONE_KEYS if k in raw]   # credential / raw-output string fields dropped
    assert "smtp said" not in raw and "go away" not in raw and "backup failed rc=2" in raw  # text after "rc=<n>" is cut, the marker stays
    json.loads(raw)


@pytest.mark.parametrize("name", NEW_FILES)
def test_every_known_leak_shape_is_scrubbed_in_every_new_file(env, monkeypatch, name):
    leaks = LEAKY + [t for t, _ in BLIND_SPOTS]
    frags = [f for f in SECRET_FRAGMENTS if f != "?token"] + [f for _, f in BLIND_SPOTS if f != "ssword"]   # "ssword" is inside "password"

    def src(*a, **k):
        doc = FAKES[name]()
        doc["zz_leaks"] = [f"warn: step failed ({t}) rc=1" for t in leaks] + leaks + [f"first line\n{t}" for t in leaks]
        doc["zz_nested"] = {"deep": [{"msg": t} for t in leaks]}
        return doc
    set_source(monkeypatch, name, src)
    assert name in env.run()
    raw = (env.out / name).read_bytes().decode()
    assert not [f for f in frags if f in raw], [f for f in frags if f in raw]


@pytest.mark.parametrize("name", NEW_FILES)
def test_paths_urls_tracebacks_and_control_characters_are_cleaned_in_new_files(env, monkeypatch, name):
    tb = 'Traceback (most recent call last):\n  File "/usr/local/lib/homelab-maint/x.py", line 12, in run\n    boom()\nValueError: no mounts'

    def src(*a, **k):
        doc = FAKES[name]()
        doc["zz"] = {"home": "removed /home/ohmz/.gradle/caches/modules-2/files-2.1/org/x.jar now", "url": "https://u:p@h.example/a/b?x=1#f",
                     "tb": tb, "ctl": "a\x00b\x1b[31m\tc", "multi": "first\nsecond secret", "long": "x" * 5000,
                     "surrogate": "bad\udcffname", "n": float("nan"), "i": float("inf"), "big": 10 ** 400}
        return doc
    set_source(monkeypatch, name, src)
    assert name in env.run()
    raw = (env.out / name).read_bytes().decode()                              # strict UTF-8
    zz = json.loads(raw, parse_constant=lambda c: pytest.fail(f"{name} contains {c}"))["zz"]
    assert zz["home"] == "removed /home/ohmz/.gradle/caches/modules-2/... now"
    assert zz["url"] == "https://h.example/a/b" and zz["tb"] == "ValueError: no mounts"
    assert zz["ctl"] == "a b [31m c" and zz["multi"] == "first" and zz["surrogate"] == "bad?name"
    assert len(zz["long"]) == P.STR_MAX and zz["long"].endswith("...")
    assert zz["n"] is None and zz["i"] is None and zz["big"] is None
    assert "Traceback" not in raw and 'File "' not in raw and "/usr/local/lib" not in raw and "boom()" not in raw


def test_dict_keys_are_scrubbed_and_unknown_types_become_scrubbed_text(env, monkeypatch):
    class Odd:
        def __str__(self):
            return "odd /home/ohmz/a/b/c/d/e password=hunter2"
    set_source(monkeypatch, "slo.json", lambda now, history=None: {**fake_slo(now), "zz": {"token=hunter2": 1, "k" * 200: 2, 5: 3, "obj": Odd(),
                                                                          "path": Path("/home/ohmz/a/b/c/d/e"), "raw": b"password=hunter2"}})
    assert "slo.json" in env.run()
    zz = env.load("slo.json")["zz"]
    assert "hunter2" not in json.dumps(zz) and "token=[redacted]" in zz and "5" in zz
    assert max(map(len, zz)) <= 60 and zz["obj"] == "odd /home/ohmz/a/b/c/... password=[redacted]"
    assert zz["path"] == "/home/ohmz/a/b/c/..." and zz["raw"] != "password=hunter2"   # bytes are never passed through raw


def test_sensitive_keys_are_dropped_at_any_depth_but_other_values_survive(env, monkeypatch):
    set_source(monkeypatch, "jobs.json", lambda now: {"generated_at": now, "jobs": [{"job": "a", "title": "A", "auth": "x", "cookie": "y",
                                                                                      "meta": {"deep": {"private_key": "z", "ok": "fine",
                                                                                                        "pass": 5, "api_key": ["k"]}}}]})
    env.run()
    row = env.load("jobs.json")["jobs"][0]
    assert row == {"job": "a", "title": "A", "meta": {"deep": {"ok": "fine", "pass": 5}}}           # only STRING values are dropped


def test_oversized_text_values_are_capped(env, monkeypatch):
    doc = fake_incidents()
    doc["recent"][0]["postmortem_md"] = "\n".join(f"- line {i} " + "w" * 60 for i in range(500))
    doc["recent"][0]["summary"] = "s" * 10_000
    set_source(monkeypatch, "incidents.json", lambda now: doc)
    env.run()
    r = env.load("incidents.json")["recent"][0]
    assert len(r["postmortem_md"]) <= P.TEXT_MAX and r["postmortem_md"].endswith("...") and r["postmortem_md"].startswith("- line 0")
    assert len(r["summary"]) == P.STR_MAX


# --------------------------------------------------------------------------- the postmortem is multi-line Markdown
PM = ("# Postmortem: Disk space (INC-20261001-001)\n\n- Severity: sev2  |  Check: `disk_forecast`\n\n## Summary\n\n"
      "Disk was confirmed crit at 07:31. What the check said: / 2% free password=hunter2 rc=1: stderr tail.\n\n## Timeline\n\n"
      "- 07:31 [detect] confirmed crit\n- 07:40 [action] docker_cache freed 21.2 GiB on /home/ohmz/.cache/a/b/c/d/e\n"
      "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END OPENSSH PRIVATE KEY-----\n"
      'Traceback (most recent call last):\n  File "/usr/local/lib/homelab-maint/x.py", line 12, in run\n    boom()\n'
      "ValueError: no usable mounts\n\n## Follow-ups\n\n- [ ] Review the `disk_forecast` playbook\n- [ ] token ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz\n")


def test_postmortem_markdown_keeps_its_structure_and_loses_its_secrets(env, monkeypatch):
    doc = fake_incidents()
    doc["recent"][0]["postmortem_md"] = PM
    set_source(monkeypatch, "incidents.json", lambda now: doc)
    env.run()
    md = env.load("incidents.json")["recent"][0]["postmortem_md"]
    for keep in ("# Postmortem: Disk space (INC-20261001-001)\n", "\n## Summary\n\n", "- 07:31 [detect] confirmed crit\n",
                 "- [ ] Review the `disk_forecast` playbook", "ValueError: no usable mounts", "/home/ohmz/.cache/a/b/..."):
        assert keep in md, keep
    assert md.count("\n") >= 18                                               # headings, blank lines and list items survive
    for gone in ("hunter2", "BEGIN OPENSSH", "b3BlbnNz", "Traceback", 'File "', "boom()", "/usr/local/lib", "stderr tail", "ghp_1A2b"):
        assert gone not in md, gone
    assert "password=[redacted] rc=1" in md and "token [redacted]" in md


def test_other_multi_line_strings_are_still_cut_to_their_first_line(env, monkeypatch):
    doc = fake_incidents()
    doc["open"][0]["summary"] = "first line\nsecond line password=hunter2"
    doc["open"][0]["playbook"]["meaning"] = "line one\nline two"
    set_source(monkeypatch, "incidents.json", lambda now: doc)
    env.run()
    o = env.load("incidents.json")["open"][0]
    assert o["summary"] == "first line" and o["playbook"]["meaning"] == "line one"


@pytest.mark.parametrize("text, want", [
    ("a\n\nb\n- c", "a\n\nb\n- c"), ("\n\n  x  \n\n", "x"), ("", ""), ("only", "only"),
    ('Traceback (most recent call last):\n  File "/a.py", line 1, in f\nKeyError: k', "KeyError: k"),
    ('before\nTraceback (most recent call last):\n  File "/a.py", line 1, in f\n    x()\n\nafter', "before\nafter"),
    ('File "/a.py", line 3, in loose\nnext', "next"),
    ("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nafter", "[redacted]\nafter"),
    ("-----BEGIN PRIVATE KEY-----\nabc\nno end marker\nafter", "[redacted]"),
])
def test_clean_text_cases(text, want):
    assert P._clean_text(text) == want


def test_clean_text_is_fast_on_pathological_input():
    t0 = time.time()
    for s in ("a." * 3000, "password=" + "w " * 4000, "-----BEGIN PRIVATE KEY-----" * 300, "Traceback (most recent call last):\n" * 800,
              "\n".join(["x=y " * 40] * 200), "http://a/" + "b/" * 4000):
        P._clean_text(s)
    assert time.time() - t0 < 1.0


def test_scrub_depth_and_cycle_safety(env, monkeypatch):
    deep: dict = {}
    cur = deep
    for _ in range(200):
        cur["d"] = {}
        cur = cur["d"]
    cyc: dict = {"a": 1}
    cyc["self"] = cyc
    set_source(monkeypatch, "slo.json", lambda now, history=None: {**fake_slo(now), "zz": deep})
    assert "slo.json" in env.run()                                            # the depth guard cuts it: no RecursionError
    node = env.load("slo.json")["zz"]
    for _ in range(8):
        assert isinstance(node, dict)
        node = node["d"]
    assert node is None
    set_source(monkeypatch, "slo.json", lambda now, history=None: {**fake_slo(now), "zz": cyc})
    assert "slo.json" in env.run()                                            # a cycle ends at the depth limit too
    assert len((env.out / "slo.json").read_bytes()) < 100_000


# --------------------------------------------------------------------------- size caps
def big(name):
    """The fake document with its main list blown up far past 200 KB, newest / most important rows FIRST."""
    d = FAKES[name]()
    if name == "routine.json":
        d["changes"] = [{"ts": NOW - i, "task": "docker_cache", "kind": "cleanup", "detail": "pruned something " + "ab cd " * 10 + str(i), "bytes": i,
                         "outcome": "done", "verified": True} for i in range(4000)]
    elif name == "incidents.json":
        d["recent"] = [dict(d["recent"][0], id=f"INC-{i:05d}", postmortem_md="\n".join(["- line"] * 400)) for i in range(300)]
    elif name == "pressure.json":
        d["spikes"] = [{"t": NOW - i, "peak": 2, "duration_s": i, "contributors": ["tunarr"] * 5, "outcome": "x" * 100} for i in range(2500)]
    elif name == "jobs.json":
        d["jobs"] = [dict(d["jobs"][0], job=f"job-{i}", why="w" * 100) for i in range(2500)]
    elif name == "monitors.json":
        d["probes"] = [dict(d["probes"][0], name=f"probe-{i}", detail="w" * 100) for i in range(2500)]
    elif name == "notifications.json":
        d["recent"] = [dict(d["recent"][0], ts=NOW - i, title="t" * 90) for i in range(2500)]
    elif name == "migration.json":
        d["items"] = [dict(d["items"][0], name=f"item-{i}", title="t" * 100) for i in range(2500)]
    return d


TRIMMABLE = ["routine.json", "incidents.json", "pressure.json", "jobs.json", "monitors.json", "notifications.json", "migration.json"]
MAIN_LIST = {"routine.json": "changes", "incidents.json": "recent", "pressure.json": "spikes", "jobs.json": "jobs",
             "monitors.json": "probes", "notifications.json": "recent", "migration.json": "items"}


@pytest.mark.parametrize("name", TRIMMABLE)
def test_oversized_new_documents_are_trimmed_oldest_first_to_fit(env, monkeypatch, name):
    src = big(name)
    assert len(json.dumps(src)) > 400_000
    set_source(monkeypatch, name, lambda *a, **k: json.loads(json.dumps(src)))
    assert name in env.run()
    p = env.out / name
    assert 0 < p.stat().st_size <= P.MAX_FILE_BYTES < 200_000
    doc = env.load(name)
    rows, orig = doc[MAIN_LIST[name]], json.loads(json.dumps(src[MAIN_LIST[name]]))
    assert 0 < len(rows) < len(orig) and rows == orig[:len(rows)]                # a prefix: the newest rows stay, only the tail was shed
    assert env.load("overview.json")["export_errors"] == [] and doc["generated_at"] == NOW


def test_routine_sheds_changes_before_the_calendar_and_pressure_sheds_the_oldest_history_last(env, monkeypatch):
    d = big("routine.json")
    d["calendar"] = [dict(c, items=c["items"] * 400) for c in d["calendar"]]
    set_source(monkeypatch, "routine.json", lambda *a, **k: json.loads(json.dumps(d)))
    env.run()
    r = env.load("routine.json")
    assert r["changes"] == [] or len(r["changes"]) < 4000
    assert len(r["calendar"]) >= 1 and r["calendar"][0]["date"] == "2026-10-01"      # today stays, the far future goes first
    p = fake_pressure()
    p["spikes"], p["actions"] = [], []
    p["history"] = [{"t": NOW - 900 * i, "level": 1, "note": "n" * 80} for i in range(5000, 0, -1)]            # oldest first
    set_source(monkeypatch, "pressure.json", lambda *a, **k: json.loads(json.dumps(p)))
    env.run()
    h = env.load("pressure.json")["history"]
    assert 0 < len(h) < 5000 and h[-1]["t"] == NOW - 900                              # the newest points stay, the oldest are shed


def test_a_document_that_cannot_be_trimmed_is_skipped_not_truncated(env, monkeypatch):
    env.run()
    old = (env.out / "slo.json").read_bytes()
    d = fake_slo()
    d["objectives"] = [dict(d["objectives"][0], name=f"o{i}", note="n" * 400) for i in range(1000)]
    set_source(monkeypatch, "slo.json", lambda now, history=None: d)
    assert "slo.json" not in env.run(now=NOW + 60)
    assert (env.out / "slo.json").read_bytes() == old and env.load("overview.json")["export_errors"] == ["slo.json"]


def test_the_cap_applies_with_a_smaller_limit_too(env, monkeypatch):
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 6_000)
    set_source(monkeypatch, "notifications.json", lambda *a, **k: big("notifications.json"))
    env.run()
    doc = env.load("notifications.json")
    assert (env.out / "notifications.json").stat().st_size <= 6_000 and 0 < len(doc["recent"]) < 2500 and doc["recent"][0]["ts"] == NOW


# --------------------------------------------------------------------------- monthly tier
def test_overview_has_a_monthly_tier(env):
    st = make_status()
    st["tier_runs"]["monthly"] = {"last_run": NOW - 9 * 86400, "dry_run": False}
    st["tasks"]["routine_trends"] = entry(tier="monthly", title="Long-term trends")
    st["tasks"]["routine_rotate"] = entry("C1", "monthly", title="Rotate tool logs", mode="apply")
    env.run(st)
    o = env.load("overview.json")
    check(o, SCHEMAS["overview.json"])
    assert o["tiers"]["monthly"] == {"last_run": NOW - 9 * 86400, "next_run": NOW + 10 * 86400, "mode": "apply"}   # next run: routine.json
    assert o["tiers"]["check"]["next_run"] == NOW + 600 and o["tiers"]["daily"]["next_run"] == NOW + 19 * 3600     # timers keep priority
    row = {r["name"]: r for r in env.load("checks.json")["checks"]}
    assert row["routine_trends"]["tier"] == "monthly" and row["routine_rotate"]["tier"] == "monthly"
    assert row["routine_trends"]["mode"] == "check" and row["routine_rotate"]["mode"] == "apply"


def test_monthly_last_run_falls_back_to_the_task_entries_and_mode_to_report(env):
    st = make_status()
    st["tasks"]["routine_trends"] = entry(tier="monthly", last_run=NOW - 5 * 86400)
    st["tasks"]["routine_rotate"] = entry("C1", "monthly", last_run=NOW - 6 * 86400)
    env.run(st)
    t = env.load("overview.json")["tiers"]["monthly"]
    assert t["last_run"] == NOW - 5 * 86400 and t["mode"] == "report"          # the newest of the monthly tasks
    env.run(make_status())
    assert env.load("overview.json")["tiers"]["monthly"]["last_run"] is None   # nothing monthly ever ran


def test_a_monthly_timer_if_there_is_one_beats_the_routine_forecast(env, monkeypatch):
    monkeypatch.setattr(P, "_list_timers", lambda: [dict(t) for t in TIMERS] + [
        {"unit": "homelab-maint-monthly.timer", "next": int((NOW + 3 * 86400) * 1e6), "last": int((NOW - 27 * 86400) * 1e6)}])
    monkeypatch.setattr(P, "_timer_specs", lambda units: {u: SPECS[u] for u in units if u in SPECS} | {
        "homelab-maint-monthly.timer": "*-*-1 07:50:00"})
    env.run()
    assert env.load("overview.json")["tiers"]["monthly"]["next_run"] == NOW + 3 * 86400
    tm = env.load("schedule.json")["timers"]
    assert [t["unit"] for t in tm][:4] == ["homelab-maint-check.timer", "homelab-maint-daily.timer", "homelab-maint-weekly.timer",
                                           "homelab-maint-monthly.timer"]
    m = tm[3]
    assert m["title"] == "Monthly review" and m["last"] == NOW - 27 * 86400 and m["next"] == NOW + 3 * 86400


def test_next_run_is_null_when_neither_a_timer_nor_the_routine_knows_it(env, monkeypatch):
    monkeypatch.setattr(P, "_routine_export", lambda now, timers: {**fake_routine(now), "routine": [
        {"name": "daily", "cadence": "daily", "next_run": "soon"}, {"name": "x", "cadence": "monthly", "next_run": None}, "junk"]})
    env.run()
    t = env.load("overview.json")["tiers"]
    assert t["monthly"]["next_run"] is None and t["check"]["next_run"] == NOW + 600
    assert t["daily"]["next_run"] == NOW + 19 * 3600                            # the daily timer exists: the routine is not consulted


def test_without_systemctl_the_routine_forecast_fills_daily_weekly_and_monthly_but_not_the_check_tier(env, monkeypatch):
    monkeypatch.setattr(P, "_list_timers", lambda: None)
    env.run()
    t = env.load("overview.json")["tiers"]
    assert (t["check"]["next_run"], t["daily"]["next_run"], t["weekly"]["next_run"], t["monthly"]["next_run"]) == (
        None, NOW + 19 * 3600, NOW + 5 * 86400, NOW + 10 * 86400)
    assert env.load("schedule.json")["timers"] == []


def test_a_failing_routine_export_leaves_next_run_null_and_is_reported_once(env, monkeypatch):
    monkeypatch.setattr(P, "_list_timers", lambda: None)
    monkeypatch.setattr(P, "_routine_export", lambda now, timers: boom())
    env.run()
    o = env.load("overview.json")
    assert o["tiers"]["monthly"]["next_run"] is None and o["export_errors"] == ["routine.json"]


def test_garbled_routine_documents_do_not_break_the_overview(env, monkeypatch):
    for bad in ({"routine": "x"}, {"routine": [None, 5, {"cadence": "monthly", "next_run": float("nan")}]}, {"routine": {"a": 1}}, {}):
        monkeypatch.setattr(P, "_routine_export", lambda now, timers, _b=bad: _b)
        assert "overview.json" in env.run()
        assert env.load("overview.json")["tiers"]["monthly"]["next_run"] is None


# --------------------------------------------------------------------------- live_age_s
def put_live(env, doc=None, raw=None, mtime=None):
    env.out.mkdir(parents=True, exist_ok=True)
    p = env.out / "live.json"
    p.write_bytes(raw if raw is not None else json.dumps(doc).encode())
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_live_age_is_null_without_the_daemon_and_publish_never_writes_live_json(env):
    written = env.run()
    assert env.load("overview.json")["live_age_s"] is None
    assert "live.json" not in written and not (env.out / "live.json").exists()


def test_live_age_comes_from_the_daemons_own_timestamp(env):
    p = put_live(env, {"schema": 1, "generated_at": NOW - 7.5, "host": {}})
    before = p.read_bytes(), p.stat().st_mtime_ns
    written = env.run()
    o = env.load("overview.json")
    assert o["live_age_s"] == 7.5
    check(o, SCHEMAS["overview.json"])
    assert "live.json" not in written and (p.read_bytes(), p.stat().st_mtime_ns) == before      # read, never touched
    put_live(env, {"generated_at": NOW - 3600})
    env.run()
    assert env.load("overview.json")["live_age_s"] == 3600.0                     # a dead daemon: the age keeps growing
    put_live(env, {"generated_at": NOW + 2})
    env.run()
    assert env.load("overview.json")["live_age_s"] == 0.0                        # a stamp slightly ahead of this clock is not negative


@pytest.mark.parametrize("raw", [b"{broken", b"", b"[1,2]", b'"x"', b"null", b'{"generated_at": "now"}', b'{"generated_at": true}',
                                 b'{"generated_at": 1e999}', b"\xff\xfe\xfd", b"[" * 60_000])
def test_live_age_falls_back_to_the_file_time_when_the_content_is_unusable(env, raw):
    put_live(env, raw=raw, mtime=NOW - 42)
    env.run()
    assert env.load("overview.json")["live_age_s"] == 42.0


def test_live_age_ignores_a_timestamp_from_the_future_and_a_huge_file(env):
    put_live(env, {"generated_at": NOW + 10 * 86400}, mtime=NOW - 30)           # clock trouble: believe the file time instead
    env.run()
    assert env.load("overview.json")["live_age_s"] == 30.0
    put_live(env, raw=b'{"generated_at": %d, "pad": "%s"}' % (NOW - 5, b"x" * (P.LIVE_MAX + 10)), mtime=NOW - 99)
    env.run()
    assert env.load("overview.json")["live_age_s"] == 99.0                       # too big to read: the mtime answers
    put_live(env, {"generated_at": 1e18}, mtime=NOW + 500)
    env.run()
    assert env.load("overview.json")["live_age_s"] == 0.0


def test_live_age_survives_a_live_json_that_is_not_a_file(env):
    env.out.mkdir(parents=True, exist_ok=True)
    (env.out / "live.json").mkdir()
    assert "overview.json" in env.run() and isinstance(env.load("overview.json")["live_age_s"], (int, float))


# --------------------------------------------------------------------------- bind-mount safety
def test_the_public_dir_and_reports_dir_are_never_recreated_and_foreign_files_are_never_touched(env):
    env.out.mkdir(parents=True)
    (env.out / "notes.txt").write_text("owner notes")
    (env.out / "live.json").write_text('{"generated_at": %d}' % (NOW - 1))
    rdir = env.out / "reports"
    rdir.mkdir()
    (rdir / "readme.txt").write_text("not ours")
    before = (env.out.stat().st_ino, rdir.stat().st_ino)
    for i in range(3):
        env.run(now=NOW + i)
        assert (env.out.stat().st_ino, rdir.stat().st_ino) == before
    assert (env.out / "notes.txt").read_text() == "owner notes" and (rdir / "readme.txt").read_text() == "not ours"
    assert (env.out / "live.json").read_text() == '{"generated_at": %d}' % (NOW - 1)
    assert sorted(p.name for p in env.out.iterdir()) == sorted(ALL_FILES + ["notes.txt", "live.json", "reports"])


def test_a_symlinked_public_dir_stays_a_symlink(env, tmp_path):
    """A bind mount or a symlink at STATE_DIR/public must keep pointing at the same directory."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    env.out.symlink_to(target)
    env.run()
    assert env.out.is_symlink() and os.readlink(env.out) == str(target) and (target / "routine.json").exists()
    assert (target / "reports").is_dir()


# --------------------------------------------------------------------------- reports/ (written by the report tasks)
def report_doc(rid="2026-09-30", kind="daily", end=None, score=97, **over):
    t0 = float(timegm((int(rid[:4]), int(rid[5:7]), int(rid[8:10]), 0, 0, 0))) if kind == "daily" else NOW - 7 * 86400
    d = {"schema": 1, "id": rid, "kind": kind, "generated_at": NOW - 1000, "headline": f"Report {rid}",
         "period": {"start": t0, "end": end if end is not None else t0 + 86400, "label": rid, "tz": "UTC", "days": 1, "hours": 24},
         "health": {"score": score, "grade": "A", "worst_status": "ok"}, "highlights": ["all good"]}
    d.update(over)
    return d


def put_report(env, doc, mode=0o644):
    rdir = env.out / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / f"{doc['id']}.json"
    p.write_text(json.dumps(doc))
    p.chmod(mode)
    return p


def put_index(env, entries=None, raw=None, mode=0o644):
    rdir = env.out / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / "index.json"
    p.write_text(raw if raw is not None else json.dumps(entries))
    p.chmod(mode)
    return p


def entry_of(doc):
    return {"id": doc["id"], "kind": doc["kind"], "period_start": doc["period"]["start"], "period_end": doc["period"]["end"],
            "generated_at": doc["generated_at"], "headline": doc["headline"], "health": {"score": doc["health"]["score"], "grade": "A",
                                                                                         "worst_status": "ok"}}


def test_reports_dir_is_created_empty_with_no_index_when_no_report_exists(env):
    written = env.run()
    rdir = env.out / "reports"
    assert rdir.is_dir() and oct(rdir.stat().st_mode & 0o777) == "0o755" and os.listdir(rdir) == []
    assert written == ALL_FILES and env.load("overview.json")["export_errors"] == []    # "no reports yet" is not an error, nothing to index


def test_a_consistent_index_is_left_alone(env):
    docs = [report_doc("2026-09-30"), report_doc("2026-10-01"), report_doc("2026-W40", "weekly", end=NOW - 86400)]
    for d in docs:
        put_report(env, d)
    ip = put_index(env, [entry_of(d) for d in sorted(docs, key=lambda d: d["id"], reverse=True)])
    before = (ip.read_bytes(), ip.stat().st_mtime_ns, ip.stat().st_ino)
    for i in range(3):
        assert env.run(now=NOW + i) == ALL_FILES                                  # reports/index.json is not rewritten, so not listed
    assert (ip.read_bytes(), ip.stat().st_mtime_ns, ip.stat().st_ino) == before
    assert env.load("overview.json")["export_errors"] == []


def test_a_missing_index_is_rebuilt_from_the_report_files(env):
    docs = [report_doc("2026-09-29", score=90), report_doc("2026-09-30", score=95), report_doc("2026-10-01", score=99)]
    for d in docs:
        put_report(env, d)
    written = env.run()
    assert written == ALL_FILES + ["reports/index.json"]
    idx = json.loads((env.out / "reports" / "index.json").read_text())
    assert [e["id"] for e in idx] == ["2026-10-01", "2026-09-30", "2026-09-29"]          # newest first, like reports.write_report
    assert idx[0] == entry_of(docs[2]) and oct((env.out / "reports" / "index.json").stat().st_mode & 0o777) == "0o644"
    assert env.run(now=NOW + 1) == ALL_FILES                                              # healed: the next run has nothing to do
    assert sorted(p.name for p in (env.out / "reports").iterdir()) == ["2026-09-29.json", "2026-09-30.json", "2026-10-01.json", "index.json"]


@pytest.mark.parametrize("raw", ["{broken", "", "{}", '"x"', "[1, 2]", '[{"id": "2026-09-30"}, {"id": "2026-09-30"}]',     # duplicate id
                                 '[{"id": "../etc/passwd"}]', '[{"id": 5}]', '[{"id": "2026-09-30", "x": NaN}]', "[" * 60_000,
                                 '[{"id": "' + "9" * 40 + '"}]'],
                         ids=["broken", "empty", "object", "string", "ints", "duplicate-id", "traversal-id", "int-id", "nan", "deep", "long-id"])
def test_a_damaged_index_is_rebuilt(env, raw):
    d = report_doc("2026-09-30")
    put_report(env, d)
    put_index(env, raw=raw)
    assert env.run() == ALL_FILES + ["reports/index.json"]
    assert json.loads((env.out / "reports" / "index.json").read_text()) == [entry_of(d)]


def test_an_index_that_disagrees_with_the_files_is_rebuilt(env):
    a, b, c = report_doc("2026-09-28"), report_doc("2026-09-29"), report_doc("2026-09-30")
    put_report(env, a), put_report(env, b)
    put_index(env, [entry_of(b), entry_of(a), entry_of(c)])                       # lists a report whose file does not exist
    assert env.run() == ALL_FILES + ["reports/index.json"]
    assert [e["id"] for e in json.loads((env.out / "reports" / "index.json").read_text())] == ["2026-09-29", "2026-09-28"]
    put_report(env, c)                                                             # a report the index does not know
    assert env.run(now=NOW + 1) == ALL_FILES + ["reports/index.json"]
    assert [e["id"] for e in json.loads((env.out / "reports" / "index.json").read_text())] == ["2026-09-30", "2026-09-29", "2026-09-28"]


def test_a_stale_index_without_any_report_file_is_emptied(env):
    put_index(env, [entry_of(report_doc("2026-09-30"))])
    assert env.run() == ALL_FILES + ["reports/index.json"]
    assert json.loads((env.out / "reports" / "index.json").read_text()) == []


def test_unreadable_report_files_are_left_out_of_the_rebuilt_index_and_do_not_rewrite_it_forever(env):
    good = report_doc("2026-09-30")
    put_report(env, good)
    (env.out / "reports" / "2026-09-29.json").write_text("{garbage")             # a report file nobody can parse
    assert env.run() == ALL_FILES + ["reports/index.json"]
    assert [e["id"] for e in json.loads((env.out / "reports" / "index.json").read_text())] == ["2026-09-30"]
    assert env.run(now=NOW + 1) == ALL_FILES                                       # same index again: no rewrite, no churn
    assert (env.out / "reports" / "2026-09-29.json").read_text() == "{garbage"     # and the file is never deleted or rewritten here


def test_more_files_than_the_retention_limit_are_a_consistent_state(env, monkeypatch):
    """reports.write_report evicts files beyond the limit on its NEXT write: an index that is full is not out of step."""
    monkeypatch.setattr(P, "REPORT_KEEP", 3)
    docs = [report_doc(f"2026-09-{d:02d}") for d in range(20, 26)]
    for d in docs:
        put_report(env, d)
    kept = [entry_of(d) for d in reversed(docs[-3:])]
    ip = put_index(env, kept)
    before = ip.read_bytes()
    assert env.run() == ALL_FILES and ip.read_bytes() == before
    assert len(list((env.out / "reports").glob("2026-*.json"))) == 6              # nothing deleted


def test_other_file_names_in_reports_are_ignored_and_never_indexed(env, tmp_path):
    put_report(env, report_doc("2026-09-30"))
    rdir = env.out / "reports"
    for name in ("evil.json", "2026-9-30.json", "2026-09-30.json.bak", "2026-W4.json", "notes.txt", ".hidden.json"):
        (rdir / name).write_text("{}")
    (rdir / "sub").mkdir()
    (rdir / "sub" / "2026-09-01.json").write_text("{}")
    (tmp_path / "outside.json").write_text("{}")
    (rdir / "2026-09-01.json").symlink_to(tmp_path / "outside.json")              # a symlink is not a report file
    env.run()
    assert [e["id"] for e in json.loads((rdir / "index.json").read_text())] == ["2026-09-30"]
    assert (tmp_path / "outside.json").read_text() == "{}"


def test_report_permissions_are_made_readable_for_the_web_container(env):
    p = put_report(env, report_doc("2026-09-30"), mode=0o600)
    ip = put_index(env, [entry_of(report_doc("2026-09-30"))], mode=0o600)
    rdir = env.out / "reports"
    rdir.mkdir(exist_ok=True)
    rdir.chmod(0o700)
    env.run()
    assert oct(rdir.stat().st_mode & 0o777) == "0o755"
    assert oct(p.stat().st_mode & 0o777) == "0o644" and oct(ip.stat().st_mode & 0o777) == "0o644"
    q = put_report(env, report_doc("2026-09-29"), mode=0o664)                      # already world readable: left exactly as it is
    env.run(now=NOW + 1)
    assert oct(q.stat().st_mode & 0o777) == "0o664"


def test_temp_files_of_a_killed_report_writer_are_swept_after_ten_minutes_only(env):
    rdir = env.out / "reports"
    rdir.mkdir(parents=True)
    old, fresh, mine = rdir / ".rep-old.tmp", rdir / ".rep-new.tmp", rdir / ".pub-old.tmp"
    for p in (old, fresh, mine):
        p.write_text("x")
    for p in (old, mine):
        os.utime(p, (time.time() - 3600, time.time() - 3600))
    env.run()
    assert not old.exists() and not mine.exists() and fresh.exists()


def test_a_report_being_written_is_not_waited_for_and_its_index_is_not_overwritten(env):
    put_report(env, report_doc("2026-09-30"))
    lock = open(env.state / "reports.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX)                                                # the report writer holds the lock
    try:
        t0 = time.time()
        written = env.run()
        assert time.time() - t0 < 1.0                                               # NOT waited for
        assert written == ALL_FILES and not (env.out / "reports" / "index.json").exists()
        assert env.load("overview.json")["export_errors"] == []                     # the writer refreshes the index itself
    finally:
        lock.close()
    assert env.run(now=NOW + 1) == ALL_FILES + ["reports/index.json"]                # once it is free the index is healed


def test_an_unopenable_lock_file_skips_the_rebuild_quietly(env):
    put_report(env, report_doc("2026-09-30"))
    (env.state / "reports.lock").mkdir()                                            # open(..., "a") fails with IsADirectoryError
    assert env.run() == ALL_FILES and not (env.out / "reports" / "index.json").exists()
    assert env.load("overview.json")["export_errors"] == []


def test_a_failing_index_rebuild_is_an_export_error_and_nothing_else_breaks(env, monkeypatch):
    put_report(env, report_doc("2026-09-30"))
    from homelab_maint import reports
    monkeypatch.setattr(reports, "load_index", boom)
    written = env.run()
    assert written == ALL_FILES and env.load("overview.json")["export_errors"] == ["reports/index.json"]
    assert "hunter2" not in env.raw().decode() and "/home/ohmz" not in env.raw().decode()


def test_a_rebuilt_index_scrubs_the_headlines_it_copies_out_of_report_files(env):
    leak = "disk 2% free ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz mail ohmz@example.org password=hunter2"
    d = report_doc("2026-09-30", headline=leak)
    p = put_report(env, d)
    assert env.run() == ALL_FILES + ["reports/index.json"]
    raw = (env.out / "reports" / "index.json").read_text()
    assert not [f for f in ("hunter2", "ghp_1A2b", "ohmz@") if f in raw] and "disk 2% free" in raw
    assert json.loads(p.read_text())["headline"] == leak                              # the report file itself is the report task's business


def test_the_rebuild_matches_what_the_report_writer_produces(env):
    """Same shape and order as reports.write_report: build real reports, lose the index, publish, compare."""
    from homelab_maint import reports
    for d in (report_doc("2026-09-29"), report_doc("2026-09-30"), report_doc("2026-W40", "weekly", end=NOW - 3600)):
        reports.write_report(d)
    written_by_reports = json.loads((env.out / "reports" / "index.json").read_text())
    assert [e["id"] for e in written_by_reports] == ["2026-W40", "2026-09-30", "2026-09-29"]
    (env.out / "reports" / "index.json").unlink()
    assert env.run() == ALL_FILES + ["reports/index.json"]
    assert json.loads((env.out / "reports" / "index.json").read_text()) == written_by_reports
    assert env.run(now=NOW + 1) == ALL_FILES


def test_reports_are_passed_through_byte_for_byte(env):
    """Report files are written (and scrubbed) by their own task; publish never rewrites or deletes one."""
    docs = [report_doc("2026-09-29"), report_doc("2026-09-30")]
    paths = [put_report(env, d) for d in docs]
    put_index(env, [entry_of(d) for d in reversed(docs)])
    before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in paths]
    for i in range(3):
        env.run(now=NOW + i)
    assert [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in paths] == before


# --------------------------------------------------------------------------- the real modules, over tmp dirs and the shipped etc/*.toml
def test_real_modules_export_the_spec_shapes(real_env):
    t0 = time.time()
    written = real_env.run()
    took = time.time() - t0
    assert written == ALL_FILES and real_env.load("overview.json")["export_errors"] == [], (written, real_env.load("overview.json"))
    for n in NEW_FILES:
        doc = real_env.load(n)
        assert not doc.get("unavailable"), n
        check(doc, SCHEMAS[n], n)
        assert 0 < (real_env.out / n).stat().st_size < 200_000
    assert real_env.load("jobs.json")["jobs"] and real_env.load("monitors.json")["probes"] and real_env.load("migration.json")["items"]
    assert took < 1.5, took
    check(real_env.load("overview.json"), SCHEMAS["overview.json"])


def test_real_exports_survive_the_scrubber_unchanged(real_env):
    """Ordinary real data (names, times, windows, class words) must not be mangled by the redaction rules."""
    real_env.run()
    from homelab_maint import incidents, legacy, notify, scheduler
    from homelab_maint import routine
    from homelab_maint.tasks import monitors, pressure
    timers = {str(t.get("unit")): {"next": P._us(t.get("next")), "last": P._us(t.get("last"))} for t in TIMERS}
    want = {"routine.json": lambda: routine.export(NOW, timers=timers), "incidents.json": lambda: incidents.export_incidents(NOW),
            "slo.json": lambda: incidents.export_slo(None, NOW), "pressure.json": lambda: pressure.export(NOW),
            "jobs.json": lambda: scheduler.export(NOW), "monitors.json": lambda: monitors.export(NOW),
            "notifications.json": lambda: notify.export(NOW), "migration.json": lambda: legacy.export_public(NOW)}
    for n, fn in want.items():
        raw = json.loads(json.dumps(fn(), default=str))
        got = real_env.load(n)
        assert got == raw, (n, [k for k in raw if raw[k] != got.get(k)])


def test_real_modules_with_a_month_of_history_stay_fast_and_valid(real_env):
    """30 days of 15-minute runs of 20 checks (57,600 lines, ~6 MB) and 3,000 audit rows, every real module: well under 1.5 s."""
    real_env.history([{"t": NOW - 900 * k, "kind": "task", "task": f"t{i}", "status": "ok", "reclaimed": 0, "dur": 1.0, "metrics": {"a": 1}}
                      for k in range(30 * 96) for i in range(20)])
    real_env.audit([rec(NOW - 60 * i, target=f"/home/ohmz/a/b/c/d/f{i}") for i in range(3000)])
    t0 = time.time()
    assert real_env.run() == ALL_FILES
    assert time.time() - t0 < 1.5
    for n in ALL_FILES:
        assert (real_env.out / n).stat().st_size < 200_000, n


def test_real_modules_publish_unavailable_when_the_inventory_is_unusable(real_env):
    """A group-writable inventory is refused (it names units and scripts that run as root): legacy.export_public() returns None, the
    Migration card is hidden, and that is not an export error."""
    (core.CONF_DIR / "legacy-retirement.toml").chmod(0o666)
    assert real_env.run() == ALL_FILES
    assert real_env.load("migration.json") == {"unavailable": True, "generated_at": NOW}
    assert real_env.load("overview.json")["export_errors"] == []


def test_real_modules_spawn_nothing_but_the_two_systemctl_reads(real_env, monkeypatch):
    """No docker, no nvidia-smi, no network, no writes to the host (the 1.5 s budget and the read-only promise)."""
    spawned = []
    real_popen = subprocess.Popen.__init__

    def spy(self, cmd, *a, **k):
        spawned.append(cmd if isinstance(cmd, str) else " ".join(map(str, cmd)))
        return real_popen(self, cmd, *a, **k)
    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    monkeypatch.setattr(P, "_list_timers", REAL_LIST_TIMERS)                         # the real readers, so their commands are visible
    monkeypatch.setattr(P, "_timer_specs", REAL_TIMER_SPECS)
    real_env.run()
    assert len(spawned) <= 2 and all(c.startswith("systemctl ") for c in spawned) and all(
        c.split()[1] in ("list-timers", "show") for c in spawned), spawned


# --------------------------------------------------------------------------- slo.json / incidents.json: one pass over the files
def slo_history_lines(rng, names, days=40):
    """40 days of 15-minute runs for the checks the shipped objectives name, plus every odd record the pass must treat like the module does."""
    lines = []
    for k in range(days * 96):
        t = NOW - 900 * k - rng.randint(0, 300)
        for n in names:
            st = rng.choice(["ok"] * 25 + ["warn", "crit", "error", "skipped", "info"])
            r = {"t": t, "kind": "task", "task": n, "status": st}
            if rng.random() < 0.03:
                r["alert"] = rng.choice([True, False, "yes", 0, None])
            lines.append(json.dumps(r, separators=(",", ":")) if rng.random() < 0.8 else json.dumps(r))      # compact and spaced JSON
    t = NOW - 5000
    lines += ['{"t":%s,"kind":"task","task":["a"],"status":"warn"}' % t,                                        # task is not a string: skipped
              '{"t":%s,"kind":"task","task":"%s","status":["warn"]}' % (t, names[0]),                          # odd status type
              '{"t":%s,"kind":"task","task":"%s","status":"crit","alert":false}' % (t - 900, names[0]),        # alert flag AFTER status
              '{"t":%s,"kind":"task","task":"%s","alert":true,"status":"crit"}' % (t - 1800, names[0]),        # key order differs
              '{"t":%s,"kind":"task","task":"%s","status":"crit"}' % (NOW + 5 * 86400, names[0]),              # far future: never counted
              '{"t":%s,"kind":"task","task":"%s","status":"crit"}' % (NOW + 600, names[0]),                    # a few minutes ahead of the clock
              '{"t":"x","kind":"task","task":"%s","status":"crit"}' % names[0], "{broken", "", "[1]", "null",
              '{"t":%s,"kind":"disk","mount":"/","free":5}' % t, '{"t":%s,"kind":"sample","c":{}}' % t]
    return lines


def test_slo_computed_from_the_shared_history_pass_equals_the_module_reading_the_file(real_env):
    from homelab_maint import incidents
    names = sorted({c for o in incidents.load_config()["slo"] for c in o.get("checks", [])})
    assert len(names) >= 6
    lines = slo_history_lines(random.Random(7), names)
    random.Random(8).shuffle(lines)
    (real_env.state / "history.jsonl").write_text("\n".join(lines) + "\n")
    assert "slo.json" in real_env.run()
    want = json.loads(json.dumps(incidents.export_slo(None, NOW)))                  # the module parsing the whole file by itself
    got = real_env.load("slo.json")
    assert got == want
    assert any(o["availability_pct"] is not None and o["availability_pct"] < 100 for o in got["objectives"])   # not a trivial comparison
    assert any(o["status"] != "ok" or o["burn_rate_1d"] for o in got["objectives"])


def test_slo_with_a_longer_window_than_publish_keeps_makes_the_module_read_the_file(real_env, monkeypatch):
    from homelab_maint import incidents
    calls = []
    real_load, real_slo = incidents.load_config, incidents.export_slo

    def load():
        cfg = real_load()
        cfg["slo_defaults"] = {**cfg["slo_defaults"], "window_days": 45}
        return cfg
    monkeypatch.setattr(incidents, "load_config", load)
    monkeypatch.setattr(incidents, "export_slo", lambda history, now, cfg=None: calls.append(history) or real_slo(history, now, cfg))
    names = sorted({c for o in real_load()["slo"] for c in o.get("checks", [])})
    (real_env.state / "history.jsonl").write_text("\n".join(slo_history_lines(random.Random(3), names, days=50)) + "\n")
    assert "slo.json" in real_env.run()
    assert calls == [None]                                                           # the module reads 45 days itself
    assert real_env.load("slo.json")["window_days"] == 45
    assert real_env.load("slo.json") == json.loads(json.dumps(real_slo(None, NOW, load())))


def test_slo_gets_the_shared_records_in_the_shape_the_module_reads(real_env, monkeypatch):
    from homelab_maint import incidents
    seen = []
    real_slo = incidents.export_slo
    monkeypatch.setattr(incidents, "export_slo", lambda history, now, cfg=None: seen.append(history) or real_slo(history, now, cfg))
    real_env.history([{"t": NOW - 900, "kind": "task", "task": "disk_forecast", "status": "warn"},
                      '{"t":%s,"kind":"task","task":"failed_units","status":"ok","alert":false}' % (NOW - 800),
                      {"t": NOW - 10, "kind": "disk", "mount": "/", "free": 1}, {"t": NOW - 40 * 86400, "kind": "task", "task": "x", "status": "ok"}])
    real_env.run()
    assert seen == [[{"t": NOW - 900, "kind": "task", "task": "disk_forecast", "status": "warn"},
                     {"t": NOW - 800, "kind": "task", "task": "failed_units", "status": "ok", "alert": False}]]


def test_history_pass_collects_task_records_with_their_alert_flag_and_parses_only_the_odd_lines(env, monkeypatch):
    t = NOW - 600
    lines = ['{"t":%s,"kind":"task","task":"a","status":"warn"}' % t,                                             # fast path, no flag
             '{"t":%s,"kind":"task","task":"b","status":"crit","alert":false,"dur":1}' % (t + 1),                  # fast path + flag
             '{"t": %s, "kind": "task", "task": "c", "status": "warn", "alert": true}' % (t + 2),                  # spaced
             '{"t":%s,"kind":"task","task":"d","status":"warn","dur":1,"alert":false}' % (t + 3),                  # flag elsewhere
             '{"t":%s,"kind":"task","task":"e","status":"warn","alert":"no"}' % (t + 4),                           # not a bool: no flag
             '{"t":%s,"kind":"task","task":"f","status":"warn","alerting":false}' % (t + 5),                       # a different key
             '{"t":%s,"kind":"task","task":["g"],"status":"warn"}' % (t + 6),                                      # not a record we can use
             '{"alert":false,"t":%s,"kind":"task","task":"h","status":"crit"}' % (t + 7)]                          # key order differs
    (env.state / "history.jsonl").write_text("\n".join(lines) + "\n")
    parsed = []
    real = json.loads
    monkeypatch.setattr(P.json, "loads", lambda s_, *a, **k: parsed.append(s_) or real(s_, *a, **k))
    h = P._History(NOW, set())
    monkeypatch.setattr(P.json, "loads", real)
    assert h.recs == [(t, "a", "warn", None), (t + 1, "b", "crit", False), (t + 2, "c", "warn", True), (t + 3, "d", "warn", False),
                      (t + 4, "e", "warn", None), (t + 5, "f", "warn", None), (t + 7, "h", "crit", False)]
    assert len(parsed) == 4, parsed                                              # d, e, g, h: only what the regex cannot read ("alerting" is no flag)
    assert sum(h.runs.values()) == 8 and h.runs["a"] == 1                          # the SPEC2 counters see every task record


def test_incidents_export_reads_no_audit_trail_unless_an_incident_is_open(monkeypatch):
    from homelab_maint import incidents
    calls = []

    def fake(now, audit_rows=None):
        calls.append(audit_rows)
        return {"generated_at": now, "open": [{"id": "INC-1"}] if audit_rows is None and OPEN[0] else [], "recent": [], "stats": {}}
    OPEN = [False]
    monkeypatch.setattr(incidents, "export_incidents", fake)
    assert REAL_SEAMS["_incidents_export"](NOW)["open"] == [] and calls == [[]]       # nothing open: an empty trail, one cheap call
    calls.clear()
    OPEN[0] = True
    monkeypatch.setattr(incidents, "export_incidents",
                        lambda now, audit_rows=None: calls.append(audit_rows) or {"generated_at": now, "recent": [], "stats": {},
                                                                                 "open": [{"id": "INC-1"}] if audit_rows == [] or audit_rows is None else []})
    assert REAL_SEAMS["_incidents_export"](NOW)["open"] == [{"id": "INC-1"}] and calls == [[], None]     # open: the module reads the trail


def test_a_real_open_incident_still_gets_its_related_actions(real_env):
    from homelab_maint import incidents
    t0 = NOW - 4 * 900
    status = {"tasks": {}}
    for i in range(4):                                                                # crit on 4 consecutive runs: confirmed and open
        t = t0 + 900 * i
        real_env.history([{"t": t, "kind": "task", "task": "disk_forecast", "status": "crit"}])
        status["tasks"]["disk_forecast"] = entry(status="crit", summary="crit: / 2% free", title="Disk space", last_run=t)
        status["generated_at"] = t + 1
        incidents.update(status, None, t + 5)
        if i == 2:
            real_env.audit([{"ts": iso(t + 20, 0), "task": "disk_forecast", "action": "note", "target": "/", "bytes": 0, "outcome": "done"}])
    want = incidents.export_incidents(NOW)
    assert want["open"] and want["open"][0]["related_actions"], want["open"]            # the scenario really has an action to relate
    assert "incidents.json" in real_env.run(status)
    got = real_env.load("incidents.json")
    assert got["open"][0]["id"] == want["open"][0]["id"] and got["open"][0]["related_actions"] == json.loads(json.dumps(want["open"][0]["related_actions"]))
    assert got == json.loads(json.dumps(want))


def test_every_file_the_web_backend_serves_is_published_here_or_by_the_live_daemon():
    """Contract check against web/app.py (read as text, not imported): a new /api name nobody publishes would show 'unavailable' forever."""
    import ast
    src = ROOT / "web" / "app.py"
    if not src.exists():
        pytest.skip("web/app.py not present")
    m = re.search(r"^API_NAMES\s*=\s*(\(.*?\))", src.read_text(), re.S | re.M)
    if not m:
        pytest.skip("web/app.py has no API_NAMES tuple")
    served = set(ast.literal_eval(m.group(1)))
    ours = {f[: -len(".json")] for f in ALL_FILES + REGISTRY_FILES + ACK_FILES[:1]}              # acks.json (once the feature is installed), rules, rules-history, manifest
    assert served == ours | {"live"}, (sorted(served), ALL_FILES)                                # live.json: the daemon, not publish
    assert 'REPORTS = "reports"' in src.read_text()                                                     # /api/reports[/<id>] -> reports/...


# --------------------------------------------------------------------------- the action counters
def test_action_counters_count_done_records_per_window(env):
    env.audit([rec(NOW - 60, outcome="done"), rec(NOW - 3600, task="trash", outcome="done"), rec(NOW - 30, outcome="dry-run"),
               rec(NOW - 40, outcome="refused-protected"), rec(NOW - 50, task="notify", action="send", target="x", outcome="sent"),
               rec(NOW - 2 * 86400, outcome="done"), rec(NOW - 10 * 86400, outcome="done"), rec(NOW - 29 * 86400, outcome="done"),
               rec(NOW - 31 * 86400, outcome="done"), rec(NOW - 80, task="gate", action="defer", outcome="done")])
    env.run()
    t = env.load("actions.json")["totals"]
    assert (t["actions_24h"], t["actions_7d"], t["actions_30d"]) == (2, 3, 5)         # notify / gate / dry-run / refused never count
    check(env.load("actions.json"), SCHEMAS["actions.json"])


def test_action_counters_are_zero_without_an_audit_log_and_keep_the_old_keys(env):
    env.run()
    t = env.load("actions.json")["totals"]
    assert t == {"freed_24h": 0, "freed_7d": 0, "freed_30d": 0, "actions_24h": 0, "actions_7d": 0, "actions_30d": 0}


# =========================================================================== CLI
def test_cli_entry_point(tmp_path):
    state, log = tmp_path / "state", tmp_path / "log"
    state.mkdir()
    (state / "status.json").write_text(json.dumps(make_status(generated_at=time.time())))
    e = {**os.environ, "HOMELAB_MAINT_STATE": str(state), "HOMELAB_MAINT_LOG": str(log), "HOMELAB_MAINT_RUN": str(tmp_path / "run"),
         "HOMELAB_MAINT_CONF": str(tmp_path / "conf")}
    r = subprocess.run([sys.executable, "-m", "homelab_maint.publish"], cwd=ROOT, env=e, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "overview.json" in r.stdout and "checks.json" in r.stdout
    assert json.loads((state / "public" / "overview.json").read_text())["host"] == "ohmz-homelab"
    (state / "status.json").unlink()
    (state / "public" / "overview.json").unlink()
    r = subprocess.run([sys.executable, "-m", "homelab_maint.publish"], cwd=ROOT, env=e, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and not (state / "public" / "overview.json").exists()


# =========================================================================== acknowledged issues (SPEC5)
# status.json entries carry `fp` and, when the owner acknowledged that exact error, `acked` (acks.apply_to_status). They are shown MUTED
# (status word "info", never red), the headline says how many, every failing row carries the issue id for the Acknowledge buttons, and
# acks.json / ack/tokens.json appear once the feature is installed. Without it the published set is exactly what it was before.
ACK_FILES = ["acks.json", "ack/tokens.json"]
FP_FAILED = "0123456789abcdef"


def acked_status(**over):
    """make_status() with failed_units acknowledged (warn) and every other failing entry carrying only its issue id."""
    st = make_status(**over)
    t = st["tasks"]
    t["failed_units"]["fp"] = FP_FAILED
    t["failed_units"]["acked"] = {"fp": FP_FAILED, "until": NOW + 90 * 86400, "by": "email", "note": "known flap", "severity": "warn", "since": NOW - 3600}
    t["disk_forecast"]["fp"] = "fedcba9876543210"
    st["overall"] = "crit"
    return st


def test_without_the_feature_the_published_set_is_unchanged(env):
    assert env.run() == ALL_FILES and not (env.state / "ack").exists() and not (env.out / "acks.json").exists()


def test_an_acknowledged_check_is_muted_not_red_and_says_who_and_until_when(env):
    env.run(acked_status())
    rows = {c["name"]: c for c in env.load("checks.json")["checks"]}
    r = rows["failed_units"]
    assert r["status"] == "info" and r["acked"] == {"until": NOW + 90 * 86400, "by": "email", "note": "known flap", "severity": "warn",
                                                    "since": NOW - 3600, "true_status": "warn"}
    assert r["fp"] == FP_FAILED and rows["disk_forecast"]["status"] == "warn" and "acked" not in rows["disk_forecast"]
    assert rows["disk_forecast"]["fp"] == "fedcba9876543210"                     # a failing row offers its issue id to the Acknowledge button
    assert "fp" not in rows["image_ledger"] and "acked" not in rows["image_ledger"]
    ov = env.load("overview.json")
    assert ov["counts"]["warn"] == 1 and ov["counts"]["info"] == 4 and ov["acknowledged"] == 1
    assert "1 acknowledged" in ov["headline"] and ov["headline"].startswith("1 critical")


def test_acknowledged_only_means_all_healthy_with_a_note_and_the_overall_follows_the_runner(env):
    st = make_status(overall="ok")
    st["tasks"] = {"failed_units": acked_status()["tasks"]["failed_units"], "image_ledger": entry(summary="ok: fine")}
    env.run(st)
    ov = env.load("overview.json")
    assert ov["overall"] == "ok" and ov["headline"] == "All 2 checks healthy (1 acknowledged)" and ov["counts"]["warn"] == 0


def test_the_overall_is_computed_without_acked_tasks_when_status_has_none(env):
    st = make_status()
    del st["overall"]
    st["tasks"] = {"failed_units": acked_status()["tasks"]["failed_units"], "image_ledger": entry(summary="ok: fine")}
    env.run(st)
    assert env.load("overview.json")["overall"] == "ok"


def test_the_acknowledgement_ends_exactly_at_until_in_the_published_state(env):
    st = acked_status()
    st["tasks"]["failed_units"]["acked"]["until"] = NOW + 1
    env.run(st)
    assert {c["name"]: c for c in env.load("checks.json")["checks"]}["failed_units"]["status"] == "info"
    env.run(st, now=NOW + 2)
    row = {c["name"]: c for c in env.load("checks.json")["checks"]}["failed_units"]
    assert row["status"] == "warn" and "acked" not in row                          # past `until` it is red again, whatever the file still says


@pytest.mark.parametrize("acked", [5, "x", [], {}, {"until": "never"}, {"until": None}, {"until": float("nan")}, {"until": NOW - 5}, {"until": float("inf")}])
def test_a_malformed_or_ended_acked_value_is_never_trusted(env, acked):
    st = make_status()
    st["tasks"]["failed_units"]["acked"] = acked
    env.run(st)
    row = {c["name"]: c for c in env.load("checks.json")["checks"]}["failed_units"]
    assert row["status"] == "warn" and "acked" not in row


def test_only_a_failing_entry_can_be_acknowledged(env):
    st = make_status()
    st["tasks"]["image_ledger"]["acked"] = {"until": NOW + 99999, "by": "cli", "note": "", "severity": "warn"}
    st["tasks"]["image_ledger"]["fp"] = FP_FAILED
    env.run(st)
    row = {c["name"]: c for c in env.load("checks.json")["checks"]}["image_ledger"]
    assert row["status"] == "ok" and "acked" not in row and "fp" not in row


def test_an_acknowledged_error_status_is_muted_too(env):
    st = make_status()
    st["tasks"]["memory_health"].update(fp=FP_FAILED, acked={"until": NOW + 99999, "by": "web", "note": "", "severity": "crit", "since": NOW})
    env.run(st)
    row = {c["name"]: c for c in env.load("checks.json")["checks"]}["memory_health"]
    assert row["status"] == "info" and row["acked"]["true_status"] == "error" and row["acked"]["severity"] == "crit"


def test_the_acked_note_is_redacted_and_bounded(env):
    st = acked_status()
    st["tasks"]["failed_units"]["acked"]["note"] = "mail ops@example.com password=hunter2 " + "x" * 400
    env.run(st)
    note = {c["name"]: c for c in env.load("checks.json")["checks"]}["failed_units"]["acked"]["note"]
    assert "example.com" not in note and "hunter2" not in note and len(note) <= 200


def test_health_history_keeps_the_true_statuses(env):
    """An acknowledgement is not an excuse for better availability: the health calendar sees warn/crit as before."""
    recs = [{"t": NOW - 900 * i, "kind": "task", "task": "failed_units", "status": "crit"} for i in range(1, 40)]
    env.history(recs)
    env.run(make_status())
    before = (env.load("health-history.json"), env.load("slo.json"))
    env.history([{**r, "acked": True} for r in recs])                                # the same runs, now flagged acknowledged
    env.run(acked_status())
    after = (env.load("health-history.json"), env.load("slo.json"))
    assert after == before and any(d["worst"] == "crit" for d in after[0]["days"])      # (slo.json itself is exercised for real in test_incidents)


def test_acks_json_and_the_token_view_appear_once_the_feature_is_installed(env):
    from homelab_maint import acks
    (env.state / "status.json").write_text(json.dumps(acked_status()))
    acks.add("failed_units", 30, "known flap", now=NOW)
    tok = acks.issue_token(FP_FAILED, "failed_units", "Services", "2 failed units", "warn", NOW)
    names = env.run(acked_status())
    assert names == ALL_FILES + ACK_FILES
    doc = env.load("acks.json")
    assert [a["task"] for a in doc["acks"]] == ["failed_units"] and doc["stats"]["active"] == 1
    assert oct((env.out / "acks.json").stat().st_mode & 0o777) == "0o644"
    tj = json.loads((env.state / "ack" / "tokens.json").read_text())
    assert list(tj) == [acks.token_hash(tok)] and oct((env.state / "ack" / "tokens.json").stat().st_mode & 0o777) == "0o644"
    assert tok.encode() not in env.raw() and acks.token_hash(tok).encode() not in env.raw()      # the PUBLIC dir holds neither plaintext nor hash
    assert not [p for p in os.listdir(env.out) if p.startswith(".")]


def test_the_ack_directories_are_never_removed_or_recreated_by_a_publish(env):
    (env.state / "ack").mkdir()
    env.run(acked_status())
    ino = ((env.state / "ack").stat().st_ino, env.out.stat().st_ino)
    (env.state / "ack" / "inbox").mkdir()
    (env.state / "ack" / "inbox" / "1790000000000-aaaaaaaa.json").write_text("{}")
    for _ in range(3):
        env.run(acked_status())
    assert ((env.state / "ack").stat().st_ino, env.out.stat().st_ino) == ino
    assert (env.state / "ack" / "inbox" / "1790000000000-aaaaaaaa.json").exists()               # nothing under ack/ is ever touched but tokens.json


def test_a_failing_acks_export_keeps_the_old_file_and_the_rest_is_published(env, monkeypatch, capsys):
    from homelab_maint import acks
    (env.state / "ack").mkdir()
    env.run(acked_status())
    old = (env.out / "acks.json").read_bytes()
    monkeypatch.setattr(acks, "public_doc", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    names = env.run(acked_status())
    assert names == ALL_FILES and (env.out / "acks.json").read_bytes() == old
    assert "acks: RuntimeError" in capsys.readouterr().err


def test_acks_json_is_scrubbed_like_every_public_file(env, monkeypatch):
    from homelab_maint import acks
    (env.state / "status.json").write_text(json.dumps(make_status()))
    acks.add("failed_units", 30, "mail ops@example.com", now=NOW)
    doc = acks.public_doc(NOW)
    doc["acks"][0]["summary"] = "failed at https://x.example/p?token=SECRET99 password=hunter2 key=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    monkeypatch.setattr(acks, "public_doc", lambda now: doc)
    env.run(make_status())
    text = (env.out / "acks.json").read_text()
    for leak in ("SECRET99", "hunter2", "AbCdEfGh", "example.com"):
        assert leak not in text


def test_acknowledgement_audit_rows_are_not_maintenance_actions(env):
    """acks.py audits every ack/unack/expire/token under the task name "acks": that is bookkeeping, not something maintenance did."""
    env.audit([rec(NOW - 60, task="acks", action="ack", target="0123456789abcdef", nbytes=0, outcome="done"),
               rec(NOW - 50, task="acks", action="suppressed", target="0123456789abcdef", nbytes=0, outcome="held back until 2026-12-20"),
               rec(NOW - 40, task="acks", action="refresh-failed", target="", nbytes=0, outcome="failed: boom"),
               rec(NOW - 30, task="trash", outcome="done")])
    env.run()
    t = env.load("actions.json")
    assert t["totals"]["actions_24h"] == 1
    assert {a["task"] for a in t["recent"]} == {"trash"}
    assert "acks" not in json.dumps(t["by_task"])


# ---- review round 2 (acknowledged issues): the overall must not follow scheduler.merge_status back to yellow, and no button for a task that may not be acknowledged
def test_the_overview_overall_ignores_an_acknowledged_task_even_when_status_json_says_warn(env):
    """scheduler.merge_status recomputes status["overall"] from the RAW statuses every tick (no `acked` check there): the website's hero must be
    derived from the muted words, or it flaps back to yellow a minute after the acknowledgement made it green."""
    st = make_status(overall="warn")                                                   # what the next tick wrote
    st["tasks"] = {"failed_units": acked_status()["tasks"]["failed_units"], "image_ledger": entry(summary="ok: fine")}
    env.run(st)
    ov = env.load("overview.json")
    assert ov["overall"] == "ok" and ov["counts"]["warn"] == 0 and ov["acknowledged"] == 1 and ov["headline"] == "All 2 checks healthy (1 acknowledged)"
    st["overall"] = "crit"
    env.run(st)
    assert env.load("overview.json")["overall"] == "ok"                                  # even a stale "crit" is not trusted while something is acknowledged
    st["tasks"]["disk_forecast"] = entry(status="warn", summary="warn: / 100.0 GiB free", title="Disk space")
    env.run(st)
    assert env.load("overview.json")["overall"] == "warn"                                # an UNacknowledged problem still counts
    st["tasks"]["disk_forecast"]["status"] = "crit"
    env.run(st)
    assert env.load("overview.json")["overall"] == "crit"
    st["tasks"]["failed_units"]["acked"]["until"] = NOW - 1                              # the acknowledgement ended: warn again (the file's word counts again)
    del st["tasks"]["disk_forecast"]
    st["overall"] = "warn"
    env.run(st)
    assert env.load("overview.json")["overall"] == "warn"


def test_without_an_acknowledgement_the_overall_still_follows_the_file(env):
    st = make_status(overall="warn")
    env.run(st)
    assert env.load("overview.json")["overall"] == "warn"                                # (make_status has a crit task: the file is authoritative when nothing is acked)


def test_a_status_fp_is_exported_only_for_a_task_that_may_be_acknowledged(env):
    """A stale `fp` that an older release left on an entry of a task without an exact-error rule (or a denied one) is not offered as a button."""
    st = make_status()
    st["tasks"]["docker_cache"] = entry("C1", "daily", status="warn", summary="warn: cache over cap", title="Docker cache", fp=FP_FAILED)
    st["tasks"]["smart_event"] = entry(status="warn", summary="warn: sda failing", title="SMART event", fp=FP_FAILED)
    st["tasks"]["job:nightly"] = entry(status="crit", summary="crit: rc=1", title="Nightly", fp=FP_FAILED)
    st["tasks"]["failed_units"]["fp"] = FP_FAILED
    st["tasks"]["memory_health"].update(status="warn", summary="warn: memory stall 6.1%", fp="fedcba9876543210")   # a ruled task
    env.run(st)
    rows = {c["name"]: c for c in env.load("checks.json")["checks"]}
    assert rows["failed_units"]["fp"] == FP_FAILED and rows["memory_health"]["fp"] == "fedcba9876543210"
    for name in ("docker_cache", "smart_event", "job:nightly"):
        assert "fp" not in rows[name], name
    st["tasks"]["memory_health"].update(status="error", summary="error: timed out")      # a check that failed to run is crit: never acknowledgeable either
    env.run(st)
    assert "fp" not in {c["name"]: c for c in env.load("checks.json")["checks"]}["memory_health"]


# --------------------------------------------------------------------------- SPEC6: self.json, rules.json, rules-history.json, manifest.json
def test_self_json_is_the_self_health_export_scrubbed_like_every_other_passed_through_file(env, monkeypatch):
    env.run()
    check(env.load("self.json"), SCHEMAS["self.json"])
    assert env.load("self.json")["headline"] == "Monitoring pipeline: healthy" and env.load("self.json")["schema"] == 2
    leaky = fake_self(NOW)
    leaky["checks"][0]["detail"] = LEAK
    leaky["verdict"]["reasons"] = ["secret=hunter2 broke " + "A" * 80]
    set_source(monkeypatch, "self.json", lambda now: leaky)
    env.run(now=NOW + 60)
    raw = (env.out / "self.json").read_text()
    assert "hunter2" not in raw and "ghp_" not in raw and "ohmz@example.org" not in raw and "416-555-1234" not in raw
    set_source(monkeypatch, "self.json", boom)                              # a crashing export keeps the old file and is announced, nothing else changes
    assert "self.json" not in env.run(now=NOW + 120) and env.load("overview.json")["export_errors"] == ["self.json"]
    assert (env.out / "self.json").read_text() == raw


def test_the_real_self_health_export_is_published_in_its_own_shape(real_env):
    assert "self.json" in real_env.run()
    d = real_env.load("self.json")
    check(d, SCHEMAS["self.json"])
    assert {"runner", "publish", "registry", "website"} <= {c["id"] for c in d["checks"]} and d["ttl"]["degraded_after_s"] < d["ttl"]["down_after_s"]


@pytest.fixture
def reg(env, monkeypatch, tmp_path):
    """The real registry over tmp dirs: the shipped etc/*.toml migrated into rules.d and applied once (no e-mail)."""
    from homelab_maint import registry as R
    conf = tmp_path / "conf"
    conf.mkdir()
    conf.chmod(0o755)
    for f in (ROOT / "etc").glob("*.toml"):
        shutil.copy(f, conf / f.name)
        (conf / f.name).chmod(0o644)
    monkeypatch.setattr(core, "CONF_DIR", conf)
    monkeypatch.setattr(R, "default_hooks", lambda: R.NO_HOOKS)
    monkeypatch.setattr(P, "_publish_registry", REAL_PUBLISH_REGISTRY)
    env.R, env.conf = R, conf
    return env


def test_without_a_registry_the_rules_files_and_the_manifest_are_still_published_and_empty(reg):
    written = reg.run()
    assert written == ALL_FILES + REGISTRY_FILES and written[-1] == "manifest.json"          # the manifest describes what is already there
    rj, hist, mf = (reg.load(n) for n in REGISTRY_FILES)
    assert rj["rules"] == [] and rj["registry_hash"] == "" and hist["history"] == [] and mf["rules_count"] == 0 and mf["registry_hash"] == ""
    for n in REGISTRY_FILES:
        assert oct((reg.out / n).stat().st_mode & 0o777) == "0o644"
    assert not [p for p in os.listdir(reg.out) if p.startswith(".")]


def test_the_manifest_lists_every_public_file_with_a_schema_and_the_runner_version(reg):
    from homelab_maint import __version__
    reg.run()
    mf = reg.load("manifest.json")
    assert mf["schema"] == 1 and mf["runner_version"] == __version__ and mf["generated_at"] == NOW
    names = {n[: -len(".json")] for n in ALL_FILES + REGISTRY_FILES if n != "manifest.json"}
    assert set(mf["files"]) == names                                          # rules-history is in it too: it was written before the manifest
    assert all(isinstance(v["schema"], int) and isinstance(v["generated_at"], (int, float)) for v in mf["files"].values())    # never a null version
    assert mf["files"]["self"]["schema"] == 2 and mf["files"]["monitors"]["schema"] == 1


def test_a_synced_registry_is_published_and_a_change_reaches_the_next_publish(reg):
    R = reg.R
    proof, _ = R.migrate(ROOT / "etc", reg.conf / "rules.d", today="2026-10-02")
    assert proof.ok
    (reg.conf / "rules.d").chmod(0o755)
    r = R.sync(reg.conf, reg.state, hooks=R.NO_HOOKS)
    assert r.status in ("applied", "adopted") or r.fresh, r.status
    t0 = time.time()                                                         # (registry.write_public compares the file's real mtime with `now`)
    reg.run(now=t0)
    inode = reg.out.stat().st_ino
    rj, hist, mf = (reg.load(n) for n in REGISTRY_FILES)
    cur = json.loads((reg.state / "rules" / "current.json").read_text())
    assert rj["registry_hash"] == cur["hash"] == mf["registry_hash"] and mf["rules_count"] == cur["rules_count"] > 100 and rj["valid"] is True
    assert len(rj["rules"]) == cur["rules_count"] and hist["history"] == rj["history"] and hist["registry_hash"] == cur["hash"]
    assert 0 < (reg.out / "rules.json").stat().st_size <= R.RULES_JSON_MAX and mf["registry_valid"] is True
    ino = {n: (reg.out / n).stat().st_ino for n in REGISTRY_FILES}
    mt = (reg.out / "rules.json").stat().st_mtime_ns
    reg.run(now=t0 + 60)                                                    # the same registry within 5 minutes: rules.json is not rebuilt
    assert (reg.out / "rules.json").stat().st_mtime_ns == mt and reg.load("manifest.json")["generated_at"] == pytest.approx(t0 + 60, abs=0.01)
    rule = reg.conf / "rules.d" / "10-checks.toml"                           # an edit that is synced: the next publish rebuilds both files at once
    rule.write_text(rule.read_text().replace("warn_free_pct = 12", "warn_free_pct = 15", 1))
    assert R.sync(reg.conf, reg.state, hooks=R.NO_HOOKS).fresh
    reg.run(now=t0 + 120)
    rj2, hist2, mf2 = (reg.load(n) for n in REGISTRY_FILES)
    assert rj2["registry_hash"] == mf2["registry_hash"] != cur["hash"] and hist2["registry_hash"] == rj2["registry_hash"]
    assert hist2["history"] and hist2["history"][0]["modified"][0]["id"] == "task.disk_forecast"       # newest first, with what changed
    assert reg.out.stat().st_ino == inode and ino != {n: (reg.out / n).stat().st_ino for n in REGISTRY_FILES}      # the directory is never recreated


def test_the_rules_json_cap_fits_what_rules_history_reads_back():
    """_write_rules_history reads rules.json back with a bounded read: a cap raised past it would silently publish no history."""
    from homelab_maint import registry
    assert registry.RULES_JSON_MAX <= 2 * P.MAX_FILE_BYTES + 400_000


def test_rules_history_is_capped_like_every_public_file_and_keeps_the_newest_changes(tmp_path):
    big = [{"ts": NOW - i, "from": "a" * 12, "to": "b" * 12, "valid": True, "applied": True, "added": [f"rule.{j}" for j in range(20)],
            "note": "x" * 9000} for i in range(50)]                          # ~450 KB: rules.json itself may hold 700 KB, a public file 195 KB
    (tmp_path / "rules.json").write_text(json.dumps({"registry_hash": "f" * 64, "history": big}))
    assert P._write_rules_history(tmp_path, NOW)
    raw = (tmp_path / "rules-history.json").read_bytes()
    d = json.loads(raw)
    assert len(raw) <= P.MAX_FILE_BYTES and 0 < len(d["history"]) < 50 and d["history"][0]["ts"] == NOW and d["registry_hash"] == "f" * 64
    assert [h["ts"] for h in d["history"]] == [NOW - i for i in range(len(d["history"]))]      # the oldest were dropped, the order kept
    for bad in ("{", "[]", '{"history": 3}', '{"history": null}'):
        (tmp_path / "rules.json").write_text(bad)
        assert P._write_rules_history(tmp_path, NOW) is False                # nothing usable: no file is invented
    (tmp_path / "rules.json").unlink()
    assert P._write_rules_history(tmp_path, NOW) is False


def test_a_failing_registry_never_breaks_the_other_files(reg, monkeypatch, capsys):
    monkeypatch.setattr(reg.R, "write_public", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret=hunter2 broke")))
    written = reg.run()
    assert written[: len(ALL_FILES)] == ALL_FILES and "rules.json" not in written                 # the other files were all written
    assert "rules export" in capsys.readouterr().err
    assert "hunter2" not in capsys.readouterr().err
    monkeypatch.setitem(sys.modules, "homelab_maint.registry", None)                                # a registry that cannot be imported: the same
    assert reg.run(now=NOW + 60)[: len(ALL_FILES)] == ALL_FILES
