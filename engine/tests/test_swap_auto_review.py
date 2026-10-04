"""Tests added after the adversarial review of the automatic swap relief: every confirmed finding, and the mutants that survived the first suites.

Hermetic: STATE_DIR, PROC, /etc/fstab and the clock are faked by test_swap_auto's autouse `isolated` fixture; nothing here touches a real swap area."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import signal
import subprocess
import time
import types

import pytest

from homelab_maint import core, swapwatch as sw
from homelab_maint.core import GIB
from homelab_maint.tasks import swap_auto
from test_swap_auto import NO_GUARDS, SAFE, UNSAFE, H, T0, an, decide, holder, isolated, world      # noqa: F401  (fixtures + helpers, not the tests)
import test_swapwatch as tw


def at_hour(h, m=0):
    """A local-time epoch at hour h:m on some fixed day (the window is judged in local time)."""
    return time.mktime((2026, 10, 3, h, m, 0, 0, 0, -1))


# =========================================================================== decision machine
def test_a_block_in_a_second_episode_does_not_crash():
    """Regression: st.setdefault('blocked_since') kept a stored None and the next line raised TypeError (the task errored and paged CRIT every run)."""
    st = {}
    decide(st, T0)
    decide(st, T0 + 1 * H, an(pct=50))                       # the watch ends below release: blocked_since is now None (key present)
    decide(st, T0 + 2 * H)                                   # a new watch
    d = decide(st, T0 + 4 * H, plan=UNSAFE)                  # persisted, plan blocked: must say "blocked", not raise
    assert d["action"] == "blocked" and d["blocked_h"] == 0.0


def test_the_cold_share_is_rechecked_when_the_timer_runs_out_and_a_real_change_ends_the_watch():
    st = {}
    decide(st, T0)                                           # 100 % cold: the watch starts
    d = decide(st, T0 + 2 * H, hold=[holder("a", 8, "active")], plan=SAFE)
    assert d["action"] == "idle" and st["over_since"] is None, "every holder is refaulting: that is working memory, not idle pages"
    st = {}
    decide(st, T0)
    mixed = [holder("a", 6, "cold"), holder("b", 4, "active")]                      # 60 %: under the 70 % to act, over the 50 % to keep watching
    d = decide(st, T0 + 2 * H, hold=mixed, plan=SAFE)
    assert d["action"] == "watching" and "cold share dipped to 60%" in d["reason"] and st["over_since"] == T0, "survives a dip, but does not relieve on it"
    assert decide(st, T0 + 2 * H + 60, hold=[holder("a", 4, "cold"), holder("b", 6, "active")], plan=SAFE)["action"] == "idle", "40 %: it ends"


@pytest.mark.parametrize("state", ["active", "thrashing"])
def test_a_swap_that_turns_busy_during_the_watch_ends_it(state):
    st = {}
    decide(st, T0)
    assert decide(st, T0 + 1 * H, an(state=state, pct=95))["action"] == "idle" and st["over_since"] is None
    assert decide(st, T0 + 2 * H)["action"] == "watching" and st["over_since"] == T0 + 2 * H, "the 2 h start over"
    assert decide({}, T0, an(state=state))["action"] == "idle"


def test_quiet_also_means_the_kernel_is_not_pushing_pages_out():
    quiet_in_but_loud_out = dict(an(), out_mib_s=20.0)
    assert decide({}, T0, quiet_in_but_loud_out)["action"] == "idle"
    assert decide({}, T0, dict(an(), out_mib_s=2.0))["action"] == "watching"
    assert decide({}, T0, quiet_in_but_loud_out, quiet_out_mib_s=50.0)["action"] == "watching"


def test_a_cancelled_watch_forgets_its_block_timer():
    st = {}
    decide(st, T0)
    decide(st, T0 + 2 * H, plan=UNSAFE)                      # blocked since T0 + 2 h
    decide(st, T0 + 3 * H, an(pct=40))                       # the watch is over
    assert st["over_since"] is None and st["blocked_since"] is None


def test_the_cold_share_needs_exactly_min_cold_share_to_start():
    assert decide({}, T0, hold=[holder("a", 7, "cold"), holder("b", 3, "active")])["action"] == "watching"                  # 70 % starts
    assert decide({}, T0, hold=[holder("a", 6, "cold"), holder("b", 4, "active")])["action"] == "idle"
    assert decide({}, T0, hold=[holder("a", 7, "cold"), holder("b", 3, "active")], min_cold_share=0.71)["action"] == "idle"


def test_the_trigger_is_inclusive_and_a_refill_inside_the_band_counts():
    assert decide({}, T0, an(pct=80.0))["action"] == "watching"
    st = {"last_relief_at": T0, "refill_checked": False}
    decide(st, T0 + 1 * H, an(pct=70))                                                       # 60..80 %: not "back above trigger": no refill
    assert st.get("refills", 0) == 0 and st["refill_checked"] is False
    st = {"last_relief_at": T0, "refill_checked": False}
    decide(st, T0 + 5 * H)                                                                   # back at 90 % five hours after a relief: still quick (refill_hours = 6)
    assert st["refills"] == 1


def test_the_cooldown_is_twelve_hours_not_less():
    st = {"last_relief_at": T0, "refill_checked": True, "over_since": T0 - 9 * H}
    assert decide(st, T0 + 6 * H, plan=SAFE)["action"] == "wait"
    assert decide(st, T0 + 12 * H, plan=SAFE)["action"] == "relieve"


def test_time_nobody_watched_never_counts_as_hanging_around():
    """Host down, PAUSE, missed runs or a clock jump: the persistence timer restarts instead of becoming true on one sample."""
    cfg = {"max_gap_hours": 0.75}
    st = {}
    sw.auto_decide(st, an(), [holder()], None, {**NO_GUARDS, **cfg}, T0)                                 # start
    d = sw.auto_decide(st, an(), [holder()], None, {**NO_GUARDS, **cfg}, T0 + 26 * H)                     # next sighting a day later
    assert d["action"] == "watching" and st["over_since"] == T0 + 26 * H, "not 26 h of 'hanging around'"
    t = T0 + 26 * H
    for i in range(1, 9):                                                                                 # 15-minute runs, like production
        d = sw.auto_decide(st, an(), [holder()], None, {**NO_GUARDS, **cfg}, t + i * 900)
    assert d["action"] == "relieve", "two real hours of 15-minute observations do count"
    d = sw.auto_decide(st, an(), [holder()], None, {**NO_GUARDS, **cfg}, t + 8 * 900 - 7200)               # a clock that jumped BACK also restarts it
    assert d["action"] == "watching"


def test_in_window_handles_the_normal_the_wrapped_and_the_always_window():
    assert sw.in_window(at_hour(3), 2, 6) and sw.in_window(at_hour(2, 0), 2, 6) and sw.in_window(at_hour(5, 59), 2, 6)
    assert not sw.in_window(at_hour(6, 0), 2, 6) and not sw.in_window(at_hour(1, 59), 2, 6) and not sw.in_window(at_hour(14), 2, 6)
    assert sw.in_window(at_hour(23), 22, 6) and sw.in_window(at_hour(5), 22, 6) and not sw.in_window(at_hour(12), 22, 6)
    assert sw.in_window(at_hour(14), 0, 0) and sw.in_window(at_hour(14), 5, 5)


def test_it_acts_only_inside_the_maintenance_window():
    st = {"over_since": at_hour(0) - 10 * H, "last_seen": at_hour(14)}
    cfg = {"window_start_hour": 2.0, "window_end_hour": 6.0, "max_gap_hours": 1e9}
    d = sw.auto_decide(st, an(), [holder()], SAFE, cfg, at_hour(14))
    assert d["action"] == "wait" and "outside the maintenance window (2:00-6:00)" in d["reason"]
    assert sw.auto_decide(st, an(), [holder()], SAFE, cfg, at_hour(3) + 86400)["action"] == "relieve"      # 03:00 the next day


def test_a_failed_attempt_backs_off_and_is_retried_afterwards():
    st = {"over_since": T0 - 9 * H, "retry_after": T0 + 30 * 60}
    d = decide(st, T0, plan=SAFE)
    assert d["action"] == "wait" and "retrying in 30 min" in d["reason"]
    assert decide(st, T0 + 31 * 60, plan=SAFE)["action"] == "relieve"


def test_one_old_quick_refill_does_not_count_as_in_a_row():
    st = {"refills": 1, "last_refill_at": T0 - 4 * 86400}
    decide(st, T0, an(pct=40))
    assert st["refills"] == 0
    st = {"refills": 1, "last_refill_at": T0 - 1 * 86400}
    decide(st, T0, an(pct=40))
    assert st["refills"] == 1


# =========================================================================== the task
def finish(world, ctx_state, started_at, **kw):
    world.finish_relief(started_at, **kw)


def run_to_launch(world, apply=True, **opts):
    st = world.run(T0, apply, **opts)[1].state
    st = world.run(T0 + 900, apply, state=st, **opts)[1].state
    t = T0 + 900 + 2 * H
    r, ctx = world.run(t, apply, state=st, **opts)
    return r, ctx, t


def test_a_launch_is_confirmed_only_by_the_ledger_outcome(world):
    r, ctx, t = run_to_launch(world)
    assert r.metrics["action"] == "started" and "last_relief_at" not in ctx.state
    r, ctx = world.run(t + 300, state=ctx.state)                                      # the unit has not written anything yet
    assert "waiting for it to start" in r.summary and not r.alert and len(world.started()) == 1
    world.snap["mem"]["SwapFree"] = 28 * GIB                                          # and it did empty the swap
    world.finish_relief(t, ok=True)
    r, ctx = world.run(t + 900, state=ctx.state)
    assert ctx.state["last_relief_at"] == t + 200 and ctx.state["failed_attempts"] == 0 and "launched_at" not in ctx.state
    assert ctx.state.get("refills", 0) == 0


def test_a_refused_or_aborted_relief_is_not_a_quick_refill_and_never_opens_the_breaker(world):
    r, ctx, t = run_to_launch(world)
    for n in range(1, 4):                                                             # three launches, each refused by the unit (a gate went busy meanwhile)
        world.finish_relief(t, ok=False, note="refused: a protected workload is working (plex)")
        r, ctx = world.run(t + 900, state=ctx.state)
        assert ctx.state["failed_attempts"] == n and "last_relief_at" not in ctx.state
        assert ctx.state["retry_after"] == t + 900 + min(1.0 * 2 ** (n - 1), 24.0) * 3600, "doubling backoff"
        assert r.metrics["action"] != "breaker" and not r.alert
        t = ctx.state["retry_after"] + 900
        r, ctx = world.run(t, state=ctx.state)                                        # the retry
        assert r.metrics["action"] == "started", r.summary
    assert world.run(t + 900, state=ctx.state)[0].metrics.get("action") != "breaker"      # (waiting for the launched unit: no decision)
    assert ctx.state.get("refills", 0) == 0 and ctx.state.get("breaker_until") is None


def test_a_relief_that_keeps_failing_is_a_visible_warning_never_a_page(world):
    r, ctx, t = run_to_launch(world)
    for n in range(3):
        world.finish_relief(t, ok=False, note="aborted: memory stall rose above 10%")
        r, ctx = world.run(t + 900, state=ctx.state)
        t = ctx.state["retry_after"] + 900
        if n < 2:
            r, ctx = world.run(t, state=ctx.state)
    assert ctx.state["failed_attempts"] == 3
    assert r.status == "warn" and not r.alert and "last 3 attempts failed" in r.summary and "memory stall" in r.summary


def test_a_launch_that_leaves_no_outcome_for_half_an_hour_is_a_failure(world):
    r, ctx, t = run_to_launch(world)
    r, ctx = world.run(t + 25 * 60, state=ctx.state)
    assert "waiting for it to start" in r.summary
    r, ctx = world.run(t + 31 * 60, state=ctx.state)
    assert ctx.state["failed_attempts"] == 1 and "launched_at" not in ctx.state and "left no outcome" in ctx.state["last_failure"]


def test_two_quick_refills_after_confirmed_reliefs_open_the_breaker_end_to_end_and_page_once(world):
    r, ctx, t = run_to_launch(world)
    assert r.metrics["action"] == "started"
    world.finish_relief(t, ok=True)                                                   # confirmed, but swap is still 91 % an hour later
    r, ctx = world.run(t + 1 * H, state=ctx.state)
    assert ctx.state["refills"] == 1
    t2 = t + 13 * H                                                                   # cooled down: second relief
    r, ctx = world.run(t2, state=ctx.state)
    assert r.metrics["action"] == "started" and ctx.state["launched_at"] == t2
    world.finish_relief(t2, ok=True)
    r, ctx = world.run(t2 + 1 * H, state=ctx.state)                                   # full again: refill two opens the breaker
    assert r.metrics["action"] == "breaker" and r.status == "warn" and r.alert and r.issue_key == "swap_auto:breaker"
    r, ctx = world.run(t2 + 2 * H, state=ctx.state)
    assert r.metrics["action"] == "breaker" and r.alert, "stays alert for the whole pause: the notifier needs 2 runs in a row to confirm, then pages once"


def test_a_busy_swap_disk_or_an_unreadable_one_blocks_the_task(world, monkeypatch):
    monkeypatch.setattr(sw, "disk_busy_pct", lambda d, *a, **k: 80.0)
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    r, ctx = world.run(T0 + 900 + 2 * H, state=st)
    assert r.metrics["action"] == "blocked" and "swap disk is 80% busy" in r.summary and not world.started()
    monkeypatch.setattr(sw, "disk_of", lambda p: None)
    r, _ = world.run(T0 + 900 + 3 * H, state=ctx.state)
    assert r.metrics["action"] == "blocked" and "could not read how busy" in r.summary and not world.started()


def test_a_block_that_lasts_max_defer_hours_is_a_warning_and_never_a_page(world):
    world.busy = (True, "plex: transcoding")
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    t = T0 + 900 + 2 * H
    r, ctx = world.run(t, state=st)
    assert r.status == "info"
    r, ctx = world.run(t + 23 * H, state=ctx.state)
    assert r.status == "info", "23 h: not yet"
    r, ctx = world.run(t + 25 * H, state=ctx.state)
    assert r.status == "warn" and not r.alert and "blocked for 25 h" in r.summary


def test_the_task_honours_its_options_and_checks_the_unit_it_starts(world):
    bump = lambda n: [h.__setitem__("refault_anon", h["refault_anon"] + n) for h in world.snap["holders"]]      # noqa: E731
    st = world.run(T0)[1].state
    bump(18_000)                                                                             # 20 pages/s over 900 s
    assert world.run(T0 + 900, state=st)[0].metrics["cold_share_pct"] == 0, "20/s >= the shipped 5/s: active"
    bump(-18_000)
    opts = {"persist_hours": 1.0, "cold_refault_pps": 50.0}
    st = world.run(T0, **opts)[1].state
    bump(18_000)
    r, ctx = world.run(T0 + 900, state=st, **opts)
    assert r.metrics["cold_share_pct"] == 100 and r.metrics["action"] == "watching", "20/s < 50/s: cold"
    r, ctx = world.run(T0 + 900 + 1 * H, state=ctx.state, **opts)
    assert r.metrics["action"] == "started", "persist_hours = 1 is honoured by BOTH auto_decide calls"
    assert any(c[:2] == ["systemctl", "is-active"] and c[-1] == "homelab-maint-swap-relief.service" for c in world.calls)
    cmd = world.started()[0]
    assert cmd[-5].startswith("/") and "--property=Nice=19" in cmd and cmd[-4:] == ["swap", "relieve", "--apply", "--auto"]


def test_the_task_waits_for_the_maintenance_window(world):
    out = {"window_start_hour": float((time.localtime(T0 + 900 + 2 * H).tm_hour + 5) % 24), "window_end_hour": float((time.localtime(T0 + 900 + 2 * H).tm_hour + 6) % 24)}
    r, ctx, t = run_to_launch(world, **out)
    assert r.metrics["action"] == "wait" and "maintenance window" in r.summary and not world.started()


def test_observations_from_another_lane_do_not_carry_over(world):
    st = world.run(T0, apply=False)[1].state
    st = world.run(T0 + 900, apply=False, state=st)[1].state
    before = st["over_since"]                                       # (st is mutated in place by the next run: keep the value)
    assert before == T0 + 900
    r, ctx = world.run(T0 + 1800, apply=True, state=st)
    assert ctx.state["lane"] == "apply" and r.metrics["action"] == "watching" and not world.started()
    assert ctx.state["over_since"] == T0 + 1800 != before, "the apply lane starts its own clock; it does not inherit the report lane's"


def test_every_cgroup_with_swap_counts_for_the_caps_including_the_small_ones(world):
    world.snap["all_holders"] = world.snap["holders"] + [dict(holder("tiny-capped", 0.02, "x", cur=0.11, mx=0.125), activity="x")]
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    r, _ = world.run(T0 + 900 + 2 * H, state=st)
    assert r.metrics["action"] == "blocked" and "tiny-capped" in r.summary and not world.started()


def test_swap_that_is_listed_but_off_is_switched_back_on_in_apply_mode_and_only_reported_otherwise(world, monkeypatch):
    monkeypatch.setattr(sw, "swap_expected_but_off", lambda *a, **k: True)
    r, ctx = world.run(T0)
    assert ["swapon", "-a"] in world.calls and r.status == "warn" and r.alert and "switched back on" in r.summary and r.issue_key == "swap_auto:swap-off"
    world.calls.clear()
    r, ctx = world.run(T0, apply=False)
    assert ["swapon", "-a"] not in world.calls and r.status == "info" and not r.alert and "would run swapon -a" in r.summary


# =========================================================================== relief_plan: constants and the cgroups it must look at
def test_the_headroom_is_sixteen_gib_or_fifteen_percent_of_ram():
    blk = lambda p: any("not enough free RAM" in b for b in p["blockers"])                    # noqa: E731
    assert blk(tw.plan(m=tw.mem(avail=32 + 15))) and not blk(tw.plan(m=tw.mem(avail=32 + 17)))                 # 94 GiB: max(16, 14.1) = 16 GiB stays free
    assert blk(tw.plan(m=tw.mem(total=40, avail=32 + 15))) and not blk(tw.plan(m=tw.mem(total=40, avail=32 + 17)))
    assert blk(tw.plan(m=tw.mem(total=200, avail=32 + 29))) and not blk(tw.plan(m=tw.mem(total=200, avail=32 + 31)))    # 200 GiB: 15 % = 30 GiB


def test_a_cap_needs_a_ten_percent_margin():
    p = tw.plan(hold=[tw.holder("nb", swap=4.5, cur=3, mx=8)])                                # 7.5 GiB fits the 8 GiB cap, not 90 % of it
    assert not p["safe"] and "memory cap" in p["blockers"][0]
    assert tw.plan(hold=[tw.holder("nb", swap=4, cur=3, mx=8)])["safe"]                        # 7.0 < 7.2


def test_memory_high_blocks_because_the_pages_would_be_pushed_straight_back_out():
    h = dict(tw.holder("svc", swap=3, cur=2), high_b=4 * GIB)
    p = tw.plan(hold=[h])
    assert not p["safe"] and "memory.high" in p["blockers"][0] and "pushed back out" in p["blockers"][0]
    assert tw.plan(hold=[dict(h, high_b=8 * GIB)])["safe"]


def test_an_ancestors_limit_bounds_the_sum_of_what_its_holders_take_back():
    anc = [{"path": "/system.slice", "max_b": 10 * GIB, "current_b": 6 * GIB}]
    a, b = dict(tw.holder("a", swap=2, cur=1), anc=anc), dict(tw.holder("b", swap=2, cur=1), anc=anc)
    assert tw.plan(hold=[a])["safe"], "6 + 2 fits 90 % of 10 GiB"
    p = tw.plan(hold=[a, b])                                                                  # 6 + 4 = 10 GiB > 9
    assert not p["safe"] and "/system.slice" in p["blockers"][0] and "OOM-kill inside it" in p["blockers"][0]


def test_holders_report_ancestor_limits_and_a_floor_of_zero_sees_the_small_ones(tmp_path):
    root = tmp_path / "cg"
    tw.cg(root, "system.slice/svc.service", 20 * 2**20, cur=100 * 2**20, mx=str(128 * 2**20))                 # 20 MiB swapped: under the 32 MiB display floor
    (root / "system.slice").mkdir(exist_ok=True)
    (root / "system.slice" / "memory.max").write_text(f"{4 * GIB}\n")
    (root / "system.slice" / "memory.current").write_text(f"{2 * GIB}\n")
    assert sw.holders(root) == []
    h = sw.holders(root, min_b=1)
    assert len(h) == 1 and h[0]["max_b"] == 128 * 2**20 and h[0]["anc"] == [{"path": "/system.slice", "max_b": 4 * GIB, "current_b": 2 * GIB}]
    p = tw.plan(hold=h)
    assert not p["safe"] and "svc" in p["blockers"][0], "the tightly capped small holder blocks the relief"
    snap = sw.snapshot(proc=tmp_path, cg=root, sample_s=0, sleep=lambda s: None)
    assert snap["holders"] == [] and len(snap["all_holders"]) == 1


def test_the_psi_and_disk_limits_are_the_shipped_ones():
    assert tw.plan(ps=tw.psi(0.5))["safe"] and not tw.plan(ps=tw.psi(1.5))["safe"]
    assert tw.plan(disk=39.0)["safe"] and not tw.plan(disk=40.0)["safe"] and not tw.plan(disk=41.0)["safe"]
    assert tw.plan(ar=tw.areas(used=1.5))["needed"] and not tw.plan(ar=tw.areas(used=0.9))["needed"]


def test_only_the_full_stall_counts_not_the_some_stall():
    assert tw.plan(ps={"some60": 30.0, "full60": 0.1})["safe"]
    assert not tw.plan(ps={"some60": 0.1, "full60": 4.0})["safe"]
    a = sw.analyse(tw.mem(swap_free=2), tw.rates(20, 5), {"some60": 9.0, "full60": 0.1})
    assert a["state"] == "active", "heavy swap-in with only a 'some' stall is busy, not thrashing"


def test_psi_mem_reads_avg60_of_some_and_of_full(tmp_path):
    p = tmp_path / "proc"
    (p / "pressure").mkdir(parents=True)
    (p / "pressure" / "memory").write_text("some avg10=1.00 avg60=2.50 avg300=3.00 total=1\nfull avg10=0.10 avg60=0.25 avg300=0.30 total=1\n")
    assert sw.psi_mem(p) == {"some60": 2.5, "full60": 0.25}


def test_holders_report_the_anon_refault_counter_and_nothing_else(tmp_path):
    root = tmp_path / "cg"
    tw.cg(root, "system.slice/x.service", 2 * GIB)
    (root / "system.slice/x.service/memory.stat").write_text("anon 1\nworkingset_refault_anon 1470\nworkingset_refault_file 916237\nworkingset_activate_anon 355\n")
    tw.cg(root, "system.slice/y.service", 1 * GIB)                                           # no memory.stat at all
    h = {x["who"]: x for x in sw.holders(root)}
    assert h["x"]["refault_anon"] == 1470 and h["y"]["refault_anon"] is None


# =========================================================================== the guarded apply
def apply_world(monkeypatch, behave, avail=60.0, psi=None, ar=None, swapon_rcs=(0,), clock=None, by="owner"):
    ar = ar or tw.areas()
    state, ran, procs, swapons = {"on": True}, [], [], iter(list(swapon_rcs) + [swapon_rcs[-1]] * 10)
    if clock is not None:
        monkeypatch.setattr(sw, "time", types.SimpleNamespace(time=sw.time.time, sleep=lambda s: None, monotonic=clock, strftime=sw.time.strftime, localtime=sw.time.localtime))
    monkeypatch.setattr(sw, "meminfo", lambda: {"MemTotal": 94 * GIB, "MemAvailable": avail * GIB})
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: psi or {"some60": 0.0, "full60": 0.0})
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: ar if state["on"] else [])
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)

    def popen(cmd, **k):
        ran.append(cmd)
        procs.append(tw.FakeProc(list(behave), state))
        return procs[-1]

    def run(cmd, **k):
        ran.append(cmd)
        rc = next(swapons)
        state["on"] = rc == 0
        return subprocess.CompletedProcess(cmd, rc, "", "swapon: failed" if rc else "")
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    return sw._apply(ar, by=by), ran, procs, state


def test_a_tight_memory_abort_really_signals_swapoff(monkeypatch):
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 3, avail=3.0)
    assert rc == 1 and procs[0].terminated, "the abort must SIGTERM swapoff, otherwise it keeps pulling pages while RAM is short"
    assert sw._ledger()["done"][-1]["ok"] is False


def test_a_memory_stall_abort_really_signals_swapoff(monkeypatch):
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 3, psi={"some60": 0.0, "full60": 12.0})
    assert rc == 1 and procs[0].terminated
    rc, ran, procs, state = apply_world(monkeypatch, ["run", "done"], psi={"some60": 40.0, "full60": 0.2})
    assert rc == 0 and not procs[0].terminated, "a high 'some' stall alone is not the abort condition"


@pytest.mark.parametrize("avail,aborts", [(7.9, True), (8.1, False)])
def test_the_abort_floor_on_a_94_gib_host_is_eight_gib(monkeypatch, avail, aborts):
    rc, ran, procs, state = apply_world(monkeypatch, ["run", "done"], avail=avail)
    assert (rc == 1) == aborts and procs[0].terminated == aborts


def test_swapoff_is_cut_after_thirty_minutes(monkeypatch):
    ticks = iter([0.0] + [1801.0] * 50)
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 5, clock=lambda: next(ticks))
    assert rc == 1 and procs[0].terminated


def test_a_failed_swapon_is_retried_and_a_persistent_failure_is_a_failed_relief(monkeypatch):
    rc, ran, procs, state = apply_world(monkeypatch, ["done"], swapon_rcs=(1,))
    assert rc != 0 and [c for c in ran if c[0] == "swapon"] == [["swapon", "/swap.img"]] * 3, "three attempts, then it says so"
    assert sw._ledger()["done"][-1]["ok"] is False
    rc, ran, procs, state = apply_world(monkeypatch, ["done"], swapon_rcs=(1, 1, 0))
    assert rc == 0 and state["on"], "a transient failure must not leave the box without swap"


def test_a_multi_area_relief_stops_at_the_first_abort(monkeypatch):
    two = [dict(tw.areas()[0], path="/swap1"), dict(tw.areas()[0], path="/swap2")]
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 3, avail=3.0, ar=two)
    assert rc == 1 and [c[-1] for c in ran if c[0] == "ionice"] == ["/swap1"]


def test_an_area_with_nothing_in_it_is_not_swapped_off(monkeypatch):
    rc, ran, procs, state = apply_world(monkeypatch, ["done"], ar=[dict(tw.areas()[0], used_b=0)])
    assert rc == 0 and ran == []


def allow(monkeypatch, mode="apply", paused=False):
    monkeypatch.setattr(core, "paused", lambda name=None: paused and name == "swap_auto_relief")      # only the TASK's own switch (the name matters)
    monkeypatch.setattr(core, "load_config", lambda *a, **k: {"tasks": {"swap_auto_relief": {"mode": mode}}})


def test_the_kill_switch_stops_an_automatic_relief_but_not_the_owners(monkeypatch):
    allow(monkeypatch, paused=True)
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 8, by="auto")
    assert rc == 1 and procs[0].terminated and state["on"], "PAUSE aborts it and the swap stays in service"
    assert "kill switch" in sw._ledger()["done"][-1]["note"] or sw._ledger()["done"][-1]["ok"] is False
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 8 + ["done"], by="owner")
    assert rc == 0 and not procs[0].terminated, "the owner's own relief is not stopped by the task's switch"


def test_switching_the_rule_back_to_report_also_stops_a_running_automatic_relief(monkeypatch):
    allow(monkeypatch, mode="report")
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 8, by="auto")
    assert rc == 1 and procs[0].terminated, "mode left apply: the unit stops itself and the swap stays in service"
    allow(monkeypatch, mode="apply")
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 8 + ["done"], by="auto")
    assert rc == 0 and not procs[0].terminated, "still allowed: it runs to the end"
    monkeypatch.setattr(core, "load_config", lambda *a, **k: (_ for _ in ()).throw(OSError("config unreadable")))
    rc, ran, procs, state = apply_world(monkeypatch, ["run"] * 8 + ["done"], by="auto")
    assert rc == 0, "an unreadable config must not stop a relief that is running"


def test_sigterm_takes_the_same_safe_path_as_ctrl_c_and_the_handler_is_restored(monkeypatch):
    before = signal.getsignal(signal.SIGTERM)
    state = {"on": True}

    class Killed(tw.FakeProc):
        def poll(self):
            if not self.terminated and not getattr(self, "sent", False):
                self.sent = True
                os.kill(os.getpid(), signal.SIGTERM)             # systemctl stop / RuntimeMaxSec
            return super().poll()
    ran = []
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: tw.areas() if state["on"] else [])
    monkeypatch.setattr(sw, "meminfo", lambda: {"MemTotal": 94 * GIB, "MemAvailable": 60 * GIB})
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: {"some60": 0.0, "full60": 0.0})
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: Killed(["run"] * 3, state))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (ran.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    assert sw._apply(tw.areas()) == 1, "an interrupted relief is not a successful one"
    assert state["on"] and not sw.relief_active() and signal.getsignal(signal.SIGTERM) == before
    assert sw._ledger()["done"][-1]["ok"] is False


def test_only_one_relief_runs_at_a_time(monkeypatch):
    with sw.relief_lock() as got:
        assert got is True
        with sw.relief_lock() as again:
            assert again is False, "a second relief cannot take the lock"
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not start swapoff while another relief holds the lock"))
        assert sw._apply(tw.areas(), by="auto") == 1
    assert sw.relief_outcome(0, by="auto")["note"].startswith("refused: another relief")
    with sw.relief_lock() as free:
        assert free is True, "the lock is released when its holder is done"


def test_a_second_live_relief_cannot_take_the_marker_but_a_dead_ones_marker_is_replaced():
    assert sw.relief_begin(pid=os.getppid(), now=time.time()) is True                    # a live process of someone else
    assert sw.relief_begin(pid=os.getpid()) is False, "another live relief owns the marker"
    d = sw._ledger()
    d["active"]["pid"] = 2 ** 22 + 99                                                    # nobody
    core.write_json_atomic(sw._ledger_path(), d, 0o644)
    assert sw.relief_begin(pid=os.getpid()) is True


def test_an_aborted_relief_records_its_failure_for_whoever_launched_it(monkeypatch):
    apply_world(monkeypatch, ["run"] * 3, avail=3.0, by="auto")
    out = sw.relief_outcome(0, by="auto")
    assert out["ok"] is False and out["by"] == "auto" and out["note"]
    assert sw.relief_outcome(0, by="owner") is None and sw.relief_outcome(out["t0"] + 1, by="auto") is None


def test_the_journal_says_who_did_it_and_whether_it_finished(monkeypatch):
    notes = []
    import homelab_maint.routine as routine
    monkeypatch.setattr(routine, "journal_note", lambda title, detail="", now=None: notes.append(title))
    apply_world(monkeypatch, ["run", "done"], by="auto")
    apply_world(monkeypatch, ["run", "done"], by="owner")
    apply_world(monkeypatch, ["run"] * 3, avail=3.0, by="owner")
    assert notes == ["Swap relieved automatically", "Swap relieved by the owner", "Swap relief stopped early"]


# =========================================================================== the CLI
def cli_world(monkeypatch, busy=(False, "all gates idle"), mode="apply", paused=False):
    snap = {"mem": tw.mem(avail=66), "rates": tw.rates(0, 0), "psi": tw.psi(0.03), "holders": [tw.holder()], "areas": tw.areas()}
    monkeypatch.setattr(sw, "snapshot", lambda **k: snap)
    monkeypatch.setattr(sw, "container_names", lambda: {})
    monkeypatch.setattr(sw, "disk_of", lambda p: "nvme0n1")
    monkeypatch.setattr(sw, "disk_busy_pct", lambda d, *a, **k: 3.0)
    monkeypatch.setattr(sw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(core, "load_config", lambda *a, **k: {"tasks": {"swap_auto_relief": {"mode": mode}}})
    monkeypatch.setattr(core, "paused", lambda name=None: paused)
    got = []
    monkeypatch.setattr(sw, "_apply", lambda a, by="owner": got.append(by) or 0)
    from homelab_maint.tasks import gates
    monkeypatch.setattr(gates, "busy", (lambda n, cfg=None: busy) if not isinstance(busy, Exception) else (lambda n, cfg=None: (_ for _ in ()).throw(busy)))
    return got


def test_the_auto_flag_reaches_the_relief_and_the_owners_run_is_unaffected(monkeypatch):
    got = cli_world(monkeypatch)
    assert sw.swap_main(["relieve", "--apply", "--auto"]) == 0 and sw.swap_main(["relieve", "--apply"]) == 0
    assert got == ["auto", "owner"]


def test_an_unattended_relief_refuses_when_not_in_apply_mode_or_paused_but_the_owner_may(monkeypatch, capsys):
    got = cli_world(monkeypatch, mode="report")
    assert sw.swap_main(["relieve", "--apply", "--auto"]) == 1 and not got
    assert "not in apply mode" in capsys.readouterr().err and sw.relief_outcome(0, by="auto")["note"].startswith("refused: swap_auto_relief is not in apply mode")
    assert sw.swap_main(["relieve", "--apply"]) == 0 and got == ["owner"], "the owner's own relief does not depend on the task's mode"
    got = cli_world(monkeypatch, paused=True)
    assert sw.swap_main(["relieve", "--apply", "--auto"]) == 1 and not got and "paused" in capsys.readouterr().err
    assert sw.swap_main(["relieve", "--apply"]) == 0


def test_an_unsafe_automatic_relief_records_why_it_refused(monkeypatch):
    got = cli_world(monkeypatch, busy=(True, "comfyui: queue running"))
    assert sw.swap_main(["relieve", "--apply", "--auto"]) == 1 and not got
    assert "comfyui" in sw.relief_outcome(0, by="auto")["note"]


def test_a_gate_check_that_raises_counts_as_busy(monkeypatch, capsys):
    got = cli_world(monkeypatch, busy=OSError("docker is down"))
    assert sw.swap_main(["relieve", "--apply"]) == 1 and not got
    assert "gate check failed" in capsys.readouterr().out


# =========================================================================== the ledger
def test_relief_pages_prorates_a_window_that_ends_inside_the_relief():
    core.write_json_atomic(sw._ledger_path(), {"done": [{"t0": 1000.0, "t1": 1200.0, "pages_in": 8_000_000}]}, 0o644)
    assert sw.relief_pages(0, 1100) == 4_000_000
    assert sw.relief_pages(1050, 1150) == 4_000_000
    assert sw.relief_pages(900, 1300) == 8_000_000


def test_a_running_relief_discounts_what_it_has_brought_back_so_far():
    sw.relief_begin(now=time.time() - 100)
    vmstat = sw.PROC / "vmstat"
    vmstat.write_text("pswpin 101000\npswpout 0\n")                                      # 100000 pages since the marker (1000 at begin)
    t = time.time()
    assert sw.relief_pages(t - 200, t) == 100_000                                       # everything it has brought back so far is inside the window
    assert sw.discount(150_000, t - 200, t) == 50_000 and sw.discount(90_000, t - 200, t) == 0


def test_a_marker_whose_process_died_is_neither_active_nor_a_discount():
    sw.relief_begin(now=time.time() - 60)
    d = sw._ledger()
    d["active"]["pid"] = 2 ** 22 + 99
    core.write_json_atomic(sw._ledger_path(), d, 0o644)
    (sw.PROC / "vmstat").write_text("pswpin 9000000\npswpout 0\n")
    t = time.time()
    assert not sw.relief_active() and sw.relief_pages(t - 600, t) == 0 and sw.discount(500_000, t - 600, t) == 500_000, "a crashed relief must not keep hiding real swap-in"


def test_a_marker_from_the_future_or_of_a_foreign_process_is_handled(monkeypatch):
    sw.relief_begin(now=5000.0)
    assert not sw.relief_active(now=4000.0), "a marker written 'in the future' is not a running relief"
    monkeypatch.setattr(sw.os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert sw.relief_active(now=5100.0), "EPERM means the process exists (another user's)"


# =========================================================================== shipped config == code defaults (no silent drift between rule, table and code)
def test_the_shipped_tables_and_the_registry_rules_equal_the_code_defaults():
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    tbl = tomllib.loads((root / "etc" / "maint.toml").read_text())["tasks"]
    auto = dict(tbl["swap_auto_relief"])
    assert auto.pop("mode") == "report", "it ships report-only: the owner switches it on"
    assert auto.pop("disruptive") is True, "a freeze holds the automatic relief"
    assert auto.pop("sample_s") == 3 and auto == sw.AUTO_DEFAULTS
    audit = dict(tbl["swap_audit"])
    assert audit.pop("sample_s") == 3 and audit.pop("cold_refault_pps") == sw.AUTO_DEFAULTS["cold_refault_pps"] and audit == sw.DEFAULTS
    rules = {}
    for f in (root / "etc" / "rules.d").glob("*.toml"):
        for r in tomllib.loads(f.read_text()).get("rule", []):
            rules[r["id"]] = r
    r = rules["task.swap_auto_relief"]
    assert r["destructive"] is True and r["mode"] == "report" and r["params"] == {k: v for k, v in tbl["swap_auto_relief"].items() if k != "mode"} and r["params"]["disruptive"] is True
    assert rules["task.swap_audit"]["params"] == tbl["swap_audit"]


# =========================================================================== second review: paging, change log, identity, limits, ledger, signals, freeze
def pages(results, now0=1_800_000_000.0):
    """How many pages the PRODUCTION notifier would send for this sequence of 15-minute Results (alert_confirm_runs = 2, reminders daily)."""
    from homelab_maint import notify
    n = 0
    for i, res in enumerate(results):
        nt = notify.HermesNotifier({"global": {"alert_confirm_runs": 2, "alert_reminder_hours": 24}}, defer=True)
        nt.evaluate("swap_auto_relief", "Automatic swap relief", res, now0 + 900 * i)
        nt.save()
    box = (core.read_json(core.STATE_DIR / "notify-state.json", {}) or {}).get("outbox") or []
    return len([b for b in box if b.get("kind") == "alert"])           # (a later "recovery" notice is separate and expected)


def test_the_breaker_really_pages_once_through_the_production_notifier(world):
    st = {"last_relief_at": T0 - 1 * H, "refill_checked": False, "refills": 1}
    results = []
    for i in range(8):
        r, ctx = world.run(T0 + 900 * i, state=st)
        st = ctx.state
        results.append(r)
    assert all(r.metrics["action"] == "breaker" for r in results) and all(r.alert for r in results)
    assert pages(results) == 1, "one page for the whole pause (not zero because of the 2-run debounce, not one per run)"


def test_a_one_run_alert_is_never_delivered_which_is_why_the_breaker_stays_alert():
    one_run = [core.Result("warn", "x", alert=a) for a in (True, False, False, False)]
    assert pages(one_run) == 0, "the notifier needs the same level on 2 runs in a row: this is the bug the second review found"


def test_the_swap_off_self_heal_is_paged_too_and_journaled(world, monkeypatch):
    notes = []
    monkeypatch.setattr(swap_auto, "_journal", lambda title, detail="": notes.append(title))
    off = iter([True])                                                                   # off on the first run only: the heal switches it back on
    monkeypatch.setattr(sw, "swap_expected_but_off", lambda *a, **k: next(off, False))
    results, st = [], {}
    for i in range(6):
        r, ctx = world.run(T0 + 900 * i, state=st)
        st = ctx.state
        results.append(r)
    assert ["swapon", "-a"] in world.calls and notes == ["Swap switched back on"]
    assert [r.alert for r in results] == [True, True, True, False, False, False], "the notice stays up for two more runs so the notifier can confirm it"
    assert all(r.issue_key == "swap_auto:swap-off" for r in results[:3])
    assert pages(results) == 1


# ---- the change log: a launch is not a change; a confirmed relief is
def test_the_confirmed_relief_leads_its_summary_with_what_was_done_for_the_change_log(world):
    r, ctx, t = run_to_launch(world)
    world.finish_relief(t, ok=True)
    r, ctx = world.run(t + 900, state=ctx.state)
    assert r.summary.startswith("swap relieved automatically: 30.5 GiB returned to RAM in 195 s; "), r.summary
    r2, _ = world.run(t + 1800, state=ctx.state)
    assert "relieved automatically" not in r2.summary, "only the run that confirmed it says so"


def test_the_breaker_summary_keeps_the_holders_inside_the_140_character_cap(world):
    world.snap["holders"][0]["who"] = "open-notebook-open_notebook-1"                  # the real, long container name
    st = {"last_relief_at": T0 - 1 * H, "refill_checked": False, "refills": 1}
    r, ctx = world.run(T0, state=st)
    assert r.metrics["action"] == "breaker" and len(r.summary) <= 140
    assert "paused until" in r.summary and "top: open-notebook-open 20.0 GiB, Seerr 9.0 GiB" in r.summary, r.summary


def test_a_launch_is_audited_as_launched_and_only_a_confirmed_relief_as_done(world, monkeypatch):
    rows = []
    rec = lambda task, what, target, size, outcome, n=1: rows.append((what, outcome))         # noqa: E731
    monkeypatch.setattr(core, "audit", rec)
    monkeypatch.setattr(swap_auto, "audit", rec)                                              # the task imported it by name
    from homelab_maint import routine
    r, ctx, t = run_to_launch(world)
    assert ("swap-relief-launch", "launched") in rows and not [x for x in rows if x[1] == "done"], "the launch must not count as a change"
    world.finish_relief(t, ok=False, note="refused: gate busy")
    world.run(t + 900, state=ctx.state)
    assert not [x for x in rows if x[1] == "done"], "a refused relief is not a change"
    rows.clear()
    r, ctx, t = run_to_launch(world)
    world.finish_relief(t, ok=True)
    world.run(t + 900, state=ctx.state)
    assert ("swap-relief", "done") in rows, "the confirmed outcome is the change"
    assert inspect_done(routine) == "outcome == 'done' only"


def inspect_done(routine):
    import inspect
    return "outcome == 'done' only" if 'r.get("outcome") == "done"' in inspect.getsource(routine._audit_done) else "changed"


def test_ctx_act_labels_a_success_with_the_outcome_the_caller_chose(monkeypatch):
    rows = []
    monkeypatch.setattr(core, "audit", lambda task, what, target, size, outcome, n=1: rows.append((what, outcome)))
    cfg = {"tasks": {"x": {"mode": "apply"}}}
    ctx = core.Ctx(cfg, "x", True, 1e9)
    assert ctx.act("start", "t:1", 0, lambda: None, outcome="launched") and ctx.act("go", "t:2", 0, lambda: None)
    assert rows == [("start", "launched"), ("go", "done")], "the default stays 'done'"


def test_a_freeze_holds_the_automatic_relief_like_the_restarts():
    import tomllib
    from pathlib import Path
    from homelab_maint import routine
    mcfg = tomllib.loads((Path(__file__).resolve().parent.parent / "etc" / "maint.toml").read_text())
    rc = routine.RoutineConfig(valid=False)
    assert routine._restart_kind(rc, mcfg, "swap_auto_relief") is True and routine._restart_kind(rc, mcfg, "caps") is False


# ---- holder identity is the cgroup path, not a display name
def test_a_holder_keeps_its_history_when_its_display_name_changes_and_distinct_cgroups_do_not_collide():
    st = {}
    a = dict(holder("open-notebook", 4, "x", refault=100), path="/system.slice/docker-aaa.scope", kind="container")
    b = dict(holder("open-notebook", 4, "x", refault=100), path="/system.slice/docker-bbb.scope", kind="container")      # same name, other cgroup
    sw.track(st, [dict(a), dict(b)], T0)
    a2 = dict(a, who="aaaaaaaaaaaa", refault_anon=110)                                    # docker ps failed: the name became the short id
    b2 = dict(b, refault_anon=100 + 90_000)                                               # the other one is really busy
    out = sw.track(st, [a2, b2], T0 + 900)
    assert [h["activity"] for h in out] == ["cold", "active"], "identity survives a name change and two same-named cgroups stay apart"


def test_holders_carry_their_cgroup_path(tmp_path):
    root = tmp_path / "cg"
    tw.cg(root, "system.slice/svc.service", 2 * GIB)
    assert sw.holders(root)[0]["path"] == "/system.slice/svc.service"


# ---- the relief's limits come from [tasks.swap_audit], for every caller
def test_relief_cfg_ignores_out_of_range_values_so_a_typo_never_switches_a_gate_off():
    for k, bad in (("relief_cap_margin", 9), ("relief_cap_margin", 1.5), ("relief_psi_full", 100), ("relief_disk_busy_pct", 1000), ("relief_headroom_gib", 0.2), ("relief_cap_margin", 0.0)):
        assert sw.relief_cfg({"tasks": {"swap_audit": {k: bad}}}) == {}, (k, bad)
    ok = {"relief_cap_margin": 0.8, "relief_psi_full": 2.0, "relief_disk_busy_pct": 30, "relief_headroom_gib": 24, "relief_min_used_gib": 2}
    assert sw.relief_cfg({"tasks": {"swap_audit": ok}}) == {k: float(v) for k, v in ok.items()}, "tightening within range is honoured"
    # the typo that would have let the swap-in OOM-kill an 8 GiB container (4 resident + 6 swapped)
    hold = [tw.holder("nb", swap=6, cur=4, mx=8)]
    assert not tw.plan(hold=hold, ps=tw.psi(0.03))["safe"]
    bad = sw.relief_cfg({"tasks": {"swap_audit": {"relief_cap_margin": 9}}})
    assert not sw.relief_plan(tw.mem(avail=66), hold, tw.psi(0.03), tw.areas(), (False, "idle"), 5.0, bad)["safe"], "9 is ignored: the 0.9 default still blocks"


def test_swap_audits_own_feasibility_uses_the_same_range_checked_limits(monkeypatch):
    from homelab_maint.tasks import swap as swap_task
    monkeypatch.setattr(sw, "snapshot", lambda **k: {"mem": tw.mem(avail=66, swap_free=2), "rates": tw.rates(0, 0), "psi": tw.psi(0.03), "holders": [tw.holder("nb", swap=6, cur=4, mx=8)], "areas": tw.areas()})
    monkeypatch.setattr(sw, "container_names", lambda: {})
    cfg = {"tasks": {"swap_audit": {"relief_cap_margin": 9}}}
    r = swap_task.swap_audit(core.Ctx(cfg, "swap_audit", False, T0))
    assert r.metrics["relief_feasible"] == 0, "a margin of 9 must not make the relief look feasible"


def test_relief_cfg_reads_the_documented_home_and_fails_closed_on_junk():
    cfg = {"tasks": {"swap_audit": {"relief_headroom_gib": 30, "relief_cap_margin": "0.5", "relief_psi_full": float("nan"), "relief_disk_busy_pct": True, "other": 1}}}
    assert sw.relief_cfg(cfg) == {"relief_headroom_gib": 30.0}, "strings, NaN, bools and unknown keys are ignored: the shipped default applies"
    assert sw.relief_cfg({}) == {} and sw.relief_cfg(None) == {} and sw.relief_cfg({"tasks": {"swap_audit": {"relief_headroom_gib": -3}}}) == {}


def test_a_tightened_headroom_in_swap_audit_binds_the_task_and_the_cli(world, monkeypatch):
    world.snap["mem"]["MemAvailable"] = 32 * GIB + 20 * GIB                              # 20 GiB left after the pages are back: fine by default (16)
    opts = {"persist_hours": 1.0}
    r, ctx = world.run(T0, **opts)
    r, ctx = world.run(T0 + 900, state=ctx.state, **opts)
    cfg = {"global": {}, "caps": {}, "protected": {}, "tasks": {"swap_auto_relief": {"mode": "apply", **NO_GUARDS, **opts}, "swap_audit": {"relief_headroom_gib": 30}}}
    c2 = core.Ctx(cfg, "swap_auto_relief", True, T0 + 900 + 1 * H)
    c2.state = ctx.state
    r = swap_auto.swap_auto_relief(c2)
    assert r.metrics["action"] == "blocked" and "not enough free RAM" in r.summary and not world.started(), "the task honours the owner's limit"
    got = cli_world(monkeypatch)
    monkeypatch.setattr(sw, "_loaded_cfg", lambda: {"tasks": {"swap_audit": {"relief_headroom_gib": 80}}})
    assert sw.swap_main(["relieve", "--apply"]) == 1 and not got, "and so does the CLI (the unit and the owner's relief)"


# ---- state handling
def test_a_manual_dry_run_of_an_apply_configured_task_never_touches_the_apply_lane(world):
    st = world.run(T0)[1].state
    st = world.run(T0 + 900, state=st)[1].state
    import copy
    before = copy.deepcopy(st)
    r, ctx = world.run(T0 + 900 + 2 * H, apply=False, state=st, cfg_mode="apply")       # `homelab-maint run --task swap_auto_relief` without --apply
    assert "would clear the swap now" in r.summary and not world.started()
    assert ctx.state == before, "the stored watch, backoff and breaker belong to the scheduled apply runs"
    r, ctx = world.run(T0 + 900 + 2 * H, apply=True, state=ctx.state)
    assert r.metrics["action"] == "started", "and the next scheduled apply run still has its persisted watch"


def test_a_failed_attempts_warning_clears_when_the_swap_recovers_on_its_own(world):
    r, ctx, t = run_to_launch(world)
    for n in range(3):
        world.finish_relief(t, ok=False, note="aborted")
        r, ctx = world.run(t + 900, state=ctx.state)
        t = ctx.state["retry_after"] + 900
        if n < 2:
            r, ctx = world.run(t, state=ctx.state)
    assert r.status == "warn" and "last 3 attempts failed" in r.summary
    world.snap["mem"]["SwapFree"] = 24 * GIB                                              # the swap emptied by itself (8 GiB used = 25 %)
    r, ctx = world.run(t + 900, state=ctx.state)
    assert ctx.state["failed_attempts"] == 0 and r.status == "ok" and ctx.state["retry_after"] is None


def test_a_long_relief_does_not_make_the_persisted_watch_look_unobserved(world):
    r, ctx, t = run_to_launch(world, max_gap_hours=0.75)
    ctx.state["over_since"] = ctx.state["over_since"]                                     # (kept: the launch does not clear it)
    over = ctx.state["over_since"]
    world.unit_active = True
    for i in range(1, 7):                                                                  # a 90-minute relief: 6 runs that only see "in progress"
        r, ctx = world.run(t + 900 * i, state=ctx.state, max_gap_hours=0.75)
        assert "in progress" in r.summary
    world.unit_active = False
    world.finish_relief(t, ok=False, note="aborted: memory stall")
    r, ctx = world.run(t + 900 * 7, state=ctx.state, max_gap_hours=0.75)
    assert ctx.state["over_since"] == over, "the watch that persisted before the relief is still the watch after a failed one"


# ---- the ledger is safe under concurrent writers
def test_concurrent_ledger_writers_lose_nothing():
    import threading
    errs = []

    def writer(i):
        try:
            sw.relief_refused("owner", f"r{i}", now=1000.0 + i)
        except Exception as exc:                                                         # noqa: BLE001
            errs.append(exc)
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    notes = sorted(x["note"] for x in sw._ledger()["done"])
    assert not errs and notes == sorted(f"refused: r{i}" for i in range(12)), "a read-modify-write without a lock would lose rows"


# ---- a signal can not cut the swapon retry short
def test_a_late_sigterm_cannot_skip_the_swapon_that_puts_the_swap_back(monkeypatch):
    state = {"on": True}
    attempts = []

    def run(cmd, **k):
        attempts.append(cmd)
        if len(attempts) == 1:
            os.kill(os.getpid(), signal.SIGTERM)                                         # lands while swapon is being retried
            return subprocess.CompletedProcess(cmd, 1, "", "busy")
        state["on"] = True
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: tw.areas() if state["on"] else [])
    monkeypatch.setattr(sw, "meminfo", lambda: {"MemTotal": 94 * GIB, "MemAvailable": 60 * GIB})
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: {"some60": 0.0, "full60": 0.0})
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: tw.FakeProc(["done"], state))
    monkeypatch.setattr(subprocess, "run", run)
    assert sw._apply(tw.areas()) == 0, "the relief FINISHED: a stop signal that lands at the very end must not turn it into a failure"
    assert len(attempts) == 2 and state["on"], "the retry ran to success before the signal was allowed through"
    assert not sw.relief_active() and sw._ledger()["done"][-1]["ok"] is True, "and the ledger says it worked (the launching task reads this)"


# ---- survivors listed by the second review's mutation runs
def test_the_cli_plan_also_sees_the_small_tightly_capped_holders(monkeypatch, capsys):
    got = cli_world(monkeypatch)
    snap = sw.snapshot()                                                                 # (the faked snapshot of cli_world)
    snap["all_holders"] = snap["holders"] + [dict(tw.holder("tiny-capped", 0.02, cur=0.11, mx=0.125))]
    monkeypatch.setattr(sw, "snapshot", lambda **k: snap)
    assert sw.swap_main(["relieve", "--apply"]) == 1 and not got
    assert "tiny-capped" in capsys.readouterr().out, "the unit's own re-check and the owner's dry run use EVERY cgroup with swap"


def test_wait_never_gives_up_on_a_live_swapoff_and_swapon_waits_for_it(monkeypatch):
    state = {"on": True}

    class Slow(tw.FakeProc):
        timeouts = 2

        def wait(self, timeout=None):
            if self.timeouts:
                self.timeouts -= 1
                raise subprocess.TimeoutExpired("swapoff", timeout)
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode
    ran = []
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: tw.areas() if state["on"] else [])
    monkeypatch.setattr(sw, "meminfo", lambda: {"MemTotal": 94 * GIB, "MemAvailable": 60 * GIB})
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: {"some60": 0.0, "full60": 0.0})
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: Slow(["run", "done"], state))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (ran.append(cmd), state.update(on=True), subprocess.CompletedProcess(cmd, 0, "", ""))[2])
    assert sw._apply(tw.areas()) == 0 and ran == [["swapon", "/swap.img"]] and state["on"], "a TimeoutExpired from wait() must not escape or skip anything"


def test_the_retry_backoff_doubles_and_is_capped_at_a_day():
    st, cfg = {}, {"retry_hours": 1.0}
    seen = []
    for n in range(10):
        swap_auto._failed(st, T0, cfg, "x")
        seen.append((st["retry_after"] - T0) / 3600)
    assert seen[:5] == [1, 2, 4, 8, 16] and seen[5:] == [24.0] * 5 and st["failed_attempts"] == 10


def test_an_outcome_is_the_automatic_ones_not_an_owners_and_not_one_from_before_the_launch():
    sw.relief_refused("owner", "x", now=5000.0)
    assert sw.relief_outcome(4000.0, by="auto") is None, "the owner's row is not the unit's outcome"
    sw.relief_refused("auto", "y", now=3000.0)
    assert sw.relief_outcome(4000.0, by="auto") is None, "a row older than the launch is not its outcome"
    sw.relief_refused("auto", "z", now=6000.0)
    assert sw.relief_outcome(4000.0, by="auto")["note"] == "refused: z" and sw.relief_outcome(4000.0)["t0"] == 6000.0


def test_the_per_task_pause_file_is_the_one_the_unit_honours(monkeypatch, tmp_path):
    """core.paused(name) with the REAL implementation: PAUSE.swap_auto_relief stops it, PAUSE.caps does not."""
    monkeypatch.setattr(core, "CONF_DIR", tmp_path)
    monkeypatch.setattr(core, "load_config", lambda *a, **k: {"tasks": {"swap_auto_relief": {"mode": "apply"}}})
    assert sw._auto_revoked() == ""
    (tmp_path / "PAUSE.caps").write_text("")
    assert sw._auto_revoked() == ""
    (tmp_path / "PAUSE.swap_auto_relief").write_text("")
    assert "kill switch" in sw._auto_revoked() and sw._auto_allowed() == (False, "paused (kill switch)")
