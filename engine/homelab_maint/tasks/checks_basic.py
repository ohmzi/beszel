"""checks_basic: the everyday read-only (C0) health checks.

disk_forecast, failed_units, backup_freshness, docker_df, memory_health, plex_media_mount_check.

Every probe fails towards "tell somebody" (warn/skipped with a reason), never towards "do
something": nothing in this module mutates the host.  Formats parsed here were inspected on the
real machine (backup status JSON, `docker system df --format json`, `docker buildx du`,
/proc/pressure/*, /proc/vmstat, /proc/self/mountinfo, `systemctl show --timestamp=unix`).

Things that look odd but are deliberate (each one fixed a reviewed defect):
  * disk_forecast does NOT use a plain least-squares slope: one bulk copy or image pull inside the
    window would read as continuous consumption.  See `_days_until_full`.
  * docker_df runs `docker buildx` as `buildx_user`: builders are stored per user, and the timers
    run as root, which only sees the `default` builder.
  * One-run findings (OOM kill, a container restart) are held for `event_hold_h`, otherwise they
    would be gone before the 2-run alert confirmation could ever page.
  * plex_media_mount_check remembers that the bind mount was once seen (`was_mounted`) until the
    owner says `expect_mounted = false`.
"""
from __future__ import annotations

import glob
import json
import os
import pwd
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .. import core, swapwatch
from ..core import GIB, Ctx, Result, human, read_history, read_json, sh, task

_RANK = {"ok": 0, "info": 0, "warn": 1, "crit": 2}


# --------------------------------------------------------------------------- small helpers
def _fit(s: str, n: int = 140) -> str:
    """Summaries may become an SMS: ASCII only, <= 140 chars."""
    return re.sub(r"[^\x20-\x7e]", "?", s)[:n]


def _status(levels) -> str:
    levels = list(levels)
    return "crit" if "crit" in levels else "warn" if "warn" in levels else "ok"


def _age_str(minutes: float) -> str:
    m = int(max(minutes, 0))
    if m >= 1440:
        return f"{m // 1440}d{(m % 1440) // 60}h"
    if m >= 60:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{m}m"


def _gib(n: float) -> float:
    return round(n / GIB, 2)


def _held(events, now: float, hold_s: float) -> list[list[float]]:
    """Events `[[t, n], ...]` kept in ctx.state that are younger than the hold window.

    Tolerates a missing or damaged list (state files are hand-editable): bad rows are dropped.
    """
    out = []
    for e in events if isinstance(events, list) else []:
        try:
            t, n = float(e[0]), int(e[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if now - t < hold_s:
            out.append([t, n])
    return out


def _read_proc(path: str) -> str:
    """Indirection so tests can feed fake /proc content."""
    with open(path) as f:
        return f.read()


def _json_objects(text: str) -> list[dict]:
    """Parse JSON-lines, a JSON array, or concatenated objects; ignore anything else."""
    text = text.strip()
    out: list = []
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            break
        out.extend(obj if isinstance(obj, list) else [obj])
    return [o for o in out if isinstance(o, dict)]


# =========================================================================== disk_forecast
_MIN_SAMPLES = 6          # spec: need >= 6 samples spanning >= 6 h before forecasting
_MIN_SPAN_S = 6 * 3600
_MAX_FORECAST_DAYS = 3650  # slower than this is "not shrinking" for any practical purpose


def _disk_usage(path: str) -> dict | None:
    """df-style numbers for a *mounted* path (None if not a mountpoint or unreadable)."""
    try:
        if not os.path.ismount(path):
            return None            # an unmounted dir would silently report the parent disk
        s = os.statvfs(path)
    except OSError:
        return None
    fr = s.f_frsize or s.f_bsize
    used = (s.f_blocks - s.f_bfree) * fr
    free = s.f_bavail * fr         # like df: excludes ext4 reserved blocks
    if used + free <= 0:
        return None
    return {"free": free, "used": used}


def _hourly(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """One point per clock hour (mean time, median free): damps the 15-minute sampling noise."""
    buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for t, f in pts:
        buckets[int(t // 3600)].append((t, f))
    return [(sum(t for t, _ in v) / len(v), statistics.median(f for _, f in v))
            for _, v in sorted(buckets.items())]


def _destep(pts: list[tuple[float, float]], capacity: float | None, step_pct: float) -> list[tuple[float, float]]:
    """Cut out one-off jumps (bulk copy, image pull, big delete) so only the background rate remains.

    An hour-to-hour change of at least `step_pct` of the capacity is an EVENT, not a trend: it is
    replaced by "no change" and the series continues from there.  Trade-off: a runaway that eats
    >= step_pct of the disk per hour is not forecast, but then the free-percent thresholds page
    within hours anyway.
    """
    if not capacity or step_pct <= 0 or not pts:
        return pts
    limit = capacity * step_pct / 100.0
    out = [pts[0]]
    for (_, f0), (t1, f1) in zip(pts, pts[1:]):
        d = f1 - f0
        out.append((t1, out[-1][1] + (0.0 if abs(d) >= limit else d)))
    return out


def _robust_slope(pts: list[tuple[float, float]]) -> float | None:
    """Theil-Sen (median of all pairwise slopes, bytes/s) of hourly points; None if too little data.

    Unlike least squares it ignores a minority (< ~29 %) of the window that moved differently, e.g.
    a finished copy that left the free space flat.
    """
    if len(pts) < _MIN_SAMPLES or pts[-1][0] - pts[0][0] < _MIN_SPAN_S:
        return None
    slopes = [(y2 - y1) / (t2 - t1) for i, (t1, y1) in enumerate(pts) for t2, y2 in pts[i + 1:] if t2 > t1]
    return statistics.median(slopes) if slopes else None


def _days_until_full(samples: list[tuple[float, float]], free_now: float, now: float, window_s: float,
                     *, capacity: float | None = None, recent_s: float = 48 * 3600,
                     step_pct: float = 3.0) -> float | None:
    """Days until free space hits zero at the BACKGROUND consumption rate, or None (no forecast).

    Plain least squares over a week turns any one-off step (a 663 GiB copy that finished days ago and
    left a flat line) into a "full in 3 days" forecast.  Instead:
      1. hourly medians, 2. one-off jumps >= step_pct of capacity removed, 3. Theil-Sen slope over the
      whole window AND over the most recent `recent_s`; the forecast needs BOTH to be falling and uses
      the slower of the two, so consumption that has already stopped (or been cleaned up) is not forecast.
    Cost: a brand-new runaway is only forecast once it fills ~30 % of the window; the free-percent
    thresholds in disk_forecast are the fast path for that.
    """
    pts = _destep(_hourly(sorted(p for p in samples if p[0] >= now - window_s)), capacity, step_pct)
    s_long = _robust_slope(pts)
    s_recent = _robust_slope([p for p in pts if p[0] >= now - min(recent_s, window_s)])
    if s_long is None or s_recent is None or s_long >= 0 or s_recent >= 0:
        return None                # free space is flat/growing, or too little history to say
    days = free_now / (min(-s_long, -s_recent) * 86400)
    return days if days <= _MAX_FORECAST_DAYS else None


def _disk_level(free_pct: float, days: float | None, o: dict) -> str:
    if free_pct <= o["crit_pct"] or (days is not None and days <= o["crit_days"]):
        return "crit"
    if free_pct <= o["warn_pct"] or (days is not None and days <= o["warn_days"]):
        return "warn"
    return "ok"


@task("disk_forecast", klass="C0", tier="check", title="Disk space", timeout=120)
def disk_forecast(ctx: Ctx) -> Result:
    o = {"warn_pct": float(ctx.opt("warn_free_pct", 12)), "crit_pct": float(ctx.opt("crit_free_pct", 6)),
         "warn_days": float(ctx.opt("warn_days", 21)), "crit_days": float(ctx.opt("crit_days", 7))}
    window_s = float(ctx.opt("trend_window_hours", 168)) * 3600
    recent_s = float(ctx.opt("trend_recent_hours", 48)) * 3600     # consumption must still be happening now
    step_pct = float(ctx.opt("step_pct", 3.0))                     # hourly jump (% of capacity) = one-off event
    watch = list(ctx.opt("watch", []) or [])
    info_only = [m for m in (ctx.opt("info_only", []) or []) if m not in watch]

    # History is read BEFORE appending so the current sample is added exactly once, in memory.
    hist: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in read_history(window_s + 3600, "disk"):
        try:
            hist[r["mount"]].append((float(r["t"]), float(r["free"])))
        except (KeyError, TypeError, ValueError):
            continue

    rows: list[tuple[dict, float]] = []          # (metrics row, exact free_pct)
    for is_info, mounts in ((False, watch), (True, info_only)):
        for m in mounts:
            u = _disk_usage(m)
            if u is None:
                continue                          # not mounted: skip, do not guess
            total = u["used"] + u["free"]
            free_pct = 100.0 * u["free"] / total
            days = None
            if not is_info:                       # history (and so a forecast) only for watch mounts
                try:
                    core.append_history({"t": ctx.now, "kind": "disk", "mount": m, "free": u["free"]})
                except OSError:
                    pass
                days = _days_until_full(hist[m] + [(ctx.now, float(u["free"]))], u["free"], ctx.now, window_s,
                                        capacity=total, recent_s=recent_s, step_pct=step_pct)
            level = "info" if is_info else _disk_level(free_pct, days, o)
            rows.append(({"mount": m, "free": u["free"], "size": total, "free_h": human(u["free"]),
                          "used_pct": round(100.0 * u["used"] / total, 1),
                          "days": None if days is None else round(days, 1),
                          "days_h": "n/a" if days is None else f"{days:.0f} d",
                          "level": level, "info": is_info}, free_pct))
    if not rows:
        return Result("warn" if watch else "skipped", "no configured mount is mounted/readable",
                      {"mounts": []})

    # watch mounts first (worst first), then the dashboard-only ones by least free space
    rows.sort(key=lambda rp: (rp[0]["info"], -_RANK[rp[0]["level"]], rp[1]))
    mounts_out = [r for r, _ in rows]
    watched = [(r, p) for r, p in rows if not r["info"]]
    status = _status(r["level"] for r, _ in watched)
    bad = [(r, p) for r, p in watched if r["level"] != "ok"]
    if bad:
        parts = [f"{r['mount']} {p:.0f}% free ({r['free_h']})" + (f", full in {r['days']:.0f}d" if r["days"] is not None else "")
                 for r, p in bad[:3]]
        summary = f"{status}: " + "; ".join(parts) + (f"; +{len(bad) - 3} more" if len(bad) > 3 else "")
    elif watched:
        r, p = min(watched, key=lambda rp: rp[1])
        summary = f"ok: {len(watched)} mounts watched; lowest free {r['mount']} {p:.0f}% ({r['free_h']})"
    else:
        summary = f"ok: no watched mounts available ({len(rows)} info-only)"
    root = next((r for r in mounts_out if r["mount"] == "/"), None)
    return Result(status, _fit(summary), {
        "mounts": mounts_out,
        "root_free_h": root["free_h"] if root else "n/a",
        "root_days": root["days"] if root else None,
    }, items=[{k: r[k] for k in ("mount", "free_h", "used_pct", "days_h", "level")} for r in mounts_out[:12]],
        issue_key=core.ikey(mounts=[r["mount"] for r, _ in bad]))        # SPEC5: ALL failing mounts, not the 3 the summary lists; never the percentages


# =========================================================================== failed_units
# Units that are known to be failed on this host; still reported, with the reason attached.
_KNOWN_UNITS = {
    "nginx.service": "known failure on this host; check `nginx -t`",
    "plexmediaserver.service": "apt Plex unit; it can steal port 32400 when the snap is down",
}
_RESTART_STATES = {"restarting"}


def _failed_system_units() -> tuple[list[str], str | None]:
    r = sh(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=30)
    if r.returncode != 0:
        return [], f"systemctl rc={r.returncode}"
    units = []
    for ln in r.stdout.splitlines():
        tok = ln.replace("●", " ").split(None, 1)       # defensive: bullet marker without --plain
        if tok and "." in tok[0]:
            units.append(tok[0])
    return units, None


def _unit_state_change(units: list[str]) -> dict[str, float]:
    """Unit -> epoch of its last state change (when it entered `failed`). Missing = unknown."""
    if not units:
        return {}
    cmd = ["systemctl", "show", "--timestamp=unix", "-p", "Id", "-p", "StateChangeTimestamp", *units]
    r = sh(cmd, timeout=30)
    out: dict[str, float] = {}
    for block in r.stdout.split("\n\n"):
        kv = dict(ln.split("=", 1) for ln in block.splitlines() if "=" in ln)
        ts = kv.get("StateChangeTimestamp", "").lstrip("@")
        try:
            out[kv["Id"]] = float(ts)
        except (KeyError, ValueError):
            continue
    return out


def _containers() -> tuple[list[dict], str | None]:
    """All containers (running or not) with state, status text. One docker call."""
    r = sh(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=30)
    if r.returncode != 0:
        return [], f"docker ps rc={r.returncode}"
    out = []
    for ln in r.stdout.splitlines():
        name, _, rest = ln.partition("|")
        state, _, status = rest.partition("|")
        if name:
            out.append({"name": name, "state": state.strip().lower(), "status": status})
    return out, None


def _restart_counts(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(names), 100):
        r = sh(["docker", "inspect", "--format", "{{.Name}}|{{.RestartCount}}", *names[i:i + 100]], timeout=30)
        for ln in r.stdout.splitlines():       # parse even on rc!=0: a container may vanish mid-call
            n, _, c = ln.partition("|")
            try:
                out[n.lstrip("/")] = int(c)
            except ValueError:
                continue
    return out


@task("failed_units", klass="C0", tier="check", title="Services & containers", timeout=90)
def failed_units(ctx: Ctx) -> Result:
    min_age_s = float(ctx.opt("failed_min_age_min", 30)) * 60
    ignore = set(ctx.opt("ignore_units", []) or [])
    expected_stopped = set(ctx.opt("expected_stopped_containers", []) or [])
    items: list[dict] = []
    problems: list[str] = []
    probe_errors: list[str] = []

    # ---- systemd (system instance only; `systemctl --user` does not exist for root)
    units, err = _failed_system_units()
    if err:
        probe_errors.append(err)
    units = [u for u in units if u not in ignore]
    changed = _unit_state_change(units)
    stale_units, recent_units = [], []
    for u in units:
        age_s = ctx.now - changed[u] if u in changed else None
        if age_s is None or age_s > min_age_s:      # unknown age counts as old: fail towards telling
            stale_units.append(u)
            items.append({"kind": "unit", "name": u, "age": "?" if age_s is None else _age_str(age_s / 60),
                          "note": _KNOWN_UNITS.get(u, ""), "level": "warn"})
        else:
            recent_units.append(u)
    if stale_units:
        problems.append(f"{len(stale_units)} failed unit(s): " + ", ".join(stale_units[:4]))

    # ---- containers
    conts, err = _containers()
    if err:
        probe_errors.append(err)
    exited = [c for c in conts if c["state"] in ("exited", "dead") and c["name"] not in expected_stopped]
    unhealthy = [c for c in conts if "(unhealthy)" in c["status"]]
    restarting = [c["name"] for c in conts if c["state"] in _RESTART_STATES]
    # A restart is a one-off: RestartCount grows once and the baseline then catches up, so a plain
    # "grew since last run" is true for a single 15 min run and could never pass the 2-run alert
    # confirmation.  Growth is therefore kept as events and reported for `event_hold_h`.
    hold_h = float(ctx.opt("event_hold_h", 6))
    ev_raw = ctx.state.get("restart_events")
    events = {n: _held(v, ctx.now, hold_h * 3600) for n, v in (ev_raw if isinstance(ev_raw, dict) else {}).items()}
    if conts:
        counts = _restart_counts([c["name"] for c in conts])
        prev = ctx.state.get("restart_counts")
        prev = {n: c for n, c in prev.items() if isinstance(c, int)} if isinstance(prev, dict) else {}   # hand-edited state
        for n, c in counts.items():
            if n in prev and c > prev[n]:
                events.setdefault(n, []).append([ctx.now, c - prev[n]])   # first sight is a baseline, not a finding
        if counts:
            ctx.state["restart_counts"] = counts
            events = {n: v for n, v in events.items() if n in counts}     # container removed: nothing to report
    events = {n: v for n, v in events.items() if v}
    ctx.state["restart_events"] = events
    grew = {n: sum(k for _, k in v) for n, v in events.items()}           # restarts inside the hold window
    for c in exited[:4]:
        items.append({"kind": "exited", "name": c["name"], "age": "", "note": c["status"][:40], "level": "warn"})
    for c in unhealthy[:4]:
        items.append({"kind": "unhealthy", "name": c["name"], "age": "", "note": c["status"][:40], "level": "warn"})
    for n in sorted(set(restarting) | set(grew))[:4]:
        note = f"{grew[n]} restart(s) in last {hold_h:g}h" if n in grew else "restarting now"
        items.append({"kind": "restarting", "name": n, "age": "", "note": note, "level": "warn"})
    if exited:
        problems.append(f"{len(exited)} unexpected exited: " + ", ".join(c["name"] for c in exited[:3]))
    if unhealthy:
        problems.append(f"{len(unhealthy)} unhealthy: " + ", ".join(c["name"] for c in unhealthy[:3]))
    if restarting or grew:
        problems.append(f"{len(set(restarting) | set(grew))} restarting: " + ", ".join(sorted(set(restarting) | set(grew))[:3]))

    metrics = {"failed_units": len(units), "failed_units_stale": len(stale_units),
               "failed_units_recent": len(recent_units),
               "known_failed": sum(1 for u in stale_units if u in _KNOWN_UNITS),
               "exited_unexpected": len(exited), "unhealthy": len(unhealthy),
               "restarting": len(set(restarting) | set(grew)), "restarts_recent": sum(grew.values()),
               "containers_total": len(conts),
               "probe_errors": probe_errors}
    if problems:
        metrics["level"] = "warn"
        key = core.ikey(units=stale_units, exited=[c["name"] for c in exited], unhealthy=[c["name"] for c in unhealthy],
                        restarting=set(restarting) | set(grew))          # SPEC5: the FULL sets (the summary names 3-4 of each), never ages or restart counts
        return Result("warn", _fit("warn: " + "; ".join(problems)), metrics, items[:12], issue_key=key)
    if probe_errors:                                  # blind is not healthy
        metrics["level"] = "warn"
        return Result("warn", _fit("warn: probe failed: " + ", ".join(probe_errors)), metrics, items[:12])
    metrics["level"] = "ok"
    extra = f"; {len(recent_units)} unit(s) failed <{int(min_age_s // 60)}m ago" if recent_units else ""
    return Result("ok", _fit(f"ok: no failed units, {len(conts)} containers fine{extra}"), metrics, items[:12])


# =========================================================================== backup_freshness
_FINISH_KEYS = ("finished", "finished_at", "ended", "end", "completed", "timestamp")


def _parse_ts(v) -> float | None:
    """'2026-09-27 01:31:22' (host-local time, as the backup scripts write it), ISO 'T', or epoch."""
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    if not isinstance(v, str) or len(v) < 19:
        return None
    try:
        return time.mktime(time.strptime(v[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _limit_for(limits, default: float, *keys: str) -> float:
    if isinstance(limits, (int, float)):
        return float(limits)
    if isinstance(limits, dict):
        for k in keys:
            if k in limits:
                return float(limits[k])
    return default


@task("backup_freshness", klass="C0", tier="check", title="Backups", timeout=60)
def backup_freshness(ctx: Ctx) -> Result:
    limits = ctx.opt("max_age_hours", {})
    default_h = float(ctx.opt("default_max_age_hours", 200))
    rows: list[dict] = []

    for path in sorted(glob.glob(ctx.opt("status_glob", "/var/log/backup/*-status.json"))):
        # The FAILED-*.txt marker files are never read: they persist forever after one failure.
        data = read_json(Path(path))
        stem = Path(path).name.removesuffix(".json").removesuffix("-status")
        if not isinstance(data, dict):
            rows.append({"name": f"backup-{stem}", "age_h": None, "age": "?", "result": "unreadable",
                         "limit_h": default_h, "level": "warn"})
            continue
        job = str(data.get("job") or stem)
        name = f"backup-{job}"
        finished = next((t for t in (_parse_ts(data.get(k)) for k in _FINISH_KEYS) if t), None)
        if finished is None:
            try:
                finished = os.stat(path).st_mtime
            except OSError:
                finished = None
        result = str(data.get("result", "unknown")).lower()
        limit = _limit_for(limits, default_h, name, job, stem)
        age_h = None if finished is None else max(ctx.now - finished, 0) / 3600
        if result != "ok":
            level = "crit"
        elif age_h is None or age_h > limit:
            level = "warn"
        else:
            level = "ok"
        rows.append({"name": name, "age_h": None if age_h is None else round(age_h, 1),
                     "age": "?" if age_h is None else _age_str(age_h * 60),
                     "result": result, "limit_h": limit, "level": level})

    last_ok = ctx.opt("stack_backup_last_ok")
    if last_ok:                                       # nightly stack backup stamps this file on success
        limit = float(ctx.opt("stack_backup_max_hours", 26))
        try:
            age_h = max(ctx.now - os.stat(last_ok).st_mtime, 0) / 3600
            rows.append({"name": "stack-backup", "age_h": round(age_h, 1), "age": _age_str(age_h * 60),
                         "result": "ok", "limit_h": limit, "level": "warn" if age_h > limit else "ok"})
        except OSError:
            rows.append({"name": "stack-backup", "age_h": None, "age": "?", "result": "missing",
                         "limit_h": limit, "level": "warn"})

    if not rows:
        return Result("warn", "no backup status files found", {"backups": [], "failed": 0, "level": "warn"})
    status = _status(r["level"] for r in rows)
    bad = [r for r in rows if r["level"] != "ok"]
    if bad:
        def why(r):
            if r["result"] not in ("ok",):
                return f"{r['name']} {r['result'].upper()} ({r['age']} ago)"
            return f"{r['name']} {r['age']} old (limit {r['limit_h']:.0f}h)"
        more = f"; +{len(bad) - 3} more" if len(bad) > 3 else ""                        # a 4th late backup must not be invisible (an ack keys the marker)
        summary = _fit(f"{status}: " + "; ".join(why(r) for r in bad[:3]), 140 - len(more)) + more
    else:
        summary = "ok: " + ", ".join(f"{r['name'].removeprefix('backup-')} {r['age']}" for r in rows)
    ages = [r["age_h"] for r in rows if r["age_h"] is not None]
    # SPEC5: every bad backup (the summary shows 3). A failed one is "name=RESULT"; a late one carries the DECADE of its age in hours, because a
    # backup a day late and a month late are both "warn": "name=b2" (10-99 h) alerts again at "name=b3" (100-999 h), never on the hour count.
    key = core.ikey(backups=[f"{r['name']}={r['result'].upper()}" if r["result"] != "ok"
                             else f"{r['name']}=b{len(str(int(r['age_h'])))}" if r["age_h"] is not None else f"{r['name']}=?" for r in bad])
    return Result(status, _fit(summary), {
        "backups": rows, "worst_age_h": max(ages) if ages else None,
        "failed": sum(1 for r in rows if r["result"] not in ("ok",)), "level": status,
    }, items=[{k: r[k] for k in ("name", "age", "result", "level")} for r in rows[:12]], issue_key=key)


# =========================================================================== docker_df
_SIZE_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "pb": 1e15,
               "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30, "tib": 2 ** 40}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)")


def parse_size(s) -> int | None:
    """Docker prints SI units ('17.87GB (17%)', '251.7kB', '0B'); returns bytes."""
    m = _SIZE_RE.match(str(s or ""))
    if not m:
        return None
    mult = _SIZE_UNITS.get(m.group(2).lower() or "b")
    return None if mult is None else int(float(m.group(1)) * mult)


def _buildx(args: list[str], user: str, timeout: int):
    """Run `docker buildx ARGS` as `user`, the owner of the builders.

    buildx stores builder definitions per user (~/.docker/buildx).  The timers run as root, and root's
    `buildx ls` only shows the `default` builder, so the extra builders (and their cache) would be
    silently missed.  Hence runuser, with HOME set the way core.Notifier does.  Never point
    DOCKER_CONFIG at the user's dir as root: that would create root-owned files in it.
    Not root (a manual run as the owner) or no user configured: run directly.
    """
    cmd = ["docker", "buildx", *args]
    if not user or os.geteuid() != 0 or user == "root":
        return sh(cmd, timeout=timeout)
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = f"/home/{user}"
    return sh(["runuser", "-u", user, "--", *cmd], timeout=timeout, env={"HOME": home})


def _extra_builders(user: str = "") -> tuple[list[dict], list[str]]:
    """Cache held by non-default buildx builders (the default builder IS `Build Cache` in system df).

    Returns (builders, errors).  A failed probe is reported rather than treated as "no cache":
    the total is wrong without it.
    """
    r = _buildx(["ls", "--format", "json"], user, 15)
    if r.returncode != 0:
        return [], [f"buildx ls rc={r.returncode}"]
    out, errors = [], []
    for b in _json_objects(r.stdout)[:4]:
        name = b.get("Name")
        if not name or b.get("Driver") == "docker":
            continue
        du = _buildx(["du", "--builder", str(name)], user, 20)
        total = reclaim = None
        for ln in du.stdout.splitlines():
            k, _, v = ln.partition(":")
            if k.strip() == "Total":
                total = parse_size(v)
            elif k.strip() == "Reclaimable":
                reclaim = parse_size(v)
        if du.returncode == 0 and total is not None:
            out.append({"name": str(name), "size": total, "reclaim": reclaim if reclaim is not None else 0})
        else:
            errors.append(f"buildx du {name} rc={du.returncode}")
    return out, errors


@task("docker_df", klass="C0", tier="check", title="Docker disk", timeout=90)
def docker_df(ctx: Ctx) -> Result:
    r = sh(["docker", "system", "df", "--format", "json"], timeout=60)
    rows = {d["Type"]: d for d in _json_objects(r.stdout) if d.get("Type")} if r.returncode == 0 else {}
    if not rows:
        # failed_units already complains when docker is down; stay quiet here instead of double-paging
        return Result("skipped", _fit(f"docker system df unavailable (rc={r.returncode})"), {"level": "info"})

    def sz(kind: str, key: str = "Size") -> int:
        return parse_size(rows.get(kind, {}).get(key)) or 0

    # buildx builders are per user: default to the same account the alert bridge runs as (see _buildx)
    buildx_user = ctx.opt("buildx_user", ctx.cfg.get("global", {}).get("notify_handle", "ohmz"))
    builders, builder_errors = _extra_builders(buildx_user)
    img, img_rec = sz("Images"), sz("Images", "Reclaimable")
    cont, vol = sz("Containers"), sz("Local Volumes")
    bc = sz("Build Cache") + sum(b["size"] for b in builders)
    bc_rec = sz("Build Cache", "Reclaimable") + sum(b["reclaim"] for b in builders)
    total = img + cont + vol + bc
    # Volumes are deliberately excluded from "safe reclaim": this tool never suggests pruning them.
    safe = img_rec + bc_rec

    bc_warn = float(ctx.opt("build_cache_warn_gib", 25))
    img_warn = float(ctx.opt("images_reclaimable_warn_gib", 40))
    why = []
    if bc / GIB >= bc_warn:
        why.append(f"build cache {human(bc)} (>= {bc_warn:g} GiB)")
    if img_rec / GIB >= img_warn:
        why.append(f"images reclaimable {human(img_rec)} (>= {img_warn:g} GiB)")
    status = "warn" if why else "ok"

    def row(label: str, kind: str, size: int, rec: int | None) -> dict:
        d = rows.get(kind, {})
        return {"type": label, "size_h": human(size), "reclaim_h": "-" if rec is None else human(rec),
                "count": d.get("TotalCount", ""), "active": d.get("Active", "")}

    metrics = {
        "images_gib": _gib(img), "images_h": human(img), "images_reclaim_gib": _gib(img_rec),
        "images_reclaim_h": human(img_rec), "images_n": rows.get("Images", {}).get("TotalCount", ""),
        "containers_gib": _gib(cont), "containers_h": human(cont),
        "volumes_gib": _gib(vol), "volumes_h": human(vol),
        "build_cache_gib": _gib(bc), "build_cache_h": human(bc),
        "build_cache_reclaim_gib": _gib(bc_rec), "build_cache_reclaim_h": human(bc_rec),
        "total_gib": _gib(total), "total_h": human(total),
        "safe_reclaim_gib": _gib(safe), "safe_reclaim_h": human(safe),
        "builders": [{"name": b["name"], "size_h": human(b["size"]), "reclaim_h": human(b["reclaim"])} for b in builders],
        "builders_error": builder_errors[0] if builder_errors else None, "buildx_user": buildx_user or "",
        "level": status,
    }
    items = [row("Images", "Images", img, img_rec), row("Containers", "Containers", cont, None),
             row("Volumes", "Local Volumes", vol, None),
             row("Build cache", "Build Cache", bc, bc_rec)]
    summary = (f"{status}: " + "; ".join(why) if why else
               f"ok: images {human(img)} ({human(img_rec)} reclaimable), build cache {human(bc)}, volumes {human(vol)}")
    if builder_errors:                                    # the cache total is an undercount: say so
        summary += f" [{builder_errors[0]}]"
    # alert=False: cache size is housekeeping that the daily cleaners own; disk_forecast pages if the disk fills.
    return Result(status, _fit(summary), metrics, items, alert=False)


# =========================================================================== memory_health
def _psi(text: str) -> dict[str, dict[str, float]]:
    """'some avg10=0.00 avg60=0.00 avg300=0.00 total=123' lines => {'some': {...}, 'full': {...}}."""
    out: dict[str, dict[str, float]] = {}
    for ln in text.splitlines():
        parts = ln.split()
        if not parts:
            continue
        out[parts[0]] = {k: float(v) for k, _, v in (p.partition("=") for p in parts[1:]) if v}
    return out


def _meminfo(text: str) -> dict[str, int]:
    out = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        num = v.split()
        if num and num[0].isdigit():
            out[k.strip()] = int(num[0]) * (1024 if len(num) > 1 and num[1].lower() == "kb" else 1)
    return out


def _vmstat(text: str) -> dict[str, int]:
    out = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(" ")
        if v.strip().lstrip("-").isdigit():
            out[k] = int(v)
    return out


def _sleep(s: float) -> None:        # indirection for tests
    time.sleep(s)


@task("memory_health", klass="C0", tier="check", title="Memory pressure", timeout=60)
def memory_health(ctx: Ctx) -> Result:
    try:
        psi = {k: _psi(_read_proc(f"/proc/pressure/{k}")) for k in ("memory", "io", "cpu")}
        mem = _meminfo(_read_proc("/proc/meminfo"))
        vm1 = _vmstat(_read_proc("/proc/vmstat"))
    except OSError as exc:
        return Result("error", _fit(f"cannot read /proc: {exc}"))
    if "MemAvailable" not in mem:
        return Result("error", "MemAvailable missing from /proc/meminfo")

    o = {"psi_warn": float(ctx.opt("psi_mem_full_warn", 5.0)), "psi_crit": float(ctx.opt("psi_mem_full_crit", 15.0)),
         "avail_warn": float(ctx.opt("mem_available_warn_gib", 10)) * GIB,
         "avail_crit": float(ctx.opt("mem_available_crit_gib", 4)) * GIB,
         "swin_warn": float(ctx.opt("swap_in_pages_per_s_warn", 2000))}
    sample_s = float(ctx.opt("swap_sample_s", 5))

    # swap-in rate: a live sample (instantaneous) plus the average since the previous run (sustained).
    # The average uses the counter from BEFORE the live sample, and the baseline saved for the next run
    # is the one AFTER it, so the live sample's own pages never count towards the average.
    pre_in = vm1.get("pswpin", 0)
    live = None
    if sample_s > 0:
        _sleep(sample_s)
        try:
            vm2 = _vmstat(_read_proc("/proc/vmstat"))
        except OSError:
            vm2 = vm1
        live = max(vm2.get("pswpin", 0) - vm1.get("pswpin", 0), 0) / sample_s
        if swapwatch.relief_active(ctx.now):      # the owner is emptying the swap on purpose (homelab-maint swap relieve): that swap-in is expected
            live = 0.0
        out_live = max(vm2.get("pswpout", 0) - vm1.get("pswpout", 0), 0) / sample_s
        vm1 = vm2
    else:
        out_live = 0.0
    prev = ctx.state.get("vm", {})
    avg = None
    dt = ctx.now - prev.get("t", ctx.now)
    if prev and 60 <= dt <= 3 * 3600 and pre_in >= prev.get("pswpin", 0):
        avg = swapwatch.discount(pre_in - prev["pswpin"], prev["t"], ctx.now) / dt      # minus the pages of a deliberate relief inside this window
    oom_total = vm1.get("oom_kill", 0)
    oom_prev = prev.get("oom_kill")
    # a counter that went down means a reboot: everything counted now happened since the last run
    oom_delta = 0 if oom_prev is None else (oom_total - oom_prev if oom_total >= oom_prev else oom_total)
    ctx.state["vm"] = {"t": ctx.now, "pswpin": vm1.get("pswpin", 0), "pswpout": vm1.get("pswpout", 0),
                       "oom_kill": oom_total}
    # An OOM kill is a one-off: the delta is non-zero for exactly one 15 min run, which the alert's
    # confirm-before-page rule can never see.  Hold it for `event_hold_h`.
    hold_h = float(ctx.opt("event_hold_h", 6))
    oom_events = _held(ctx.state.get("oom_events"), ctx.now, hold_h * 3600)
    if oom_delta > 0:
        oom_events.append([ctx.now, oom_delta])
    ctx.state["oom_events"] = oom_events
    oom_recent = sum(n for _, n in oom_events)

    swap_in_pps = live if live is not None else (avg or 0.0)
    # "sustained": the live burst must be backed by a non-trivial average since the last run
    sustained = (live is not None and live >= o["swin_warn"] and (avg is None or avg >= o["swin_warn"] * 0.1)) \
        or (live is None and avg is not None and avg >= o["swin_warn"])

    avail = mem["MemAvailable"]
    swap_used = max(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), 0)
    full60 = psi["memory"].get("full", {}).get("avg60", 0.0)
    some60 = psi["memory"].get("some", {}).get("avg60", 0.0)
    io60 = psi["io"].get("some", {}).get("avg60", 0.0)

    crit, warn = [], []
    if full60 >= o["psi_crit"]:
        crit.append(f"memory stall {full60:.1f}%")
    if avail < o["avail_crit"]:
        crit.append(f"only {human(avail)} available")
    if not crit:
        if full60 >= o["psi_warn"]:
            warn.append(f"memory stall {full60:.1f}%")
        if avail < o["avail_warn"]:
            warn.append(f"{human(avail)} available")
        if sustained:
            warn.append(f"swap-in {swap_in_pps:.0f} pages/s")
    if oom_recent > 0:
        warn.append(f"{oom_recent} OOM kill(s) in last {hold_h:g}h")
    status = "crit" if crit else "warn" if warn else "ok"

    cached = mem.get("Cached", 0) + mem.get("SReclaimable", 0)
    tail = f"{human(avail)} avail; cache {human(cached)} is reclaimable"
    if swap_used:
        tail += f"; swap {human(swap_used)} used is harmless alone"
    summary = (f"{status}: " + ", ".join(crit + warn) + f" ({tail}; io {io60:.0f}%)") if status != "ok" else \
        f"ok: {tail}; mem stall {full60:.1f}%, io {io60:.0f}%"

    def prow(res: str) -> dict:
        p = psi[res]
        return {"res": res, "some10": p.get("some", {}).get("avg10", 0.0), "some60": p.get("some", {}).get("avg60", 0.0),
                "some300": p.get("some", {}).get("avg300", 0.0), "full60": p.get("full", {}).get("avg60", 0.0)}

    return Result(status, _fit(summary), {
        "mem_available_h": human(avail), "mem_available_gib": _gib(avail), "swap_used_h": human(swap_used),
        "cached_h": human(mem.get("Cached", 0)), "sreclaimable_h": human(mem.get("SReclaimable", 0)),
        "psi_mem_full60": full60, "psi_mem_some60": some60, "psi_io_some60": io60,
        "psi_cpu_some60": psi["cpu"].get("some", {}).get("avg60", 0.0),
        "swap_in_pps": round(swap_in_pps, 1), "swap_out_pps": round(out_live, 1),
        "swap_in_avg_pps": None if avg is None else round(avg, 1),
        "oom_kills_total": oom_total, "oom_kills_delta": oom_delta, "oom_kills_recent": oom_recent,
        "level": status,
    }, items=[prow("memory"), prow("io"), prow("cpu")])


# =========================================================================== plex_media_mount_check
_OCT = re.compile(r"\\([0-7]{3})")


def _unescape(s: str) -> str:
    """mountinfo escapes space/tab/newline/backslash as \\040 \\011 \\012 \\134."""
    return _OCT.sub(lambda m: chr(int(m.group(1), 8)), s)


def parse_mountinfo(text: str) -> list[dict]:
    """`id parent maj:min root mountpoint opts [optional...] - fstype source superopts`."""
    out = []
    for ln in text.splitlines():
        try:
            left, right = ln.split(" - ", 1)
            f, r = left.split(" "), right.split(" ")
            out.append({"dev": f[2], "root": _unescape(f[3]), "mp": _unescape(f[4]),
                        "fstype": r[0], "source": _unescape(r[1]) if len(r) > 1 else ""})
        except (ValueError, IndexError):
            continue
    return out


def _backing_path(entry: dict, mounts: list[dict]) -> str | None:
    """Directory a (bind) mount is backed by = where its device is mounted + the entry's root.

    A bind mount of /media/SandiskSSD/plex/Media shows up with source /dev/sdh1 and root
    /plex/Media; the SSD's own mount (/media/SandiskSSD, root /) on the same maj:min tells us
    where /plex/Media lives.  None when that cannot be determined.
    """
    src = entry["source"]
    if src.startswith("/") and not src.startswith("/dev/"):
        return src                                   # fuse/union filesystems that report a path
    best = None
    for m in mounts:
        if m is entry or m["dev"] != entry["dev"]:
            continue
        base = m["root"].rstrip("/")
        if entry["root"] == m["root"] or entry["root"].startswith(base + "/"):
            key = (len(base), -len(m["mp"]))         # deepest root, then shortest mountpoint
            if best is None or key > best[0]:
                best = (key, m, base)
    if best is None:
        return None
    _, m, base = best
    return (m["mp"].rstrip("/") + entry["root"][len(base):]) or "/"


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _tree_usage(root: str, budget_s: float) -> tuple[int, bool]:
    """Allocated bytes under root using lstat only (no `du`): one filesystem, hardlinks once,
    stops at the time budget.  Returns (bytes, complete)."""
    deadline = time.monotonic() + budget_s
    try:
        dev = os.lstat(root).st_dev
    except OSError:
        return 0, False
    total, n, seen = 0, 0, set()
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    n += 1
                    if n % 2000 == 0 and time.monotonic() > deadline:
                        return total, False
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_dev != dev:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    if st.st_nlink > 1 and not e.is_dir(follow_symlinks=False):
                        if (st.st_dev, st.st_ino) in seen:
                            continue
                        seen.add((st.st_dev, st.st_ino))
                    total += st.st_blocks * 512
        except OSError:
            continue
    return total, True


@task("plex_media_mount_check", klass="C0", tier="check", title="Plex Media mount", timeout=60)
def plex_media_mount_check(ctx: Ctx) -> Result:
    mp = os.path.normpath(ctx.opt("mount_point", ""))
    prefix = os.path.normpath(ctx.opt("expected_source_prefix", ""))
    if not ctx.opt("mount_point") or not ctx.opt("expected_source_prefix"):
        return Result("skipped", "plex_media_mount_check not configured")
    try:
        mounts = parse_mountinfo(_read_proc("/proc/self/mountinfo"))
    except OSError as exc:
        return Result("error", _fit(f"cannot read mountinfo: {exc}"))
    if not mounts:
        return Result("error", "mountinfo unparsable")

    entry = next((m for m in reversed(mounts) if os.path.normpath(m["mp"]) == mp), None)   # topmost wins
    ssd_exists = os.path.exists(prefix)
    # `was_mounted` is STICKY: set when the mount is seen, cleared only by the owner.  Clearing it on the
    # first "not mounted" run (as an earlier version did) made "SSD path vanished" last exactly one run,
    # after which the check fell back to a harmless "not migrated yet" while nothing had changed.
    # `expect_mounted = false` in maint.toml is the explicit switch for an intentional rollback.
    expect = ctx.opt("expect_mounted")
    if expect is False:
        ctx.state["was_mounted"] = False
    was_mounted = bool(ctx.state.get("was_mounted"))
    base = {"mounted": entry is not None, "source": "", "media_size_root_gib": None,
            "expected": prefix, "ssd_path_exists": ssd_exists, "was_mounted": was_mounted}

    def root_size() -> float | None:
        """Media size on the root disk, refreshed at most every `media_size_max_age_h` (walk is ~3 s)."""
        c = ctx.state.get("media_size") or {}
        if c and ctx.now - c.get("t", 0) < float(ctx.opt("media_size_max_age_h", 6)) * 3600:
            return c.get("gib")
        if not os.path.isdir(mp):
            return None
        b, complete = _tree_usage(mp, float(ctx.opt("media_size_budget_s", 10)))
        ctx.state["media_size"] = {"t": ctx.now, "gib": _gib(b), "complete": complete}
        return _gib(b)

    if entry is not None:
        src = _backing_path(entry, mounts)
        shown = src or entry["source"]
        ctx.state["was_mounted"] = True
        if src and _under(src, prefix):
            return Result("ok", _fit(f"ok: Plex Media is mounted from {src}"),
                          {**base, "source": src, "media_size_root_gib": 0.0, "level": "ok"})
        # Mounted, but from somewhere else.  On the same device as / it protects nothing.
        root_dev = next((m["dev"] for m in mounts if m["mp"] == "/"), None)
        level = "crit" if root_dev is not None and entry["dev"] == root_dev else "warn"
        return Result(level, _fit(f"{level}: Plex Media mounted from {shown or '?'}, expected under {prefix}"),
                      {**base, "source": shown, "level": level})

    # ---- not a mountpoint (note: was_mounted is deliberately NOT cleared here)
    if expect is False:                                # owner rolled back on purpose: nothing to guard
        return Result("info", "info: Plex Media is not bind-mounted (expect_mounted = false)",
                      {**base, "level": "info"})
    if ssd_exists or was_mounted:
        gib = root_size()
        where = f" ({gib:.1f} GiB there now)" if gib else ""
        why = "SSD path exists" if ssd_exists else "SSD path vanished"
        return Result("crit", _fit(f"crit: {mp.rsplit('/', 1)[-1]} dir is not mounted ({why}); Plex would regenerate Media on the root disk{where}"),
                      {**base, "media_size_root_gib": gib, "level": "crit"})
    return Result("info", _fit(f"info: Plex not migrated yet (no bind mount, no {prefix})"),
                  {**base, "level": "info"})
