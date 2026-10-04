"""Tests for tasks/checks_basic.py: mocked command output, fake /proc text, tmp dirs only."""
import json
import os
import subprocess
import time

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core
from homelab_maint.core import GIB
from homelab_maint.tasks import checks_basic as cb

H = 3600


# --------------------------------------------------------------------------- helpers
def mk(tmp_path, monkeypatch, name, now=None, **opts):
    """A Ctx whose state/history/audit live under tmp_path."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    cfg = {"global": {}, "caps": {}, "protected": {}, "tasks": {name: opts}}
    return core.Ctx(cfg, name, False, time.time() if now is None else now)


class FakeSh:
    """Replacement for `sh`: first matching (prefix, stdout[, rc]) wins; unknown commands => rc 127."""

    def __init__(self, *table):
        self.table = list(table)
        self.calls: list[str] = []
        self.envs: list = []          # the `env=` passed with each call (None when not given)

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        self.envs.append(kw.get("env"))
        for row in self.table:
            prefix, out = row[0], row[1]
            rc = row[2] if len(row) > 2 else 0
            if key.startswith(prefix):
                out = out(key) if callable(out) else out
                return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 127, "", "not found")


def ascii_ok(res):
    assert len(res.summary) <= 140 and res.summary.isascii(), res.summary
    assert len(res.items) <= 12
    json.dumps(res.metrics)  # must be serialisable


def seed_history(tmp_path, recs):
    p = tmp_path / "state"
    p.mkdir(parents=True, exist_ok=True)
    with open(p / "history.jsonl", "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def ts(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def test_all_six_tasks_registered():
    for n in ("disk_forecast", "failed_units", "backup_freshness", "docker_df", "memory_health",
              "plex_media_mount_check"):
        t = core.REGISTRY[n]
        assert (t.klass, t.tier) == ("C0", "check")


# --------------------------------------------------------------------------- disk_forecast
def fake_usage(table):
    def f(path):
        v = table.get(path)
        return None if v is None else {"free": int(v[0]), "used": int(v[1])}
    return f


OPTS = dict(watch=["/", "/data", "/gone"], info_only=["/cold"], warn_free_pct=12, crit_free_pct=6,
            warn_days=21, crit_days=7, trend_window_hours=168)


def test_disk_levels_skip_unmounted_and_info_never_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({
        "/": (50 * GIB, 950 * GIB),          # 5% free  -> crit
        "/data": (500 * GIB, 500 * GIB),     # 50% free -> ok
        "/cold": (1 * GIB, 999 * GIB),       # 0.1% free but info_only
    }))
    ctx = mk(tmp_path, monkeypatch, "disk_forecast", **OPTS)
    res = cb.disk_forecast(ctx)
    assert res.status == "crit" and res.summary.startswith("crit: / 5% free")
    ascii_ok(res)
    by = {m["mount"]: m for m in res.metrics["mounts"]}
    assert "/gone" not in by                              # not mounted => skipped, not guessed
    assert by["/cold"]["level"] == "info" and by["/cold"]["info"] is True
    assert by["/"]["free_h"] == "50.0 GiB" and by["/"]["used_pct"] == 95.0
    assert res.metrics["root_free_h"] == "50.0 GiB" and res.metrics["root_days"] is None
    # watch mounts come first, worst first; info-only last
    assert [m["mount"] for m in res.metrics["mounts"]] == ["/", "/data", "/cold"]
    # history recorded for watch mounts only
    recs = core.read_history(60, "disk")
    assert sorted(r["mount"] for r in recs) == ["/", "/data"]
    assert all(set(r) == {"t", "kind", "mount", "free"} for r in recs)


def test_disk_warn_boundary_and_info_only_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({"/": (110 * GIB, 890 * GIB), "/data": (900 * GIB, 100 * GIB)}))
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", **OPTS))
    assert res.status == "warn"                           # 11% free < 12
    only_info = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", watch=[], info_only=["/"]) )
    assert only_info.status == "ok"


def test_disk_nothing_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "_disk_usage", lambda p: None)
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", **OPTS))
    assert res.status == "warn" and res.metrics["mounts"] == []


def _decline(tmp_path, now, n, step_gib, span_h, mount="/data", start_gib=2000):
    """n samples ending just before `now`, free dropping step_gib per sample."""
    seed_history(tmp_path, [{"t": now - (n - i) * span_h * H / n, "kind": "disk", "mount": mount,
                             "free": (start_gib - i * step_gib) * GIB} for i in range(n)])


def test_forecast_crit_when_filling_fast(tmp_path, monkeypatch):
    now = time.time()
    _decline(tmp_path, now, 12, 10, 24, start_gib=720)      # 10 GiB / 2 h = 120 GiB/day, ends ~600 GiB
    free_now = 600
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({"/data": (free_now * GIB, 1400 * GIB)}))
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", now=now, **{**OPTS, "watch": ["/data"]}))
    row = res.metrics["mounts"][0]
    assert 4.0 < row["days"] < 6.0, row                    # ~5 days
    assert row["level"] == "crit" and res.status == "crit" and "full in 5d" in res.summary


def test_forecast_warn_when_slowly_filling_with_plenty_free(tmp_path, monkeypatch):
    now = time.time()
    _decline(tmp_path, now, 12, 2, 24, start_gib=424)       # 24 GiB/day, ~400 GiB free of 2000 => 20% free
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({"/data": (400 * GIB, 1600 * GIB)}))
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", now=now, **{**OPTS, "watch": ["/data"]}))
    row = res.metrics["mounts"][0]
    assert 14 < row["days"] < 20 and row["level"] == "warn"
    assert row["days_h"].endswith(" d")


def test_forecast_none_when_free_grows_or_too_little_data(tmp_path, monkeypatch):
    now = time.time()
    opts = {**OPTS, "watch": ["/data"]}
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({"/data": (400 * GIB, 1600 * GIB)}))
    # growing free space
    _decline(tmp_path, now, 12, -5, 24, start_gib=300)
    assert cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", now=now, **opts)).metrics["mounts"][0]["days"] is None
    # fewer than 6 samples (new tmp state)
    t2 = tmp_path / "b"
    t2.mkdir()
    _decline(t2, now, 4, 10, 24)
    assert cb.disk_forecast(mk(t2, monkeypatch, "disk_forecast", now=now, **opts)).metrics["mounts"][0]["days"] is None
    # 8 samples but spanning only 2 h
    t3 = tmp_path / "c"
    t3.mkdir()
    _decline(t3, now, 8, 10, 2)
    assert cb.disk_forecast(mk(t3, monkeypatch, "disk_forecast", now=now, **opts)).metrics["mounts"][0]["days"] is None


CAP = 1790 * GIB                                              # the SSD in the reviewer's scenario


def series(now, fn, span_h=168, step_h=0.25):
    """15-min samples of free space; fn(h) -> GiB where h = hours relative to `now` (<= 0)."""
    pts, t = [], now - span_h * H
    while t <= now:
        pts.append((t, fn((t - now) / H) * GIB))
        t += step_h * H
    return pts


def copy_then_flat(ended_ago_h, dur_h=6, start_gib=1136, end_gib=473):
    """Flat, then a bulk copy lasting dur_h that finished ended_ago_h ago, then flat again."""
    def fn(h):
        s, e = -(ended_ago_h + dur_h), -ended_ago_h
        return start_gib if h < s else end_gib if h > e else start_gib - (start_gib - end_gib) * (h - s) / (e - s)
    return fn


@pytest.mark.parametrize("ended_ago_h", [1, 24, 72, 144])
def test_forecast_ignores_a_finished_bulk_copy(ended_ago_h):
    """Regression: 1136 -> 473 GiB in 6 h, then flat. Least squares called this 'full in 3-35 days'."""
    now = 10_000_000.0
    pts = series(now, copy_then_flat(ended_ago_h))
    assert cb._days_until_full(pts, 473 * GIB, now, 168 * H, capacity=CAP) is None


def test_forecast_copy_inside_a_short_history_is_not_a_trend():
    """Just after the timers are installed the whole history is only ~14 h and contains the copy."""
    now = 10_000_000.0
    pts = series(now, copy_then_flat(4), span_h=14)
    assert cb._days_until_full(pts, 473 * GIB, now, 168 * H, capacity=CAP) is None


def test_forecast_disk_forecast_end_to_end_copy_then_flat_is_ok(tmp_path, monkeypatch):
    now = time.time()
    pts = series(now, copy_then_flat(24))
    seed_history(tmp_path, [{"t": t, "kind": "disk", "mount": "/ssd", "free": int(f)} for t, f in pts[:-1]])
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({"/ssd": (473 * GIB, 1317 * GIB)}))
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", now=now, **{**OPTS, "watch": ["/ssd"]}))
    row = res.metrics["mounts"][0]
    assert row["days"] is None and row["level"] == "ok" and res.status == "ok", res.summary


def test_forecast_stopped_consumption_is_not_forecast():
    """Falling for 5 days, then flat for the last 3: the disk is no longer filling."""
    now = 10_000_000.0
    pts = series(now, lambda h: 600 if h > -72 else 600 + (-h - 72))          # 1 GiB/h until 3 d ago
    assert cb._days_until_full(pts, 600 * GIB, now, 168 * H, capacity=CAP) is None


def test_forecast_steady_fill_still_works_and_a_step_inside_it_is_ignored():
    now = 10_000_000.0
    steady = series(now, lambda h: 473 - h)                                    # 1 GiB/h = 24 GiB/day
    assert cb._days_until_full(steady, 473 * GIB, now, 168 * H, capacity=CAP) == pytest.approx(473 / 24, rel=0.01)
    stepped = series(now, lambda h: 900 - h - (500 if h > -84 else 0))        # same trend + a 500 GiB copy mid-window
    assert cb._days_until_full(stepped, 400 * GIB, now, 168 * H, capacity=CAP) == pytest.approx(400 / 24, rel=0.05)


def test_days_until_full_ignores_samples_outside_window():
    now = 1_000_000.0
    old = [(now - 400 * H - i * H, 9000 * GIB) for i in range(10)]            # outside a 168 h window
    assert cb._days_until_full(old, 100 * GIB, now, 168 * H) is None


def test_disk_usage_uses_bavail_like_df(monkeypatch):
    class SV:                                              # 1000 blocks of 4096; 100 free, only 40 available to users
        f_frsize = 4096; f_bsize = 4096; f_blocks = 1000; f_bfree = 100; f_bavail = 40
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(os, "statvfs", lambda p: SV)
    u = cb._disk_usage("/x")
    assert u == {"free": 40 * 4096, "used": 900 * 4096}
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    assert cb._disk_usage("/x") is None


def test_disk_real_root_smoke(tmp_path, monkeypatch):
    res = cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", watch=["/"], info_only=[]))
    assert res.status in ("ok", "warn", "crit") and res.metrics["mounts"][0]["mount"] == "/"


# --------------------------------------------------------------------------- failed_units
PS_FMT = "docker ps -a"
SYSTEMCTL_FAILED = ("nginx.service              loaded failed failed A high performance web server\n"
                    "plexmediaserver.service loaded failed failed Plex Media Server\n")


def show_block(now, ages_s):
    return "\n\n".join(f"Id={u}\nStateChangeTimestamp=@{int(now - a)}" for u, a in ages_s.items()) + "\n"


def units_sh(now, failed=SYSTEMCTL_FAILED, ages=None, ps="", counts=None, ps_rc=0):
    ages = ages if ages is not None else {"nginx.service": 7 * 86400, "plexmediaserver.service": 7 * 86400}
    return FakeSh(
        ("systemctl --failed", failed),
        ("systemctl show", show_block(now, ages)),
        (PS_FMT, ps, ps_rc),
        ("docker inspect", lambda k: "".join(f"/{n}|{c}\n" for n, c in (counts or {}).items())),
    )


def test_failed_units_old_units_warn_with_notes(tmp_path, monkeypatch):
    ctx = mk(tmp_path, monkeypatch, "failed_units")
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, ps="web|running|Up 3 hours (healthy)\n"))
    res = cb.failed_units(ctx)
    assert res.status == "warn" and "nginx.service" in res.summary
    ascii_ok(res)
    notes = {i["name"]: i["note"] for i in res.items if i["kind"] == "unit"}
    assert "32400" in notes["plexmediaserver.service"] and notes["nginx.service"]
    assert res.metrics["failed_units_stale"] == 2 and res.metrics["known_failed"] == 2


def test_failed_units_recent_failure_is_not_yet_a_warning(tmp_path, monkeypatch):
    ctx = mk(tmp_path, monkeypatch, "failed_units")
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, ages={"nginx.service": 5 * 60, "plexmediaserver.service": 29 * 60}))
    res = cb.failed_units(ctx)
    assert res.status == "ok" and res.metrics["failed_units_recent"] == 2 and res.metrics["failed_units_stale"] == 0


def test_failed_units_unknown_age_counts_as_old_and_ignore_list(tmp_path, monkeypatch):
    ctx = mk(tmp_path, monkeypatch, "failed_units", ignore_units=["nginx.service"])
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, ages={}))        # systemctl show returned nothing
    res = cb.failed_units(ctx)
    assert res.status == "warn" and "plexmediaserver.service" in res.summary and "nginx.service" not in res.summary
    assert [i["age"] for i in res.items] == ["?"]


def test_failed_units_containers(tmp_path, monkeypatch):
    ctx = mk(tmp_path, monkeypatch, "failed_units", expected_stopped_containers=["comfyui"])
    ps = ("comfyui|exited|Exited (137) 2 hours ago\n"
          "oneshot|exited|Exited (1) 5 minutes ago\n"
          "web|running|Up 3 hours (healthy)\n"
          "db|running|Up 2 hours (unhealthy)\n"
          "loopy|restarting|Restarting (1) 3 seconds ago\n")
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, failed="", ages={}, ps=ps, counts={"web": 0, "db": 0}))
    res = cb.failed_units(ctx)
    assert res.status == "warn"
    assert res.metrics["exited_unexpected"] == 1 and res.metrics["unhealthy"] == 1 and res.metrics["restarting"] == 1
    kinds = {(i["kind"], i["name"]) for i in res.items}
    assert ("exited", "oneshot") in kinds and ("exited", "comfyui") not in kinds
    assert ("unhealthy", "db") in kinds and ("restarting", "loopy") in kinds
    ascii_ok(res)


def test_failed_units_restart_count_growth_uses_state(tmp_path, monkeypatch):
    ps = "web|running|Up 1 hour\napi|running|Up 1 hour\n"
    ctx = mk(tmp_path, monkeypatch, "failed_units")
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, failed="", ages={}, ps=ps, counts={"web": 3, "api": 0}))
    assert cb.failed_units(ctx).status == "ok"                      # first sight is a baseline
    assert ctx.state["restart_counts"] == {"web": 3, "api": 0}
    ctx.save_state()
    ctx2 = mk(tmp_path, monkeypatch, "failed_units")                 # fresh Ctx re-reads saved state
    monkeypatch.setattr(cb, "sh", units_sh(ctx2.now, failed="", ages={}, ps=ps, counts={"web": 5, "api": 0}))
    res = cb.failed_units(ctx2)
    assert res.status == "warn" and "web" in res.summary
    assert any("2 restart(s) in last 6h" in i["note"] for i in res.items)
    ctx2.save_state()
    # the finding is HELD: still a warning 15 min later (so the 2-run alert confirmation can see it) ...
    ctx3 = mk(tmp_path, monkeypatch, "failed_units", now=ctx2.now + 900)
    monkeypatch.setattr(cb, "sh", units_sh(ctx3.now, failed="", ages={}, ps=ps, counts={"web": 5, "api": 0}))
    assert cb.failed_units(ctx3).status == "warn"
    ctx3.save_state()
    # ... and quiet again once the hold window has passed with no further growth
    ctx4 = mk(tmp_path, monkeypatch, "failed_units", now=ctx2.now + 7 * H)
    monkeypatch.setattr(cb, "sh", units_sh(ctx4.now, failed="", ages={}, ps=ps, counts={"web": 5, "api": 0}))
    res4 = cb.failed_units(ctx4)
    assert res4.status == "ok" and ctx4.state["restart_events"] == {}


def test_failed_units_blind_probe_is_not_healthy(tmp_path, monkeypatch):
    ctx = mk(tmp_path, monkeypatch, "failed_units")
    monkeypatch.setattr(cb, "sh", FakeSh())                          # every tool missing (rc 127)
    res = cb.failed_units(ctx)
    assert res.status == "warn" and "probe failed" in res.summary
    assert len(res.metrics["probe_errors"]) == 2


# --------------------------------------------------------------------------- backup_freshness
def write_status(d, job, result="ok", finished=None, name=None):
    p = d / (name or f"{job}-status.json")
    p.write_text(json.dumps({"job": job, "result": result, "exit_code": 0 if result == "ok" else 1,
                             "started": ts(finished - 600), "finished": ts(finished), "target": "/mnt/backup/x"}))
    return p


def bopts(d, **kw):
    return dict(status_glob=str(d / "*-status.json"), max_age_hours={"backup-system": 200, "backup-immich": 200},
                stack_backup_last_ok=str(d / "LAST_OK"), stack_backup_max_hours=26, **kw)


def stamp(path, age_h, now):
    path.write_text(ts(now))
    os.utime(path, (now - age_h * H, now - age_h * H))


def test_backup_all_fresh(tmp_path, monkeypatch):
    now = time.time()
    write_status(tmp_path, "system", finished=now - 100 * H)
    write_status(tmp_path, "immich", finished=now - 3 * H)
    stamp(tmp_path / "LAST_OK", 5, now)
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    assert res.status == "ok" and res.metrics["failed"] == 0
    ascii_ok(res)
    by = {b["name"]: b for b in res.metrics["backups"]}
    assert set(by) == {"backup-system", "backup-immich", "stack-backup"}
    assert abs(by["backup-system"]["age_h"] - 100) < 0.2 and by["backup-immich"]["result"] == "ok"


def test_backup_stale_is_warn_with_limit_in_summary(tmp_path, monkeypatch):
    now = time.time()
    write_status(tmp_path, "system", finished=now - 250 * H)
    stamp(tmp_path / "LAST_OK", 30, now)                              # limit 26
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    assert res.status == "warn"
    assert "backup-system" in res.summary and "limit 200h" in res.summary and "stack-backup" in res.summary


def test_a_fourth_late_backup_is_counted_in_the_summary(tmp_path, monkeypatch):
    now = time.time()
    for job in ("system", "immich", "photos", "vault"):
        write_status(tmp_path, job, finished=now - 250 * H)
    stamp(tmp_path / "LAST_OK", 30, now)
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    assert res.status == "warn" and res.summary.endswith("; +2 more") and len(res.summary) <= 140
    ascii_ok(res)


def test_backup_failed_result_is_crit_even_if_recent(tmp_path, monkeypatch):
    now = time.time()
    write_status(tmp_path, "system", result="failed", finished=now - 2 * H)
    stamp(tmp_path / "LAST_OK", 1, now)
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    assert res.status == "crit" and "backup-system FAILED" in res.summary and res.metrics["failed"] == 1


def test_backup_failed_marker_txt_is_ignored(tmp_path, monkeypatch):
    now = time.time()
    (tmp_path / "FAILED-backup-system.service.txt").write_text("FAILED at long ago")
    write_status(tmp_path, "system", finished=now - 1 * H)
    stamp(tmp_path / "LAST_OK", 1, now)
    assert cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path))).status == "ok"


def test_backup_missing_or_corrupt_inputs_are_warnings(tmp_path, monkeypatch):
    now = time.time()
    (tmp_path / "system-status.json").write_text("{not json")
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    by = {b["name"]: b for b in res.metrics["backups"]}
    assert res.status == "warn" and by["backup-system"]["result"] == "unreadable" and by["stack-backup"]["result"] == "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, status_glob=str(empty / "*.json")))
    assert res.status == "warn" and "no backup status" in res.summary


def test_backup_falls_back_to_mtime_when_no_timestamp_and_default_limit(tmp_path, monkeypatch):
    now = time.time()
    p = tmp_path / "other-status.json"
    p.write_text(json.dumps({"job": "other", "result": "ok"}))
    os.utime(p, (now - 300 * H, now - 300 * H))
    res = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, status_glob=str(tmp_path / "*-status.json")))
    assert res.status == "warn" and res.metrics["backups"][0]["limit_h"] == 200.0   # default limit for unknown jobs


def test_backup_parse_ts_variants():
    assert cb._parse_ts("2026-09-27 01:31:22") == time.mktime(time.strptime("2026-09-27 01:31:22", "%Y-%m-%d %H:%M:%S"))
    assert cb._parse_ts("2026-09-27T01:31:22-04:00") is not None
    assert cb._parse_ts(1_700_000_000) == 1_700_000_000.0
    assert cb._parse_ts("garbage") is None and cb._parse_ts(None) is None


# --------------------------------------------------------------------------- docker_df
SYSTEM_DF = "\n".join(json.dumps(d) for d in [
    {"Active": "69", "Reclaimable": "17.87GB (17%)", "Size": "104.7GB", "TotalCount": "91", "Type": "Images"},
    {"Active": "72", "Reclaimable": "251.7kB (0%)", "Size": "3.532GB", "TotalCount": "73", "Type": "Containers"},
    {"Active": "32", "Reclaimable": "593.5MB (1%)", "Size": "35.93GB", "TotalCount": "54", "Type": "Local Volumes"},
    {"Active": "0", "Reclaimable": "21.16GB", "Size": "21.16GB", "TotalCount": "303", "Type": "Build Cache"},
]) + "\n"
BUILDX_LS = json.dumps({"Name": "immaculaterr-builder", "Driver": "docker-container", "Current": True}) + "\n" + \
            json.dumps({"Name": "default", "Driver": "docker", "Current": False}) + "\n"
BUILDX_DU = "ID   RECLAIMABLE SIZE LAST ACCESSED\nabc  true  462.6MB  3 hours ago\nReclaimable:\t9.53GB\nTotal:\t\t9.53GB\n"


def df_sh(system=SYSTEM_DF, ls=BUILDX_LS, du=BUILDX_DU, rc=0):
    return FakeSh(("docker system df", system, rc), ("docker buildx ls", ls), ("docker buildx du --builder immaculaterr-builder", du))


def test_parse_size_si_and_binary_units():
    assert cb.parse_size("17.87GB (17%)") == 17_870_000_000
    assert cb.parse_size("251.7kB") == 251_700
    assert cb.parse_size("0B") == 0 and cb.parse_size("12") == 12
    assert cb.parse_size("1.5GiB") == int(1.5 * GIB)
    assert cb.parse_size("") is None and cb.parse_size("n/a") is None and cb.parse_size("3 parsecs") is None


def test_docker_df_sizes_builders_and_levels(tmp_path, monkeypatch):
    fake = df_sh()
    monkeypatch.setattr(cb, "sh", fake)
    ctx = mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=25, images_reclaimable_warn_gib=40)
    res = cb.docker_df(ctx)
    m = res.metrics
    # default-builder cache (system df) + the extra builder (buildx du): 21.16 GB + 9.53 GB
    assert m["build_cache_gib"] == pytest.approx((21.16e9 + 9.53e9) / GIB, abs=0.01)
    assert m["builders"] == [{"name": "immaculaterr-builder", "size_h": cb.human(9.53e9), "reclaim_h": cb.human(9.53e9)}]
    assert m["images_gib"] == pytest.approx(104.7e9 / GIB, abs=0.01) and m["images_reclaim_gib"] == pytest.approx(17.87e9 / GIB, abs=0.01)
    assert res.status == "warn" and "build cache" in res.summary and res.alert is False   # housekeeping: never pages
    assert not any(c.startswith("docker buildx du --builder default") for c in fake.calls)  # default is not double counted
    ascii_ok(res)


def test_docker_df_ok_below_thresholds_and_volumes_not_in_safe_reclaim(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "sh", df_sh(ls="", du=""))                # no extra builders
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=25, images_reclaimable_warn_gib=40))
    assert res.status == "ok" and res.summary.startswith("ok:")
    expect = int(17.87e9 + 21.16e9)                                    # images + build cache, never volumes
    assert res.metrics["safe_reclaim_gib"] == pytest.approx(expect / GIB, abs=0.01)
    vol_row = next(i for i in res.items if i["type"] == "Volumes")
    assert vol_row["reclaim_h"] == "-"
    assert "prune" not in res.summary.lower()                          # never suggests pruning anything


def test_docker_df_images_reclaimable_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "sh", df_sh(ls=""))
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=500, images_reclaimable_warn_gib=10))
    assert res.status == "warn" and "images reclaimable" in res.summary


def test_docker_df_unavailable_is_skipped_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "sh", FakeSh())
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df"))
    assert res.status == "skipped" and "rc=127" in res.summary
    monkeypatch.setattr(cb, "sh", df_sh(system="garbage\n"))
    assert cb.docker_df(mk(tmp_path, monkeypatch, "docker_df")).status == "skipped"


def test_docker_df_ignores_broken_extra_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "sh", df_sh(du="error: no such builder\n"))   # du output without totals
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df"))
    assert res.metrics["builders"] == [] and res.metrics["build_cache_gib"] == pytest.approx(21.16e9 / GIB, abs=0.01)
    assert res.metrics["builders_error"] == "buildx du immaculaterr-builder rc=0"     # undercount is reported
    assert "[buildx du immaculaterr-builder" in res.summary


ONLY_DEFAULT = json.dumps({"Name": "default", "Driver": "docker", "Current": True}) + "\n"


def root_df_sh(user_ls=BUILDX_LS, user_rc=0):
    """What the root-run timer sees: bare `docker buildx ls` lists only `default`; the owner lists the real builder."""
    return FakeSh(("docker system df", SYSTEM_DF),
                  ("docker buildx ls", ONLY_DEFAULT),                                   # root's own (empty) view
                  ("runuser -u ohmz -- docker buildx ls", user_ls, user_rc),
                  ("runuser -u ohmz -- docker buildx du --builder immaculaterr-builder", BUILDX_DU))


def test_docker_df_buildx_listing_with_only_default_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cb.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cb, "sh", df_sh(ls=ONLY_DEFAULT))
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=25))
    assert res.metrics["builders"] == [] and res.metrics["builders_error"] is None
    assert res.metrics["build_cache_gib"] == pytest.approx(21.16e9 / GIB, abs=0.01) and res.status == "ok"


def test_docker_df_as_root_runs_buildx_as_the_builder_owner(tmp_path, monkeypatch):
    """Regression: timers run as root; root's `buildx ls` only has `default`, so the 9.5 GB builder was missed
    and 28.6 GiB of cache was reported as 19.7 GiB (ok instead of warn)."""
    monkeypatch.setattr(cb.os, "geteuid", lambda: 0)
    fake = root_df_sh()
    monkeypatch.setattr(cb, "sh", fake)
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=25))
    assert res.metrics["build_cache_gib"] == pytest.approx((21.16e9 + 9.53e9) / GIB, abs=0.01)
    assert res.metrics["builders"][0]["name"] == "immaculaterr-builder" and res.status == "warn"
    assert res.metrics["buildx_user"] == "ohmz" and res.metrics["builders_error"] is None
    bx = [(c, e) for c, e in zip(fake.calls, fake.envs) if "buildx" in c]
    assert bx and all(c.startswith("runuser -u ohmz -- docker buildx") for c, _ in bx)   # never a bare root buildx
    assert all(e and e["HOME"] for _, e in bx)                                           # HOME set for the owner, as Notifier does
    assert "DOCKER_CONFIG" not in str([e for _, e in bx])                                # no root-owned files in ~ohmz/.docker


def test_docker_df_buildx_user_option_and_non_root_run_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(cb.os, "geteuid", lambda: 0)
    fake = FakeSh(("docker system df", SYSTEM_DF), ("runuser -u builder -- docker buildx ls", ""))
    monkeypatch.setattr(cb, "sh", fake)
    cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", buildx_user="builder"))
    assert any(c.startswith("runuser -u builder -- docker buildx ls") for c in fake.calls)
    fake2 = FakeSh(("docker system df", SYSTEM_DF), ("docker buildx ls", ""))
    monkeypatch.setattr(cb, "sh", fake2)
    cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", buildx_user=""))              # root, but no user configured
    assert "runuser" not in " ".join(fake2.calls) and "docker buildx ls --format json" in fake2.calls
    monkeypatch.setattr(cb.os, "geteuid", lambda: 1000)                                # the owner running it by hand
    fake3 = FakeSh(("docker system df", SYSTEM_DF), ("docker buildx ls", ""))
    monkeypatch.setattr(cb, "sh", fake3)
    cb.docker_df(mk(tmp_path, monkeypatch, "docker_df"))
    assert "runuser" not in " ".join(fake3.calls)


def test_docker_df_failed_builder_probe_is_reported_not_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(cb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cb, "sh", root_df_sh(user_ls="", user_rc=1))                  # e.g. runuser could not start
    res = cb.docker_df(mk(tmp_path, monkeypatch, "docker_df", build_cache_warn_gib=25))
    assert res.metrics["builders_error"] == "buildx ls rc=1" and "[buildx ls rc=1]" in res.summary
    ascii_ok(res)


# --------------------------------------------------------------------------- memory_health
def psi_text(s60=0.0, f60=0.0, s10=0.0, f10=0.0, s300=0.0, f300=0.0):
    return (f"some avg10={s10:.2f} avg60={s60:.2f} avg300={s300:.2f} total=1\n"
            f"full avg10={f10:.2f} avg60={f60:.2f} avg300={f300:.2f} total=1\n")


def meminfo_text(avail_gib=70, cached_gib=60, swap_total_gib=32, swap_free_gib=6):
    k = lambda g: int(g * 1024 * 1024)
    return (f"MemTotal:       {k(94)} kB\nMemFree:        {k(1.5)} kB\nMemAvailable:   {k(avail_gib)} kB\n"
            f"Buffers:        {k(6)} kB\nCached:         {k(cached_gib)} kB\nSwapCached: 0 kB\n"
            f"SwapTotal:      {k(swap_total_gib)} kB\nSwapFree:       {k(swap_free_gib)} kB\nSReclaimable:   {k(4)} kB\n")


class FakeProc:
    """Fake /proc whose vmstat advances `swpin_per_s` pages/s during the live sample."""

    def __init__(self, mem=None, mp=None, io=None, cpu=None, pswpin=1000, pswpout=2000, oom=19, swpin_per_s=0, sample=5):
        self.files = {"/proc/pressure/memory": mp or psi_text(), "/proc/pressure/io": io or psi_text(),
                      "/proc/pressure/cpu": cpu or psi_text(), "/proc/meminfo": mem or meminfo_text()}
        self.vm = {"pswpin": pswpin, "pswpout": pswpout, "oom_kill": oom}
        self.rate = swpin_per_s
        self.sample = sample

    def read(self, path):
        if path == "/proc/vmstat":
            return "nr_free_pages 123\n" + "".join(f"{k} {v}\n" for k, v in self.vm.items())
        return self.files[path]

    def sleep(self, s):
        self.vm["pswpin"] += int(self.rate * s)


def install(monkeypatch, fp):
    monkeypatch.setattr(cb, "_read_proc", fp.read)
    monkeypatch.setattr(cb, "_sleep", fp.sleep)


MOPTS = dict(psi_mem_full_warn=5.0, psi_mem_full_crit=15.0, mem_available_warn_gib=10, mem_available_crit_gib=4,
             swap_in_pages_per_s_warn=2000)


def test_memory_big_cache_and_used_swap_alone_are_fine(tmp_path, monkeypatch):
    install(monkeypatch, FakeProc(mem=meminfo_text(avail_gib=70, cached_gib=60, swap_free_gib=2),
                                  io=psi_text(s60=63.0, f60=58.0)))
    res = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS))
    assert res.status == "ok" and "cache" in res.summary and "reclaimable" in res.summary and "harmless" in res.summary
    ascii_ok(res)
    m = res.metrics
    assert m["swap_used_h"] == "30.0 GiB" and m["mem_available_h"] == "70.0 GiB"
    assert m["psi_io_some60"] == 63.0                 # high IO stall is reported but is not a memory problem
    for k in ("psi_mem_full60", "swap_in_pps", "oom_kills_total", "oom_kills_delta"):
        assert k in m
    assert [i["res"] for i in res.items] == ["memory", "io", "cpu"]


def test_memory_crit_on_psi_and_on_available(tmp_path, monkeypatch):
    install(monkeypatch, FakeProc(mp=psi_text(s60=30, f60=16.0)))
    res = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS))
    assert res.status == "crit" and "memory stall 16.0%" in res.summary
    install(monkeypatch, FakeProc(mem=meminfo_text(avail_gib=3)))
    res = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS))
    assert res.status == "crit" and "only 3.0 GiB available" in res.summary


def test_memory_warn_on_thresholds(tmp_path, monkeypatch):
    install(monkeypatch, FakeProc(mp=psi_text(f60=6.0)))
    assert cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS)).status == "warn"
    install(monkeypatch, FakeProc(mem=meminfo_text(avail_gib=8)))
    assert cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS)).status == "warn"


def test_memory_sustained_swap_in_warns_but_a_lone_burst_does_not(tmp_path, monkeypatch):
    t0 = 1_000_000.0
    # run 1: establishes the baseline, live rate 3000 pages/s over 5 s => warn (no average yet)
    fp = FakeProc(pswpin=1000, swpin_per_s=3000)
    install(monkeypatch, fp)
    ctx = mk(tmp_path, monkeypatch, "memory_health", now=t0, **MOPTS)
    res = cb.memory_health(ctx)
    assert res.metrics["swap_in_pps"] == 3000 and res.status == "warn" and "swap-in" in res.summary
    ctx.save_state()
    # run 2, 15 min later: counter barely moved since run 1 (tiny average) => the burst alone is not "sustained"
    fp.vm["pswpin"] += 50                                   # 50 pages in 900 s ~ 0.05 pages/s average
    ctx2 = mk(tmp_path, monkeypatch, "memory_health", now=t0 + 900, **MOPTS)
    res2 = cb.memory_health(ctx2)
    assert res2.metrics["swap_in_pps"] == 3000 and res2.metrics["swap_in_avg_pps"] < 5
    assert res2.status == "ok"
    ctx2.save_state()
    # run 3: heavy average (1.8M pages over 900 s = 2000/s) plus live burst => warn
    fp.vm["pswpin"] += 1_800_000
    res3 = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 1800, **MOPTS))
    assert res3.status == "warn" and res3.metrics["swap_in_avg_pps"] >= 2000


def test_memory_does_not_read_a_deliberate_swap_relief_as_pressure(tmp_path, monkeypatch):
    """`homelab-maint swap relieve` brings ~30 GiB back on purpose: neither the live sample nor the next run's average may warn about it."""
    from homelab_maint import swapwatch
    t0 = 3_000_000.0
    opts = {**MOPTS, "swap_sample_s": 0}
    fp = FakeProc(pswpin=0)
    install(monkeypatch, fp)
    ctx = mk(tmp_path, monkeypatch, "memory_health", now=t0, **opts)
    assert cb.memory_health(ctx).status == "ok"
    ctx.save_state()
    fp.vm["pswpin"] = 8_000_000                                   # 8M pages in 15 min = ~8.9k pages/s average
    control = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 900, **opts))
    assert control.status == "warn" and "swap-in" in control.summary, "control: without a ledger this is pressure"
    core.write_json_atomic(swapwatch._ledger_path(), {"done": [{"t0": t0 + 100, "t1": t0 + 400, "pages_in": 8_000_000}]}, 0o644)
    res = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 900, **opts))
    assert res.status == "ok" and res.metrics["swap_in_avg_pps"] == 0, res.summary
    # and while it is running, the live sample is expected too (3000 pages/s would warn on its own)
    fp2 = FakeProc(pswpin=0, swpin_per_s=3000)
    install(monkeypatch, fp2)
    core.write_json_atomic(swapwatch._ledger_path(), {}, 0o644)
    assert cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 20000, **MOPTS)).status == "warn"
    swapwatch.relief_begin(now=t0 + 19990)
    res2 = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 20000, **MOPTS))
    assert res2.metrics["swap_in_pps"] == 0 and res2.status == "ok"


def test_memory_without_live_sample_uses_average(tmp_path, monkeypatch):
    t0 = 2_000_000.0
    fp = FakeProc(pswpin=0)
    install(monkeypatch, fp)
    opts = {**MOPTS, "swap_sample_s": 0}
    ctx = mk(tmp_path, monkeypatch, "memory_health", now=t0, **opts)
    assert cb.memory_health(ctx).status == "ok"
    ctx.save_state()
    fp.vm["pswpin"] = 3600 * 2500                           # 2500 pages/s average over the next hour
    res = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 3600, **opts))
    assert res.status == "warn" and res.metrics["swap_in_avg_pps"] == 2500.0


def test_memory_oom_kills_delta_and_reboot(tmp_path, monkeypatch):
    t0 = 3_000_000.0
    fp = FakeProc(oom=19)
    install(monkeypatch, fp)
    ctx = mk(tmp_path, monkeypatch, "memory_health", now=t0, **MOPTS)
    r1 = cb.memory_health(ctx)
    assert r1.metrics["oom_kills_total"] == 19 and r1.metrics["oom_kills_delta"] == 0 and r1.status == "ok"
    ctx.save_state()
    fp.vm["oom_kill"] = 21
    ctx2 = mk(tmp_path, monkeypatch, "memory_health", now=t0 + 900, **MOPTS)
    r2 = cb.memory_health(ctx2)
    assert r2.metrics["oom_kills_delta"] == 2 and r2.status == "warn" and "2 OOM kill(s) in last 6h" in r2.summary
    ctx2.save_state()
    fp.vm["oom_kill"] = 1                                   # counter reset => reboot; the 1 kill is new
    r3 = cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", now=t0 + 1800, **MOPTS))
    assert r3.metrics["oom_kills_delta"] == 1


def test_memory_oom_warning_is_held_so_a_two_run_alert_can_confirm_it(tmp_path, monkeypatch):
    """Regression: the delta is non-zero for one 15 min run; the warn must outlive it (event_hold_h)."""
    t0 = 4_000_000.0
    fp = FakeProc(oom=19)
    install(monkeypatch, fp)

    def run(t, **extra):
        ctx = mk(tmp_path, monkeypatch, "memory_health", now=t, **MOPTS, **extra)
        res = cb.memory_health(ctx)
        ctx.save_state()
        return res

    assert run(t0).status == "ok"
    fp.vm["oom_kill"] = 21
    r2 = run(t0 + 900)
    assert r2.status == "warn" and r2.metrics["oom_kills_delta"] == 2
    r3 = run(t0 + 1800)                                       # nothing new, but the evidence is not gone yet
    assert r3.metrics["oom_kills_delta"] == 0 and r3.metrics["oom_kills_recent"] == 2
    assert r3.status == "warn" and "2 OOM kill(s) in last 6h" in r3.summary
    fp.vm["oom_kill"] = 22                                    # another kill inside the window accumulates
    assert run(t0 + 3600).metrics["oom_kills_recent"] == 3
    assert run(t0 + 3600 + 6 * H - 1).status == "warn"        # still inside the hold of the newest kill
    r6 = run(t0 + 3600 + 6 * H + 1)
    assert r6.status == "ok" and r6.metrics["oom_kills_recent"] == 0
    # the hold window is configurable
    fp.vm["oom_kill"] = 23
    assert run(t0 + 10 * H, event_hold_h=1).status == "warn"
    assert run(t0 + 10 * H + 2 * H, event_hold_h=1).status == "ok"


def test_held_tolerates_damaged_state():
    assert cb._held(None, 100.0, 50) == [] and cb._held("junk", 100.0, 50) == []
    assert cb._held([[90.0, 2], [10.0, 5], "x", [1], None, ["a", "b"]], 100.0, 50) == [[90.0, 2]]


def test_failed_units_restart_events_accumulate_drop_removed_containers_and_survive_bad_state(tmp_path, monkeypatch):
    ps = "web|running|Up 1 hour\napi|running|Up 1 hour\n"
    t0 = time.time()

    def run(t, counts, state=None):
        ctx = mk(tmp_path, monkeypatch, "failed_units", now=t)
        if state is not None:
            ctx.state.update(state)
        monkeypatch.setattr(cb, "sh", units_sh(t, failed="", ages={}, ps=ps, counts=counts))
        res = cb.failed_units(ctx)
        ctx.save_state()
        return res, ctx

    run(t0, {"web": 0, "api": 0})
    r, _ = run(t0 + 900, {"web": 1, "api": 0})
    assert r.status == "warn" and r.metrics["restarts_recent"] == 1
    r, _ = run(t0 + 1800, {"web": 3, "api": 0})
    assert r.metrics["restarts_recent"] == 3 and any("3 restart(s) in last 6h" in i["note"] for i in r.items)
    # container removed (api and web gone from docker ps -a): its events are dropped, not reported forever
    monkeypatch.setattr(cb, "sh", units_sh(t0 + 2700, failed="", ages={}, ps="api|running|Up 1 hour\n", counts={"api": 0}))
    ctx = mk(tmp_path, monkeypatch, "failed_units", now=t0 + 2700)
    res = cb.failed_units(ctx)
    assert res.status == "ok" and ctx.state["restart_events"] == {}
    # damaged state is ignored rather than crashing the check
    r, _ = run(t0 + 3600, {"web": 0, "api": 0}, state={"restart_events": ["junk"], "restart_counts": {"web": 0, "api": 0}})
    assert r.status == "ok"
    r, _ = run(t0 + 4500, {"web": 0, "api": 0}, state={"restart_events": {"web": "junk", "api": [[t0 + 4400, 2]]}})
    assert r.metrics["restarts_recent"] == 2
    r, _ = run(t0 + 5400, {"web": 0, "api": 0}, state={"restart_counts": {"web": "junk", "api": None}, "restart_events": {}})
    assert r.status == "ok"                                                     # unusable baseline => re-baseline, no crash
    r, _ = run(t0 + 6300, {"web": 0, "api": 0}, state={"restart_counts": ["junk"], "restart_events": {}})
    assert r.status == "ok"


def test_memory_unreadable_proc_is_error_not_ok(tmp_path, monkeypatch):
    def boom(path):
        raise OSError("nope")
    monkeypatch.setattr(cb, "_read_proc", boom)
    assert cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS)).status == "error"
    fp = FakeProc(mem="MemTotal: 1 kB\n")
    install(monkeypatch, fp)
    assert cb.memory_health(mk(tmp_path, monkeypatch, "memory_health", **MOPTS)).status == "error"


def test_psi_parser_handles_missing_full_line():
    p = cb._psi("some avg10=1.50 avg60=2.00 avg300=3.00 total=9\n")
    assert p == {"some": {"avg10": 1.5, "avg60": 2.0, "avg300": 3.0, "total": 9.0}}


# --------------------------------------------------------------------------- plex_media_mount_check
def esc(p):
    return str(p).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def mi(id_, dev, root, mp, fstype="ext4", src="/dev/sdh1"):
    return f"{id_} 30 {dev} {esc(root)} {esc(mp)} rw,relatime shared:{id_} - {fstype} {esc(src)} rw"


@pytest.fixture
def plex(tmp_path):
    media = tmp_path / "var" / "snap" / "Plex Media Server" / "Media"          # note the spaces
    media.mkdir(parents=True)
    (media / "a.bundle").write_bytes(b"x" * 20000)
    ssd = tmp_path / "ssd"
    return {"media": media, "ssd": ssd, "prefix": ssd / "plex",
            "root": mi(30, "259:2", "/", "/", src="/dev/nvme0n1p2"),
            "ssd_mount": mi(165, "8:113", "/", ssd, src="/dev/sdh1")}


def popts(plex, **kw):
    return dict(mount_point=str(plex["media"]), expected_source_prefix=str(plex["prefix"]), **kw)


def feed(monkeypatch, *lines):
    text = "\n".join(lines) + "\n"
    monkeypatch.setattr(cb, "_read_proc", lambda p: text)


def test_plex_ok_when_bind_mounted_from_ssd(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    bind = mi(300, "8:113", "/plex/Media", plex["media"])                       # escaped spaces in mountpoint
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "ok", res.summary
    assert res.metrics["mounted"] is True and res.metrics["source"] == f"{plex['ssd']}/plex/Media"
    assert res.metrics["media_size_root_gib"] == 0.0
    ascii_ok(res)


def test_plex_crit_when_not_mounted_but_ssd_path_exists(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    feed(monkeypatch, plex["root"], plex["ssd_mount"])
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))
    res = cb.plex_media_mount_check(ctx)
    assert res.status == "crit" and "regenerate Media on the root disk" in res.summary
    assert res.metrics["mounted"] is False and res.metrics["ssd_path_exists"] is True
    assert res.metrics["media_size_root_gib"] is not None
    ascii_ok(res)


def test_plex_info_when_not_migrated(tmp_path, monkeypatch, plex):
    feed(monkeypatch, plex["root"])                                            # no bind mount, no ssd/plex dir
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "info" and "not migrated" in res.summary and res.metrics["mounted"] is False


def test_plex_crit_if_it_was_mounted_and_everything_vanishes(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    bind = mi(300, "8:113", "/plex/Media", plex["media"])
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))
    assert cb.plex_media_mount_check(ctx).status == "ok"
    ctx.save_state()
    plex["prefix"].rmdir()                                                     # SSD path gone and bind mount gone
    feed(monkeypatch, plex["root"])
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "crit" and "vanished" in res.summary


def test_plex_vanished_crit_is_sticky_not_one_run(tmp_path, monkeypatch, plex):
    """Regression: 'SSD path vanished' used to last one run, then fall back to a harmless 'not migrated'."""
    plex["prefix"].mkdir(parents=True)
    bind = mi(300, "8:113", "/plex/Media", plex["media"])
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))
    assert cb.plex_media_mount_check(ctx).status == "ok"
    ctx.save_state()
    plex["prefix"].rmdir()                                                     # e.g. reboot with the SSD absent
    feed(monkeypatch, plex["root"])
    for i in range(4):                                                         # the 2-run alert confirmation needs >= 2
        ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", now=time.time() + i * 900, **popts(plex))
        res = cb.plex_media_mount_check(ctx)
        ctx.save_state()
        assert res.status == "crit" and "vanished" in res.summary, (i, res.summary)
        assert res.metrics["was_mounted"] is True and ctx.state["was_mounted"] is True
    # and it recovers by itself when the mount comes back
    plex["prefix"].mkdir(parents=True)
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "ok"


def test_plex_expect_mounted_false_is_the_explicit_rollback_switch(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    bind = mi(300, "8:113", "/plex/Media", plex["media"])
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))
    cb.plex_media_mount_check(ctx)
    ctx.save_state()
    feed(monkeypatch, plex["root"], plex["ssd_mount"])                         # bind mount removed, SSD path still there
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "crit"
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex, expect_mounted=False))
    res = cb.plex_media_mount_check(ctx)                                       # owner says: rolled back on purpose
    assert res.status == "info" and "expect_mounted = false" in res.summary and ctx.state["was_mounted"] is False
    ctx.save_state()
    plex["prefix"].rmdir()                                                     # the flag was forgotten: back to 'not migrated'
    feed(monkeypatch, plex["root"])
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "info" and "not migrated" in res.summary


def test_plex_wrong_source_other_disk_warns_and_root_disk_is_crit(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    other = mi(166, "8:200", "/", tmp_path / "other", src="/dev/sdx1")
    bind_other = mi(301, "8:200", "/media", plex["media"], src="/dev/sdx1")      # backed by /other/media
    feed(monkeypatch, plex["root"], plex["ssd_mount"], other, bind_other)
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "warn" and "expected under" in res.summary
    bind_root = mi(302, "259:2", "/var/lib/plexcopy", plex["media"], src="/dev/nvme0n1p2")
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind_root)
    res = cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex)))
    assert res.status == "crit"                                                # on the root device: protects nothing


def test_plex_prefix_match_is_path_component_aware(tmp_path, monkeypatch, plex):
    (plex["ssd"] / "plex").mkdir(parents=True)
    bind = mi(303, "8:113", "/plexextra/Media", plex["media"])                  # '/plexextra' is NOT under '/plex'
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "warn"


def test_plex_topmost_of_stacked_mounts_wins(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    old = mi(300, "259:2", "/junk", plex["media"], src="/dev/nvme0n1p2")
    new = mi(310, "8:113", "/plex/Media", plex["media"])
    feed(monkeypatch, plex["root"], plex["ssd_mount"], old, new)
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "ok"


def test_plex_root_size_is_cached_between_runs(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    feed(monkeypatch, plex["root"], plex["ssd_mount"])
    calls = []
    monkeypatch.setattr(cb, "_tree_usage", lambda p, b: (calls.append(p), (5 * GIB, True))[1])
    t0 = 5_000_000.0
    ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", now=t0, **popts(plex))
    res = cb.plex_media_mount_check(ctx)
    assert res.metrics["media_size_root_gib"] == 5.0 and "5.0 GiB" in res.summary
    ctx.save_state()
    ctx2 = mk(tmp_path, monkeypatch, "plex_media_mount_check", now=t0 + 3600, **popts(plex))
    assert cb.plex_media_mount_check(ctx2).metrics["media_size_root_gib"] == 5.0
    assert len(calls) == 1                                                      # second run used the cache
    ctx3 = mk(tmp_path, monkeypatch, "plex_media_mount_check", now=t0 + 7 * H, **popts(plex))
    cb.plex_media_mount_check(ctx3)
    assert len(calls) == 2                                                      # refreshed after 6 h


def test_plex_unconfigured_and_unreadable_mountinfo(tmp_path, monkeypatch, plex):
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check")).status == "skipped"
    def boom(p):
        raise OSError("x")
    monkeypatch.setattr(cb, "_read_proc", boom)
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "error"
    monkeypatch.setattr(cb, "_read_proc", lambda p: "")
    assert cb.plex_media_mount_check(mk(tmp_path, monkeypatch, "plex_media_mount_check", **popts(plex))).status == "error"


def test_mountinfo_real_lines_and_escapes():
    text = ("165 30 8:113 / /media/SandiskSSD rw,relatime shared:138 - ext4 /dev/sdh1 rw\n"
            "400 30 8:113 /plex/Media /var/snap/plexmediaserver/common/Library/Application\\040Support/Plex\\040Media\\040Server/Media rw,relatime shared:138 - ext4 /dev/sdh1 rw\n"
            "2568 220 0:4 mnt:[4026533339] /run/snapd/ns/plexmediaserver.mnt rw - nsfs nsfs rw\n"
            "garbage line\n")
    ms = cb.parse_mountinfo(text)
    assert len(ms) == 3
    assert ms[1]["mp"] == "/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Media"
    assert ms[1]["root"] == "/plex/Media" and ms[1]["source"] == "/dev/sdh1"
    assert cb._backing_path(ms[1], ms) == "/media/SandiskSSD/plex/Media"
    assert cb._backing_path(ms[0], ms) is None            # whole-device mount: no directory to resolve
    assert cb._unescape("a\\040b\\134c") == "a b\\c"


def test_backing_path_prefers_deepest_root_and_root_mountpoint(tmp_path):
    ms = cb.parse_mountinfo("\n".join([
        mi(1, "8:1", "/", "/", src="/dev/sda1"),
        mi(2, "8:1", "/srv/plex", "/media/plexdisk", src="/dev/sda1"),
        mi(3, "8:1", "/srv/plex/Media", "/target", src="/dev/sda1"),
    ]))
    assert cb._backing_path(ms[2], ms) == "/media/plexdisk/Media"      # via the deeper (/srv/plex) mount
    only = cb.parse_mountinfo(mi(5, "9:9", "/x/y", "/z", src="/dev/sdz"))
    assert cb._backing_path(only[0], only) is None


def test_tree_usage_counts_hardlinks_once_and_respects_budget(tmp_path):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    (d / "f1").write_bytes(b"a" * 8192)
    os.link(d / "f1", d / "sub" / "f1-link")
    (d / "sub" / "f2").write_bytes(b"b" * 8192)
    os.symlink("/etc", d / "link-to-etc")                               # symlinks are never followed
    size, complete = cb._tree_usage(str(d), 5)
    assert complete is True
    expect = os.stat(d / "f1").st_blocks * 512 + os.stat(d / "sub" / "f2").st_blocks * 512
    assert expect <= size < expect + 3 * 4096 + 4096                   # + dirs and the symlink inode
    assert cb._tree_usage(str(tmp_path / "missing"), 5) == (0, False)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(2100):
        (big / f"f{i}").write_bytes(b"")
    assert cb._tree_usage(str(big), -1)[1] is False                    # budget exhausted => reported incomplete


# --------------------------------------------------------------------------- alerting glue (real core.Notifier)
# These pin what the checks rely on from core.Notifier.evaluate: a persistent warn/crit is paged once after `alert_confirm_runs`
# consecutive runs. (They were non-strict xfails until the core.py confirm fix landed; a regression now turns them red.)


def drive_notifier(tmp_path, monkeypatch, statuses):
    """Feed `statuses` (one per 15 min run, alerts.json persisted in between) to the real Notifier; return what it sent."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    sent: list[str] = []
    monkeypatch.setattr(core.Notifier, "_send", lambda self, name, subject, body, now: (sent.append(subject), True)[1])
    for i, s in enumerate(statuses):
        n = core.Notifier({"global": {"alert_confirm_runs": 2}})
        n.evaluate("t", "T", s if isinstance(s, core.Result) else core.Result(s, "m"), 1000 + i * 900)
        n.save()
    return sent


@pytest.mark.parametrize("seq, expect", [
    (["ok", "warn", "warn"], ["WARN T"]),                          # confirmed on the 2nd consecutive run
    (["ok", "warn", "ok"], []),                                    # a one-run blip never pages
    (["warn"] * 6, ["WARN T"]),                                    # persistent: paged once, not every run
    (["crit"] * 6, ["CRIT T"]),
    (["warn", "warn", "warn", "ok", "ok"], ["WARN T", "OK T: recovered"]),
    (["warn", "warn", "crit", "crit"], ["WARN T", "CRIT T"]),      # escalation is confirmed the same way
])
def test_notifier_confirms_a_persistent_level_and_ignores_blips(tmp_path, monkeypatch, seq, expect):
    assert drive_notifier(tmp_path, monkeypatch, seq) == expect


def test_held_oom_warning_pages_through_the_real_notifier(tmp_path, monkeypatch):
    """End to end: one OOM kill in ONE run still produces the two consecutive warn runs a page needs."""
    t0 = 6_000_000.0
    fp = FakeProc(oom=19)
    install(monkeypatch, fp)
    results = []
    for i, oom in enumerate((19, 20, 20, 20)):
        fp.vm["oom_kill"] = oom
        ctx = mk(tmp_path, monkeypatch, "memory_health", now=t0 + i * 900, **MOPTS)
        results.append(cb.memory_health(ctx))
        ctx.save_state()
    assert [r.status for r in results] == ["ok", "warn", "warn", "warn"]
    assert drive_notifier(tmp_path, monkeypatch, results) == ["WARN T"]


def test_plex_vanished_crit_pages_through_the_real_notifier(tmp_path, monkeypatch, plex):
    plex["prefix"].mkdir(parents=True)
    bind = mi(300, "8:113", "/plex/Media", plex["media"])
    feed(monkeypatch, plex["root"], plex["ssd_mount"], bind)
    results = []
    for i in range(4):
        if i == 1:
            plex["prefix"].rmdir()                                                 # SSD absent after a reboot
            feed(monkeypatch, plex["root"])
        ctx = mk(tmp_path, monkeypatch, "plex_media_mount_check", now=time.time() + i * 900, **popts(plex))
        results.append(cb.plex_media_mount_check(ctx))
        ctx.save_state()
    assert [r.status for r in results] == ["ok", "crit", "crit", "crit"]
    assert drive_notifier(tmp_path, monkeypatch, results) == ["CRIT T"]


# --------------------------------------------------------------------------- contract-level
def test_every_task_survives_run_task_with_empty_config(tmp_path, monkeypatch):
    """Through the real runner wrapper (C0 never mutates): no task may crash or break the Result contract."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(cb, "sh", FakeSh())
    monkeypatch.setattr(cb, "_sleep", lambda s: None)
    cfg = {"global": {}, "caps": {}, "protected": {}, "tasks": {"disk_forecast": {"watch": ["/"]},
                                                                   "backup_freshness": {"status_glob": str(tmp_path / "*.json")}}}
    for name in ("disk_forecast", "failed_units", "backup_freshness", "docker_df", "memory_health", "plex_media_mount_check"):
        res, _ = core.run_task(core.REGISTRY[name], cfg, apply=True)
        assert res.status in ("ok", "info", "warn", "crit", "skipped"), (name, res.status, res.summary)
        ascii_ok(res)


# --------------------------------------------------------------------------- SPEC5 issue_key: the FULL failing set, never the volatile numbers
def ik_fp(name, res):
    """The acknowledgement id of a Result: explicit (the task knows which error this is) and so passing the task policy without a rule in
    ack.toml. Whether that severity may be acknowledged at all is the separate [ack] severities rule (crit never is), so assert policy_ok,
    not the full ackability, here."""
    from homelab_maint import acks
    fp = acks.fingerprint(name, res, res.status)
    assert fp.mode == "explicit" and acks.policy_ok(name, fp.mode) and res.issue_key, (name, res.issue_key)
    return str(fp)


def test_core_ikey_is_sorted_escaped_and_none_when_nothing_fails():
    assert core.ikey() is None and core.ikey(a=[], b=set()) is None
    assert core.ikey(units=["b", "a", "a"], exited=["x"]) == "exited:x;units:a,b"                    # sorted, de-duplicated, keyed by kind
    assert core.ikey(a=["x,y"]) != core.ikey(a=["x", "y"]) and core.ikey(a=["p;q:r"]) == "a:p%3Bq:r"      # two different sets can never read alike
    assert core.ikey(a=["%2C"]) != core.ikey(a=[","])


def disk_run(tmp_path, monkeypatch, table, **over):
    monkeypatch.setattr(cb, "_disk_usage", fake_usage({m: (int(p * 10) * GIB // 10, int((100 - p) * 10) * GIB // 10) for m, p in table.items()}))
    return cb.disk_forecast(mk(tmp_path, monkeypatch, "disk_forecast", **{**dict(watch=list(table), info_only=[], warn_free_pct=12, crit_free_pct=6,
                                                                                warn_days=21, crit_days=7), **over}))


def test_disk_forecast_key_is_every_failing_mount_and_not_the_percentages(tmp_path, monkeypatch):
    a = disk_run(tmp_path, monkeypatch, {"/": 5.0, "/data": 4.0, "/mnt/a": 3.0, "/mnt/b": 2.0, "/mnt/ok": 50.0})
    assert a.issue_key == "mounts:/,/data,/mnt/a,/mnt/b"                                              # all four, the summary names three
    b = disk_run(tmp_path, monkeypatch, {"/": 4.0, "/data": 3.5, "/mnt/a": 1.0, "/mnt/b": 0.5, "/mnt/ok": 47.0})
    assert b.issue_key == a.issue_key and ik_fp("disk_forecast", a) == ik_fp("disk_forecast", b)       # volatile: free space moved
    assert disk_run(tmp_path, monkeypatch, {"/": 5.0, "/data": 4.0, "/mnt/a": 3.0, "/mnt/b": 50.0}).issue_key != a.issue_key     # one recovered
    assert disk_run(tmp_path, monkeypatch, {"/": 5.0, "/data": 4.0, "/mnt/a": 3.0, "/mnt/b": 2.0, "/mnt/c": 1.0}).issue_key != a.issue_key    # one more
    assert disk_run(tmp_path, monkeypatch, {"/": 50.0, "/data": 50.0}).issue_key is None             # nothing failing: nothing to acknowledge


def test_disk_forecast_key_sees_a_same_count_swap_among_the_hidden_mounts(tmp_path, monkeypatch):
    """The summary lists the three worst and counts the rest ("+1 more"): swapping which mount is the hidden fourth left the text, and so
    the old summary-based key, unchanged."""
    base = {"/": 5.0, "/data": 4.0, "/mnt/a": 3.0}
    x = disk_run(tmp_path, monkeypatch, {**base, "/mnt/b": 10.0, "/mnt/c": 50.0})
    y = disk_run(tmp_path, monkeypatch, {**base, "/mnt/b": 50.0, "/mnt/c": 10.0})
    assert x.summary == y.summary and x.summary.endswith("; +1 more")
    assert ik_fp("disk_forecast", x) != ik_fp("disk_forecast", y)


def fu_run(tmp_path, monkeypatch, *, failed=SYSTEMCTL_FAILED, ages=None, ps="", counts=None, **opts):
    ctx = mk(tmp_path, monkeypatch, "failed_units", **opts)
    monkeypatch.setattr(cb, "sh", units_sh(ctx.now, failed=failed, ages=ages if ages is not None else {}, ps=ps, counts=counts))
    return cb.failed_units(ctx)


def test_failed_units_key_is_the_full_sets_and_ignores_ages_and_restart_counts(tmp_path, monkeypatch):
    units = "".join(f"{n}.service loaded failed failed X\n" for n in ("a", "b", "c", "d", "e"))               # five: the summary names four
    old = {f"{n}.service": 7 * 86400 for n in "abcde"}
    r1 = fu_run(tmp_path, monkeypatch, failed=units, ages=old, ps="web|running|Up 1 hour (unhealthy)\n", counts={"web": 0})
    assert r1.issue_key == "unhealthy:web;units:a.service,b.service,c.service,d.service,e.service" and "e.service" not in r1.summary
    older = {k: v * 3 for k, v in old.items()}                                                       # the same failures, a lot older
    r2 = fu_run(tmp_path, monkeypatch, failed=units, ages=older, ps="web|running|Up 9 days (unhealthy)\n", counts={"web": 0})
    assert r2.issue_key == r1.issue_key and ik_fp("failed_units", r1) == ik_fp("failed_units", r2)
    five_for_six = units.replace("e.service", "f.service")                                           # a hidden unit replaced by another: same text, other error
    r3 = fu_run(tmp_path, monkeypatch, failed=five_for_six, ages={**old, "f.service": 7 * 86400}, ps="web|running|Up 1 hour (unhealthy)\n", counts={"web": 0})
    assert r3.summary == r1.summary and ik_fp("failed_units", r3) != ik_fp("failed_units", r1)
    r4 = fu_run(tmp_path, monkeypatch, failed=units, ages=old, ps="web|running|Up 1 hour (unhealthy)\napi|running|Up 1 hour (unhealthy)\n",
                counts={"web": 0, "api": 0})
    assert ik_fp("failed_units", r4) != ik_fp("failed_units", r1)                                    # another container unhealthy


def test_failed_units_key_covers_exited_and_restarting_containers_and_not_the_expected_ones(tmp_path, monkeypatch):
    ps = "oneshot|exited|Exited (1) 5 minutes ago\ncomfyui|exited|Exited (137) 2 hours ago\nloopy|restarting|Restarting (1) 3 seconds ago\n"
    r = fu_run(tmp_path, monkeypatch, failed="", ps=ps, counts={}, expected_stopped_containers=["comfyui"])
    assert r.issue_key == "exited:oneshot;restarting:loopy"
    again = fu_run(tmp_path, monkeypatch, failed="", ps=ps.replace("5 minutes", "6 hours").replace("3 seconds", "9 seconds"), counts={},
                   expected_stopped_containers=["comfyui"])
    assert again.issue_key == r.issue_key
    assert fu_run(tmp_path, monkeypatch, failed="", ps="web|running|Up 3 hours (healthy)\n", counts={"web": 0}).issue_key is None      # healthy: none


def bk_run(tmp_path, monkeypatch, now, late: dict, failed=(), stack_h=5):
    for p in tmp_path.glob("*-status.json"):
        p.unlink()
    for job, age_h in late.items():
        write_status(tmp_path, job, finished=now - age_h * H)
    for job in failed:
        write_status(tmp_path, job, result="failed", finished=now - 2 * H)
    stamp(tmp_path / "LAST_OK", stack_h, now)
    return cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))


def test_backup_key_is_every_bad_backup_with_the_decade_of_its_lateness(tmp_path, monkeypatch):
    now = time.time()
    a = bk_run(tmp_path, monkeypatch, now, {"system": 250, "immich": 260, "photos": 270, "vault": 280})
    assert a.summary.endswith("; +1 more")                                                           # four late, three shown
    assert a.issue_key == "backups:backup-immich=b3,backup-photos=b3,backup-system=b3,backup-vault=b3"
    b = bk_run(tmp_path, monkeypatch, now, {"system": 255, "immich": 299, "photos": 210, "vault": 205})
    assert b.issue_key == a.issue_key and ik_fp("backup_freshness", a) == ik_fp("backup_freshness", b)          # hours moved inside the decade
    c = bk_run(tmp_path, monkeypatch, now, {"system": 250, "immich": 260, "photos": 270, "vault": 1200})
    assert ik_fp("backup_freshness", c) != ik_fp("backup_freshness", a)                              # a month late is not a night late: b4
    d = bk_run(tmp_path, monkeypatch, now, {"system": 250, "immich": 260, "photos": 270, "zeta": 280})
    assert d.summary == a.summary                                                                    # the hidden fourth backup is another one: the same text ...
    assert ik_fp("backup_freshness", d) != ik_fp("backup_freshness", a)                              # ... and another error, which a text key could not see


def test_backup_key_tells_failed_from_late_and_an_unreadable_file(tmp_path, monkeypatch):
    now = time.time()
    late = bk_run(tmp_path, monkeypatch, now, {"system": 250})
    failed = bk_run(tmp_path, monkeypatch, now, {}, failed=["system"])
    assert failed.issue_key == "backups:backup-system=FAILED" and ik_fp("backup_freshness", failed) != ik_fp("backup_freshness", late)
    (tmp_path / "broken-status.json").write_text("[1, 2")
    r = cb.backup_freshness(mk(tmp_path, monkeypatch, "backup_freshness", now=now, **bopts(tmp_path)))
    assert "backup-broken=UNREADABLE" in r.issue_key
    assert bk_run(tmp_path, monkeypatch, now, {"system": 3}).issue_key is None                      # all fresh again (bk_run clears the files first)
