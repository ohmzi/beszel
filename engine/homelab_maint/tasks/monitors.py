"""monitors: the `probes` task (C0, check tier), the monitors.json inventory and the umbrella's own heartbeat.

The engine lives in homelab_maint/probes.py; this module only turns its state into the three things the rest of the umbrella
consumes, so there is exactly one monitoring plane:
  * task `probes`     one Result per check-tier run (status/items/metrics) for status.json, the dashboard, incidents and SLOs.
                      DETECTION is not tied to the 15-min tier: the scheduler tick runs `python3 -m homelab_maint.probes run
                      --notify` every minute (glue, jobs.toml), the engine honours each probe's own interval_s, and the task
                      just folds the latest state into status.json. (Do NOT also run the task itself every minute through
                      cmd_run: it would add a kind:"task" history record per minute, and the health calendar counts 15 min per
                      record.) [tasks.probes] options: alert_mode = "task" (default, needs no glue: core.Notifier pages on the
                      task level, up to 30 min after the tier first sees a problem, and a SECOND probe going down at the same
                      level is not announced until the 24 h reminder) | "events" (metrics.self_notifies=1: the runner skips
                      the task-level page and the tick's --notify delivers one message per probe change through notify.send,
                      dedupe_key "probe:<name>", about 2 minutes after the first failed look). Delivery is two-phase
                      (probes.claim_events -> ack/release): an event is deleted only after a handled delivery, a claim older
                      than 2 min (the deliverer was killed or hit the job timeout) is requeued, the most urgent event goes
                      first and the whole delivery is capped at notify_budget_s (default 35 s; with the engine's 45 s run
                      budget that fits the 90 s job timeout shipped in jobs.toml, 120 s leaves more room for a slow transport).
                      Status always stays honest in both modes (alert stays True, so incidents and the dashboard see real
                      outages). budget_s / workers override the probes.toml [defaults].
                      CONFIG TROUBLE is never quiet: no usable probes while a config file is present (untrusted, unreadable,
                      empty, all invalid) or after probes ran before = status "error", "MONITORING BLIND", alert=True and
                      never self_notifies (the task-level page is then the only thing that can fire); "never installed" is
                      the only quiet info. A queued `config` event announces the same thing in events mode.
  * export()          monitors.json for the website: what is monitored, how, cadence, state, availability, source.
  * heartbeat_payload()  the dead-man's switch: {"ok": bool, ages...}. ok is false when the probe plane or the check tier stopped
                      (status.json older than STALE_S), the plane is blind, or an age is NEGATIVE (a stamp in the future: the
                      clock stepped back, so "fresh" cannot be believed; `clock_skew` says so). The same fact is pushed to Kuma
                      as the `umbrella-probes` heartbeat (and the push is withheld while blind, so Kuma sees the pushes stop).
  * availability      every figure in export()/task metrics is the engine's time-honest one (probes.py: a stretch in which the
                      engine did not run is booked as failed + unobserved), shipped with `obs_*` = the share actually measured.
Read-only: nothing here touches the host; the engine writes only STATE_DIR/probes.json and kind:"probe" history records.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .. import core, probes
from ..core import Ctx, Result, task

STALE_S = 2700                 # 45 min: three check ticks; the same limit the web healthz uses for overview.json
MAX_ITEMS = 8                  # status.json items; the full inventory is in export()
NOTIFY_BUDGET_S = 35.0         # cap on one delivery pass (run budget 45 s + this = 80 s, inside the 90 s job timeout)
MIN_SEND_S = 5.0               # a send is not started with less than this left of the budget (a transport round trip takes 1-3 s)
_RANK = {"crit": 0, "warn": 1, "info": 2}
pop_events = probes.pop_events  # re-exported: the notifier glue drains this once per run


def _sev(r: dict) -> str:
    """Paging weight of a counted row: degraded = warn at most; severity=info never pages. A crit probe that is DOWN right now
    stays crit even if it flapped earlier (a service that crash-looped and then died is the worst case, not a noisy one);
    flapping only softens a probe that is currently up or degraded."""
    if r["severity"] == "info":
        return "info"
    return "crit" if r["state"] == "down" and r["severity"] == "crit" else "warn"


def _names(rows: list[dict], k: int = 3) -> str:
    out = ", ".join(r["title"][:18] for r in rows[:k])
    return out + (f" +{len(rows) - k}" if len(rows) > k else "")


def _ascii(s: Any, n: int = 140) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(s))[:n]


def build_result(rows: list[dict], rep: probes.RunReport | None, now: float, mode: str = "task") -> Result:
    """Pure: probe rows (probes.snapshot) -> Result. Status: error = monitoring is BLIND (config present but no usable probe);
    crit = a confirmed-down crit probe; warn = confirmed-down warn probe, degraded or flapping probe, or invalid probe
    definitions; info = only info-severity trouble (or nothing configured yet); ok otherwise."""
    errors = list(rep.errors) if rep else []
    cfg_items = [{"name": "probes-config", "title": "Probe configuration", "state": "down", "sev": "crit" if not rows else "warn",
                  "since_min": None, "detail": _ascii(e, 100), "avail_30d": None} for e in errors[:2]]
    counted = [r for r in rows if r["state"] in ("up", "warn", "down")]
    if not rows:
        if errors:
            # Never info, never quiet: a present-but-unusable config (untrusted, unreadable, empty, all invalid) or one that
            # vanished means NOTHING is being watched. No self_notifies either, so even in events mode this task-level page
            # fires (the engine has no per-probe events to send while it has no probes).
            return Result("error", _ascii(f"MONITORING BLIND: no probes are running. {errors[0]}"),
                          metrics={"total": 0, "counted": 0, "invalid": len(errors), "blind": 1}, items=cfg_items)
        return Result("info", "no probes configured", alert=False, metrics={"total": 0, "invalid": 0, "blind": 0})
    bad = [r for r in counted if r["state"] != "up" or r["flapping"]]
    bad.sort(key=lambda r: (_RANK[_sev(r)], r["since"] or 0))
    crit = [r for r in bad if _sev(r) == "crit"]
    warn = [r for r in bad if _sev(r) == "warn"]
    info = [r for r in bad if _sev(r) == "info"]
    status = "crit" if crit else "warn" if (warn or errors) else "info" if info else "ok"
    up = sum(1 for r in counted if r["state"] == "up")
    parts = []
    down = [r for r in bad if r["state"] == "down" and _sev(r) != "info"]
    if down:
        parts.append(f"{len(down)} down: {_names(down)}")
    deg = [r for r in bad if r["state"] == "warn" and _sev(r) != "info"]
    if deg:
        parts.append(f"{len(deg)} degraded: {_names(deg, 2)}")
    flap = [r for r in bad if r["flapping"]]
    if flap:
        parts.append(f"{len(flap)} flapping: {_names(flap, 2)}")
    if info:
        parts.append(f"{len(info)} info-only")
    if errors:
        parts.append(f"{len(errors)} bad probe defs ({_ascii(errors[0], 40)})")
    skipped = sum(1 for r in rows if r["state"] == "skipped")
    paused = sum(1 for r in rows if r["state"] == "paused")
    tail = f"{up}/{len(counted)} up" + (f", {skipped} skipped" if skipped else "") + (f", {paused} paused" if paused else "")
    summary = _ascii("; ".join(parts + [tail]) if parts else tail)
    if rep and rep.locked:
        summary = _ascii(f"{summary} (cached: a probe run is in progress)")
    avs = [r["avail_30d"] for r in counted if r["avail_30d"] is not None]
    obs = [r["obs_30d"] for r in counted if r.get("obs_30d") is not None]
    overdue = sum(1 for r in counted if r["last_run"] and now - r["last_run"] > 3 * max(r["interval_s"], 900))
    metrics = {"total": len(rows), "counted": len(counted), "up": up, "warn": len(deg), "down": len(down),
               "crit_down": len(crit), "skipped": skipped, "paused": paused,
               "unknown": sum(1 for r in rows if r["state"] == "unknown"), "flapping": len(flap), "invalid": len(errors),
               "blind": 0, "ran": len(rep.ran) if rep else 0, "overdue": overdue,
               "slowest_ms": max((r["ms"] or 0 for r in counted), default=0),
               "avail_30d_pct": round(sum(avs) / len(avs), 2) if avs else None,
               "obs_30d_pct": round(sum(obs) / len(obs), 2) if obs else None,
               "run_ms": int((rep.elapsed_s if rep else 0) * 1000)}
    shown = bad[:MAX_ITEMS - len(cfg_items)] or [r for r in rows if r["state"] in ("skipped", "paused")][:MAX_ITEMS - len(cfg_items)]
    items = [{"name": r["name"], "title": r["title"], "state": "flapping" if r["flapping"] and r["state"] != "down" else r["state"],
              "sev": _sev(r) if r in bad else "info", "since_min": max(0, int((now - r["since"]) / 60)) if r["since"] else None,
              "detail": _ascii(r["detail"], 100), "avail_30d": r["avail_30d"], "flapping": bool(r["flapping"])} for r in shown]
    items += cfg_items                     # a config problem is always visible, even when every probe is green
    if mode == "events":
        metrics["self_notifies"] = 1          # the runner must not also page on the task level (see notify_events)
    # SPEC5: ALL the probes that are down / degraded / flapping by their stable name (the summary clips the lists and shows titles) and how many
    # probe definitions are bad; never durations, availability or the up/total tally.
    key = core.ikey(down=[r["name"] for r in down], degraded=[r["name"] for r in deg], flapping=[r["name"] for r in flap],
                    baddefs=[str(len(errors))] if errors else [])
    return Result(status, summary, metrics=metrics, items=items, issue_key=key)


@task("probes", klass="C0", tier="check", title="Monitoring probes", timeout=120)
def run(ctx: Ctx) -> Result:
    d, plist, errs = probes.load_probes()
    for k in ("budget_s", "workers"):                       # per-task overrides from maint.toml [tasks.probes]
        if isinstance(ctx.opt(k), (int, float)) and not isinstance(ctx.opt(k), bool):
            d[k] = ctx.opt(k)
    conf = (d, plist, errs)
    rep = probes.run_due(ctx.now, maint_cfg=ctx.cfg, conf=conf, force=ctx.opt("force", False) is True)
    return build_result(probes.snapshot(ctx.now, conf=conf), rep, ctx.now, ctx.opt("alert_mode", "task"))


# --------------------------------------------------------------------------- notifications (alert_mode = "events")
def to_event(ev: dict, notify_mod: Any = None):
    """One probe event (probes._events) -> notify.Event. Recoveries reuse the alert's dedupe_key so the notify layer can tie
    them together; texts are short ASCII (the SMS builder folds them again), facts are strings and carry no target or host."""
    if notify_mod is None:
        from .. import notify as notify_mod                    # lazy: notify pulls in the transports
    n = notify_mod
    name, title = str(ev.get("probe", "")), _ascii(ev.get("title", ev.get("probe", "")), 60)
    to, sev = ev.get("to"), ev.get("severity", "warn")
    facts = {"Probe": name, "Class": ev.get("class", "-"), "Now": str(to)}
    if to == "up":                                              # recovery
        mins = int(ev.get("down_s", 0) // 60)
        facts["Was down for"] = f"{mins} min"
        return n.Event(kind="recovery", severity="ok", title=f"{title} recovered",
                       summary=_ascii(f"{title} is back after {mins} min"), facts=facts, status="ok",
                       dedupe_key=ev.get("dedupe_key"), task="probes")
    word = {"down": "is down", "warn": "is degraded", "flapping": "keeps flapping", "storm": "many probes changed",
            "config": "has problems"}.get(str(to), "changed")
    head = title if to == "storm" else f"{title} {word}"
    sev = sev if sev in ("warn", "crit") else "warn"
    return n.Event(kind="alert", severity=sev, title=head, summary=_ascii(ev.get("detail") or head), facts=facts,
                   status=sev, dedupe_key=ev.get("dedupe_key"), task="probes")


_TIMED_OUT = object()


def _send_within(send: Any, event: Any, budget_s: float) -> Any:
    """send(event) with a hard wait limit: a transport that hangs (the Hermes bridge can take 90 s) must not eat the whole job.
    The call runs on a daemon thread; on timeout it is abandoned (it may still complete: the event then stays claimed and
    notify's dedupe window absorbs the repeat that follows the claim expiry). Returns the Delivery, None on an exception,
    or _TIMED_OUT."""
    box: dict = {}

    def go() -> None:
        try:
            box["d"] = send(event)
        except Exception:  # noqa: BLE001
            box["d"] = None

    t = threading.Thread(target=go, name="probe-notify", daemon=True)
    t.start()
    t.join(max(0.05, budget_s))
    return box["d"] if "d" in box else _TIMED_OUT


def notify_events(send: Any = None, notify_mod: Any = None, now: float | None = None, expire_s: float = 3600,
                  budget_s: float = NOTIFY_BUDGET_S) -> dict:
    """Deliver the probe event queue through notify.send (or `send`, for tests), in two phases so a page is never lost:
      1. probes.claim_events: events are CLAIMED (kept in the state file with a claim time), most urgent first; a claim left by
         a deliverer that died (TERM, KILL, OOM, job timeout) is requeued after probes.CLAIM_TTL_S and delivered again;
      2. each event is deleted (ack) only AFTER a handled delivery (sent, or dropped on purpose by policy such as dedupe or
         quiet hours). A failed delivery goes back with a growing back-off; an event older than `expire_s` is dropped (a page
         about something that happened hours ago, e.g. after switching alert_mode, is noise).
    The whole pass is capped at `budget_s`: events left when it runs out are released untouched ("deferred"), and a send that
    hangs is abandoned. Never raises. Returns {"sent","dropped","retry","deferred","expired"}."""
    out = {"sent": 0, "dropped": 0, "retry": 0, "deferred": 0, "expired": 0}
    t0 = time.monotonic()
    try:
        now = time.time() if now is None else float(now)
        events = probes.claim_events(now)
        if not events:
            return out
        if notify_mod is None:
            from .. import notify as notify_mod
        n = notify_mod
        send = send or n.send
        for i, ev in enumerate(events):
            eid = ev.get("id")
            if now - probes._fnum(ev.get("t"), now) > expire_s:
                probes.ack_event(eid)
                out["expired"] += 1
                continue
            left = budget_s - (time.monotonic() - t0)
            if left < min(MIN_SEND_S, budget_s / 4):         # too little time to finish a send: starting one only makes an abandoned call
                rest = [e["id"] for e in events[i:]]
                probes.release_events(rest, failed=False, now=now)
                out["deferred"] = len(rest)
                break
            try:
                d = _send_within(send, to_event(ev, n), left)
            except Exception:  # noqa: BLE001
                d = None
            if d is _TIMED_OUT:                       # may or may not have gone out: stays claimed, the rest is handed back
                rest = [e["id"] for e in events[i + 1:]]
                probes.release_events(rest, failed=False, now=now)
                out["retry"] += 1
                out["deferred"] = len(rest)
                break
            if d is not None and getattr(d, "ok", False):
                probes.ack_event(eid)
                out["sent"] += 1
            elif d is not None and getattr(d, "handled", False):
                probes.ack_event(eid)
                out["dropped"] += 1
            else:
                probes.release_events([eid], failed=True, now=now)
                out["retry"] += 1
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- website inventory
def slo_state(avail: float | None, slo: float | None) -> str | None:
    """ok | at_risk (more than half of the error budget burnt) | breached (availability below the objective)."""
    if avail is None or slo is None:
        return None
    if avail < slo:
        return "breached"
    return "at_risk" if (100 - avail) > 0.5 * (100 - slo) else "ok"


def export(now: float | None = None, conf: tuple | None = None) -> dict:
    """monitors.json: the inventory of everything monitored. Targets, hosts and paths are never included (see probes.how)."""
    now = time.time() if now is None else float(now)
    conf = conf or probes.load_probes()
    rows = probes.snapshot(now, conf=conf)
    st = probes.load_state()
    last = st.get("run", {}).get("last_end")
    out = []
    for r in sorted(rows, key=lambda r: ({"down": 0, "warn": 1, "unknown": 2, "up": 3, "skipped": 4, "paused": 5}[r["state"]], r["name"])):
        out.append({"name": r["name"], "title": r["title"], "type": r["type"], "group": r["group"], "class": r["class"],
                    "source": r["source"], "kuma": r["kuma"] or None, "kuma_paused": r["kuma_paused"], "state": r["state"],
                    "since": r["since"], "last_run": r["last_run"], "interval_s": r["interval_s"], "ms": r["ms"],
                    "detail": _ascii(r["detail"], 100) if r["state"] != "up" else "", "flapping": r["flapping"],
                    "how": _ascii(r["how"], 80), "avail_7d": r["avail_7d"], "avail_30d": r["avail_30d"],
                    "obs_7d": r.get("obs_7d"), "obs_30d": r.get("obs_30d"), "slo": r["slo"],
                    "slo_status": slo_state(r["avail_30d"], r["slo"]), "tags": r["tags"][:6], "optional": r["optional"]})
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    srcs: dict[str, int] = {}
    for r in rows:
        srcs[r["source"]] = srcs.get(r["source"], 0) + 1
    return {"schema": 1, "generated_at": now, "last_run": last, "stale": last is None or now - last > STALE_S,
            "summary": {"total": len(rows), **by_state}, "sources": srcs, "errors": len(conf[2]),
            "kuma": {"ported": sum(1 for r in rows if r["kuma"]), "paused_in_kuma": [r["kuma"] for r in rows if r["kuma_paused"]],
                     "push_ready": sum(1 for r in rows if r["kuma_push_key"])},
            "probes": out}


def heartbeat_payload(now: float | None = None, conf: tuple | None = None) -> dict:
    """The umbrella's dead-man's switch for /heartbeat or an external watcher: ok=False when the probe plane or the check tier
    has not completed a run for STALE_S. Small, no secrets."""
    now = time.time() if now is None else float(now)
    st = probes.load_state()
    run, cfg = st.get("run", {}), st.get("cfg", {})
    blind = bool(cfg.get("blind"))
    status = core.read_json(core.STATE_DIR / "status.json", {}) or {}

    def age(t: Any) -> int | None:
        return int(now - t) if probes._is_num(t) else None

    def fresh(a: int | None) -> bool:                 # None = never; below -SKEW_S = stamped in the future: the clock stepped back
        return a is not None and -probes.SKEW_S <= a < STALE_S

    p_age, s_age = age(run.get("last_end")), age(status.get("generated_at"))
    rows = probes.snapshot(now, conf=conf)
    down = [r for r in rows if r["state"] == "down"]
    return {"ok": fresh(p_age) and fresh(s_age) and not blind, "t": now,
            "clock_skew": any(a is not None and a < -probes.SKEW_S for a in (p_age, s_age)),
            "blind": blind, "config_problems": int(cfg.get("n", 0) or 0),
            "probes_age_s": p_age, "status_age_s": s_age, "total": len(rows),
            "up": sum(1 for r in rows if r["state"] == "up"), "down": len(down),
            "crit_down": sum(1 for r in down if r["severity"] == "crit")}
