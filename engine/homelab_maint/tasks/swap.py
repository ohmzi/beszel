"""swap_audit: how full the swap is, who holds it, and whether it matters (C0, check tier, never touches a process).

Full swap alone is not a fault: the kernel parks cold pages there. It matters when pages are going in and out (thrashing) or when no
swap is left while RAM is scarce (the next spike has no safety valve). The relief (swapoff + swapon) is NOT done here: this task only
says whether it looks feasible; `homelab-maint swap relieve` re-checks every precondition, including the busy gates, at the moment it
runs (see swapwatch.py for the list). The shipped thresholds are swapwatch.DEFAULTS; [tasks.swap_audit] overrides any of them.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .. import swapwatch as sw
from ..core import Ctx, Result, ikey, task


def _ascii(s: Any, n: int = 140) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


@task("swap_audit", klass="C0", tier="check", title="Swap holders and pressure", timeout=60)
def swap_audit(ctx: Ctx) -> Result:
    if sw.relief_active(ctx.now):
        return Result("info", "swap relief in progress (homelab-maint swap relieve): swap-in is expected", {"relief_active": 1}, alert=False)
    if sw.swap_expected_but_off():
        return Result("warn", "swap is OFF but /etc/fstab lists it (an interrupted relief?): run sudo swapon -a", {"swap_off": 1}, issue_key=ikey(swap=["off"]))
    D = sw.DEFAULTS                      # literal option names below: the registry finds a task's knobs by reading ctx.opt("name")
    cfg = {"active_in_mib_s": float(ctx.opt("active_in_mib_s", D["active_in_mib_s"])), "thrash_in_mib_s": float(ctx.opt("thrash_in_mib_s", D["thrash_in_mib_s"])),
           "thrash_total_mib_s": float(ctx.opt("thrash_total_mib_s", D["thrash_total_mib_s"])), "thrash_psi_full": float(ctx.opt("thrash_psi_full", D["thrash_psi_full"])),
           "full_pct": float(ctx.opt("full_pct", D["full_pct"])), "min_free_swap_pct": float(ctx.opt("min_free_swap_pct", D["min_free_swap_pct"])),
           "scarce_ram_pct": float(ctx.opt("scarce_ram_pct", D["scarce_ram_pct"])), "relief_min_used_gib": float(ctx.opt("relief_min_used_gib", D["relief_min_used_gib"])),
           "relief_headroom_gib": float(ctx.opt("relief_headroom_gib", D["relief_headroom_gib"])), "relief_cap_margin": float(ctx.opt("relief_cap_margin", D["relief_cap_margin"])),
           "relief_disk_busy_pct": float(ctx.opt("relief_disk_busy_pct", D["relief_disk_busy_pct"])), "relief_psi_full": float(ctx.opt("relief_psi_full", D["relief_psi_full"]))}
    cfg.update({k: sw.relief_cfg(ctx.cfg).get(k, D[k]) for k in sw.RELIEF_KEYS})        # the same range-checked limits every relief uses
    snap = sw.snapshot(names=sw.container_names(), sample_s=max(1.0, min(float(ctx.opt("sample_s", 3)), 10.0)), sleep=time.sleep)
    an = sw.analyse(snap["mem"], snap["rates"], snap["psi"], cfg)
    if an["state"] == "none":
        return Result("info", "no swap configured", {"swap_total_gib": 0}, alert=False)
    hold = sw.track(ctx.state, snap["holders"], ctx.now, float(ctx.opt("cold_refault_pps", sw.AUTO_DEFAULTS["cold_refault_pps"])))
    plan = sw.relief_plan(snap["mem"], snap.get("all_holders", hold), snap["psi"], snap["areas"], (False, "checked when you run it"), 0.0, cfg)
    items = [{"name": h["who"], "kind": h["kind"], "swap": sw.human(h["swap_b"]), "resident": sw.human(h["current_b"]),
              "cap": "none" if not h["max_b"] else sw.human(h["max_b"]),
              "activity": h.get("activity", "unknown") + ("" if h.get("cold_h") is None else f" {h['cold_h']} h")} for h in hold[:8]]
    top = ", ".join(f"{h['who']} {sw.human(h['swap_b'])}" for h in hold[:3])
    churn = "" if an["in_mib_s"] is None else f"{an['in_mib_s']} MiB/s back in"
    metrics = {"swap_used_pct": an["used_pct"], "swap_used_gib": round(an["used_b"] / sw.GIB, 1), "swap_free_gib": round(an["free_b"] / sw.GIB, 2),
               "in_mib_s": an["in_mib_s"], "out_mib_s": an["out_mib_s"], "holders": len(hold), "relief_feasible": int(bool(plan["safe"]))}
    head = f"swap {an['used_pct']:.0f}% ({sw.human(an['used_b'])})"
    if an["state"] == "thrashing":
        return Result("warn", _ascii(f"{head} THRASHING: {churn}, {an['out_mib_s']} MiB/s out; top: {top}"), metrics, items, issue_key=ikey(swap=["thrashing"]))
    if an["exhausted"]:
        return Result("warn", _ascii(f"{head}: no free swap and RAM is scarce, a spike has no buffer; top: {top}"), metrics, items, issue_key=ikey(swap=["exhausted"]))
    if an["state"] == "active":
        return Result("info", _ascii(f"{head}, in use: {churn}; top: {top}"), metrics, items, alert=False)
    if an["free_b"] < an["total_b"] * sw.DEFAULTS["min_free_swap_pct"] / 100:
        relief = "relief looks feasible: homelab-maint swap" if plan["safe"] else "see: homelab-maint swap"
        return Result("warn", _ascii(f"{head} full but idle ({churn or 'no churn'}); no free swap left; top: {top}; {relief}"), metrics, items, alert=False)
    if an["state"] == "cold":
        return Result("info", _ascii(f"{head} holds idle pages, nothing waits on it ({churn or 'no churn'}); top: {top}"), metrics, items, alert=False)
    return Result("ok", _ascii(f"{head}, idle"), metrics, items, alert=False)
