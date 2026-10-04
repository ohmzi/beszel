"""Automatic swap relief: per-holder cold/active judgement, the decision machine (hysteresis, persistence, cooldown, breaker), the task."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import subprocess

import pytest

from homelab_maint import core, swapwatch as sw
from homelab_maint.core import GIB, Ctx
from homelab_maint.tasks import swap as swap_task
from homelab_maint.tasks import swap_auto

H = 3600.0
T0 = 1_800_000_000.0
REAL_OFF = sw.swap_expected_but_off          # the autouse fixture below stubs it for every other test


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "vmstat").write_text("pswpin 1000\npswpout 0\n")
    monkeypatch.setattr(sw, "PROC", proc)                                          # never the real /proc/vmstat
    monkeypatch.setattr(sw, "swap_expected_but_off", lambda *a, **k: False)


def an(pct=90.0, state="cold", used=29 * GIB, total=32 * GIB):
    return {"state": state, "used_pct": pct, "used_b": used, "total_b": total, "free_b": total - used, "in_mib_s": 0.0, "out_mib_s": 0.0, "exhausted": False}


def holder(who="idle-app", swap=8, act="cold", cur=1, mx=None, refault=0):
    return {"who": who, "kind": "container", "swap_b": swap * GIB, "current_b": cur * GIB, "max_b": None if mx is None else mx * GIB, "high_b": None, "procs": 1,
            "activity": act, "refault_anon": refault}


SAFE = {"safe": True, "needed": True, "blockers": [], "steps": [], "need_b": 29 * GIB, "headroom_b": 9 * GIB, "eta_s": 200, "why": ""}
UNSAFE = {**SAFE, "safe": False, "blockers": ["a protected workload is working (comfyui: queue running)"]}


# --------------------------------------------------------------------------- cold / active per holder
def test_a_holder_is_cold_active_or_unknown_by_its_refaults_and_cold_time_accumulates():
    st = {}
    h = lambda r: [dict(holder("a", refault=r), activity=None), dict(holder("b", refault=r * 1000), activity=None)]       # noqa: E731
    first = sw.track(st, h(100), T0)
    assert [x["activity"] for x in first] == ["unknown", "unknown"], "first sighting: no claim"
    h2 = [dict(holder("a", refault=100 + 60), activity=None), dict(holder("b", refault=100_000 + 60_000), activity=None)]      # 60 pages in 600 s = 0.1/s
    second = sw.track(st, h2, T0 + 600)
    assert second[0]["activity"] == "cold" and second[0]["cold_h"] == round(600 / H, 1) and second[0]["refault_pps"] == 0.1
    assert second[1]["activity"] == "active" and second[1]["cold_h"] is None
    third = sw.track(st, [dict(holder("a", refault=170), activity=None), dict(holder("b", refault=1), activity=None)], T0 + 600 + 2 * H)
    assert third[0]["cold_h"] == round((600 + 2 * H) / H, 1), "cold time keeps running from the first cold sighting"
    assert third[1]["activity"] == "unknown", "a counter that went backwards (restart) is not evidence of anything"
    assert sw.track(st, [], T0 + 9 * H) == [] and st["holders"] == {}, "holders that vanished are forgotten"


def test_cold_share_counts_only_known_cold_holders():
    assert sw.cold_share([]) == 0.0
    assert sw.cold_share([holder("a", 8, "cold"), holder("b", 2, "active")]) == 0.8
    assert sw.cold_share([holder("a", 8, "unknown")]) == 0.0


# --------------------------------------------------------------------------- the decision machine
NO_GUARDS = {"window_start_hour": 0.0, "window_end_hour": 0.0, "max_gap_hours": 1e9}      # these tests step by hours and ignore the clock; see test_swap_auto_review


def decide(st, now, a=None, hold=None, plan=None, **cfg):
    return sw.auto_decide(st, a or an(), hold if hold is not None else [holder()], plan, {**NO_GUARDS, **cfg}, now)


def test_nothing_to_watch_below_the_trigger_when_active_or_when_the_swap_is_not_cold_enough():
    st = {}
    assert decide(st, T0, an(pct=50))["action"] == "idle" and st.get("over_since") is None
    assert decide(st, T0, an(state="active"))["action"] == "idle"
    assert decide(st, T0, hold=[holder("a", 3, "cold"), holder("b", 8, "active")])["action"] == "idle", "73% active: working memory, not idle pages"
    assert "only 27% of it is cold" in decide(st, T0, hold=[holder("a", 3, "cold"), holder("b", 8, "active")])["reason"]
    assert decide(st, T0, an(state="none", pct=None))["action"] == "idle"


def test_it_waits_out_persist_hours_then_asks_for_the_plan_then_relieves():
    st = {}
    assert decide(st, T0)["action"] == "watching" and st["over_since"] == T0
    d = decide(st, T0 + 1 * H)
    assert d["action"] == "watching" and "clears after 2 h" in d["reason"]
    d = decide(st, T0 + 2 * H)
    assert d["action"] == "relieve" and d["need_plan"], "persisted: the caller must now compute the full plan (gates, disk) before anything"
    d = decide(st, T0 + 2 * H, plan=SAFE)
    assert d["action"] == "relieve" and not d.get("need_plan") and "quiet and 100% cold for 2.0 h" in d["reason"]


def test_hysteresis_the_watch_survives_dips_between_release_and_trigger_but_ends_below_release():
    st = {}
    decide(st, T0)
    assert decide(st, T0 + 1 * H, an(pct=70))["action"] == "watching", "70% is under the 80% trigger but over the 60% release: keep the timer"
    assert st["over_since"] == T0
    assert decide(st, T0 + 2 * H, an(pct=55))["action"] == "idle" and st["over_since"] is None, "under release: the watch is over"
    assert decide(st, T0 + 3 * H, an(pct=70))["action"] == "idle", "and it takes the full trigger to start again"
    assert decide(st, T0 + 3 * H + 60, an(state="active", pct=95))["action"] == "idle", "activity ends it too (someone is using that swap)"


def test_blocked_is_retried_and_never_forced_and_a_long_block_is_said_aloud():
    st = {}
    decide(st, T0)
    d = decide(st, T0 + 2 * H, plan=UNSAFE)
    assert d["action"] == "blocked" and "comfyui" in d["reason"] and st["blocked_since"] == T0 + 2 * H
    d = decide(st, T0 + 2 * H + 25 * H, plan=UNSAFE)
    assert d["action"] == "blocked" and d["blocked_h"] == 25.0
    d = decide(st, T0 + 2 * H + 26 * H, plan=SAFE)
    assert d["action"] == "relieve" and st["blocked_since"] is None


def test_cooldown_after_a_relief_and_a_quick_refill_counts_towards_the_breaker():
    st = {"last_relief_at": T0, "refill_checked": False}
    decide(st, T0 + 1 * H, an(pct=40))
    assert st.get("refills", 0) == 0 and st["refill_checked"] is False, "still inside the 6 h window and not refilled: keep looking"
    st2 = {"last_relief_at": T0, "refill_checked": False}
    d = decide(st2, T0 + 1 * H)                                           # back above 80% an hour after a relief
    assert st2["refills"] == 1 and st2["refill_checked"] is True and d["action"] == "watching"
    st2["over_since"] = T0 - 5 * H                                        # persisted long ago
    assert decide(st2, T0 + 2 * H, plan=SAFE)["action"] == "wait", "12 h cooldown since the last relief"
    # second quick refill in a row: the breaker opens and the automation pauses
    st2.update(last_relief_at=T0 + 20 * H, refill_checked=False)
    d = decide(st2, T0 + 21 * H)
    assert d["action"] == "breaker" and st2["breaker_until"] == T0 + 21 * H + 3 * 86400 and st2["refills"] == 0
    assert "paused until" in d["reason"] and decide(st2, T0 + 40 * H, plan=SAFE)["action"] == "breaker", "stays open for 3 days"
    assert decide(st2, T0 + 21 * H + 3 * 86400 + 1, a=an(pct=30))["action"] == "idle", "and closes by itself afterwards"


def test_staying_down_for_the_whole_refill_window_resets_the_count():
    st = {"last_relief_at": T0, "refill_checked": False, "refills": 1}
    decide(st, T0 + 7 * H, an(pct=35))
    assert st["refills"] == 0 and st["refill_checked"] is True


# --------------------------------------------------------------------------- the task
class World:
    def __init__(self, monkeypatch):
        self.calls, self.busy, self.unit_active = [], (False, "all gates idle"), False
        self.snap = {"mem": {"MemTotal": 94 * GIB, "MemAvailable": 66 * GIB, "SwapTotal": 32 * GIB, "SwapFree": 3 * GIB},
                     "rates": {"in_bps": 0.0, "out_bps": 0.0}, "psi": {"some60": 0.0, "full60": 0.0},
                     "holders": [{**holder("open-notebook", 20, "x", refault=100)}, {**holder("Seerr", 9, "x", refault=100)}],
                     "areas": [{"path": "/swap.img", "type": "file", "size_b": 32 * GIB, "used_b": 29 * GIB, "prio": "-2"}]}
        monkeypatch.setattr(sw, "snapshot", lambda **k: {**self.snap, "holders": [dict(h) for h in self.snap["holders"]]})
        monkeypatch.setattr(sw, "container_names", lambda: {})
        monkeypatch.setattr(sw, "disk_of", lambda p: "nvme0n1")
        monkeypatch.setattr(sw, "disk_busy_pct", lambda d, *a, **k: 3.0)
        monkeypatch.setattr(swap_auto.gates, "busy", lambda n, cfg=None: self.busy)

        def fake_sh(cmd, timeout=60, **k):
            self.calls.append(list(cmd))
            if cmd[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(cmd, 0 if self.unit_active else 3, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(swap_auto, "sh", fake_sh)
        monkeypatch.setattr(core, "audit", lambda *a, **k: None)

    def finish_relief(self, started_at, ok=True, note=""):
        """What the launched unit would have written to the ledger (the task reads the OUTCOME, never the launch)."""
        d = core.read_json(sw._ledger_path(), {}) or {}
        d.setdefault("done", []).append({"t0": started_at + 5, "t1": started_at + 200, "pages_in": 8_000_000 if ok else 0, "by": "auto", "ok": ok, "note": note})
        core.write_json_atomic(sw._ledger_path(), d, 0o644)

    def run(self, now, apply=True, state=None, cfg_mode=None, **opts):
        opts = {**NO_GUARDS, **opts}
        cfg = {"global": {}, "caps": {}, "protected": {}, "tasks": {"swap_auto_relief": {"mode": cfg_mode or ("apply" if apply else "report"), **opts}}}
        ctx = Ctx(cfg, "swap_auto_relief", apply, now)
        if state is not None:
            ctx.state = state
        r = swap_auto.swap_auto_relief(ctx)
        return r, ctx

    def started(self):
        return [c for c in self.calls if c[0] == "systemd-run"]


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_the_task_never_acts_before_persist_hours_and_only_in_apply_mode(world):
    st = world.run(T0)[1].state                                    # first sighting: holders unknown -> not cold -> idle
    r, ctx = world.run(T0 + 900, state=st)                          # now cold: watching starts
    assert r.metrics["action"] == "watching" and not world.started()
    r, ctx = world.run(T0 + 900 + 2 * H, state=ctx.state)
    assert r.metrics["action"] == "started" and len(world.started()) == 1
    cmd = world.started()[0]
    assert cmd[:5] == ["systemd-run", "--unit=homelab-maint-swap-relief", "--collect", "--no-block", "--quiet"]
    assert cmd[-4:] == ["swap", "relieve", "--apply", "--auto"] and "--property=IOSchedulingClass=idle" in cmd and "--property=RuntimeMaxSec=2700" in cmd
    assert not r.alert and "automatic swap relief started" in r.summary and "nothing is killed" in r.summary
    assert "ExecStopPost" in " ".join(cmd) and "--property=ExecStopPost=-" in " ".join(cmd) and "-a" in cmd[[i for i, c in enumerate(cmd) if c.startswith("--property=ExecStopPost")][0]]
    assert ctx.state["launched_at"] == T0 + 900 + 2 * H and "last_relief_at" not in ctx.state, "a launch is not a relief: only the outcome books one"
    assert ctx.state["over_since"] is not None, "the watch is cleared by a CONFIRMED relief, not by a launch"


def test_report_mode_says_what_it_would_do_and_starts_nothing(world):
    st = world.run(T0, apply=False)[1].state
    st = world.run(T0 + 900, apply=False, state=st)[1].state
    r, ctx = world.run(T0 + 900 + 2 * H, apply=False, state=st)
    assert not world.started() and "would clear the swap now" in r.summary and "last_relief_at" not in ctx.state and "launched_at" not in ctx.state


def test_a_busy_gate_blocks_it_and_nothing_starts(world):
    world.busy = (True, "comfyui: queue running")
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    r, ctx = world.run(T0 + 900 + 2 * H, state=st)
    assert not world.started() and r.metrics["action"] == "blocked" and "comfyui" in r.summary and not r.alert
    world.busy = (False, "all gates idle")
    r, ctx = world.run(T0 + 900 + 3 * H, state=ctx.state)
    assert r.metrics["action"] == "started", "retried on the next run and goes once the gate is idle"


def test_a_too_small_memory_cap_blocks_it(world):
    world.snap["holders"][0] = {**holder("open-notebook", 6, "x", cur=4, mx=8, refault=100)}      # 4 resident + 6 swapped > 90% of an 8 GiB cap
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    r, _ = world.run(T0 + 900 + 2 * H, state=st)
    assert r.metrics["action"] == "blocked" and "memory cap" in r.summary and not world.started()


def test_never_while_a_relief_is_running_by_ledger_or_unit(world, monkeypatch):
    sw.relief_begin(now=T0 - 10)
    r, _ = world.run(T0)
    assert "in progress" in r.summary and r.metrics["relief_active"] == 1 and not world.started()
    sw.relief_end()
    world.unit_active = True
    r, _ = world.run(T0)
    assert "in progress" in r.summary


def test_the_breaker_pages_with_the_top_holders_and_a_stable_issue_key(world):
    st = {"last_relief_at": T0 - 1 * H, "refill_checked": False, "refills": 1}
    r, ctx = world.run(T0, state=st)                                            # quick refill number two: the breaker opens on THIS run
    assert r.status == "warn" and r.alert and r.issue_key == "swap_auto:breaker" and "paused until" in r.summary and "open-notebook" in r.summary
    r2, ctx2 = world.run(T0 + 900, state=ctx.state)                             # still open: alert stays on, so the notifier can confirm it on 2 runs and page ONCE
    assert r2.status == "warn" and r2.alert and r2.issue_key == "swap_auto:breaker" and "paused until" in r2.summary
    assert not world.started()


def test_a_failing_systemd_run_backs_off_and_warns_instead_of_erroring_every_run(world, monkeypatch):
    real, tried = swap_auto.sh, []
    monkeypatch.setattr(swap_auto, "sh", lambda cmd, timeout=60, **k: (tried.append(cmd), subprocess.CompletedProcess(cmd, 1, "", "no bus"))[1] if cmd[0] == "systemd-run" else real(cmd, timeout, **k))
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    t = T0 + 900 + 2 * H
    r, ctx = world.run(t, state=st)
    assert r.status == "warn" and not r.alert and "could not start the swap relief" in r.summary and "systemd-run failed" in r.summary
    assert ctx.state["failed_attempts"] == 1 and ctx.state["retry_after"] == t + 3600 and "launched_at" not in ctx.state
    r, ctx = world.run(t + 900, state=ctx.state)                                  # next run: backing off, no second attempt
    assert "retrying in" in r.summary and len(tried) == 1


def test_a_paused_runner_does_not_start_it_and_leaves_the_stored_watch_alone(world, monkeypatch):
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    import copy
    before = copy.deepcopy(st)
    monkeypatch.setattr(core, "paused", lambda name=None: True)
    r, ctx = world.run(T0 + 900 + 2 * H, state=st)
    assert not world.started() and "would clear the swap now" in r.summary, "paused: it degrades to a report, it does not launch"
    assert ctx.state == before, "a run that cannot act must not move the apply lane's timers, backoff or breaker"


# --------------------------------------------------------------------------- swap_audit additions
def test_swap_audit_shows_how_long_a_holder_has_been_cold_and_flags_a_swap_that_is_off(monkeypatch):
    w = World(monkeypatch)
    cfg = {"tasks": {"swap_audit": {}}}
    ctx = Ctx(cfg, "swap_audit", False, T0)
    swap_task.swap_audit(ctx)
    ctx2 = Ctx(cfg, "swap_audit", False, T0 + 3 * H)
    ctx2.state = ctx.state
    r = swap_task.swap_audit(ctx2)
    assert r.items[0]["activity"] == "cold 3.0 h"
    monkeypatch.setattr(sw, "swap_expected_but_off", lambda *a, **k: True)
    r = swap_task.swap_audit(Ctx(cfg, "swap_audit", False, T0))
    assert r.status == "warn" and r.alert and "swap is OFF" in r.summary and "swapon -a" in r.summary and r.issue_key == "swap:off"


def test_swap_expected_but_off_reads_fstab_and_proc_swaps(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=x / ext4 defaults 0 1\n/swap.img none swap sw 0 0\n")
    (proc / "swaps").write_text("Filename Type Size Used Priority\n")
    monkeypatch_free = REAL_OFF
    assert monkeypatch_free(proc, fstab) is True
    (proc / "swaps").write_text("Filename Type Size Used Priority\n/swap.img file 33554428 0 -2\n")
    assert monkeypatch_free(proc, fstab) is False, "swap is on"
    (proc / "swaps").write_text("Filename Type Size Used Priority\n")
    fstab.write_text("UUID=x / ext4 defaults 0 1\n# /swap.img none swap sw 0 0\n")
    assert monkeypatch_free(proc, fstab) is False, "a commented-out swap line is not an expectation"
