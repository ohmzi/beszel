#!/usr/bin/env python3
"""Deterministic, realistic status.json fixtures (ok / warn / crit / stale) for the payload tests and for
previewing the Homarr widgets without the real runner.

  python3 widgets/make_sample_status.py            # rewrites widgets/sample-status.json (the "warn" state)
  python3 widgets/make_sample_status.py crit       # prints another state to stdout

Preview with the real server (the clock is frozen 3 minutes after generated_at, so it never goes stale):
  python3 -m homelab_maint.server --status widgets/sample-status.json --preview --port 9111
  curl -s http://127.0.0.1:9111/overview
Shapes follow cli.py (tasks.<name>.{title,klass,tier,status,summary,last_run,mode,metrics,items,alert,...},
tier_runs, reclaimed_log, overall, paused, host, generated_at) and the metric names in SPEC.md.
"""
from __future__ import annotations

import calendar
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NOW = calendar.timegm((2026, 10, 1, 19, 15, 0))     # fixed "current time" of every fixture
GIB = 1024 ** 3
STATES = ("ok", "warn", "crit", "stale")


def _h(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _mount(path, size_gib, used_pct, days=None, level="ok", info=False):
    free = size_gib * GIB * (100 - used_pct) / 100
    return {"mount": path, "free": int(free), "free_h": _h(free), "used_pct": used_pct, "days": days,
            "days_h": "n/a" if days is None else f"{days:.0f} d", "level": "info" if info else level, "info": info}


def _mounts(state: str) -> list[dict]:
    root = {"ok": (62, None, "ok"), "warn": (90, 12.3, "warn"), "crit": (97, 2.4, "crit"), "stale": (90, 12.3, "warn")}[state]
    return [
        _mount("/", 1832, root[0], root[1], root[2]),
        _mount("/media/SandiskSSD", 1833, 61),
        _mount("/mnt/backup/system", 5098, 78),
        _mount("/mnt/backup/immich", 8186, 33),
        _mount("/media/Immich", 3666, 77),
        _mount("/media/nextcloud", 915, 1),
        _mount("/media/seagate18tb", 16764, 96, info=True),
        _mount("/media/seagate16tb", 14902, 91, info=True),
        _mount("/media/toshiba12tb", 11176, 88, info=True),
        _mount("/media/WD22TB", 20490, 99.9, info=True),
        _mount("/media/WD18TB", 16764, 94, info=True),
        _mount("/media/western18to14tb", 16764, 93, info=True),
    ]


def _t(title, klass, tier, status, summary, last_run, metrics=None, items=None, alert=True, mode=None, freed=0):
    return {"title": title, "klass": klass, "tier": tier, "status": status, "summary": summary, "last_run": last_run,
            "duration_s": 1.4, "reclaimed_bytes": freed, "metrics": metrics or {}, "items": items or [], "alert": alert,
            "mode": mode or ("check" if klass == "C0" else "dry-run")}


def build(state: str = "warn", now: int = NOW) -> dict:
    """Status dict as cli.py would have written it for this state."""
    if state not in STATES:
        raise ValueError(state)
    warn = state in ("warn", "stale")
    crit = state == "crit"
    gen = now - 180 if state != "stale" else now - 2 * 3600 - 420     # stale: runner died two hours ago
    chk, daily, weekly = gen, gen - 11 * 3600 - 40 * 60, gen - 30 * 3600
    mounts = _mounts(state)
    root_free = mounts[0]["free_h"]
    root_days = mounts[0]["days"]
    root_lvl = mounts[0]["level"]
    t: dict[str, dict] = {}

    # ---- C0 checks
    t["disk_forecast"] = _t("Disk space", "C0", "check", root_lvl,
                            ("/ 97% used, full in ~2 d" if crit else "/ 90% used, ~12 d to full" if warn else "all mounts healthy"),
                            chk, {"mounts": mounts, "root_free_h": root_free, "root_days": root_days}, [{k: r[k] for k in ("mount", "free_h", "used_pct", "days_h", "level")} for r in mounts[:12]])
    t["failed_units"] = _t("Services & containers", "C0", "check", "warn" if warn else "ok",
                           "warn: 1 failed unit(s): nginx.service" if warn else "ok: no failed units, 71 containers fine",
                           chk, {"failed_units": 1 if warn else 0, "failed_units_stale": 1 if warn else 0, "exited_unexpected": 0, "unhealthy": 0,
                                 "restarting": 0, "containers_total": 71, "level": "warn" if warn else "ok"})
    def brow(name, age_h, limit, result="ok"):
        late = age_h > limit
        return {"name": name, "age_h": age_h, "age": f"{age_h / 24:.0f}d" if age_h >= 48 else f"{age_h:.0f}h", "result": result,
                "limit_h": limit, "level": "warn" if late else "ok"}
    t["backup_freshness"] = _t("Backups", "C0", "check", "warn" if warn else "ok",
                               "warn: backup-immich 9d old (limit 200h)" if warn else "ok: system 3d, immich 3d, stack-backup 9h", chk,
                               {"backups": [brow("backup-system", 54.2, 200), brow("backup-immich", 210.0 if warn else 71.5, 200),
                                            brow("stack-backup", 9.5, 26)], "failed": 0, "level": "warn" if warn else "ok"})
    t["docker_df"] = _t("Docker disk", "C0", "check", "warn" if warn else "ok",
                        "warn: build cache 31.2 GiB (>= 25 GiB)" if warn else "ok: images 142.3 GiB (38.8 GiB reclaimable), build cache 9.4 GiB",
                        chk, {"images_gib": 142.3, "images_h": "142.3 GiB", "images_reclaim_h": "38.8 GiB", "containers_h": "2.1 GiB",
                              "volumes_h": "611.8 GiB", "build_cache_gib": 31.2 if warn else 9.4,
                              "build_cache_h": "31.2 GiB" if warn else "9.4 GiB", "build_cache_reclaim_h": "22.1 GiB",
                              "safe_reclaim_h": "60.9 GiB", "level": "warn" if warn else "ok"}, alert=False)
    t["memory_health"] = _t("Memory pressure", "C0", "check", "crit" if crit else "ok",
                            "crit: memory stall 22.0%, only 3.1 GiB available (cache 40.2 GiB is reclaimable; io 14%)" if crit else
                            "ok: 70.1 GiB avail; cache 60.4 GiB is reclaimable; swap 25.0 GiB used is harmless alone; mem stall 0.0%",
                            chk, {"mem_available_h": "3.1 GiB" if crit else "70.1 GiB", "swap_used_h": "30.2 GiB" if crit else "25.0 GiB",
                                  "psi_mem_full60": 22.0 if crit else 0.0, "psi_io_some60": 14.5 if crit else 1.2,
                                  "swap_in_pps": 5400 if crit else 0, "oom_kills_total": 5 if crit else 4, "oom_kills_delta": 1 if crit else 0})
    t["plex_media_mount_check"] = _t(
        "Plex Media mount", "C0", "check", "crit" if crit else "info",
        "crit: Media dir is not mounted (SSD path exists); Plex would regenerate Media on the root disk (47.0 GiB there now)" if crit
        else "info: Plex not migrated yet (no bind mount, no /media/SandiskSSD/plex)",
        chk, {"mounted": False, "source": "", "media_size_root_gib": 47.0}, alert=crit)
    def dev(name, model, temp, realloc, power, note=""):
        return {"dev": name, "model": model, "temp_c": temp, "realloc": realloc, "pending": 0, "crc": 0, "power_on_h": power,
                "level": "ok", "note": note}
    t["smart_trend"] = _t("SMART trend", "C0", "check", "ok", "SMART ok: 4 disks, no 5/197/198/CRC growth, hottest nvme1n1 52C", chk,
                          {"devices": [dev("nvme0n1", "Samsung 990 PRO 2TB", 47, 0, 9120), dev("nvme1n1", "WD Black SN850X", 52, 0, 6410),
                                       dev("sda", "ST18000NM000J", 34, 0, 31240), dev("sdb", "WDC WD240EDGZ", 38, 8, 40110, "realloc stable")],
                           "n_disks": 4, "n_problems": 0, "hottest_dev": "nvme1n1", "hottest_c": 52})
    t["alert_path_health"] = _t("Alert path", "C0", "check", "warn" if warn else "ok",
                                "alert path: smart hook: 3 sends failed in 24h, last 06:12 rc=1: smtp auth" if warn else "alert path ok; last SMART send 02:14", chk,
                                {"bridge_ok": True, "smart_fail_24h": 3 if warn else 0, "notify_fail_24h": 0})
    gpaths = [{"path": "/var/lib/docker/containers", "size": "4.1 GiB", "rate": "+0.08 GiB/d", "limit": "0.5 GiB/d", "level": "ok"},
              {"path": "/var/log", "size": "2.4 GiB", "rate": "+0.03 GiB/d", "limit": "0.5 GiB/d", "level": "ok"},
              {"path": "/volume1/docker/kavita/config/logs", "size": "120.0 MiB", "rate": "+0.00 GiB/d", "limit": "0.5 GiB/d", "level": "ok"},
              {"path": "/var/log/journal", "size": "n/a", "rate": "n/a", "limit": "0.5 GiB/d", "level": "info"}]
    t["growth_watch"] = _t("Growth watch", "C0", "check", "ok", "growth ok: 3 paths, fastest docker/containers +0.08 GiB/d", chk,
                           {"paths": gpaths, "n_over": 0}, gpaths)
    t["spike_sampler"] = _t("Memory spike sampler", "C0", "check", "ok", "71 containers, 23.1 GiB anon; top: ollama 9.8G, immich_machine_learning 3.2G", chk,
                            {"containers": 71, "skipped": 0, "anon_total_gib": 23.1, "anon_total_h": "23.1 GiB",
                             "largest": [{"name": "ollama", "anon_h": "9.8 GiB"}, {"name": "immich_machine_learning", "anon_h": "3.2 GiB"},
                                         {"name": "comfyui", "anon_h": "2.4 GiB"}]})
    cands = [{"name": "tunarr-host-net", "anon_h": "14.2 GiB", "swap_h": "0 B", "growth_gib_h": 0.8, "cpu_pct": 0.1, "io_kib_min": 3,
              "reason": "leak/runaway: memory growing with no CPU or IO progress", "protected": True, "busy": ""},
             {"name": "some-app", "anon_h": "5.1 GiB", "swap_h": "0 B", "growth_gib_h": 0.0, "cpu_pct": 0.0, "io_kib_min": 0,
              "reason": "idle but holding: large and flat, no CPU or IO progress", "protected": False, "busy": ""}]
    t["stuck_detector"] = _t(
        "Stuck containers", "C0", "check", "warn" if warn else "ok",
        "2 candidate(s), 1 actionable: some-app 5.1 GiB idle but holding" if warn else "ok: no stuck containers (12 samples, 165 min)", chk,
        {"samples": 12, "window_min": 165, "candidates": 2 if warn else 0, "actionable": 1 if warn else 0, "pressure": "none", "enforce": False},
        cands if warn else [])
    t["orphan_report"] = _t("Orphaned processes", "C0", "check", "info" if warn else "ok",
                            "2 idle gradle daemons, 1 stray server (1.9 GiB anon)" if warn else "no orphaned or idle build processes", chk,
                            {"gradle_idle": 2 if warn else 0, "findings": 3 if warn else 0}, alert=False)
    t["image_ledger"] = _t("Image usage ledger", "C0", "check", "ok", "58 images in use, 38 unreferenced on host, ledger 412 entries", chk,
                           {"tracked": 412, "referenced": 58})
    t["config_drift"] = _t("Config drift", "C0", "weekly", "info", "2 drift(s): journald SystemMaxUse unset; tune2fs reserved blocks 5%" if warn else "no config drift",
                           weekly, alert=False)

    # ---- C1 cleaners (daily): two apply, the rest report (a report-mode run with findings is "info")
    def c1(name, title, noun, would=0.0, n=0, freed=0.0):
        applied = freed > 0
        if applied:
            t[name] = _t(title, "C1", "daily", "ok", f"freed {_h(freed * GIB)} ({n} {noun})", daily,
                         {"mode": "apply", "selected": n, "selected_h": _h(freed * GIB), "freed_h": _h(freed * GIB), "protected": 0,
                          "failed": 0}, mode="apply", freed=int(freed * GIB))
        else:
            t[name] = _t(title, "C1", "daily", "info" if n else "ok", f"report: would free {_h(would * GIB)} ({n} {noun})", daily,
                         {"mode": "report", "selected": n, "selected_h": _h(would * GIB), "freed_h": "0 B", "protected": 0, "failed": 0})
    c1("docker_cache", "Docker build cache", "builders", freed=6.2, n=2)
    c1("docker_images", "Unused Docker images", "images", 14.9, 6)
    c1("apt_clean", "APT package cache", "files", 0.8, 1)
    c1("snap_revisions", "Old snap revisions", "revisions", freed=1.4, n=2)
    c1("retention", "Retention rules", "files", 3.6, 214)
    c1("trash", "Trash", "entries", 1.1, 38)
    c1("gradle_reaper", "Idle Gradle/Kotlin daemons", "daemons", 1.9, 2)
    c1("caps", "Container memory ceilings", "containers", 0, 0)

    # ---- C2 weekly plan
    items = [{"name": "home-venv", "bytes": int(11.3 * GIB), "needs_manual_check": False},
             {"name": "kometa-backup-tarball", "bytes": int(10.8 * GIB), "needs_manual_check": False},
             {"name": "android-fold-avd", "bytes": int(9.3 * GIB), "needs_manual_check": False},
             {"name": "cursor-worktrees", "bytes": int(2.2 * GIB), "needs_manual_check": True},
             {"name": "hermes-genesis-gguf", "bytes": int(18.5 * GIB), "needs_manual_check": True},
             {"name": "orphan-docker-plex-config", "bytes": int(1.1 * GIB), "needs_manual_check": False}]
    plan = {"items": sorted(items, key=lambda r: r["name"]), "total_bytes": sum(r["bytes"] for r in items)}
    from homelab_maint.core import plan_hash
    t["c2_candidates"] = _t("Cleanup candidates", "C2", "weekly", "info", f"6 candidates, {_h(plan['total_bytes'])}; plan {plan_hash(plan)}",
                            weekly, {"mode": "report", "candidates": 6, "total_h": _h(plan["total_bytes"]), "plan_hash": plan_hash(plan)},
                            alert=False)
    t["c2_candidates"].update(plan=plan, plan_hash=plan_hash(plan))
    if state == "crit":                                   # one check that failed to run at all
        t["growth_watch"].update(status="error", summary="TimeoutError: timed out after 120s")

    worst = 0
    for e in t.values():
        if e["alert"]:
            worst = max(worst, {"ok": 0, "info": 0, "skipped": 0, "warn": 1, "crit": 2, "error": 2}[e["status"]])
    return {
        "schema": 1, "generated_at": gen, "host": "ohmz-ai", "paused": False, "overall": ("ok", "warn", "crit")[worst],
        "tier_runs": {"check": {"last_run": chk, "dry_run": True}, "daily": {"last_run": daily, "dry_run": False},
                      "weekly": {"last_run": weekly, "dry_run": False}},
        "reclaimed_log": [
            {"t": daily + 90, "task": "docker_cache", "bytes": int(6.2 * GIB)},
            {"t": daily + 150, "task": "snap_revisions", "bytes": int(1.4 * GIB)},
            {"t": daily - 86400 + 90, "task": "docker_cache", "bytes": int(4.8 * GIB)},
            {"t": daily - 2 * 86400 + 120, "task": "apt_clean", "bytes": int(0.9 * GIB)},
            {"t": daily - 5 * 86400 + 300, "task": "docker_images", "bytes": int(11.6 * GIB)},
            {"t": daily - 20 * 86400 + 300, "task": "retention", "bytes": int(2.3 * GIB)},
        ] if state != "ok" else [],
        "tasks": copy.deepcopy(t),
    }


def stress(now: int = NOW) -> dict:
    """Worst case for the size budget: every list full, every string at maximum length."""
    st = build("warn", now)
    long = "x" * 140
    for e in st["tasks"].values():
        e["summary"], e["title"] = long, "T" * 40
    d = st["tasks"]["disk_forecast"]["metrics"]
    d["mounts"] = [dict(d["mounts"][i % 12], mount="/media/" + "m" * 60 + str(i)) for i in range(30)]
    st["tasks"]["stuck_detector"]["items"] = [{"name": "c" * 60, "anon_h": "999.9 GiB", "reason": "r" * 80, "protected": i % 2 == 0,
                                               "busy": "" if i % 2 else "b" * 50} for i in range(12)]
    st["tasks"]["backup_freshness"]["metrics"]["backups"] = [{"name": "b" * 40, "age_h": 9999, "result": "failed", "level": "crit"}] * 8
    st["reclaimed_log"] = [{"t": now - i * 3600, "task": "docker_cache", "bytes": 5 * GIB} for i in range(200)]
    for i in range(40):
        st["tasks"][f"extra_{i}"] = {"title": "E" * 40, "klass": "C1" if i % 2 else "C0", "tier": "daily", "status": "warn",
                                     "summary": long, "last_run": now - 5 * 86400, "metrics": {}, "items": [], "alert": True,
                                     "mode": "dry-run", "reclaimed_bytes": 3 * GIB}
    for e in st["tasks"].values():                          # report-mode cleaners with pending bytes
        if e["klass"] == "C1":
            e["metrics"] = {"mode": "report", "selected": 9, "selected_h": "999.9 GiB"}
    return st


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(build(sys.argv[1]), indent=1, sort_keys=True))
    else:
        out = Path(__file__).with_name("sample-status.json")
        out.write_text(json.dumps(build("warn"), indent=1, sort_keys=True) + "\n")
        print(f"wrote {out}")
