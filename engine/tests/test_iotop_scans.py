"""iotop.py (top disk readers, scan finder) and tasks/scans.py (stuck_scans): fake /proc trees under tmp_path, no real process is touched."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
from pathlib import Path

import pytest

from homelab_maint import core, iotop
from homelab_maint.core import Ctx
from homelab_maint.tasks import gates, scans

MIB = 1024 ** 2
CID = "a" * 64


def proc_tree(tmp_path: Path, up: float = 100000.0) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text(f"{up} 1.0\n")
    (root / "pressure").mkdir()
    (root / "pressure" / "io").write_text("some avg10=40.00 avg60=43.64 avg300=30.00 total=1\nfull avg10=30.00 avg60=41.15 avg300=20.00 total=1\n")
    return root


def add_proc(root: Path, pid: int, comm: str, argv: list[str], ppid: int = 1000, age_s: float = 5000.0, cgroup: str = "0::/user.slice/x.scope",
             rb: int = 0, wb: int = 0, tty: int = 0, up: float = 100000.0) -> None:
    d = root / str(pid)
    d.mkdir()
    start = int((up - age_s) * iotop.CLK_TCK)
    d.joinpath("stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} {tty} -1 4194560 1 0 0 0 1 1 0 0 20 0 1 0 {start} 1000 100 18446744073709551615\n")
    d.joinpath("cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    d.joinpath("cgroup").write_text(cgroup + "\n")
    set_io(root, pid, rb, wb)


def set_io(root: Path, pid: int, rb: int, wb: int) -> None:
    (root / str(pid) / "io").write_text(f"rchar: 1\nwchar: 1\nsyscr: 1\nsyscw: 1\nread_bytes: {rb}\nwrite_bytes: {wb}\ncancelled_write_bytes: 0\n")


# --------------------------------------------------------------------------- parsing and description
def test_parse_stat_survives_spaces_and_parens_in_the_command_name():
    st = iotop.parse_stat("42 (my (weird) proc) S 7 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 12345 0 0\n")
    assert st == {"comm": "my (weird) proc", "ppid": 7, "tty": 0, "start_ticks": 12345}
    assert iotop.parse_stat("garbage") is None and iotop.parse_stat(None) is None


def test_the_name_is_argv0_not_the_kernel_comm(tmp_path):
    """Claude Code embeds bfs and re-execs itself: comm says '2.1.287', argv[0] says bfs."""
    root = proc_tree(tmp_path)
    add_proc(root, 500, "2.1.287", ["bfs", "-S", "dfs", "/", "-name", "secret-token-xyz"], ppid=1)
    d = iotop.describe(500, root)
    assert d["name"] == "bfs" and d["age_s"] == 5000


def test_orphan_means_nobody_is_waiting_and_it_is_not_a_service_units_own_job(tmp_path):
    root = proc_tree(tmp_path)
    add_proc(root, 10, "systemd", ["systemd", "--user"], ppid=1)
    add_proc(root, 11, "bash", ["bash"], ppid=900)
    add_proc(root, 20, "find", ["find", "/"], ppid=1, cgroup="0::/user.slice/u/vte-spawn-1.scope")                    # re-parented to init
    add_proc(root, 21, "find", ["find", "/"], ppid=10, cgroup="0::/user.slice/u/app.slice/x.scope")                    # adopted by systemd --user
    add_proc(root, 22, "find", ["find", "/"], ppid=11, cgroup="0::/user.slice/u/session-3.scope")                      # a shell is waiting
    add_proc(root, 23, "updatedb", ["updatedb"], ppid=1, cgroup="0::/system.slice/plocate-updatedb.service")           # a unit's own nightly job
    add_proc(root, 24, "find", ["find", "/"], ppid=1, cgroup=f"0::/system.slice/docker-{CID}.scope")                    # inside a container
    got = {pid: iotop.describe(pid, root, names={CID: "immich_server"})["orphan"] for pid in (20, 21, 22, 23, 24)}
    assert got == {20: True, 21: True, 22: False, 23: False, 24: False}
    assert iotop.describe(24, root, names={CID: "immich_server"})["container"] == "immich_server"


def test_a_published_row_never_carries_the_command_line(tmp_path):
    root = proc_tree(tmp_path)
    add_proc(root, 30, "python3", ["python3", "app.py", "--password=hunter2", "--token", "abc"], ppid=1)
    row = iotop.public_row(iotop.describe(30, root), read_bps=1)
    assert "hunter2" not in json.dumps(row) and "token" not in json.dumps(row) and "argv" not in row


# --------------------------------------------------------------------------- top readers
def test_the_sampler_ranks_by_rate_drops_quiet_and_reset_counters_and_skips_the_first_call(tmp_path):
    root = proc_tree(tmp_path)
    clock = iter([0.0, 10.0])
    add_proc(root, 1, "systemd", ["systemd"], ppid=0, rb=0)
    add_proc(root, 2, "bfs", ["bfs", "/"], ppid=1, rb=1000)
    add_proc(root, 3, "ml", ["python3", "train.py"], ppid=1, rb=0, wb=5000)
    add_proc(root, 4, "quiet", ["sleep", "1"], ppid=1, rb=0)
    add_proc(root, 5, "reborn", ["x"], ppid=1, rb=10 * MIB)
    s = iotop.IoSampler(root, clock=lambda: next(clock))
    assert s.sample() == {"readers": [], "window_s": None}
    set_io(root, 2, 1000 + 100 * MIB, 0)           # 10 MiB/s read
    set_io(root, 3, 0, 5000 + 30 * MIB)            # 3 MiB/s write
    set_io(root, 4, 1024, 0)                       # 100 B/s: below the floor
    set_io(root, 5, 5, 0)                          # counter went backwards (pid reuse): dropped
    r = s.sample()
    assert [x["name"] for x in r["readers"]] == ["bfs", "python3"] and r["window_s"] == 10.0
    assert r["readers"][0]["read_bps"] == 10 * MIB and r["readers"][1]["write_bps"] == 3 * MIB


# --------------------------------------------------------------------------- scan finder
def test_find_scans_knows_scan_tools_recursive_grep_and_skips_containers(tmp_path):
    root = proc_tree(tmp_path)
    add_proc(root, 40, "2.1.287", ["bfs", "-S", "dfs", "/", "-name", "deploy-homarr.sh"], ppid=1, age_s=13000)
    add_proc(root, 41, "du", ["du", "-sh", "/media/WD24to10TB"], ppid=900)
    add_proc(root, 42, "grep", ["grep", "-rn", "needle", "/etc"], ppid=900)
    add_proc(root, 43, "grep", ["grep", "needle", "file.txt"], ppid=900)                  # not recursive: not a scan
    add_proc(root, 44, "rg", ["rg", "needle"], ppid=900)                                  # recursive by default
    add_proc(root, 45, "find", ["find", "/data"], ppid=1, cgroup=f"0::/system.slice/docker-{CID}.scope")
    add_proc(root, 46, "vim", ["vim", "notes.txt"], ppid=900)
    got = {d["pid"]: d for d in iotop.find_scans(root, names={CID: "immich_server"})}
    assert set(got) == {40, 41, 42, 44}
    assert got[40]["root"] == "/" and got[40]["name"] == "bfs" and got[40]["orphan"] is True
    assert got[41]["root"] == "/media/WD24to10TB" and got[42]["root"] == "/etc" and got[44]["root"] == "."
    assert "deploy-homarr" not in json.dumps({k: v for k, v in got[40].items() if k != "argv"})      # only the directory, never the pattern


def test_psi_io_parses_the_kernel_file(tmp_path):
    assert iotop.psi_io(proc_tree(tmp_path)) == {"some60": 43.64, "full60": 41.15}
    assert iotop.psi_io(tmp_path / "nowhere") == {"some60": None, "full60": None}


# --------------------------------------------------------------------------- the stuck_scans task
def scan(pid=1, name="bfs", age_s=3 * 3600, orphan=True, root="/"):
    return {"pid": pid, "name": name, "age_s": age_s, "orphan": orphan, "root": root, "container": None, "argv": [name], "unit": "", "ppid": 1, "tty": False}


@pytest.fixture
def world(monkeypatch):
    w = type("W", (), {})()
    w.scans, w.io, w.psi, w.after = [], {}, {"some60": 40.0, "full60": 30.0}, False

    def find_scans(proc=None, names=None):
        w.after = False                                    # every run starts before its sleep
        return list(w.scans)

    def read_io(pid, proc=None):
        return w.io.get(("after", pid)) if w.after and ("after", pid) in w.io else w.io.get(pid, (0, 0))
    monkeypatch.setattr(scans.iotop, "find_scans", find_scans)
    monkeypatch.setattr(scans.iotop, "read_io", read_io)
    monkeypatch.setattr(scans.iotop, "psi_io", lambda proc=None: w.psi)
    monkeypatch.setattr(scans.gates, "container_info", lambda: {})
    monkeypatch.setattr(scans.time, "sleep", lambda s: setattr(w, "after", True))
    monkeypatch.setattr(scans.time, "monotonic", iter([0.0, 4.0] * 50).__next__)
    return w


def run(cfg=None):
    return scans.stuck_scans(Ctx({"tasks": {"stuck_scans": cfg or {}}}, "stuck_scans", False, 1e9))


def reading(w, pid, mib_s):
    w.io[pid] = (0, 0)
    w.io[("after", pid)] = (int(mib_s * MIB * 4), 0)


def test_no_scan_is_ok_and_quiet(world):
    r = run()
    assert r.status == "ok" and not r.alert and r.metrics["scans"] == 0


def test_an_orphaned_old_scan_that_is_reading_is_a_runaway_that_pages(world):
    world.scans = [scan(pid=3519525, age_s=3 * 3600 + 31 * 60)]
    reading(world, 3519525, 4.8)
    r = run()
    assert r.status == "warn" and r.alert and r.metrics["runaway"] == 1
    assert "runaway scan: bfs pid 3519525 orphaned 3h31m" in r.summary and "kill 3519525" in r.summary and "I/O stall 30%" in r.summary
    assert r.issue_key == "scan:bfs /" and r.items[0]["verdict"] == "runaway" and len(r.summary) <= 140
    assert "argv" not in json.dumps(r.items)


def test_an_attached_scan_is_listed_not_paged_until_it_is_very_long(world):
    world.scans = [scan(orphan=False, age_s=60 * 60)]
    reading(world, 1, 5)
    assert run().status == "ok" and not run().alert                                      # an hour in a terminal: someone is waiting
    world.scans = [scan(orphan=False, age_s=4 * 3600)]
    r = run()
    assert r.status == "warn" and not r.alert and "long scan" in r.summary


def test_a_young_orphan_and_an_idle_old_one_do_not_page(world):
    world.scans = [scan(age_s=5 * 60)]
    reading(world, 1, 9)
    assert not run().alert and run().status == "ok"                                      # 5 minutes: let it finish
    world.scans = [scan(age_s=5 * 3600)]
    world.io = {1: (0, 0), ("after", 1): (1024, 0)}                                      # old but not reading: report, never page
    r = run()
    assert r.status == "info" and not r.alert and "not reading" in r.summary


def test_thresholds_come_from_the_task_config(world):
    world.scans = [scan(age_s=10 * 60)]
    reading(world, 1, 1)
    assert not run().alert
    assert run({"orphan_min_age_min": 5}).alert                                           # tightened by the owner
    assert not run({"orphan_min_age_min": 5, "min_read_kib_s": 4096}).alert               # 1 MiB/s is under a 4 MiB/s floor
