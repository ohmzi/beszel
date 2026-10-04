"""swap_auto_relief: when a quiet, mostly-cold, nearly-full swap has hung around too long, empty it (swapoff + swapon) without touching a process.

C1, check tier, report-only until the owner sets mode = "apply". Nothing is ever killed or restarted: `swapoff` only moves pages back to RAM.

What it judges (the standard used by systemd-oomd, Meta's oomd and the proactive-reclaim work: pressure and refaults, never how full swap is):
  * every holder (cgroup memory.swap.current) is COLD (not refaulting its swapped pages: idle), ACTIVE (refaulting: in use) or unknown;
  * the swap is "hanging around" when it is at least trigger_pct (80) full, the system is quiet (no swap-in worth the name, swap-out under
    quiet_out_mib_s) and at least min_cold_share (70 %) of it is held by cold holders, continuously for persist_hours (2). The watch ends only
    below release_pct (60) or when the cold share falls under min_cold_share - 0.2: no flapping around the line, but a real change of
    character stops it. Time we did not observe (host down, PAUSE, missed runs) never counts: a gap over max_gap_hours restarts the timer;
  * it acts only inside the maintenance window (local window_start_hour..window_end_hour, 02:00-06:00), because swapoff can stall a process
    that is mapping memory while its pages are read back, which the busy gates cannot see;
  * then every precondition of the relief is checked again (swapwatch.relief_plan, over EVERY cgroup that holds swap: free RAM with headroom
    clear of the host's own alert bands, the busy gates for ComfyUI / Ollama / Plex / backups / builds / Immich, the swap disk's load, memory
    stall, and memory.max / memory.high of each holder and of its ancestors). Blocked is retried every run and never forced; blocked for
    max_defer_hours (24) becomes a visible warning, never a page;
  * a cooldown (min_interval_hours, 12) and a circuit breaker: if swap is back above trigger_pct within refill_hours (6) after
    breaker_after_refills (2) CONFIRMED reliefs in a row, the workload does not fit in RAM or something leaks, so the automation pauses for
    breaker_days (3) and the owner is paged with the top holders (the notifier confirms it on 2 runs in a row, sends ONE page, then a reminder
    every alert_reminder_hours). Relieving again and again would only hide that.

The relief runs as its own systemd unit (homelab-maint-swap-relief), not inside this task: it takes minutes and has its own guards (it aborts and
keeps the swap if available RAM or the memory stall turns bad, or when PAUSE appears or the rule leaves apply mode, and always runs swapon again;
the unit also runs `swapon -a` when it stops, however it stops). The relief's safety limits (relief_headroom_gib, relief_cap_margin, ...) are the
ones documented under [tasks.swap_audit]; every caller that plans or runs a relief reads them there. A freeze holds the task (disruptive = true). This task only decides and starts it, and then reads the OUTCOME from the relief ledger: a launch is
never booked as a relief. A relief that was refused, aborted or left no outcome backs off (retry_hours, doubling, at most 24 h) and, after
max_failed_attempts, shows up as a warning.
"""
from __future__ import annotations

import copy
import re
import shutil
import time
from typing import Any

from .. import swapwatch as sw
from ..core import CapExceeded, Ctx, Result, audit, ikey, sh, task
from . import gates

UNIT = "homelab-maint-swap-relief"


def _ascii(s: Any, n: int = 140) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


@task("swap_auto_relief", klass="C1", tier="check", title="Automatic swap relief", timeout=120)
def swap_auto_relief(ctx: Ctx) -> Result:
    res = _run(ctx)
    note = getattr(ctx, "_relief_note", "")
    if note:                                                  # the change log's detail is the run's summary: lead with what was actually done
        res.summary = _ascii(note + "; " + res.summary)
    n = int(ctx.state.get("heal_notice_runs", 0)) if ctx.apply else 0
    if n > 0 and res.issue_key != "swap_auto:swap-off":      # a self-heal notice must be seen on 2 runs in a row for the notifier to page it
        ctx.state["heal_notice_runs"] = n - 1
        res.status, res.alert, res.issue_key = "warn", True, ikey(swap_auto=["swap-off"])
        res.summary = _ascii("swap was switched back on after an interrupted relief; " + res.summary)
    return res


def _run(ctx: Ctx) -> Result:
    D = sw.AUTO_DEFAULTS                  # literal option names below: the registry finds a task's knobs by reading ctx.opt("name")
    cfg = {"trigger_pct": float(ctx.opt("trigger_pct", D["trigger_pct"])), "release_pct": float(ctx.opt("release_pct", D["release_pct"])),
           "persist_hours": float(ctx.opt("persist_hours", D["persist_hours"])), "min_cold_share": float(ctx.opt("min_cold_share", D["min_cold_share"])),
           "cold_refault_pps": float(ctx.opt("cold_refault_pps", D["cold_refault_pps"])), "min_interval_hours": float(ctx.opt("min_interval_hours", D["min_interval_hours"])),
           "refill_hours": float(ctx.opt("refill_hours", D["refill_hours"])), "breaker_after_refills": float(ctx.opt("breaker_after_refills", D["breaker_after_refills"])),
           "breaker_days": float(ctx.opt("breaker_days", D["breaker_days"])), "max_defer_hours": float(ctx.opt("max_defer_hours", D["max_defer_hours"])),
           "quiet_out_mib_s": float(ctx.opt("quiet_out_mib_s", D["quiet_out_mib_s"])), "window_start_hour": float(ctx.opt("window_start_hour", D["window_start_hour"])),
           "window_end_hour": float(ctx.opt("window_end_hour", D["window_end_hour"])), "max_gap_hours": float(ctx.opt("max_gap_hours", D["max_gap_hours"])),
           "retry_hours": float(ctx.opt("retry_hours", D["retry_hours"])), "max_failed_attempts": float(ctx.opt("max_failed_attempts", D["max_failed_attempts"]))}
    sample_s = max(1.0, min(float(ctx.opt("sample_s", 3)), 10.0))
    mode = "apply" if ctx.apply else "report"
    cfg_mode = "apply" if ctx.tcfg.get("mode") == "apply" else "report"          # what the OWNER configured: the lane the observations belong to
    # A manual `run --task X` (or --dry-run) of an apply-configured task says what it WOULD do but never touches the real watch, the
    # backoff or the breaker: only the scheduled apply runs own that state.
    st = copy.deepcopy(ctx.state) if (not ctx.apply and cfg_mode == "apply") else ctx.state

    if sw.relief_active(ctx.now) or sh(["systemctl", "is-active", "--quiet", UNIT + ".service"], timeout=10).returncode == 0:
        if isinstance(st.get("last_seen"), (int, float)):
            st["last_seen"] = ctx.now                # we ARE watching (the relief is running): a long relief must not look like a gap in observations
        return Result("info", "swap relief in progress: not deciding anything until it ends", {"mode": mode, "relief_active": 1}, alert=False)

    if st.get("lane") != cfg_mode:                   # observations made under another configured mode are not this mode's evidence
        st["lane"], st["over_since"], st["blocked_since"] = cfg_mode, None, None

    # Self-heal: swap that /etc/fstab lists but that is not on (a relief killed between swapoff and swapon) is switched back on.
    if sw.swap_expected_but_off():
        done = ctx.act("swapon-restore", "swap:fstab", 0, lambda: _swapon_all(), outcome="restored")
        if done:
            st["heal_notice_runs"] = 2               # the notifier only pages a level it has seen on 2 runs in a row: keep the notice up for the next one
            _journal("Swap switched back on", "swap was off although /etc/fstab lists it (an interrupted relief?); swapon -a restored it")
        return Result("warn" if done else "info", "swap was OFF although /etc/fstab lists it (an interrupted relief?): " + ("switched back on with swapon -a" if done else "would run swapon -a"),
                      {"mode": mode, "swap_off": 1}, alert=bool(done), issue_key=ikey(swap_auto=["swap-off"]) if done else None)

    # Reconcile the last launch with what the relief itself recorded: a launch is not a relief.
    la = st.get("launched_at")
    if la is not None:
        out = sw.relief_outcome(la - 120, by="auto")
        if out is not None:
            st.pop("launched_at", None)
            if out.get("ok"):
                st.update(last_relief_at=out["t1"], refill_checked=False, failed_attempts=0, retry_after=None, over_since=None, blocked_since=None)
                if ctx.apply:
                    audit(ctx.name, "swap-relief", "swap:" + (st.get("launched_path") or "swap"), 0, "done")      # the change log and reports count THIS, not the launch
                    moved = int(out.get("pages_in") or 0) * 4096
                    ctx._relief_note = "swap relieved automatically" + (f": {sw.human(moved)} returned to RAM in {max(1, int(out['t1'] - out['t0']))} s" if moved else "")
            else:
                _failed(st, ctx.now, cfg, out.get("note") or "the relief did not complete")
        elif ctx.now - la > 1800:
            st.pop("launched_at", None)
            _failed(st, ctx.now, cfg, "the relief unit left no outcome")
        else:
            if isinstance(st.get("last_seen"), (int, float)):
                st["last_seen"] = ctx.now
            return Result("info", "automatic swap relief launched: waiting for it to start", {"mode": mode, "launched": 1}, alert=False)

    snap = sw.snapshot(names=sw.container_names(), sample_s=sample_s, sleep=time.sleep)
    an = sw.analyse(snap["mem"], snap["rates"], snap["psi"])
    hold = sw.track(st, snap["holders"], ctx.now, cfg["cold_refault_pps"])
    d = sw.auto_decide(st, an, hold, None, cfg, ctx.now)
    if d["action"] == "relieve" and d.get("need_plan"):      # only now pay for the busy gates (~10 s) and the disk sample (2 s)
        areas = snap["areas"]
        busy = gates.busy("any")
        disk = sw.disk_of(areas[0]["path"]) if areas else None
        plan = sw.relief_plan(snap["mem"], snap.get("all_holders", hold), snap["psi"], areas, busy, sw.disk_busy_pct(disk) if disk else None, sw.relief_cfg(ctx.cfg))
        d = sw.auto_decide(st, an, hold, plan, cfg, ctx.now)
    items = [{"name": h["who"], "kind": h["kind"], "swap": sw.human(h["swap_b"]), "activity": h.get("activity", "unknown"),
              "cold_for": "-" if h.get("cold_h") is None else f"{h['cold_h']} h"} for h in hold[:8]]
    pct = an.get("used_pct")
    if pct is not None and pct < cfg["release_pct"]:         # the swap is fine again: an old failure is no longer news
        st["failed_attempts"], st["retry_after"] = 0, None
    fails = int(st.get("failed_attempts", 0))
    m = {"mode": mode, "action": d["action"], "swap_used_pct": pct, "cold_share_pct": round(100 * sw.cold_share(hold)),
         "held_h": None if d.get("held_h") is None else round(d["held_h"], 1), "refills": int(st.get("refills", 0)), "failed_attempts": fails}
    act = d["action"]

    def done(res: Result) -> Result:
        """A relief that keeps failing is made visible (a warning on the dashboard, never a page)."""
        if fails >= cfg["max_failed_attempts"] and res.status != "warn":
            res.status = "warn"
            res.summary = _ascii(res.summary + f"; last {fails} attempts failed: {st.get('last_failure', '?')}")
            res.alert = False
        return res

    if act == "breaker":
        top = ", ".join(f"{h['who'][:18]} {sw.human(h['swap_b'])}" for h in hold[:2])
        # alert stays True for the whole pause: the notifier pages a level only after it has seen it on alert_confirm_runs (2) runs in a row,
        # then sends ONE page and a reminder every alert_reminder_hours (24). A one-run alert would never be delivered.
        return Result("warn", _ascii(f"{d['reason']}; top: {top}"), m, items, alert=True, issue_key=ikey(swap_auto=["breaker"]))
    if act == "idle":
        return done(Result("ok", _ascii(f"swap {pct:.0f}%: {d['reason']}" if pct is not None else d["reason"]), m, items, alert=False))
    if act in ("watching", "wait"):
        return done(Result("info", _ascii(d["reason"]), m, items, alert=False))
    if act == "blocked":
        long_ = d.get("blocked_h", 0) >= cfg["max_defer_hours"]
        return done(Result("warn" if long_ else "info", _ascii(f"swap {pct:.0f}% for {d['held_h']:.0f} h, relief blocked" + (f" for {d['blocked_h']:.0f} h" if long_ else "") + f": {d['reason']}"),
                           m, items, alert=False))

    # act == "relieve": start it as its own unit, through the mutation gate (audited; report mode only says what it would do)
    path = (snap["areas"][0]["path"] if snap["areas"] else "swap")

    def launch() -> None:
        exe = shutil.which("homelab-maint") or "/usr/local/sbin/homelab-maint"
        swapon = shutil.which("swapon") or "/usr/sbin/swapon"
        r = sh(["systemd-run", f"--unit={UNIT}", "--collect", "--no-block", "--quiet", "--description=homelab-maint automatic swap relief",
                "--property=Nice=19", "--property=IOSchedulingClass=idle", "--property=RuntimeMaxSec=2700",
                f"--property=ExecStopPost=-{swapon} -a",            # however the unit ends (even SIGKILL), swap listed in fstab is switched back on
                exe, "swap", "relieve", "--apply", "--auto"], timeout=30)
        if r.returncode != 0:
            raise RuntimeError("systemd-run failed: " + _ascii(r.stderr.strip(), 100))

    try:
        started = ctx.act("swap-relief-launch", f"swap:{path}", 0, launch, outcome="launched")
    except CapExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. systemd-run failing: back off and say so, instead of erroring (and paging CRIT) every 15 minutes
        _failed(st, ctx.now, cfg, f"could not start the relief unit: {exc}")
        return Result("warn", _ascii(f"could not start the swap relief: {exc}; retrying in {max(1, round((st['retry_after'] - ctx.now) / 60))} min"), m, items, alert=False)
    if started:
        st["launched_at"], st["launched_path"] = ctx.now, path        # last_relief_at is set (and the watch cleared) only when the OUTCOME says it happened; a failure keeps the persisted watch
        m["action"] = "started"
        return Result("info", _ascii(f"automatic swap relief started: {d['reason']}; about {max(1, round(an['used_b'] / (150 * sw.MIB) / 60))} min, nothing is killed"), m, items, alert=False)
    return Result("info", _ascii(f"report: would clear the swap now ({d['reason']}); set mode = apply to let it"), m, items, alert=False)


def _journal(title: str, detail: str) -> None:
    try:
        from ..routine import journal_note
        journal_note(title, detail)
    except Exception:  # noqa: BLE001
        pass


def _failed(st: dict, now: float, cfg: dict, why: str) -> None:
    """A relief that was refused, aborted or left no outcome: back off (doubling, at most 24 h) and remember why. It is NOT a quick refill."""
    n = int(st.get("failed_attempts", 0)) + 1
    st["failed_attempts"], st["last_failure"] = n, _ascii(why, 80)
    st["retry_after"] = now + min(cfg["retry_hours"] * (2 ** (n - 1)), 24.0) * 3600


def _swapon_all() -> None:
    r = sh(["swapon", "-a"], timeout=30)
    if r.returncode != 0:
        raise RuntimeError("swapon -a failed: " + _ascii(r.stderr.strip(), 100))
