"""The relief ledger: a swap relief the owner ran on purpose must not look like trouble to anything that judges swap-in."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import subprocess

import pytest

from homelab_maint import core, swapwatch as sw
from homelab_maint.core import GIB, Ctx
from homelab_maint.tasks import guard
from homelab_maint.tasks import swap as swap_task


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(sw, "PROC", proc)
    (proc / "vmstat").write_text("pswpin 1000\npswpout 0\n")
    return proc


def vm(proc, pin):
    (proc / "vmstat").write_text(f"pswpin {pin}\npswpout 0\n")


def test_a_running_relief_is_active_while_its_process_lives_and_a_stale_marker_is_ignored(isolated):
    assert not sw.relief_active()
    sw.relief_begin(now=1000.0, proc=isolated)
    assert sw.relief_active(now=1100.0)                                     # this very process is alive
    assert not sw.relief_active(now=1000.0 + 7200)                          # a marker older than an hour is a crash leftover
    d = sw._ledger()
    d["active"]["pid"] = 2 ** 22 + 12345                                    # a pid nobody has
    core.write_json_atomic(sw._ledger_path(), d, 0o644)
    assert not sw.relief_active(now=1100.0)
    sw.relief_end(now=1200.0, proc=isolated)
    assert not sw.relief_active(now=1100.0) and "active" not in sw._ledger()


def test_a_finished_relief_leaves_its_page_count_and_discount_removes_exactly_those_pages(isolated):
    sw.relief_begin(now=1000.0, proc=isolated)
    vm(isolated, 1000 + 8_000_000)                                          # 8M pages came back during the relief
    sw.relief_end(now=1200.0, proc=isolated)
    assert sw._ledger()["done"] == [{"t0": 1000.0, "t1": 1200.0, "pages_in": 8_000_000, "by": "owner", "ok": True, "note": ""}]
    assert sw.relief_pages(0, 2000, isolated) == 8_000_000                  # the whole relief is inside the window
    assert sw.relief_pages(1100, 1200, isolated) == 4_000_000               # half of it overlaps this window
    assert sw.relief_pages(3000, 4000, isolated) == 0                       # a window after it
    assert sw.discount(8_100_000, 0, 2000, isolated) == 100_000             # what is left is real swap-in
    assert sw.discount(1_000, 0, 2000, isolated) == 0                       # never negative


def test_the_ledger_keeps_only_the_last_twenty_reliefs(isolated):
    for i in range(25):
        sw.relief_begin(now=1000.0 + i * 10, proc=isolated)
        sw.relief_end(now=1005.0 + i * 10, proc=isolated)
    assert len(sw._ledger()["done"]) == 20


def test_a_garbled_ledger_never_breaks_a_check(isolated):
    sw._ledger_path().write_text("{not json")
    assert not sw.relief_active() and sw.relief_pages(0, 10, isolated) == 0 and sw.discount(5, 0, 10, isolated) == 5
    core.write_json_atomic(sw._ledger_path(), {"done": [{"t0": "x"}, 7], "active": {"pid": "no"}}, 0o644)
    assert not sw.relief_active() and sw.relief_pages(0, 10, isolated) == 0


# --------------------------------------------------------------------------- the apply path writes the ledger, even when it aborts
class _P:
    def __init__(self, rc):
        self.returncode, self.stderr = rc, None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass


def test_apply_marks_the_relief_and_always_ends_it(monkeypatch, isolated):
    seen = {}
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: [{"path": "/swap.img", "used_b": GIB, "size_b": 32 * GIB, "type": "file", "prio": "-2"}])
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: (seen.update(active=sw.relief_active()), _P(0))[1])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    area = [{"path": "/swap.img", "used_b": GIB, "size_b": 32 * GIB, "type": "file", "prio": "-2"}]
    assert sw._apply(area) == 0
    assert seen["active"] is True and not sw.relief_active() and len(sw._ledger()["done"]) == 1
    monkeypatch.setattr(sw, "_apply_areas", lambda a, s, by="owner": (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        sw._apply(area)
    assert not sw.relief_active(), "the marker must be cleared even when the apply path blows up"


# --------------------------------------------------------------------------- the consumers
def test_swap_audit_says_a_relief_is_in_progress_and_never_pages(monkeypatch, isolated):
    sw.relief_begin(proc=isolated)
    monkeypatch.setattr(sw, "snapshot", lambda **k: pytest.fail("a relief in progress must not even be sampled as thrashing"))
    r = swap_task.swap_audit(Ctx({"tasks": {"swap_audit": {}}}, "swap_audit", False, None))
    assert r.status == "info" and not r.alert and "relief in progress" in r.summary


def test_the_guards_pressure_verdict_does_not_count_a_deliberate_relief(isolated):
    t0 = 1_000_000.0
    win = [{"t": t0, "host": {"pswpin": 0, "psi_mem_full60": 0.1, "mem_avail": 60 * GIB}},
           {"t": t0 + 900, "host": {"pswpin": 8_000_000, "psi_mem_full60": 0.1, "mem_avail": 40 * GIB}}]
    ctx = Ctx({"tasks": {"memory_health": {"swap_in_pages_per_s_warn": 2000}}}, "stuck_detector", False, t0 + 900)
    assert guard._pressure(ctx, win)[0] == "warn", "control: 8.9k pages/s with no relief is pressure"
    core.write_json_atomic(sw._ledger_path(), {"done": [{"t0": t0 + 100, "t1": t0 + 400, "pages_in": 8_000_000}]}, 0o644)
    lvl, info = guard._pressure(ctx, win)
    assert lvl == "none" and info["swap_in_pps"] == 0
