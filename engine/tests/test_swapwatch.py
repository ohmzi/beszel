"""swapwatch.py (verdict, holders, relief plan, guarded apply) and tasks/swap.py (swap_audit): fake /proc and cgroup trees, no real swapoff."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import subprocess
from pathlib import Path

import pytest

from homelab_maint import swapwatch as sw
from homelab_maint.core import Ctx
from homelab_maint.tasks import swap as swap_task

GIB, MIB = sw.GIB, sw.MIB
CID = "b" * 64


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """No test here may read the live /proc/swaps, /etc/fstab or /proc/vmstat, or write into another test's state dir."""
    monkeypatch.setattr(sw.core, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    proc = tmp_path / "fakeproc"
    proc.mkdir()
    (proc / "vmstat").write_text("pswpin 1000\npswpout 0\n")
    monkeypatch.setattr(sw, "PROC", proc)
    monkeypatch.setattr(sw, "swap_expected_but_off", lambda *a, **k: False)


def mem(avail=60, total=94, swap_total=32, swap_free=0):
    return {"MemTotal": total * GIB, "MemAvailable": avail * GIB, "SwapTotal": swap_total * GIB, "SwapFree": swap_free * GIB}


def rates(i=0.0, o=0.0):
    return {"in_bps": None if i is None else i * MIB, "out_bps": None if o is None else o * MIB}


def psi(full=0.0):
    return {"some60": full, "full60": full}


# --------------------------------------------------------------------------- the verdict is about churn, not about how full it is
def test_full_but_idle_swap_is_cold_not_a_fault():
    a = sw.analyse(mem(swap_free=0), rates(0.01, 0.0), psi(0.03))
    assert a["state"] == "cold" and a["used_pct"] == 100.0 and not a["exhausted"] and a["full"]


def test_each_state_comes_from_swap_in_out_and_memory_stall():
    assert sw.analyse(mem(swap_total=0, swap_free=0), rates(), psi())["state"] == "none"
    assert sw.analyse(mem(swap_free=31), rates(0, 0), psi())["state"] == "idle"
    assert sw.analyse(mem(swap_free=2), rates(3, 0), psi())["state"] == "active"
    assert sw.analyse(mem(swap_free=2), rates(20, 5), psi(5.0))["state"] == "thrashing"       # heavy swap-in AND memory stall
    assert sw.analyse(mem(swap_free=2), rates(20, 5), psi(0.1))["state"] == "active"          # heavy swap-in but nothing stalls: busy, not hurting
    assert sw.analyse(mem(swap_free=2), rates(40, 40), psi(0.0))["state"] == "thrashing"      # sheer volume
    assert sw.analyse(mem(swap_free=2), rates(None, None), psi())["state"] == "cold"          # no rates yet: no claim of activity


def test_exhausted_needs_no_free_swap_and_scarce_ram():
    assert not sw.analyse(mem(avail=60, swap_free=0), rates(0, 0), psi())["exhausted"]       # plenty of RAM: nothing to fear
    assert sw.analyse(mem(avail=8, swap_free=0), rates(0, 0), psi())["exhausted"]            # the safety valve is gone and RAM is short
    assert not sw.analyse(mem(avail=8, swap_free=10), rates(0, 0), psi())["exhausted"]


def test_rate_reader_needs_two_readings_and_ignores_a_counter_reset(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    t = iter([0.0, 10.0, 20.0])
    r = sw.RateReader(p, clock=lambda: next(t))
    (p / "vmstat").write_text("pswpin 100\npswpout 200\n")
    assert r.read() == {"in_bps": None, "out_bps": None}
    (p / "vmstat").write_text("pswpin 2660\npswpout 200\n")            # 2560 pages * 4 KiB / 10 s = 1 MiB/s in
    assert r.read() == {"in_bps": 1 * MIB, "out_bps": 0.0}
    (p / "vmstat").write_text("pswpin 5\npswpout 5\n")                 # reboot / reset
    assert r.read() == {"in_bps": None, "out_bps": None}


# --------------------------------------------------------------------------- who holds it
def cg(root: Path, rel: str, swap: int, cur=0, mx="max", procs="1\n") -> None:
    d = root / rel
    d.mkdir(parents=True)
    (d / "memory.swap.current").write_text(f"{swap}\n")
    (d / "memory.current").write_text(f"{cur}\n")
    (d / "memory.max").write_text(f"{mx}\n")
    (d / "memory.high").write_text("max\n")
    (d / "cgroup.procs").write_text(procs)


def test_holders_group_by_cgroup_name_them_and_ignore_aggregates_and_crumbs(tmp_path):
    root = tmp_path / "cg"
    cg(root, f"system.slice/docker-{CID}.scope", 3 * GIB, cur=50 * MIB, mx=str(8 * GIB))
    cg(root, "system.slice/teamviewerd.service", 400 * MIB)
    cg(root, "user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-jetbrains\\x2dstudio-1902847.scope", 7 * GIB, cur=6 * GIB)
    cg(root, "user.slice/user-1000.slice/session-736.scope", 1 * GIB)
    cg(root, "system.slice/tiny.service", 4 * MIB)                         # below the floor
    cg(root, "system.slice/empty.service", 5 * GIB, procs="")              # no processes: an aggregate or a leftover, not a holder
    h = sw.holders(root, names={CID: "open-notebook"})
    assert [(x["who"], x["kind"]) for x in h] == [("jetbrains-studio", "app"), ("open-notebook", "container"), ("login 736", "session"), ("teamviewerd", "service")]
    nb = h[1]
    assert nb["swap_b"] == 3 * GIB and nb["current_b"] == 50 * MIB and nb["max_b"] == 8 * GIB and nb["high_b"] is None
    assert sw.holders(root, names={CID: "x"}, top=2)[1]["who"] == "x"
    assert sw.classify("docker-" + "c" * 64 + ".scope")[1] == "c" * 12          # an unknown container still gets a short id, never an empty name


# --------------------------------------------------------------------------- the relief plan
def areas(used=32):
    return [{"path": "/swap.img", "type": "file", "size_b": 32 * GIB, "used_b": used * GIB, "prio": "-2"}]


def holder(who="a", swap=2, cur=1, mx=None):
    return {"who": who, "kind": "container", "swap_b": swap * GIB, "current_b": cur * GIB, "max_b": None if mx is None else mx * GIB, "high_b": None, "procs": 1}


def plan(m=None, hold=None, ps=None, ar=None, busy=(False, "all gates idle"), disk=5.0):
    return sw.relief_plan(m or mem(avail=66), [holder()] if hold is None else hold, ps or psi(0.03), ar or areas(), busy, disk)


def test_relief_is_safe_when_everything_is_calm_and_says_exactly_what_it_would_run():
    p = plan()
    assert p["safe"] and not p["blockers"]
    assert p["steps"] == ["ionice -c3 nice -n19 swapoff /swap.img      # pages return to RAM; nothing is killed, work continues",
                          "swapon /swap.img                              # back in service, empty"]


@pytest.mark.parametrize("kw,needle", [
    ({"m": mem(avail=36)}, "not enough free RAM"),                                                  # 36 - 32 = 4 GiB left, 9.4 must stay
    ({"busy": (True, "comfyui: queue running")}, "protected workload is working (comfyui"),
    ({"busy": None}, "busy gates were not checked"),
    ({"disk": 70.0}, "swap disk is 70% busy"),
    ({"disk": None}, "could not read how busy the swap disk is"),
    ({"ps": psi(4.0)}, "memory is under pressure"),
    ({"ps": {"some60": None, "full60": None}}, "memory is under pressure"),
    ({"hold": [holder("open-notebook", swap=4, cur=4.5, mx=8)]}, "memory cap 8.0 GiB is too small to take back its 4.0 GiB"),
])
def test_each_precondition_blocks_relief_and_is_named(kw, needle):
    p = plan(**kw)
    assert not p["safe"] and not p["steps"] and any(needle in b for b in p["blockers"]), p["blockers"]


def test_a_cap_that_fits_does_not_block_and_a_small_swap_is_not_worth_relieving():
    assert plan(hold=[holder("nb", swap=3.7, cur=0.05, mx=8)])["safe"]                              # the real open-notebook numbers
    p = plan(ar=areas(used=0.5))
    assert not p["safe"] and not p["needed"] and "nothing worth relieving" in p["why"]


# --------------------------------------------------------------------------- the whole-disk lookup
def test_disk_of_resolves_a_partition_to_its_whole_disk(tmp_path):
    f = tmp_path / "swap.img"
    f.write_bytes(b"x")
    import os
    st = os.stat(f)
    sysdev = tmp_path / "sysdev"
    disk = tmp_path / "devices" / "nvme0n1"
    (disk / "nvme0n1p2").mkdir(parents=True)
    (disk / "nvme0n1p2" / "partition").write_text("2\n")
    sysdev.mkdir()
    (sysdev / f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}").symlink_to(disk / "nvme0n1p2")
    assert sw.disk_of(str(f), sysdev) == "nvme0n1"
    assert sw.disk_of(str(tmp_path / "missing")) is None


def test_disk_busy_is_the_io_ticks_share(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    row = lambda t: f"259 0 nvme0n1 1 0 1 1 1 0 1 1 0 {t} {t} 0 0 0 0 0 0\n"      # noqa: E731  (field 13 = io_ticks ms)
    (p / "diskstats").write_text(row(1000))
    assert sw.disk_busy_pct("nvme0n1", 2.0, p, sleep=lambda s: (p / "diskstats").write_text(row(1000 + 1000))) == 50.0     # 1000 ms busy of 2000 ms
    assert sw.disk_busy_pct("sdz", 0.0, p, sleep=lambda s: None) is None


# --------------------------------------------------------------------------- the guarded apply
class FakeProc:
    def __init__(self, behave, state):
        self.behave, self.state, self.returncode, self.terminated = behave, state, None, False
        self.stderr = None

    def poll(self):
        if self.terminated:
            self.returncode = self.returncode if self.returncode is not None else 1
            return self.returncode
        step = self.behave.pop(0) if self.behave else "done"
        if step == "run":
            return None
        self.state["on"] = False                  # swapoff finished: the area is gone from /proc/swaps
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated, self.returncode = True, 1                     # EINTR: the kernel leaves the area in service

    def wait(self, timeout=None):
        return self.returncode


def drive(monkeypatch, behave, avail_gib=60.0, psi_full=0.0):
    state, ran = {"on": True}, []
    mi = lambda: {"MemTotal": 94 * GIB, "MemAvailable": avail_gib * GIB}                                       # noqa: E731
    monkeypatch.setattr(sw, "meminfo", mi)
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: {"some60": psi_full, "full60": psi_full})
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: areas() if state["on"] else [])
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: (ran.append(cmd), FakeProc(behave, state))[1])

    def run(cmd, **k):
        ran.append(cmd)
        state["on"] = True
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    return sw._apply(areas()), ran, state


def test_apply_runs_swapoff_at_idle_priority_then_swapon(monkeypatch):
    rc, ran, state = drive(monkeypatch, ["run", "run"])
    assert rc == 0 and ran[0] == ["ionice", "-c3", "nice", "-n19", "swapoff", "/swap.img"] and ran[1] == ["swapon", "/swap.img"] and state["on"]


def test_apply_aborts_when_memory_gets_tight_and_leaves_the_area_in_service(monkeypatch):
    rc, ran, state = drive(monkeypatch, ["run", "run", "run"], avail_gib=3.0)
    assert rc == 1 and state["on"], "the area must still be in service"
    assert ["swapon", "/swap.img"] not in ran, "it never went off, so nothing to switch back on"


def test_apply_aborts_on_a_memory_stall(monkeypatch):
    rc, ran, state = drive(monkeypatch, ["run", "run"], psi_full=12.0)
    assert rc == 1 and state["on"]


def test_apply_always_switches_the_area_back_on_even_if_swapoff_was_cut_short_after_it_left(monkeypatch):
    """If the area is off at the end for any reason (aborted late, killed), swapon runs: the box is never left without its swap."""
    state = {"on": True}

    class P(FakeProc):
        def poll(self):
            state["on"] = False
            self.returncode = 1
            return 1
    monkeypatch.setattr(sw, "meminfo", lambda: {"MemTotal": 94 * GIB, "MemAvailable": 60 * GIB})
    monkeypatch.setattr(sw, "psi_mem", lambda proc=sw.PROC: {"some60": 0.0, "full60": 0.0})
    monkeypatch.setattr(sw, "swap_areas", lambda proc=sw.PROC: areas() if state["on"] else [])
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    ran = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: P([], state))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (ran.append(cmd), state.update(on=True), subprocess.CompletedProcess(cmd, 0, "", ""))[2])
    assert sw._apply(areas()) == 1 and ran == [["swapon", "/swap.img"]] and state["on"]


def test_the_cli_refuses_apply_when_not_root_or_not_safe(monkeypatch, capsys):
    snap = {"mem": mem(avail=66), "rates": rates(0, 0), "psi": psi(0.03), "holders": [holder()], "areas": areas()}
    monkeypatch.setattr(sw, "snapshot", lambda **k: snap)
    monkeypatch.setattr(sw, "container_names", lambda: {})
    monkeypatch.setattr(sw, "disk_of", lambda p: "nvme0n1")
    monkeypatch.setattr(sw, "disk_busy_pct", lambda d, *a, **k: 3.0)
    from homelab_maint.tasks import gates
    monkeypatch.setattr(gates, "busy", lambda n, cfg=None: (False, "all gates idle"))
    monkeypatch.setattr(sw.os, "geteuid", lambda: 1000)
    assert sw.swap_main(["relieve", "--apply"]) == 1
    assert "needs root" in capsys.readouterr().err
    monkeypatch.setattr(sw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gates, "busy", lambda n, cfg=None: (True, "plex: transcoding"))
    called = []
    monkeypatch.setattr(sw, "_apply", lambda a, by="owner": called.append((a, by)) or 0)
    assert sw.swap_main(["relieve", "--apply"]) == 1 and not called
    out = capsys.readouterr()
    assert "NOT safe right now" in out.out and "plex: transcoding" in out.out and "refusing" in out.err
    monkeypatch.setattr(gates, "busy", lambda n, cfg=None: (False, "all gates idle"))
    assert sw.swap_main(["relieve"]) == 0 and not called                                      # a dry run never applies
    assert "SAFE now" in capsys.readouterr().out
    assert sw.swap_main(["relieve", "--apply"]) == 0 and called                               # only now
    assert sw.swap_main(["bogus"]) == 2


# --------------------------------------------------------------------------- the swap_audit task
def audit(monkeypatch, m, r, ps, hold, cfg=None, ar=None):
    monkeypatch.setattr(sw, "snapshot", lambda **k: {"mem": m, "rates": r, "psi": ps, "holders": hold, "areas": ar if ar is not None else areas()})
    monkeypatch.setattr(sw, "container_names", lambda: {})
    return swap_task.swap_audit(Ctx({"tasks": {"swap_audit": cfg or {}}}, "swap_audit", False, 1e9))


def test_full_idle_swap_is_shown_but_does_not_page(monkeypatch):
    r = audit(monkeypatch, mem(avail=66, swap_free=0), rates(0.01, 0), psi(0.03), [holder("open-notebook", 3.7, 0.05, 8), holder("Seerr", 2.5)])
    assert r.status == "warn" and not r.alert and "full but idle" in r.summary and "open-notebook 3.7 GiB" in r.summary
    assert "relief looks feasible" in r.summary and r.metrics["relief_feasible"] == 1 and r.metrics["swap_used_pct"] == 100.0
    assert [i["name"] for i in r.items] == ["open-notebook", "Seerr"] and len(r.summary) <= 140


def test_cold_but_with_free_swap_is_info(monkeypatch):
    r = audit(monkeypatch, mem(avail=66, swap_free=12), rates(0, 0), psi(0.0), [holder("a", 2)])
    assert r.status == "info" and not r.alert and "nothing waits on it" in r.summary


def test_thrashing_and_exhaustion_page_with_a_stable_issue_key(monkeypatch):
    r = audit(monkeypatch, mem(avail=20, swap_free=2), rates(30, 10), psi(6.0), [holder("a", 20)])
    assert r.status == "warn" and r.alert and "THRASHING" in r.summary and r.issue_key == "swap:thrashing"
    r = audit(monkeypatch, mem(avail=8, swap_free=0), rates(0, 0), psi(0.0), [holder("a", 20)])
    assert r.status == "warn" and r.alert and "no buffer" in r.summary and r.issue_key == "swap:exhausted"


def test_active_swap_is_informational_and_no_swap_is_fine(monkeypatch):
    r = audit(monkeypatch, mem(avail=60, swap_free=10), rates(3, 0), psi(0.0), [holder("a", 20)])
    assert r.status == "info" and not r.alert and "in use" in r.summary
    r = audit(monkeypatch, mem(swap_total=0, swap_free=0), rates(), psi(), [])
    assert r.status == "info" and "no swap configured" in r.summary


def test_thresholds_are_configurable(monkeypatch):
    r = audit(monkeypatch, mem(avail=60, swap_free=10), rates(0.5, 0), psi(0.0), [holder("a", 20)], cfg={"active_in_mib_s": 0.25})
    assert "in use" in r.summary
