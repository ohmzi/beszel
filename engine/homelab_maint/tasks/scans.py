"""stuck_scans: a filesystem walk (find, bfs, du, ncdu, tree, recursive grep ...) that has outlived whatever started it.

C0, check tier. It NEVER signals a process: it names the scan, who it belongs to (its pid, age, whether anything is still waiting for its
output) and what it costs, and prints the one command that stops it. Killing is the owner's call (a long `du` on a 24 TB archive is
legitimate; the same `find /` left behind by a closed terminal is not).

The rule (detect by cost and abandonment, never by size or name alone):
  * RUNAWAY (pages)  - ORPHANED (re-parented to init or a `systemd --user` manager, not a service unit's own job), older than
                       orphan_min_age_min (20), and reading at least min_read_kib_s (256 KiB/s). Nobody is waiting for it and it is
                       using the disk.
  * LONG (dashboard) - attached to a live parent (someone may be waiting), older than attached_min_age_min (180) and still reading.
  * IDLE (info)      - old but not reading (waiting on a dead mount?): reported, never paged.
Scans inside containers are that application's business and are skipped. Items carry the program, pid, age and the ONE directory it
walks, never the command line (the status JSON is served by the website).
"""
from __future__ import annotations

import re
import time
from typing import Any

from .. import iotop
from ..core import Ctx, Result, ikey, task
from . import gates

MIB = 1024 ** 2


def _ascii(s: Any, n: int = 140) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def _dur(s: float | None) -> str:
    if s is None:
        return "?"
    m = int(s // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _rate(bps: float) -> str:
    return f"{bps / MIB:.1f} MiB/s" if bps >= MIB else f"{bps / 1024:.0f} KiB/s"


@task("stuck_scans", klass="C0", tier="check", title="Runaway filesystem scans", timeout=60)
def stuck_scans(ctx: Ctx) -> Result:
    orphan_min = float(ctx.opt("orphan_min_age_min", 20))
    attached_min = float(ctx.opt("attached_min_age_min", 180))
    min_bps = float(ctx.opt("min_read_kib_s", 256)) * 1024
    sample_s = max(1.0, min(float(ctx.opt("sample_s", 4)), 15.0))

    info = gates.container_info() or {}
    scans = iotop.find_scans(names={c.id: n for n, c in info.items()})
    if not scans:
        return Result("ok", "no filesystem scan is running", {"scans": 0, "runaway": 0}, alert=False)

    a = {d["pid"]: iotop.read_io(d["pid"]) for d in scans}
    t0 = time.monotonic()
    time.sleep(sample_s)
    dt = max(time.monotonic() - t0, 0.001)
    psi = iotop.psi_io()
    runaway, long_, idle, rows = [], [], [], []
    for d in scans:
        b = iotop.read_io(d["pid"])
        bps = 0.0
        if a.get(d["pid"]) and b and b[0] >= a[d["pid"]][0] and b[1] >= a[d["pid"]][1]:
            bps = ((b[0] - a[d["pid"]][0]) + (b[1] - a[d["pid"]][1])) / dt
        age_min = (d["age_s"] or 0) / 60
        reading = bps >= min_bps
        if d["orphan"] and age_min >= orphan_min and reading:
            verdict = "runaway"
            runaway.append((d, bps))
        elif not d["orphan"] and age_min >= attached_min and reading:
            verdict = "long"
            long_.append((d, bps))
        elif age_min >= (orphan_min if d["orphan"] else attached_min) and not reading:
            verdict = "idle"
            idle.append((d, bps))
        else:
            verdict = "running"
        rows.append({"name": d["name"], "pid": d["pid"], "age": _dur(d["age_s"]), "orphaned": d["orphan"], "reading": _rate(bps),
                     "root": d.get("root", "."), "verdict": verdict})
    rows.sort(key=lambda r: ({"runaway": 0, "long": 1, "idle": 2, "running": 3}[r["verdict"]], -r["pid"]))
    full = psi.get("full60")
    stall = "" if full is None else f"; I/O stall {full:.0f}%"
    metrics = {"scans": len(scans), "runaway": len(runaway), "long": len(long_), "idle": len(idle), "psi_io_full60": full}
    if runaway:
        if len(runaway) == 1:
            d, bps = runaway[0]
            s = f"runaway scan: {d['name']} pid {d['pid']} orphaned {_dur(d['age_s'])}, reading {_rate(bps)} on {d.get('root', '.')}{stall}; stop: kill {d['pid']}"
        else:
            s = (f"{len(runaway)} runaway scans: " + ", ".join(f"{d['name']} {d['pid']} ({_dur(d['age_s'])})" for d, _ in runaway[:4])
                 + f"{stall}; stop: kill PID")
        return Result("warn", _ascii(s), metrics, rows[:10], issue_key=ikey(scan=[f"{d['name']} {d.get('root', '.')}" for d, _ in runaway]))
    if long_:
        d, bps = long_[0]
        return Result("warn", _ascii(f"long scan: {d['name']} pid {d['pid']} running {_dur(d['age_s'])}, reading {_rate(bps)}{stall} (attached, so not paged)"),
                      metrics, rows[:10], alert=False)
    if idle:
        d, _ = idle[0]
        return Result("info", _ascii(f"old scan not reading: {d['name']} pid {d['pid']} {_dur(d['age_s'])} (stuck on a dead mount?)"), metrics, rows[:10], alert=False)
    return Result("ok", _ascii(f"{len(scans)} filesystem scan(s) running, none runaway"), metrics, rows[:10], alert=False)
