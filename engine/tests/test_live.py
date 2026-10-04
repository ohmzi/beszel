"""Tests for homelab_maint/live.py (the 5-second live monitor) and its systemd unit.

Everything runs against fake /proc, /sys, cgroup v2 and hwmon trees under tmp_path, a fake Docker unix socket, local HTTP
servers on ephemeral loopback ports and a stubbed `sh` (so nvidia-smi / docker / systemctl are never forked), with
HOMELAB_MAINT_* pointed at tmp dirs. The only tests that look at the real host are the explicitly named "real host" ones and
they only read. Run: cd /home/ohmz/homelab-maint && python3 -m pytest tests/test_live.py -q
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import fcntl
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab_maint import live
from homelab_maint.live import GIB, MIB, POINTS, SERIES

ROOT = Path(__file__).resolve().parent.parent
HEX = "0123456789abcdef"


def cid(n: int) -> str:
    """A fake 64-hex container id, distinct per n."""
    return (HEX[n % 16] * 4 + f"{n:060x}")[:64]


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DOCKER_ACTIVE = ("systemctl", "is-active", "docker.service")


def dockerd_up(env, pid: int = 4820, state: str = "active") -> None:
    """Make the fake host look like real docker does: /run/docker.pid names a process whose comm is `dockerd`, and
    `systemctl is-active docker.service` says `state`."""
    write(env.proc / str(pid) / "comm", "dockerd\n")
    write(env.tmp / "docker.pid", f"{pid}\n")
    env.sh_answers[DOCKER_ACTIVE] = (0 if state == "active" else 3, state + "\n")


def forbid_sockets(monkeypatch, only_unix: bool = False) -> list:
    """Any connect() (the docker socket is AF_UNIX, services are AF_INET; `only_unix` lets TCP through) is a test failure: a
    connect to the socket-activated docker.socket would start dockerd. Returns the list of attempted addresses, because a
    probe swallows exceptions: tests must assert it stays empty rather than rely on the AssertionError surfacing."""
    touched: list = []
    real = socket.socket.connect

    def boom(self, addr):
        if only_unix and self.family != socket.AF_UNIX:
            return real(self, addr)
        touched.append(addr)
        raise AssertionError(f"socket connect to {addr!r} while dockerd is down")
    monkeypatch.setattr(socket.socket, "connect", boom)
    return touched


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


# =========================================================================== fixtures
@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Isolated dirs and fake sysfs/procfs; `sh` is a stub that records the command and answers "not installed" (rc 127)."""
    d = SimpleNamespace(tmp=tmp_path)
    for name in ("state", "log", "run", "conf", "proc", "cg", "blk", "net", "hw"):
        p = tmp_path / name
        p.mkdir()
        setattr(d, name, p)
    for attr, p in (("STATE_DIR", d.state), ("LOG_DIR", d.log), ("RUN_DIR", d.run), ("CONF_DIR", d.conf), ("PROC", d.proc),
                    ("CGROUP", d.cg), ("SYS_BLOCK", d.blk), ("SYS_NET", d.net), ("HWMON", d.hw)):
        monkeypatch.setattr(live, attr, p)
    monkeypatch.setattr(live, "DOCKER_SOCK", str(tmp_path / "no-docker.sock"))
    monkeypatch.setattr(live, "DOCKER_PIDFILE", tmp_path / "docker.pid")           # absent = dockerd is down (fail closed)
    monkeypatch.setattr(live, "EXPORTER", ("127.0.0.1", free_port()))          # nothing listens there
    d.sh_calls, d.sh_answers = [], {}

    def fake_sh(cmd, timeout=60, **_kw):
        d.sh_calls.append(list(cmd))
        # an answer can be keyed by the whole command ("systemctl", "is-active", "docker.service") or by its first word
        rc, out = d.sh_answers.get(tuple(cmd[:3]), d.sh_answers.get(cmd[0], (127, "")))
        return subprocess.CompletedProcess(cmd, rc, out, "")
    monkeypatch.setattr(live, "sh", fake_sh)
    live._WARNED.clear()
    return d


class Host:
    """A mutable fake host: set fields, `.write()` renders /proc and /sys files that look like the real ones."""

    def __init__(self, env):
        self.env = env
        self.cpu = [1000, 0, 500, 8000, 500, 0, 0, 0]          # user nice system idle iowait irq softirq steal (jiffies)
        self.mem_kb = {"MemTotal": 64 * 1024 * 1024, "MemFree": 1024 * 1024, "MemAvailable": 16 * 1024 * 1024,
                       "Buffers": 1024 * 1024, "Cached": 8 * 1024 * 1024, "SReclaimable": 1024 * 1024,
                       "SwapTotal": 32 * 1024 * 1024, "SwapFree": 24 * 1024 * 1024}
        self.load = "1.50 1.20 1.00 3/900 12345"
        self.uptime = "641105.52 2000.00"
        self.psi = {"memory": {"some": (0.05, 1_000_000), "full": (0.01, 500_000)},
                    "io": {"some": (56.51, 10_000_000), "full": (49.44, 9_000_000)},
                    "cpu": {"some": (0.03, 777_000)}}
        self.disks = {"nvme0n1": [1_000_000, 500_000, 100_000], "sda": [10, 20, 30]}     # sectors read/written, ms doing io
        self.extra_disk_lines = ["259 1 nvme0n1p1 5 0 99 1 5 0 99 1 0 1 1 0 0 0 0 0 0", "7 0 loop0 5 0 8 1 0 0 0 0 0 1 1 0 0 0 0 0 0",
                                 "252 0 dm-0 5 0 8 1 0 0 0 0 0 1 1 0 0 0 0 0 0", "11 0 sr0 5 0 8 1 0 0 0 0 0 1 1 0 0 0 0 0 0"]
        self.nics = {"eth0": [10_000_000, 4_000_000], "lo": [1, 1], "docker0": [5, 5], "veth1a2b": [7, 7]}
        self.phys_nics = {"eth0"}
        self.mountinfo = ("22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
                          "40 22 8:1 / /media/SandiskSSD rw,relatime shared:5 - ext4 /dev/sda1 rw\n"
                          "41 22 8:17 / /mnt/my\\040disk rw,relatime shared:6 - ext4 /dev/sdb1 rw\n")
        self.locks = ""

    def write(self):
        p, e = self.env.proc, self.env
        write(p / "stat", "cpu  " + " ".join(map(str, self.cpu)) + " 0 0\ncpu0 1 2 3 4 5 6 7 8 0 0\nintr 1 2 3\n")
        write(p / "meminfo", "".join(f"{k}:{v:>14} kB\n" for k, v in self.mem_kb.items()) + "HugePages_Total:       0\n")
        write(p / "loadavg", self.load + "\n")
        write(p / "uptime", self.uptime + "\n")
        for name, kinds in self.psi.items():
            write(p / "pressure" / name, "".join(f"{k} avg10=0.00 avg60={a:.2f} avg300=0.00 total={t}\n"
                                                 for k, (a, t) in kinds.items()))
        lines = [f"259 0 {dev} 10 0 {rs} 5 10 0 {ws} 6 0 {ms} 8 0 0 0 0 0 0" for dev, (rs, ws, ms) in self.disks.items()]
        write(p / "diskstats", "\n".join(lines + self.extra_disk_lines) + "\n")
        for dev in self.disks:
            (e.blk / dev / "device").mkdir(parents=True, exist_ok=True)          # whole physical disks have /device
        head = "Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop\n"
        write(p / "net" / "dev", head + "".join(f"  {n}: {rx} 100 0 0 0 0 0 0 {tx} 100 0 0 0 0 0 0\n"
                                                for n, (rx, tx) in self.nics.items()))
        for n in self.nics:
            (e.net / n).mkdir(parents=True, exist_ok=True)
            if n in self.phys_nics:
                (e.net / n / "device").mkdir(exist_ok=True)
        (p / "self").mkdir(exist_ok=True)
        write(p / "self" / "mountinfo", self.mountinfo)
        write(p / "self" / "statm", "10000 5000 100 10 0 0 0\n")
        write(p / "locks", self.locks)
        return self


@pytest.fixture
def host(env):
    return Host(env).write()


def make_ct(env, n: int, name: str, usec: int, anon: int, state: str = "running", health: str | None = None):
    """A docker row (as read_docker returns it) plus its fake cgroup v2 dir."""
    d = env.cg / "system.slice" / f"docker-{cid(n)}.scope"
    write(d / "cpu.stat", f"usage_usec {usec}\nuser_usec {usec // 2}\nsystem_usec {usec // 2}\nnr_periods 0\n")
    write(d / "memory.stat", f"anon {anon}\nfile 4096\nkernel 0\n")
    return {"name": name, "id": cid(n), "state": state, "health": health, "cg": d if state == "running" else None}


def set_ct(env, n: int, usec: int, anon: int):
    d = env.cg / "system.slice" / f"docker-{cid(n)}.scope"
    write(d / "cpu.stat", f"usage_usec {usec}\nuser_usec 1\n")
    write(d / "memory.stat", f"anon {anon}\nfile 4096\n")


@pytest.fixture
def http(request):
    """`start({"/path": (status, delay_s)}) -> (port, hits)`: loopback HTTP servers, shut down after the test."""
    servers = []

    def start(routes):
        hits: list[str] = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                hits.append(self.path)
                status, delay = routes.get(path, (404, 0))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    if status in (301, 302):
                        self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                except OSError:
                    pass                                          # the client gave up (timeout test)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1], hits

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


class FakeDocker:
    """A docker engine API on a unix socket (short path: sun_path is limited to 107 bytes). Records every request."""

    def __init__(self, containers, delay=0.0, status=200):
        self.dir = tempfile.mkdtemp(prefix="hml")
        self.path = os.path.join(self.dir, "d.sock")
        self.requests: list[tuple[str, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                outer.requests.append((self.command, self.path))
                time.sleep(delay)
                body = json.dumps(containers).encode()
                try:
                    self.send_response(status if self.command == "GET" else 405)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass
            do_GET = do_POST = do_PUT = do_DELETE = _do

            def log_message(self, *a):
                pass
        self.srv = socketserver.ThreadingUnixStreamServer(self.path, H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)


@pytest.fixture
def docker_api(env):
    made = []

    def start(containers, **kw):
        dockerd_up(env)                                   # a docker API that answers implies a running dockerd
        d = FakeDocker(containers, **kw)
        made.append(d)
        return d
    yield start
    for d in made:
        d.close()


def api_row(n, name, state="running", status="Up 3 hours"):
    return {"Id": cid(n), "Names": [f"/{name}"], "State": state, "Status": status, "Image": "x", "Labels": {"a": "b"}}


def quiet_cfg(**kw):
    """A Cfg with every probe off, so a Live() built from it only samples the (fake) host."""
    base = dict(gpu=False, sensors=False, docker=False, disk=False, io_top=False, swap=False, services=[])
    base.update(kw)
    return live.Cfg(**base)


# =========================================================================== parsers and sampling math
def test_cpu_percentages_from_proc_stat_deltas():
    a = live.parse_cpu("cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n")
    assert a == (1000, 0, 500, 8000, 500, 0, 0, 0)          # guest columns are not read: they are already inside user/nice
    # deltas: user 1000, nice 200, system 480, idle 9000, iowait 1200, irq 60, softirq 60 = 12000 jiffies (24 cores x 5 s)
    b = (a[0] + 1000, a[1] + 200, a[2] + 480, a[3] + 9000, a[4] + 1200, a[5] + 60, a[6] + 60, 0)
    assert live.cpu_pcts(a, b) == {"cpu_pct": 15.0, "cpu_user": 10.0, "cpu_sys": 5.0, "cpu_iowait": 10.0}   # iowait is idle time


def test_cpu_edge_cases_never_give_negative_or_nonsense():
    a = (1000, 0, 500, 8000, 500, 0, 0, 0)
    assert live.cpu_pcts(a, a) is None                                               # no time passed
    assert live.cpu_pcts(a, (900, 0, 500, 8100, 500, 0, 0, 0)) is None               # a counter went backwards (reboot)
    assert live.parse_cpu(None) is None and live.parse_cpu("") is None
    assert live.parse_cpu("cpu0 1 2 3 4 5") is None                                  # per-cpu line is not the aggregate
    assert live.parse_cpu("cpu  1 2 x 4 5") is None
    assert live.parse_cpu("cpu  1 2 3") is None
    assert live.parse_cpu("cpu  1 2 3 4 5") == (1, 2, 3, 4, 5, 0, 0, 0)               # an old kernel with 5 columns


def test_meminfo_and_psi_and_loadavg_parsers():
    m = live.parse_meminfo("MemTotal:  1000 kB\nMemAvailable: 400 kB\nBogus: 1 kB\nCached: x kB\nSwapFree: 5 kB\n")
    assert m == {"MemTotal": 1024000, "MemAvailable": 409600, "SwapFree": 5120}
    p = live.parse_psi("some avg10=1.50 avg60=2.25 avg300=0.10 total=12345\nfull avg10=0.00 avg60=0.50 avg300=0.00 total=99\n")
    assert p["some"] == {"avg10": 1.5, "avg60": 2.25, "avg300": 0.1, "total": 12345.0} and p["full"]["avg60"] == 0.5
    assert live.parse_psi("some avg10=0.00 avg60=0.01 avg300=0.00 total=5\n").get("full") is None   # cpu has no `full` on old kernels
    assert live.parse_psi(None) == {} and live.parse_psi("garbage") == {}
    assert live.parse_loadavg("26.99 23.15 22.15 5/1700 99999") == [26.99, 23.15, 22.15]
    assert live.parse_loadavg("x y z") is None and live.parse_loadavg("") is None


def test_diskstats_and_netdev_parsers_pick_whole_physical_devices_only():
    txt = ("259 0 nvme0n1 10 0 2000 5 10 0 1000 6 0 70 8 0 0 0 0 0 0\n"
           "259 1 nvme0n1p1 10 0 1 5 10 0 1 6 0 7 8 0 0 0 0 0 0\n"
           "  8 0 sda 1 0 3 5 1 0 4 6 0 9 8\n"                                         # an older, shorter line still has 13 fields
           "short line\n")
    got = live.parse_diskstats(txt, lambda d: d in ("nvme0n1", "sda"))
    assert got == {"nvme0n1": (2000, 1000, 70), "sda": (3, 4, 9)}
    net = ("Inter-|   Receive |  Transmit\n face |bytes packets\n"
           "    lo: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n  eth0: 123456 9 0 0 0 0 0 0 7890 8 0 0 0 0 0 0\n"
           "veth0: 5 1 0 0 0 0 0 0 6 1 0 0 0 0 0 0\n  bad: x y\n")
    assert live.parse_netdev(net, lambda i: i == "eth0") == {"eth0": (123456, 7890)}


def test_mount_parser_decodes_octal_escapes():
    m = live.parse_mounts("1 2 8:1 / /mnt/my\\040disk rw - ext4 /dev/x rw\n3 4 8:2 / /media/a rw - ext4 /dev/y rw\n")
    assert m == {"/mnt/my disk", "/media/a"}


def test_read_host_math_end_to_end(env, host):
    s = live.Sampler(live.Classes())
    h1, pt1 = s.read_host(100.0, None)
    assert h1["cpu_pct"] is None and h1["net"] == {"rx_bps": None, "tx_bps": None} and h1["io"] == []   # no rates on the first sample
    assert pt1["cpu"] is None and pt1["psi_mem"] is None and pt1["net_rx"] is None
    assert pt1["mem_pct"] == 75                                                       # level values do not need a previous sample
    # five seconds later
    host.cpu = [1000 + 1000 + 200, 0, 500 + 480 + 60 + 60, 8000 + 9000, 500 + 1200, 0, 0, 0]
    host.cpu = [2200, 0, 1100, 17000, 1700, 0, 0, 0]          # deltas 1200 / 0 / 600 / 9000 / 1200 = 12000 -> 15 / 10 / 5 / 10 %
    host.disks["nvme0n1"] = [1_000_000 + 20480, 500_000 + 10240, 100_000 + 2500]
    host.nics["eth0"] = [10_000_000 + 5_000_000, 4_000_000 + 2_500_000]
    host.nics["lo"] = [10 ** 9, 10 ** 9]                                              # loopback is not "the network"
    host.psi["memory"]["some"] = (0.05, 1_000_000 + 250_000)
    host.psi["io"]["some"] = (56.51, 10_000_000 + 1_000_000)
    host.write()
    disks = [{"mount": "/", "free_b": 1, "size_b": 2, "used_pct": 50.0}]
    h, pt = s.read_host(105.0, disks)
    assert (h["cpu_pct"], h["cpu_user"], h["cpu_sys"], h["cpu_iowait"]) == (15.0, 10.0, 5.0, 10.0)
    assert h["cores"] == (os.cpu_count() or 1) and h["uptime_s"] == 641105 and h["load"] == [1.5, 1.2, 1.0]
    assert h["mem"] == {"total": 64 * GIB, "used": 48 * GIB, "avail": 16 * GIB, "cache": 10 * GIB,
                        "swap_used": 8 * GIB, "swap_total": 32 * GIB}
    assert h["psi"] == {"mem_some60": 0.05, "mem_full60": 0.01, "io_some60": 56.51, "io_full60": 49.44, "cpu_some60": 0.03}
    # 20480 sectors * 512 B / 5 s = 2 MiB/s read, 1 MiB/s written, 2500 ms busy in 5000 ms = 50 %; sda idle; partitions/loop/dm/sr skipped
    assert h["io"] == [{"dev": "nvme0n1", "read_bps": 2 * MIB, "write_bps": 1 * MIB, "util_pct": 50.0},
                       {"dev": "sda", "read_bps": 0, "write_bps": 0, "util_pct": 0.0}]
    assert h["net"] == {"rx_bps": 1_000_000, "tx_bps": 500_000}                      # physical interface only
    assert h["disk"] == disks
    assert pt == {"cpu": 15.0, "mem_pct": 75, "psi_mem": 5.0, "psi_io": 20.0, "net_rx": 1.0, "net_tx": 0.5,
                  "disk_r": 2.0, "disk_w": 1.0, "swap_pct": 25, "load1": 1.5}          # swap 8 of 32 GiB; vram_pct comes with the GPU reading


def test_counter_resets_drop_that_source_for_one_tick_not_below_zero(env, host):
    s = live.Sampler(live.Classes())
    s.read_host(0.0, None)
    host.nics["eth0"] = [10, 4_000_000 + 5000]                    # rx counter reset (interface bounce), tx moved
    host.disks["nvme0n1"] = [5, 500_000, 100_000]                 # read counter reset
    host.psi["io"]["some"] = (1.0, 5)                             # stall counter reset
    host.write()
    h, pt = s.read_host(5.0, None)
    assert h["net"] == {"rx_bps": 0, "tx_bps": 0}                 # a reset interface is dropped as a whole for that tick
    assert [r["dev"] for r in h["io"]] == ["sda"]
    assert pt["psi_io"] is None and pt["net_rx"] == 0.0


def test_missing_proc_files_give_nulls_not_exceptions(env):
    s = live.Sampler(live.Classes())                              # nothing under env.proc at all
    h, pt = s.read_host(1.0, None)
    h2, pt2 = s.read_host(6.0, None)
    assert h2["cpu_pct"] is None and h2["load"] is None and h2["uptime_s"] is None
    assert h2["mem"] == {"total": None, "used": None, "avail": None, "cache": None, "swap_used": None, "swap_total": None}
    assert all(v is None for v in h2["psi"].values()) and pt2["mem_pct"] is None
    assert h2["io"] == [] and h2["disk"] == []


def test_device_classification_physical_disks_and_nics(env, host):
    host.extra_disk_lines.append("8 16 sdz 5 0 8 1 0 0 0 0 0 1 1 0 0 0 0 0 0")    # a block device without /device (virtual)
    host.write()
    s = live.Sampler(live.Classes())
    assert s._is_disk("nvme0n1") and s._is_disk("sda")
    assert not s._is_disk("nvme0n1p1") and not s._is_disk("loop0") and not s._is_disk("dm-0") and not s._is_disk("sdz")
    assert s._is_phys("eth0")
    assert not s._is_phys("lo") and not s._is_phys("docker0") and not s._is_phys("veth1a2b") and not s._is_phys("nope")


def test_read_disks_only_mounted_df_style(env, host):
    class SV:
        f_frsize, f_blocks, f_bfree, f_bavail = 4096, 1000, 400, 300           # 100 blocks reserved for root
    got = live.read_disks(["/", "/media/SandiskSSD", "/mnt/my disk", "/not/mounted"], statvfs=lambda m: SV)
    assert [g["mount"] for g in got] == ["/", "/media/SandiskSSD", "/mnt/my disk"]
    g = got[0]
    assert g["size_b"] == 1000 * 4096 and g["free_b"] == 300 * 4096                  # free = f_bavail, as df shows it
    assert g["used_pct"] == round(100 * 600 / (600 + 300), 1) == 66.7                # df: used / (used + avail)

    def boom(m):
        raise OSError("stale file handle")
    assert live.read_disks(["/"], statvfs=boom) == []                                # a dead mount is skipped, not fatal


# =========================================================================== containers
def classes_toml(env):
    write(env.conf / "classes.toml", '[classes]\nP0 = ["^dockerd$"]\nP1 = ["^immich_server$", "^homarr$", "["]\n'
                                    'P2 = ["^tunarr", "^immich"]\nP3 = ["^build"]\n')


def test_container_cpu_and_memory_math_top_lists_and_classes(env):
    classes_toml(env)
    cl = live.Classes()
    s = live.Sampler(cl)
    rows = [make_ct(env, 1, "tunarr-host-net", 1_000_000, 8 * GIB),
            make_ct(env, 2, "immich_server", 5_000_000, GIB // 2, health="healthy"),
            make_ct(env, 3, "homarr", 0, GIB // 4, health="unhealthy"),
            make_ct(env, 4, "mystery", 0, 1 * GIB),
            make_ct(env, 5, "stopped-one", 0, 0, state="exited")]
    for i in range(6, 16):
        rows.append(make_ct(env, i, f"filler{i:02d}", 0, i * MIB))
    first = s.read_containers(100.0, rows, False)
    assert first["running"] == 14 and first["top_cpu"] == []   # 15 rows, one of them exited                     # no CPU rates on the first pass
    assert [r["name"] for r in first["top_mem"]][:3] == ["tunarr-host-net", "mystery", "immich_server"]
    set_ct(env, 1, 1_000_000 + 10_000_000, 8 * GIB)             # 10 CPU-seconds in 5 s = 200 % of one core
    set_ct(env, 2, 5_000_000 + 2_500_000, GIB // 2)             # 50 %
    set_ct(env, 3, 0, GIB // 4)                                 # idle: 0.0 %, not listed in top_cpu
    for i in range(6, 16):
        set_ct(env, i, i * 50_000, i * MIB)                     # i * 50_000 us in 5 s = i % (6 % .. 15 %)
    out = s.read_containers(105.0, rows, False)
    cpu = out["top_cpu"]
    assert cpu[0] == {"name": "tunarr-host-net", "class": "P2", "cpu_pct": 200.0, "mem_gib": 8.0}
    assert cpu[1] == {"name": "immich_server", "class": "P1", "cpu_pct": 50.0, "mem_gib": 0.5}
    assert len(cpu) == 8 and [r["cpu_pct"] for r in cpu] == sorted((r["cpu_pct"] for r in cpu), reverse=True)
    assert "homarr" not in [r["name"] for r in cpu]
    assert cpu[2]["name"] == "filler15" and cpu[2]["cpu_pct"] == 15.0                # 15 * 50_000 us / 5 s = 15 %
    mem = out["top_mem"]
    assert len(mem) == 8 and mem[0]["name"] == "tunarr-host-net" and mem[0]["mem_gib"] == 8.0
    assert [r["name"] for r in mem[:4]] == ["tunarr-host-net", "mystery", "immich_server", "homarr"]
    assert next(r for r in mem if r["name"] == "mystery")["class"] == "P2"            # unknown => P2 (batch)
    assert out["unhealthy"] == ["homarr"] and out["running"] == 14 and out["stale"] is False
    assert cl.of("immich_server") == "P1"                                              # most protective match wins (P1 before P2)
    assert cl.of("build-x") == "P3" and cl.of("dockerd") == "P0"


def test_container_counter_reset_and_vanished_containers(env):
    s = live.Sampler(live.Classes())
    a = make_ct(env, 1, "app", 1_000_000, GIB)
    ghost = make_ct(env, 2, "ghost", 0, GIB)
    s.read_containers(0.0, [a, ghost], False)
    set_ct(env, 1, 3_000_000, GIB)
    for f in (ghost["cg"] / "cpu.stat", ghost["cg"] / "memory.stat"):
        f.unlink()                                                                      # exited between the docker refresh and now
    out = s.read_containers(5.0, [a, ghost], False)
    assert [r["name"] for r in out["top_cpu"]] == ["app"] and out["top_cpu"][0]["cpu_pct"] == 40.0
    assert out["running"] == 2 and [r["name"] for r in out["top_mem"]] == ["app"]     # still "running" per docker, no numbers to show
    # same name, lower counter (cgroup recreated): no negative CPU, no spike
    set_ct(env, 1, 100, GIB)
    assert s.read_containers(10.0, [a], False)["top_cpu"] == []
    # same name, NEW container id: its first pass has no rate either
    b = make_ct(env, 3, "app", 50_000_000, GIB)
    assert s.read_containers(15.0, [b], False)["top_cpu"] == []
    set_ct(env, 3, 55_000_000, GIB)
    assert s.read_containers(20.0, [b], False)["top_cpu"][0]["cpu_pct"] == 100.0


def test_unhealthy_and_restarting_lists_and_docker_unavailable(env):
    s = live.Sampler(live.Classes())
    rows = [make_ct(env, 1, "ok", 1, 1, health="healthy"), make_ct(env, 2, "sick", 1, 1, health="unhealthy"),
            {"name": "loop", "id": cid(3), "state": "restarting", "health": None, "cg": None},
            {"name": "dead", "id": cid(4), "state": "exited", "health": "unhealthy", "cg": None}]
    out = s.read_containers(0.0, rows, True)
    assert out["unhealthy"] == ["loop", "sick"] and out["running"] == 2 and out["stale"] is True    # an exited one is not "unhealthy"
    nothing = s.read_containers(5.0, None, True)                                    # docker never answered
    assert nothing == {"running": None, "unhealthy": [], "top_cpu": [], "top_mem": [], "stale": True}


def test_classes_reload_and_broken_patterns(env):
    cl = live.Classes()
    cl.refresh(0.0)
    assert cl.of("anything") == "P2"                                                   # no classes.toml: everything is batch
    write(env.conf / "classes.toml", '[classes]\nP1 = ["^a$"]\n')
    cl.refresh(10.0)                                                                   # checked at most once a minute
    assert cl.of("a") == "P2"
    cl.refresh(61.0)
    assert cl.of("a") == "P1"
    write(env.conf / "classes.toml", "this is [not toml")
    cl.refresh(200.0)
    assert cl.of("a") == "P2"                                                          # unreadable: fail to "unknown", never crash
    write(env.conf / "classes.toml", '[classes]\nP3 = "not-a-list"\nP1 = ["("]\nP2 = ["^ok$"]\n')
    cl.refresh(400.0)
    assert cl.of("ok") == "P2" and cl.of("(") == "P2"


# =========================================================================== docker over the socket
def test_norm_container_reads_state_health_and_rejects_junk():
    ok = live.norm_container({"Id": cid(1), "Names": ["/immich_server"], "State": "running", "Status": "Up 3 days (healthy)"})
    assert ok == {"name": "immich_server", "id": cid(1), "state": "running", "health": "healthy"}
    assert live.norm_container({"Id": cid(2), "Names": ["/a"], "State": "Running", "Status": "Up 1 hour (unhealthy)"})["health"] == "unhealthy"
    assert live.norm_container({"Id": cid(3), "Names": ["/a"], "State": "running", "Status": "Up 2 seconds (health: starting)"})["health"] == "starting"
    assert live.norm_container({"Id": cid(4), "Names": ["/a"], "State": "running", "Status": "Up 5 days"})["health"] is None
    for bad in (None, {}, {"Id": "xyz", "Names": ["/a"]}, {"Id": cid(1), "Names": []}, {"Names": ["/a"]}, "str", {"Id": cid(1), "Names": 5}):
        assert live.norm_container(bad) is None
    assert live.norm_container({"Id": cid(1), "Names": ["/café"], "State": "running", "Status": ""})["name"] == "caf?"


def test_read_docker_uses_the_socket_with_a_single_get_and_finds_cgroups(env, monkeypatch, docker_api):
    d = docker_api([api_row(1, "immich_server", status="Up 3 days (healthy)"), api_row(2, "old", state="exited", status="Exited (0) 2 days ago"),
                    {"Id": "nonsense", "Names": ["/bad"]}])
    monkeypatch.setattr(live, "DOCKER_SOCK", d.path)
    cg = env.cg / "system.slice" / f"docker-{cid(1)}.scope"
    cg.mkdir(parents=True)
    rows = live.read_docker(2.0)
    assert [(r["name"], r["state"], r["health"]) for r in rows] == [("immich_server", "running", "healthy"), ("old", "exited", None)]
    assert rows[0]["cg"] == cg and rows[1]["cg"] is None                              # only running containers get a cgroup dir
    assert d.requests == [("GET", "/containers/json?all=1")]                          # the monitor never sends anything but this GET
    assert env.sh_calls == [list(DOCKER_ACTIVE)]                                      # its only fork: the liveness gate (no `docker ps`)


def test_cgroupfs_driver_layout_is_found_too(env):
    p = env.cg / "docker" / cid(7)
    p.mkdir(parents=True)
    assert live.cg_dir(cid(7)) == p and live.cg_dir(cid(8)) is None


def test_read_docker_falls_back_to_the_cli_only_when_the_socket_is_missing(env):
    dockerd_up(env)
    env.sh_answers["docker"] = (0, f"{cid(1)}|immich_server|running|Up 2 hours (unhealthy)\n{cid(2)}|a,b|exited|Exited (1) 3 min ago\n")
    rows = live.read_docker(2.0)                                                       # DOCKER_SOCK points at nothing
    assert [(r["name"], r["state"], r["health"]) for r in rows] == [("immich_server", "running", "unhealthy"), ("a", "exited", None)]
    assert env.sh_calls[0] == list(DOCKER_ACTIVE) and env.sh_calls[1][:3] == ["docker", "ps", "-a"]   # gate first, then the CLI
    env.sh_answers["docker"] = (1, "")
    with pytest.raises(RuntimeError):
        live.read_docker(2.0)


def test_slow_docker_times_out_quickly_and_does_not_retry_through_the_cli(env, monkeypatch, docker_api):
    d = docker_api([api_row(1, "x")], delay=2.0)
    monkeypatch.setattr(live, "DOCKER_SOCK", d.path)
    t0 = time.monotonic()
    with pytest.raises(OSError):                                                       # socket.timeout is an OSError
        live.read_docker(0.3)
    assert time.monotonic() - t0 < 1.5
    assert env.sh_calls == [list(DOCKER_ACTIVE)]                                       # no `docker ps` retry: hammering a struggling daemon twice helps nobody


def test_docker_api_error_status_and_garbage_raise(env, monkeypatch, docker_api):
    d = docker_api([], status=500)
    monkeypatch.setattr(live, "DOCKER_SOCK", d.path)
    with pytest.raises(RuntimeError):
        live.read_docker(2.0)
    d2 = docker_api({"not": "a list"})
    monkeypatch.setattr(live, "DOCKER_SOCK", d2.path)
    with pytest.raises(ValueError):
        live.read_docker(2.0)


# =========================================================================== docker is socket-activated: never wake it
# Review finding: docker.service is TriggeredBy=docker.socket (`dockerd -H fd://`) and docker.socket stays active when only the
# service is stopped, so a connect() to /var/run/docker.sock while dockerd is down (or still stopping) STARTS it. The monitor
# probes every 10 s; `systemctl stop docker` for maintenance must not be undone by the monitor.
def _gate_stopped(env):                                  # service stopped cleanly: pidfile removed, process gone
    pass


def _gate_recycled_pid(env):                             # stale pidfile (dockerd was SIGKILLed), pid reused by something else
    write(env.tmp / "docker.pid", "777\n")
    write(env.proc / "777" / "comm", "bash\n")


def _gate_dead_pid(env):                                 # stale pidfile, no such process
    write(env.tmp / "docker.pid", "4820\n")


def _gate_garbage_pidfile(env):
    write(env.tmp / "docker.pid", "dockerd\n")


def _gate_negative_pid(env):
    write(env.tmp / "docker.pid", "-5\n")


def _gate_huge_pid(env):
    write(env.tmp / "docker.pid", "9" * 40 + "\n")


def _gate_empty_pidfile(env):
    write(env.tmp / "docker.pid", "")


def _gate_only_proxy_in_cgroup(env):                     # docker.service's cgroup still holds a docker-proxy, but no dockerd
    write(env.proc / "9455" / "comm", "docker-proxy\n")
    write(env.cg / "system.slice" / "docker.service" / "cgroup.procs", "9455\n")


@pytest.mark.parametrize("setup", [_gate_stopped, _gate_recycled_pid, _gate_dead_pid, _gate_garbage_pidfile, _gate_negative_pid,
                                   _gate_huge_pid, _gate_empty_pidfile, _gate_only_proxy_in_cgroup])
def test_a_stopped_dockerd_is_never_touched_neither_socket_nor_cli(env, monkeypatch, setup):
    setup(env)
    touched = forbid_sockets(monkeypatch)
    for _ in range(3):                                                                # the probe retries every 10 s: still nothing
        with pytest.raises(live.DockerDown):
            live.read_docker(2.0)
    assert touched == [] and env.sh_calls == []                                       # not even `systemctl` or `docker ps` is forked


@pytest.mark.parametrize("state,rc", [("deactivating", 3), ("inactive", 3), ("activating", 3), ("failed", 3), ("reloading", 0),
                                      ("", 127), ("", 124)])
def test_a_stopping_or_not_active_docker_service_is_never_touched(env, monkeypatch, state, rc):
    """The process is still there (dockerd removes its pidfile only AFTER stopping every container, 10+ s with 70 of them), but
    systemd says the service is not `active`: a connect now would queue on docker.socket and restart docker after the stop."""
    dockerd_up(env)
    env.sh_answers[DOCKER_ACTIVE] = (rc, state + "\n" if state else "")              # rc 127/124: systemctl missing / hung
    touched = forbid_sockets(monkeypatch)
    with pytest.raises(live.DockerDown) as e:
        live.read_docker(2.0)
    assert (state or "unknown") in str(e.value)
    assert touched == [] and env.sh_calls == [list(DOCKER_ACTIVE)]                   # asked systemd (read-only), and nothing else


def test_the_docker_cli_fallback_is_skipped_while_dockerd_is_down(env, monkeypatch):
    env.sh_answers["docker"] = (0, f"{cid(1)}|immich_server|running|Up 2 hours\n")    # would answer, if anybody asked
    with pytest.raises(live.DockerDown):
        live.read_docker(2.0)                                                         # no pidfile: the old code ran `docker ps` here
    assert env.sh_calls == []
    dockerd_up(env, state="inactive")
    with pytest.raises(live.DockerDown):
        live.read_docker(2.0)
    assert all(c[0] != "docker" for c in env.sh_calls)


def test_dockerd_that_dies_between_the_check_and_the_connect_does_not_get_woken_by_the_cli(env, monkeypatch):
    """The check passes, the socket path is not there (default env), and by now the process is gone: the CLI fallback would
    connect to the activating socket exactly like the API, so it is not tried."""
    dockerd_up(env)
    env.sh_answers["docker"] = (0, f"{cid(1)}|x|running|Up\n")
    real = live.sh

    def sh_then_die(cmd, *a, **k):
        r = real(cmd, *a, **k)
        if cmd[0] == "systemctl":
            (env.tmp / "docker.pid").unlink()                                         # dockerd exits right after being checked
        return r
    monkeypatch.setattr(live, "sh", sh_then_die)
    with pytest.raises(live.DockerDown):
        live.read_docker(2.0)
    assert all(c[0] != "docker" for c in env.sh_calls)


def test_a_running_dockerd_is_found_through_the_pidfile_or_through_its_cgroup(env):
    assert live.dockerd_process_alive() is False
    dockerd_up(env)
    assert live.dockerd_process_alive() is True                                       # pidfile -> /proc/<pid>/comm == dockerd
    (env.tmp / "docker.pid").unlink()
    assert live.dockerd_process_alive() is False
    write(env.proc / "9455" / "comm", "docker-proxy\n")                               # the proxy alone is not dockerd ...
    write(env.cg / "system.slice" / "docker.service" / "cgroup.procs", "9455\n4820\nxyz\n\n")
    assert live.dockerd_process_alive() is True                                       # ... but dockerd in the cgroup is (custom pidfile path)


def test_gate_open_means_the_socket_is_used_exactly_once_per_probe(env, monkeypatch, docker_api):
    d = docker_api([api_row(1, "a")])
    monkeypatch.setattr(live, "DOCKER_SOCK", d.path)
    assert [r["name"] for r in live.read_docker(2.0)] == ["a"]
    assert [r["name"] for r in live.read_docker(2.0)] == ["a"]
    assert d.requests == [("GET", "/containers/json?all=1")] * 2


def test_stopping_docker_for_maintenance_leaves_the_monitor_running_and_http_decides(env, host, http, monkeypatch):
    """The scenario from the review: `systemctl stop docker`. The whole monitor keeps producing live.json on time, the docker
    probe goes stale, the services read plain HTTP, nothing connects to the docker socket, and nothing was forked but nothing."""
    port, _ = http({"/ok": (200, 0)})
    cfg = live.Cfg(gpu=False, sensors=False, disk=False, http_timeout=1.0, mounts=[],
                   services=[live.clean_service({"name": "Kavita", "container": "kavita", "url": f"http://127.0.0.1:{port}/ok"})])
    L = live.Live(cfg)
    assert "docker" in L.probes and L.probes["services"].after == [L.probes["docker"]]
    touched = forbid_sockets(monkeypatch, only_unix=True)                             # loopback HTTP still works, the docker socket does not
    for _ in range(4):                                                                # four probe cycles = 40 s of the monitor
        L.probes["docker"].run_once()
    L.probes["services"].run_once()
    out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    assert_schema(out, SCHEMA)
    assert L.probes["docker"].fails == 4 and out["probes"]["docker"]["stale"] is True
    assert out["containers"] == {"running": None, "unhealthy": [], "top_cpu": [], "top_mem": [], "stale": True}
    assert out["services"][0]["state"] == "up" and out["services"][0]["detail"].startswith("docker state unknown")
    assert out["self"]["errors"] == 0 and out["host"]["mem"]["total"] == 64 * GIB      # the rest of the file is unaffected
    assert touched == [] and env.sh_calls == []                                        # no connect to docker.sock, no fork at all


def test_the_docker_probe_warns_once_in_the_journal_not_every_cycle(env, capsys):
    p = live.Probe("docker", lambda: live.read_docker(1.0), 10.0)
    for _ in range(5):
        p.run_once()
    err = capsys.readouterr().err
    assert err.count("probe docker failed: DockerDown: dockerd process not found") == 1       # throttled: one line per 5 minutes


def test_dockerd_comes_back_and_the_probe_recovers(env, monkeypatch, docker_api):
    d = docker_api([api_row(1, "a")])
    monkeypatch.setattr(live, "DOCKER_SOCK", d.path)
    (env.tmp / "docker.pid").unlink()                                                  # undo the fixture's "up": docker is stopped
    p = live.Probe("docker", lambda: live.read_docker(2.0), 10.0)
    p.run_once()
    assert p.get()[0] is None and p.fails == 1 and d.requests == []
    dockerd_up(env)                                                                    # `systemctl start docker`
    p.run_once()
    assert [r["name"] for r in p.get()[0]] == ["a"] and p.fails == 0 and p.get()[1] is False


@pytest.mark.skipif(not Path("/run/docker.pid").exists() or not Path("/usr/bin/systemctl").exists(),
                    reason="needs this host's systemd-managed dockerd")
def test_real_host_docker_gate_formats_match_reality(monkeypatch, env):
    """Read-only: the real pidfile / comm / cgroup.procs / `systemctl is-active` formats. It never opens the docker socket."""
    from homelab_maint import core
    for attr, p in (("PROC", "/proc"), ("CGROUP", "/sys/fs/cgroup")):
        monkeypatch.setattr(live, attr, Path(p))
    monkeypatch.setattr(live, "DOCKER_PIDFILE", Path("/run/docker.pid"))
    monkeypatch.setattr(live, "sh", core.sh)
    touched = forbid_sockets(monkeypatch)
    if live.unit_state("docker.service") != "active":
        pytest.skip("docker.service is not active right now")
    assert live.dockerd_process_alive() is True
    live.dockerd_ready()                                                               # returns, does not raise
    monkeypatch.setattr(live, "DOCKER_PIDFILE", Path("/nonexistent/docker.pid"))
    assert live.dockerd_process_alive() is True                                        # the cgroup fallback finds it as well
    assert touched == []


# =========================================================================== sensors and GPU
def make_chip(env, idx, name, files):
    d = env.hw / f"hwmon{idx}"
    write(d / "name", name + "\n")
    for k, v in files.items():
        write(d / k, f"{v}\n")


def full_hwmon(env):
    make_chip(env, 0, "acpitz", {"temp1_input": 27800})
    make_chip(env, 1, "nvme", {"temp1_label": "Composite", "temp1_input": 51900, "temp2_label": "Sensor 1", "temp2_input": 99000})
    make_chip(env, 2, "coretemp", {"temp1_label": "Package id 0", "temp1_input": 70000, "temp2_label": "Core 0", "temp2_input": 99000})
    make_chip(env, 3, "nct6798", {"fan1_input": 1600, "fan2_input": 400, "fan3_input": 500, "fan4_input": 1700, "fan5_input": 600,
                                  "fan6_input": 0, "fan7_input": 0})
    make_chip(env, 4, "spd5118", {"temp1_input": 34000})
    make_chip(env, 5, "spd5118", {"temp1_input": 38000})
    make_chip(env, 6, "drivetemp", {"temp1_input": 41000})


def test_hwmon_temperatures_and_fans_without_forking(env):
    full_hwmon(env)
    got = live.read_hwmon((1, 4), (2, 3, 5))
    assert got == {"cpu_temp": 70.0, "ram_temp": 36.0, "nvme_temp": 51.9, "gpu_temp": None,
                   "cpu_fan_rpm": 1650, "case_fan_rpm": 500}                          # mean of the fans that turn; fan6/7 (0 rpm) are ignored
    assert env.sh_calls == []


def test_hwmon_can_skip_the_expensive_fan_chip(env, monkeypatch):
    full_hwmon(env)
    opened = []
    real = live._read
    monkeypatch.setattr(live, "_read", lambda p: opened.append(str(p)) or real(p))
    got = live.read_hwmon((1, 4), (2, 3, 5), fans=False)
    assert got["cpu_temp"] == 70.0 and got["nvme_temp"] == 51.9 and got["ram_temp"] == 36.0
    assert got["cpu_fan_rpm"] is None and got["case_fan_rpm"] is None
    assert not [p for p in opened if Path(p).name.startswith("fan")]                                         # the Super-I/O tachs were not touched


def test_fans_are_read_on_their_own_slower_cadence(env, monkeypatch):
    full_hwmon(env)
    clk = Clock(1000.0)
    read = live.make_sensors_reader(live.Cfg(fans_every=60.0), clock=clk)
    a = read()
    assert a["cpu_fan_rpm"] == 1650 and a["case_fan_rpm"] == 500 and a["cpu_temp"] == 70.0
    write(env.hw / "hwmon3" / "fan1_input", "3000\n")
    write(env.hw / "hwmon3" / "fan4_input", "3000\n")
    write(env.hw / "hwmon2" / "temp1_input", "80000\n")
    opened = []
    real = live._read
    monkeypatch.setattr(live, "_read", lambda p: opened.append(str(p)) or real(p))
    clk.t += 15
    b = read()
    assert b["cpu_temp"] == 80.0 and b["cpu_fan_rpm"] == 1650 and b["case_fan_rpm"] == 500      # temperature is fresh, fans are the cached ones
    assert not [p for p in opened if Path(p).name.startswith("fan")]
    clk.t += 46                                                                                  # 61 s after the last fan read
    c = read()
    assert c["cpu_fan_rpm"] == 3000 and c["case_fan_rpm"] == 500 and [p for p in opened if Path(p).name.startswith("fan")]


def test_hwmon_fans_all_stopped_is_zero_but_no_fan_chip_is_unknown(env):
    make_chip(env, 0, "nct6798", {"fan1_input": 0, "fan2_input": 0, "fan3_input": 0, "fan4_input": 0, "fan5_input": 0})
    got = live.read_hwmon((1, 4), (2, 3, 5))
    assert got["cpu_fan_rpm"] == 0 and got["case_fan_rpm"] == 0                       # a stopped fan IS a reading
    for f in (env.hw / "hwmon0").glob("fan*"):
        f.unlink()
    (env.hw / "hwmon0" / "name").write_text("acpitz\n")
    none = live.read_hwmon((1, 4), (2, 3, 5))
    assert none["cpu_fan_rpm"] is None and none["case_fan_rpm"] is None              # no fan chip: unknown, not "0 rpm"


def test_hwmon_ignores_garbage_and_implausible_values(env):
    make_chip(env, 0, "coretemp", {"temp1_label": "Package id 0", "temp1_input": "notanumber"})
    make_chip(env, 1, "nvme", {"temp1_label": "Composite", "temp1_input": 900000})                # 900 C: a bad read
    got = live.read_hwmon((1, 4), (2, 3, 5))
    assert got["cpu_temp"] is None and got["nvme_temp"] is None


def test_sensors_fall_back_to_the_exporter_rarely_and_only_for_what_is_missing(env, monkeypatch):
    make_chip(env, 0, "coretemp", {"temp1_label": "Package id 0", "temp1_input": 70000})          # hwmon has the CPU, nothing else
    payload = json.dumps({"cpu_temp": 99, "gpu_temp": 62, "nvme_temp": 57.9, "cpu_fan": 1623, "case_fan": 490}).encode()
    hits: list = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(live, "EXPORTER", ("127.0.0.1", srv.server_address[1]))
    try:
        read = live.make_sensors_reader(live.Cfg())
        a = read()
        assert a["cpu_temp"] == 70.0                                                      # hwmon wins where it has data
        assert (a["nvme_temp"], a["gpu_temp"], a["cpu_fan_rpm"], a["case_fan_rpm"]) == (57.9, 62.0, 1623, 490)
        read()
        read()
        assert len(hits) == 1                                                             # at most once per exporter_every (60 s)
    finally:
        srv.shutdown()
        srv.server_close()


def test_sensors_with_exporter_down_keep_hwmon_values_and_with_nothing_raise(env):
    make_chip(env, 0, "coretemp", {"temp1_label": "Package id 0", "temp1_input": 70000})
    read = live.make_sensors_reader(live.Cfg())
    got = read()                                                                          # exporter port is closed: refused
    assert got["cpu_temp"] == 70.0 and got["nvme_temp"] is None and got["cpu_fan_rpm"] is None
    for f in env.hw.rglob("*"):
        if f.is_file():
            f.unlink()
    with pytest.raises(RuntimeError):                                                     # nothing anywhere: the probe goes stale
        live.make_sensors_reader(live.Cfg())()


def test_exporter_junk_is_rejected(env, monkeypatch):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"[1,2]\n")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(live, "EXPORTER", ("127.0.0.1", srv.server_address[1]))
    try:
        with pytest.raises(ValueError):
            live.read_exporter(1.0)
    finally:
        srv.shutdown()
        srv.server_close()


def test_gpu_parsing_and_missing_nvidia_smi(env):
    g = live.parse_gpu("0, 1345, 24576, 62, 125.76, 47\n")
    assert g == {"util": 0, "mem_used": 1345 * MIB, "mem_total": 24576 * MIB, "temp": 62, "power_w": 125.8, "fan_pct": 47}
    g = live.parse_gpu("97, 1345, 24576, 62, [N/A], [N/A]\n")                              # cards without a fan/power sensor
    assert g["util"] == 97 and g["power_w"] is None and g["fan_pct"] is None
    for junk in ("", "\n", "[N/A], [N/A], [N/A], [N/A], [N/A], [N/A]", "No devices were found"):
        with pytest.raises(ValueError):
            live.parse_gpu(junk)
    with pytest.raises(RuntimeError):
        live.read_gpu()                                                                   # stub sh: rc 127 = nvidia-smi not installed
    assert env.sh_calls[0][0] == "nvidia-smi" and "--format=csv,noheader,nounits" in env.sh_calls[0]
    env.sh_answers["nvidia-smi"] = (124, "")
    with pytest.raises(RuntimeError):
        live.read_gpu()                                                                   # hung driver: sh() timeout is rc 124
    env.sh_answers["nvidia-smi"] = (0, "12, 100, 200, 50, 30.5, 40\n")
    assert live.read_gpu()["util"] == 12


# =========================================================================== services
def spec(**kw):
    d = {"name": "X", "container": "x", "url": "http://127.0.0.1:1/"}
    d.update(kw)
    return live.clean_service(d)


RUN_OK = {"name": "x", "id": cid(1), "state": "running", "health": "healthy", "cg": None}


@pytest.mark.parametrize("ct,res,unit,kw,want", [
    (RUN_OK, (200, 5, ""), None, {}, ("up", "healthy, 5 ms", 5)),
    (dict(RUN_OK, health=None), (200, 5, ""), None, {}, ("up", "5 ms", 5)),
    (None, (200, 5, ""), None, {}, ("down", "container missing", None)),
    (dict(RUN_OK, state="exited"), (200, 5, ""), None, {}, ("down", "container exited", None)),
    (dict(RUN_OK, state="restarting"), None, None, {}, ("down", "container restarting", None)),
    (dict(RUN_OK, health="unhealthy"), (200, 5, ""), None, {}, ("degraded", "unhealthy, 5 ms", 5)),
    (dict(RUN_OK, health="starting"), (200, 5, ""), None, {}, ("degraded", "starting, 5 ms", 5)),
    (RUN_OK, (500, 7, ""), None, {}, ("down", "HTTP 500", 7)),
    (RUN_OK, (503, 7, ""), None, {}, ("down", "HTTP 503", 7)),
    (RUN_OK, (None, 5000, "timeout"), None, {}, ("down", "timeout", None)),
    (RUN_OK, (None, 1, "refused"), None, {}, ("down", "refused", None)),
    (RUN_OK, (302, 40, ""), None, {}, ("up", "healthy, 40 ms", 40)),                         # tunarr answers 302 and that is fine
    (RUN_OK, (404, 4, ""), None, {}, ("degraded", "healthy, HTTP 404", 4)),
    (RUN_OK, (401, 4, ""), None, {"expect": [401]}, ("up", "healthy, 4 ms", 4)),
    (RUN_OK, (200, 4, ""), None, {"expect": [401]}, ("degraded", "healthy, HTTP 200", 4)),
    (RUN_OK, (200, 3000, ""), None, {}, ("degraded", "healthy, slow 3000 ms", 3000)),
    (RUN_OK, (200, 3000, ""), None, {"slow_ms": 5000}, ("up", "healthy, 3000 ms", 3000)),
    (live.UNKNOWN, (200, 6, ""), None, {}, ("up", "docker state unknown, 6 ms", 6)),            # docker is not trusted: HTTP decides
    (live.UNKNOWN, (None, 6, "refused"), None, {}, ("down", "refused", None)),
])
def test_service_state_matrix(ct, res, unit, kw, want):
    assert live.service_state(spec(**kw), ct, res, unit, 2000) == want


def test_service_state_unit_only_and_unit_fallback():
    unit_only = live.clean_service({"name": "U", "unit": "snap.plex.service"})
    assert live.service_state(unit_only, None, None, "active", 2000) == ("up", "unit active", None)
    assert live.service_state(unit_only, None, None, "inactive", 2000) == ("down", "unit inactive", None)
    assert live.service_state(unit_only, None, None, None, 2000) == ("down", "unit unknown", None)
    plex = live.clean_service({"name": "Plex", "unit": "snap.plex.service", "url": "http://127.0.0.1:32400/identity"})
    assert live.service_state(plex, None, (200, 2, ""), None, 2000) == ("up", "2 ms", 2)             # HTTP is enough while it answers
    assert live.service_state(plex, None, (None, 2, "refused"), "failed", 2000) == ("down", "unit failed", None)


def test_service_spec_validation_and_loopback_only():
    assert live.clean_service("x") is None and live.clean_service({}) is None and live.clean_service({"name": "A"}) is None
    assert live.clean_service({"name": "", "url": "http://127.0.0.1/"}) is None
    s = live.clean_service({"name": "Out", "url": "https://example.com/health"})
    assert s["addr"] is None and s["bad_url"] is True                                    # kept (visible as degraded), never requested
    assert live.service_state(s, None, None, None, 2000) == ("degraded", "bad url in config", None)
    assert live.clean_service({"name": "L", "url": "http://localhost:8080/a?b=1"})["addr"] == ("localhost", 8080, "/a?b=1")
    assert live.clean_service({"name": "L", "url": "http://192.168.1.5:80/"})["bad_url"] is True      # LAN hosts are not "this machine"
    s2 = live.clean_service({"name": "E", "container": "e", "expect": [200, "x", True, 99, 700, 204]})
    assert s2["expect"] == [200, 204] and s2["slow_ms"] is None
    assert live.clean_service({"name": "Né", "container": "c"})["name"] == "N?"


def test_http_get_status_redirect_timeout_refused(http):
    port, hits = http({"/ok": (200, 0), "/moved": (302, 0), "/boom": (500, 0), "/slow": (200, 2.0)})
    assert live.http_get("127.0.0.1", port, "/ok", 2.0)[::2] == (200, "")
    assert live.http_get("127.0.0.1", port, "/moved", 2.0)[0] == 302                       # redirects are reported, not followed
    assert live.http_get("127.0.0.1", port, "/boom", 2.0)[0] == 500
    assert live.http_get("127.0.0.1", port, "/nope", 2.0)[0] == 404
    t0 = time.monotonic()
    code, ms, err = live.http_get("127.0.0.1", port, "/slow", 0.3)
    assert (code, err) == (None, "timeout") and 250 <= ms < 1500 and time.monotonic() - t0 < 1.5
    assert live.http_get("127.0.0.1", free_port(), "/", 1.0)[::2] == (None, "refused")
    assert hits[0] == "/ok"


def test_services_reader_runs_in_parallel_and_a_hung_service_does_not_block_the_rest(env, http):
    port, _ = http({"/up": (200, 0), "/bad": (500, 0), "/hang": (200, 3.0)})
    cfg = live.Cfg(http_timeout=0.4, services=[
        spec(name="Up", container="up", url=f"http://127.0.0.1:{port}/up"),
        spec(name="Gone", container="gone", url=f"http://127.0.0.1:{port}/up"),
        spec(name="Bad", container="bad", url=f"http://127.0.0.1:{port}/bad"),
        spec(name="Hang", container="hang", url=f"http://127.0.0.1:{port}/hang"),
        spec(name="Dead", container="dead", url=f"http://127.0.0.1:{free_port()}/"),
        spec(name="Outside", container="up", url="https://example.com/")])
    rows = [dict(RUN_OK, name=n) for n in ("up", "bad", "hang", "dead")]
    read = live.make_services_reader(cfg, lambda: (rows, False))
    t0 = time.monotonic()
    out = {r["name"]: r for r in read()}
    assert time.monotonic() - t0 < 1.5                                                    # the hung one costs one http_timeout, in parallel
    assert [out[n]["state"] for n in ("Up", "Gone", "Bad", "Hang", "Dead", "Outside")] == \
        ["up", "down", "down", "down", "down", "degraded"]
    assert out["Gone"]["detail"] == "container missing" and out["Hang"]["detail"] == "timeout" and out["Dead"]["detail"] == "refused"
    assert out["Outside"]["detail"] == "bad url in config"


def test_services_reader_with_untrusted_docker_lets_http_decide(env, http):
    port, _ = http({"/up": (200, 0)})
    cfg = live.Cfg(services=[spec(name="Up", container="up", url=f"http://127.0.0.1:{port}/up")])
    for dk, stale in ((None, True), ([], True)):
        out = live.make_services_reader(cfg, lambda: (dk, stale))()
        assert out[0]["state"] == "up" and out[0]["detail"].startswith("docker state unknown")


def test_services_reader_survives_a_crashing_probe_and_asks_systemd_only_when_needed(env, http, monkeypatch):
    port, _ = http({"/up": (200, 0)})
    cfg = live.Cfg(services=[spec(name="A", container="a", url=f"http://127.0.0.1:{port}/up"),
                             live.clean_service({"name": "Plex", "unit": "snap.plex.service", "url": f"http://127.0.0.1:{port}/up"}),
                             live.clean_service({"name": "Down", "unit": "x.service", "url": f"http://127.0.0.1:{free_port()}/"})])
    env.sh_answers["systemctl"] = (3, "inactive\n")
    rows = [dict(RUN_OK, name="a")]
    out = {r["name"]: r for r in live.make_services_reader(cfg, lambda: (rows, False))()}
    assert out["A"]["state"] == "up" and out["Plex"]["state"] == "up"
    assert out["Down"]["state"] == "down" and out["Down"]["detail"] == "unit inactive"
    assert [c for c in env.sh_calls if c[0] == "systemctl"] == [["systemctl", "is-active", "x.service"]]   # only the one whose HTTP failed

    def crash(host, p, path, timeout):
        raise ValueError("boom")
    monkeypatch.setattr(live, "http_get", crash)
    out = live.make_services_reader(cfg, lambda: (rows, False))()
    assert {r["detail"] for r in out} == {"probe error"} and {r["state"] for r in out} == {"degraded"}      # the list survives


def test_services_reader_overall_deadline(env, monkeypatch):
    cfg = live.Cfg(http_timeout=0.1, services=[spec(name="Stuck", container="s", url="http://127.0.0.1:1/"),
                                                spec(name="Fine", container="f", url="http://127.0.0.1:1/")])
    release = threading.Event()

    def stuck(host, port, path, timeout):                                                  # a probe that ignores its own timeout
        return (200, 1, "") if path == "/fine" else (release.wait(30) and (200, 1, ""))
    cfg.services[1]["addr"] = ("127.0.0.1", 1, "/fine")
    monkeypatch.setattr(live, "http_get", stuck)
    t0 = time.monotonic()
    out = live.make_services_reader(cfg, lambda: ([dict(RUN_OK, name="s"), dict(RUN_OK, name="f")], False), grace=0.3)()
    release.set()
    assert time.monotonic() - t0 < 2.0
    assert out == [{"name": "Stuck", "state": "degraded", "detail": "probe timed out", "ms": None},
                   {"name": "Fine", "state": "up", "detail": "healthy, 1 ms", "ms": 1}]


# =========================================================================== configuration
LEAD_TOML = '''
# [live] is read ONCE when the daemon starts: after editing it run `systemctl restart homelab-maint-live` (safe, ~5 s gap in the graph).
[live]
interval_s = 5                # sample + write cadence in seconds (1..60); the history ring is 720 points at this step
gpu = true                    # nvidia-smi probe (every gpu_every_s)
sensors = true                # hwmon temperatures and fans; the sensor-exporter on :9110 is only a rare fallback
docker = true                 # container list over the docker socket (every docker_every_s)
disk = true                   # statvfs of the mounts (every disk_every_s)
gpu_every_s = 10
sensors_every_s = 15          # temperatures
fans_every_s = 60             # fan speeds: the Super-I/O chip is the dearest read (~17 ms of kernel time), fans change slowly
docker_every_s = 10
services_every_s = 30
disk_every_s = 30
http_timeout_s = 5            # per service probe
slow_ms = 2000                # a service that answers slower than this reads "degraded"
cpu_fans = [1, 4]             # nct6798 fan headers, same grouping as sensor-exporter
case_fans = [2, 3, 5]
# mounts = ["/", "/media/SandiskSSD"]     # default: [tasks.disk_forecast].watch

# One [[live.services]] table per service. name = label on the website. container = docker container name (state + health).
# unit = systemd unit (asked only when HTTP fails). url = loopback http URL (anything else is refused). expect = HTTP codes that
# count as healthy (default 200-399). slow_ms = per-service override. At least one of container / unit / url is required.
[[live.services]]
name = "Plex"
unit = "snap.plexmediaserver.plexmediaserver.service"
url = "http://127.0.0.1:32400/identity"

[[live.services]]
name = "Immich"
container = "immich_server"
url = "http://127.0.0.1:2283/api/server/ping"

[[live.services]]
name = "Kavita"
container = "kavita"
url = "http://127.0.0.1:5000/api/health"

[[live.services]]
name = "Seerr"
container = "Seerr"
url = "http://127.0.0.1:5056/api/v1/status"

[[live.services]]
name = "Open WebUI"
container = "open-webui"
url = "http://127.0.0.1:4567/health"

[[live.services]]
name = "Homarr"
container = "homarr"
url = "http://127.0.0.1:7575/"

[[live.services]]
name = "Uptime Kuma"
container = "uptime-kuma"
url = "http://127.0.0.1:3011/api/entry-page"

[[live.services]]
name = "Nextcloud"
container = "nextcloud"
url = "http://127.0.0.1:8090/status.php"

[[live.services]]
name = "Tunarr"
container = "tunarr-host-net"
url = "http://127.0.0.1:8000/"

[[live.services]]
name = "Radarr"
container = "Radarr"
url = "http://127.0.0.1:7878/ping"

[[live.services]]
name = "Sonarr"
container = "Sonarr"
url = "http://127.0.0.1:8989/ping"
'''


def test_defaults_describe_this_host():
    c = live.load_cfg({})
    assert [s["name"] for s in c.services] == ["Plex", "Immich", "Kavita", "Seerr", "Open WebUI", "Homarr", "Uptime Kuma",
                                               "Nextcloud", "Tunarr", "Radarr", "Sonarr"]
    assert all(s["addr"] and s["addr"][0] == "127.0.0.1" and not s["bad_url"] for s in c.services)
    assert c.interval == 5.0 and (c.gpu_every, c.sensors_every, c.docker_every, c.services_every) == (10.0, 15.0, 10.0, 30.0)
    assert c.fans_every == 60.0
    assert c.mounts == ["/"] and c.cpu_fans == (1, 4) and c.case_fans == (2, 3, 5)


def test_toml_snippet_for_maint_toml_matches_the_defaults_exactly():
    """The exact [live] block handed to the lead parses and is equivalent to running with no [live] section at all."""
    from_toml = live.load_cfg(tomllib.loads(LEAD_TOML))
    dflt = live.load_cfg({})
    assert from_toml == dflt


def test_load_cfg_reads_live_section_and_watch_mounts():
    raw = {"tasks": {"disk_forecast": {"watch": ["/", "/media/SandiskSSD", "relative/ignored"]}},
           "live": {"interval_s": 2, "gpu": False, "services_every_s": 60, "fans_every_s": 120, "http_timeout_s": 1.5, "slow_ms": 500,
                    "cpu_fans": [2], "services": [{"name": "Mine", "container": "mine", "url": "http://localhost:9/x", "expect": [200, 204]},
                                                  {"name": ""}, "junk", {"name": "NoTarget"}]}}
    c = live.load_cfg(raw)
    assert c.interval == 2.0 and c.gpu is False and c.sensors is True and c.services_every == 60.0 and c.fans_every == 120.0
    assert c.http_timeout == 1.5 and c.slow_ms == 500 and c.cpu_fans == (2,)
    assert c.mounts == ["/", "/media/SandiskSSD"]
    assert [s["name"] for s in c.services] == ["Mine"] and c.services[0]["expect"] == [200, 204]
    assert live.load_cfg({"live": {"mounts": ["/data"]}}).mounts == ["/data"]                # [live].mounts beats the watch list
    assert live.load_cfg({"live": {"services": []}}).services == []                          # an empty list switches the probe off


def test_load_cfg_clamps_and_ignores_malformed_values():
    c = live.load_cfg({"live": {"interval_s": 0, "gpu_every_s": "soon", "docker_every_s": 10 ** 9, "http_timeout_s": -1,
                                "gpu": "yes", "sensors": 1, "cpu_fans": [1, "x"], "case_fans": [99], "mounts": "nope",
                                "services": "nope"}})
    d = live.load_cfg({})
    assert c.interval == d.interval and c.gpu_every == d.gpu_every and c.docker_every == d.docker_every
    assert c.http_timeout == d.http_timeout and c.gpu is True and c.sensors is True
    assert c.cpu_fans == d.cpu_fans and c.case_fans == d.case_fans and c.mounts == d.mounts and c.services == d.services
    assert live.load_cfg({"live": "not a table", "tasks": "x"}).interval == 5.0
    many = {"live": {"services": [{"name": f"s{i}", "container": f"c{i}"} for i in range(100)], "mounts": [f"/m{i}" for i in range(100)]}}
    assert len(live.load_cfg(many).services) == 40 and len(live.load_cfg(many).mounts) == 24


def test_load_cfg_reads_the_config_dir_and_survives_a_broken_file(env, capsys):
    write(env.conf / "maint.toml", "[live]\ninterval_s = 3\n")
    assert live.load_cfg().interval == 3.0
    write(env.conf / "maint.toml", "[live\nthis is broken")
    c = live.load_cfg()
    assert c.interval == 5.0 and len(c.services) == 11                                     # defaults, not a crash
    assert "config unreadable" in capsys.readouterr().err


def test_the_repo_config_parses_with_live_cfg():
    c = live.load_cfg(tomllib.loads((ROOT / "etc" / "maint.toml").read_text()))
    names = [s["name"] for s in c.services]
    assert "Plex" in names and "Immich" in names
    assert "/" in c.mounts and all(m.startswith("/") for m in c.mounts)


# =========================================================================== probes: cadence, last good value, stale marking
class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_probe_keeps_the_last_good_value_and_marks_it_stale_after_three_cadences():
    clk = Clock()
    box = {"v": 1, "fail": False}

    def fn():
        if box["fail"]:
            raise OSError("down")
        return box["v"]
    p = live.Probe("x", fn, 10.0, clock=clk)
    assert p.get() == (None, True) and p.age() is None and not p.first.is_set()           # never ran: stale, no value
    p.run_once()
    assert p.first.is_set() and p.get() == (1, False) and p.age() == 0.0
    clk.t += 29
    assert p.get() == (1, False)
    clk.t += 2                                                                             # 31 s > 3 x 10 s
    assert p.get() == (1, True) and p.age() == 31.0                                        # the last good value is kept, flagged stale
    box["fail"] = True
    p.run_once()
    assert p.get() == (1, True) and p.fails == 1
    p.run_once()
    assert p.fails == 2
    box["fail"], box["v"] = False, 2
    p.run_once()
    assert p.get() == (2, False) and p.fails == 0                                          # recovery clears it


def test_probe_stale_threshold_has_a_floor():
    assert live.Probe("fast", lambda: 1, 2.0).stale_after == 15.0 and live.Probe("slow", lambda: 1, 30.0).stale_after == 90.0


def test_probe_first_event_is_set_even_when_the_first_attempt_fails():
    def boom():
        raise RuntimeError("no")
    p = live.Probe("x", boom, 10.0)
    p.run_once()
    assert p.first.is_set() and p.get() == (None, True)


def test_probe_thread_runs_on_cadence_waits_for_dependencies_and_stops_on_signal():
    order = []
    slow = live.Probe("docker", lambda: (time.sleep(0.3), order.append("docker"))[1] or 1, 5.0)
    dep = live.Probe("services", lambda: order.append("services") or 2, 0.05)
    dep.after = [slow]
    stop = live.Stop()
    ts = [slow.start(stop), dep.start(stop)]
    assert wait_for(lambda: order.count("services") >= 3, 5)
    assert order[0] == "docker"                                                            # services waited for docker's first answer
    stop.set()
    for t in ts:
        t.join(2.0)
        assert not t.is_alive()


def test_warnings_are_rate_limited(capsys):
    for _ in range(5):
        live._warn("k", "same thing")
    live._warn("other", "different")
    err = capsys.readouterr().err
    assert err.count("same thing") == 1 and err.count("different") == 1


def test_stop_flag_wakes_every_waiter_and_cannot_block_a_signal_handler():
    st = live.Stop()
    t0 = time.monotonic()
    assert st.wait(0.1) is False and time.monotonic() - t0 >= 0.09
    woke = []
    ts = [threading.Thread(target=lambda: woke.append(st.wait(5.0))) for _ in range(4)]
    for t in ts:
        t.start()
    time.sleep(0.1)
    st.set()
    for t in ts:
        t.join(1.0)
    assert woke == [True] * 4 and st.is_set()
    for _ in range(70_000):                                                                # more than the pipe holds: still never blocks
        st.set()
    assert st.wait(0) is True


def test_stop_works_with_file_descriptors_above_the_select_limit():
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1300
    if hard != resource.RLIM_INFINITY and hard < want:
        pytest.skip("hard fd limit too low to open >1024 descriptors")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, want), hard))
    fds = []
    try:
        while len(fds) < 1100:
            fds.append(os.dup(0))
        st = live.Stop()
        assert st.r > 1024
        t0 = time.monotonic()
        assert st.wait(0.05) is False and time.monotonic() - t0 < 1.0
        st.set()
        assert st.wait(1.0) is True
    finally:
        for fd in fds:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# =========================================================================== history ring
def test_history_is_a_720_point_ring_with_a_linear_time_axis():
    h = live.History(5.0)
    for i in range(800):
        h.push(1000.0 + 5 * i, {"cpu": float(i), "mem_pct": i})
    e = h.export()
    assert POINTS == 720 and all(len(e[k]) == 720 for k in SERIES)
    assert e["cpu"][0] == 80.0 and e["cpu"][-1] == 799.0 and e["mem_pct"][-1] == 799      # oldest 80 dropped, newest last
    assert e["t0"] == 1000.0 + 5 * 80 and e["step_s"] == 5.0
    assert e["t0"] + 719 * 5 == 1000.0 + 5 * 799                                           # point i is at t0 + i * step
    assert e["gpu"] == [None] * 720                                                         # a series nobody fed is all gaps
    assert e["units"]["net_rx"] == "MiB/s" and e["units"]["cpu"] == "%"


def test_history_gaps_become_null_points_and_jitter_does_not():
    h = live.History(5.0)
    h.push(1000.0, {"cpu": 1.0})
    h.push(1005.4, {"cpu": 2.0})                                                           # +/- half a step is "the next point"
    h.push(1009.2, {"cpu": 3.0})
    h.push(1030.0, {"cpu": 4.0})                                                           # 20 s later: four steps, three missing
    e = h.export()
    assert e["cpu"] == [1.0, 2.0, 3.0, None, None, None, 4.0]
    assert e["t0"] == 1000.0 and e["t0"] + 6 * 5 == 1030.0


def test_history_clock_going_backwards_or_a_huge_gap_starts_over():
    h = live.History(5.0)
    for i in range(5):
        h.push(1000.0 + 5 * i, {"cpu": float(i)})
    h.push(500.0, {"cpu": 9.0})                                                            # the wall clock was stepped back
    assert h.export()["cpu"] == [9.0] and h.export()["t0"] == 500.0
    h.push(500.0 + 5 * (POINTS + 5), {"cpu": 1.0})                                         # down for longer than the whole window
    assert h.export()["cpu"] == [1.0]
    h.push(h.t_last - 1.0, {"cpu": 2.0})                                                   # a small step back is just the next point
    assert h.export()["cpu"] == [1.0, 2.0]


def test_history_persist_and_restart_recovery(env):
    now = time.time()
    h = live.History(5.0)
    for i in range(10):
        h.push(now - 100 + 5 * i, {"cpu": 10.0 + i, "mem_pct": 50 + i, "psi_mem": None})
    blob = h.dump()
    back = live.History(5.0)
    assert back.load(blob.decode(), now) is True
    e = back.export()
    assert e["cpu"] == [10.0 + i for i in range(10)] and e["mem_pct"] == [50 + i for i in range(10)]
    assert all(isinstance(x, int) for x in e["mem_pct"])                                   # ints stay ints (smaller file)
    assert e["psi_mem"] == [None] * 10 and e["t0"] == h.export()["t0"]
    back.push(now + 20, {"cpu": 99.0})                                                      # last point was at now-55: 75 s away = 15 steps
    e2 = back.export()
    assert e2["cpu"][:10] == [10.0 + i for i in range(10)] and e2["cpu"][-1] == 99.0
    assert e2["cpu"][10:-1] == [None] * 14 and len(e2["cpu"]) == 25                         # a visible gap, not a flat line
    assert e2["t0"] == h.export()["t0"] and back.t_last == h.t_last + 75


@pytest.mark.parametrize("text", [
    None, "", "not json", "[]", "{}", '{"v": 2, "step_s": 5.0, "t_last": 1, "series": {}}',
    '{"v": 1, "step_s": 1.0, "t_last": 1, "series": {}}',                                   # written with another step
    '{"v": 1, "step_s": 5.0, "t_last": "x", "series": {}}',
    '{"v": 1, "step_s": 5.0, "t_last": 1, "series": {"cpu": [1]}}',                         # long ago + missing series
])
def test_history_load_rejects_bad_dumps_and_changes_nothing(text):
    h = live.History(5.0)
    assert h.load(text, time.time()) is False and h.t_last is None and h.export()["cpu"] == []


def test_history_load_rejects_future_and_too_old_dumps_and_sanitises_values():
    now = time.time()
    good = {"v": 1, "step_s": 5.0, "t_last": now - 5, "series": {k: [1, 2, 3] for k in SERIES}}
    h = live.History(5.0)
    assert h.load(json.dumps(good), now) is True
    for t_last in (now + 600, now - POINTS * 5 - 60):
        assert live.History(5.0).load(json.dumps({**good, "t_last": t_last}), now) is False
    dirty = {**good, "series": {k: [1, "x", None, True, float("nan"), 10 ** 30, 2.5] for k in SERIES}}
    h2 = live.History(5.0)
    assert h2.load(json.dumps(dirty), now) is True
    assert h2.export()["cpu"] == [1, None, None, None, None, None, 2.5]
    ragged = {**good, "series": {**good["series"], "cpu": [1, 2, 3, 4, 5, 6]}}
    h3 = live.History(5.0)
    assert h3.load(json.dumps(ragged), now) is True and all(len(h3.export()[k]) == 3 for k in SERIES)   # series stay aligned


# =========================================================================== atomic write and size cap
def test_write_atomic_mode_content_and_no_leftovers(env):
    old = os.umask(0o077)
    try:
        p = env.state / "public" / "live.json"
        live.write_atomic(p, b'{"a":1}', 0o644)
    finally:
        os.umask(old)
    assert p.read_bytes() == b'{"a":1}' and (p.stat().st_mode & 0o777) == 0o644        # umask 077 cannot make it private
    assert (p.parent.stat().st_mode & 0o777) == 0o755                                    # nor can it hide the dir the web container reads
    assert [x.name for x in p.parent.iterdir()] == ["live.json"]
    live.write_atomic(p, b"[2]", 0o600)
    assert p.read_bytes() == b"[2]" and (p.stat().st_mode & 0o777) == 0o600


def test_write_atomic_readers_never_see_a_partial_file(env):
    p = env.state / "public" / "live.json"
    live.write_atomic(p, json.dumps({"n": 0, "pad": "x" * 30000}).encode())
    stop, bad, reads = threading.Event(), [], [0]

    def reader():
        while not stop.is_set():
            try:
                json.loads(p.read_bytes())
                reads[0] += 1
            except FileNotFoundError:
                bad.append("missing")
            except ValueError as exc:
                bad.append(str(exc))
    t = threading.Thread(target=reader)
    t.start()
    for i in range(1, 300):
        live.write_atomic(p, json.dumps({"n": i, "pad": "y" * (30000 + i * 50)}).encode())
    stop.set()
    t.join(5)
    assert bad == [] and reads[0] > 10


def test_write_atomic_failure_keeps_the_old_file_and_cleans_up(env, monkeypatch):
    p = env.state / "public" / "live.json"
    live.write_atomic(p, b"old")

    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        live.write_atomic(p, b"new")
    assert p.read_bytes() == b"old" and [x.name for x in p.parent.iterdir()] == ["live.json"]


def test_encode_drops_the_oldest_history_points_to_stay_under_the_cap():
    n = 720
    hist = {"t0": 1000.0, "step_s": 5.0, "units": {}, **{k: [round(1234.56789 + i, 5) for i in range(n)] for k in SERIES}}
    payload = {"schema": 1, "history": hist, "pad": "z" * 500}
    data = live.encode(payload, cap=20_000)
    out = json.loads(data)
    assert len(data) <= 20_000
    h = out["history"]
    k = len(h["cpu"])
    assert 1 < k < n and all(len(h[s]) == k for s in SERIES)                                 # still aligned
    assert h["cpu"][-1] == round(1234.56789 + n - 1, 5)                                      # the NEWEST points are kept
    assert h["t0"] == 1000.0 + (n - k) * 5.0                                                  # and t0 follows the drop
    small = live.encode({"schema": 1, "history": {"t0": 1.0, "step_s": 5.0, **{s: [1] for s in SERIES}}}, cap=10_000)
    assert json.loads(small)["history"]["cpu"] == [1]


def test_a_full_720_point_file_with_realistic_busy_values_fits_the_file_budget(env, host):
    L = live.Live(quiet_cfg())
    t = time.time() - 5 * 800
    for i in range(800):
        L.hist.push(t + 5 * i, {"cpu": 87.3, "mem_pct": 74, "psi_mem": 3.4, "psi_io": 22.1, "gpu": 96, "net_rx": 85.2,
                                "net_tx": 12.7, "disk_r": 412.9, "disk_w": 103.7, "swap_pct": 38, "vram_pct": 93, "load1": 31.42})
    data = L.tick(wall=t + 5 * 800, mono=1000.0, write=False)
    h = json.loads(data)["history"]
    assert len(data) < live.FILE_CAP and len(h["cpu"]) == 720                                # not even trimmed in the common case
    pathological = {"t0": 0.0, "step_s": 5.0, **{k: [1234.5] * 720 for k in SERIES}}
    assert len(live.encode({"history": pathological})) <= live.FILE_CAP


# =========================================================================== activity, pressure
def test_flock_keys_from_proc_locks_match_stat(tmp_path):
    f = tmp_path / "x.lock"
    fd = os.open(f, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        held = live.parse_flock_keys(Path("/proc/locks").read_text())
        assert live._lock_key(f) in held                                                       # same key format as this kernel prints
    finally:
        os.close(fd)
    assert live._lock_key(f) not in live.parse_flock_keys(Path("/proc/locks").read_text())
    assert live._lock_key(tmp_path / "missing") is None


def test_parse_flock_keys_ignores_posix_locks_waiters_and_junk():
    txt = ("1: POSIX  ADVISORY  WRITE 100 08:01:111 0 EOF\n"
           "2: FLOCK  ADVISORY  WRITE 200 00:1a:222 0 EOF\n"
           "3: -> FLOCK  ADVISORY  WRITE 300 00:1a:333 0 EOF\n"                              # a waiter holds nothing
           "4: OFDLCK ADVISORY  READ  400 00:1a:444 0 EOF\n"
           "5: FLOCK  ADVISORY  READ  500 103:02:555 0 EOF\n"
           "garbage\n6: FLOCK ADVISORY WRITE 1 notakey 0 EOF\n")
    assert live.parse_flock_keys(txt) == {"00:1a:222", "103:02:555"}
    assert live.parse_flock_keys(None) == set()


def test_maintenance_running_comes_from_held_tier_locks(env, monkeypatch):
    monkeypatch.setattr(live, "PROC", Path("/proc"))                                           # the real kernel's lock table
    a = live.Activity()
    assert a.running() == ([], False)                                                          # no lock files at all
    held = []
    for name in ("daily", "check", "weekly"):
        f = open(env.run / f"{name}.lock", "w")
        held.append(f)
    try:
        assert a.running() == ([], False)                                                      # the files exist but nobody holds them
        fcntl.flock(held[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert a.running() == (["daily"], False)
        fcntl.flock(held[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert a.running() == (["daily"], True)                                                # the 15-min check is not "maintenance"
        fcntl.flock(held[2], fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert a.running() == (["daily", "weekly"], True)
        fcntl.flock(held[0], fcntl.LOCK_UN)
        assert a.running() == (["weekly"], True)
    finally:
        for f in held:
            f.close()
    assert a.running() == ([], False)


def test_scheduler_tick_lock_is_not_maintenance(env, monkeypatch):
    """The scheduler holds RUN_DIR/tick.lock for a moment every minute: that is not a maintenance run."""
    monkeypatch.setattr(live, "PROC", Path("/proc"))
    a = live.Activity()
    f = open(env.run / "tick.lock", "w")
    g = open(env.run / "weekly.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert a.running() == ([], False)
        fcntl.flock(g, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert a.running() == (["weekly"], False)
    finally:
        f.close()
        g.close()


def sched_json(env, jobs):
    write(env.state / "sched.json", json.dumps({"schema": 1, "meta": {}, "jobs": jobs}))


def test_scheduler_jobs_that_run_long_enough_count_as_maintenance(env):
    """sched.json `jobs.<name>.running` (pid alive, >= 10 s old) is listed; a fresh, dead, malformed or idle entry is not."""
    (env.proc / "4242").mkdir()                                                                  # the supervisor of backup-system is alive
    (env.proc / "4343").mkdir()
    now = 1_000_000.0
    sched_json(env, {
        "backup-system": {"running": {"pid": 4242, "started": now - 600, "run_id": "r1"}},
        "just-started": {"running": {"pid": 4343, "started": now - 3}},                          # under MIN_JOB_S: would flicker
        "dead": {"running": {"pid": 9999, "started": now - 600}},                                # supervisor gone (no /proc/9999)
        "no-pid-yet": {"running": {"pid": None, "started": now - 600}},
        "bool-pid": {"running": {"pid": True, "started": now - 600}},
        "idle": {"last_status": "ok"},
        "junk": "text",
        "odd": {"running": ["not", "a", "dict"]},
        "café-job": {"running": {"pid": 4242, "started": now - 60}},
    })
    a = live.Activity()
    assert a.running(now) == (["backup-system", "caf?-job"], False)                              # ASCII only, sorted
    assert a.running(now + 20) == (["backup-system", "caf?-job", "just-started"], False)          # it has now run for 23 s
    (env.proc / "4242").rmdir()                                                                  # the supervisor exits
    assert a.running(now + 20) == (["just-started"], False)


def test_scheduler_jobs_merge_with_tier_locks_and_survive_a_bad_sched_file(env, monkeypatch):
    monkeypatch.setattr(live, "PROC", Path("/proc"))
    now = time.time()
    sched_json(env, {"nightly": {"running": {"pid": os.getpid(), "started": now - 120}}})      # a pid that certainly exists
    f = open(env.run / "daily.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        a = live.Activity()
        assert a.running() == (["daily", "nightly"], False)                                      # `now` defaults to the wall clock
        write(env.state / "sched.json", "{not json")                                             # a half-written / corrupt file
        assert a.running() == (["daily"], False)
        (env.state / "sched.json").unlink()
        assert a.running() == (["daily"], False)
    finally:
        f.close()


def test_scheduler_state_is_parsed_only_when_the_file_changes(env, monkeypatch):
    sched_json(env, {})
    a = live.Activity()
    a.running(100.0)
    calls = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *x, **k: (calls.append(self.name), real(self, *x, **k))[1])
    for _ in range(5):
        a.running(100.0)
    assert "sched.json" not in calls                                                             # unchanged: not re-read
    sched_json(env, {"j": {"running": {"pid": 1, "started": 1.0}}})
    a.running(100.0)
    assert calls.count("sched.json") == 1


def test_maintenance_running_probing_does_not_take_the_locks(env, monkeypatch):
    monkeypatch.setattr(live, "PROC", Path("/proc"))
    f = open(env.run / "daily.lock", "w")
    live.Activity().running()
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)                                              # the runner can still take it right after
    f.close()


def test_maintenance_running_with_an_unreadable_lock_table_is_empty(env):
    write(env.run / "daily.lock", "")
    assert live.Activity().running() == ([], False)                                            # env.proc has no `locks` file


def audit_line(ts, task, action, outcome):
    return json.dumps({"ts": ts, "task": task, "action": action, "target": "/secret/path", "bytes": 5, "outcome": outcome}) + "\n"


def test_last_action_is_the_newest_real_action_and_rereads_only_on_change(env):
    a = live.Activity()
    assert a.last_action() is None                                                              # no audit log yet
    log = env.log / "audit.jsonl"
    write(log, audit_line("2026-10-01T07:30:01-0400", "docker_cache", "builder prune", "done")
          + audit_line("2026-10-01T07:31:00-0400", "docker_images", "image rm", "dry-run")
          + audit_line("2026-10-01T07:32:00-0400", "notify", "send", "done")
          + audit_line("2026-10-01T07:33:00-0400", "caps", "docker update", "refused-protected")
          + "not json at all\n")
    got = a.last_action()
    assert got == {"ts": datetime(2026, 10, 1, 11, 30, 1, tzinfo=timezone.utc).timestamp(), "task": "docker_cache",
                   "action": "builder prune"}                                                    # only the real action; no target/path leaks
    assert "secret" not in json.dumps(got)
    # same size and mtime: not re-read (the cached answer survives even though the bytes changed)
    st = log.stat()
    log.write_bytes(log.read_bytes().replace(b"docker_cache", b"XXXXXXXXXXXX"))
    os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert a.last_action()["task"] == "docker_cache"
    with open(log, "a") as f:                                                                    # a new line: re-read
        f.write(audit_line("2026-10-02T01:00:00-0400", "retention", "unlink", "done"))
    assert a.last_action()["task"] == "retention"
    log.unlink()
    assert a.last_action() is None                                                                # the log disappeared


def test_last_action_reads_only_the_tail_of_a_big_log(env):
    log = env.log / "audit.jsonl"
    with open(log, "w") as f:
        f.write(audit_line("2026-01-01T00:00:00-0400", "old", "ancient", "done"))
        for _ in range(3000):
            f.write(audit_line("2026-10-01T00:00:00-0400", "x", "dry", "dry-run"))
    assert log.stat().st_size > 131072
    assert live.Activity().last_action() is None                                                # the only "done" is beyond the tail window


def test_pressure_level_is_read_from_status_json_when_it_changes(env):
    p = live.PressureSrc()
    none = {"level": None, "gate_level": None, "level_name": None, "why": "", "age_s": None}
    assert p.read(1000.0) == none                                                              # no status.json: unknown, not 0
    st = env.state / "status.json"
    write(st, json.dumps({"tasks": {"pressure_state": {"summary": "x", "last_run": 940.0,
                                                       "metrics": {"level": 2, "why": "io psi 30% (some)"}}}}))
    assert p.read(1000.0) == {"level": 2, "gate_level": 2, "level_name": None, "why": "io psi 30% (some)", "age_s": 60}   # no gate_level: = level
    assert p.read(1030.0)["age_s"] == 90                                                       # age moves, the file is not re-parsed
    write(st, json.dumps({"tasks": {"pressure_state": {"last_run": 990.0, "metrics": {"level": 9, "why": "café"}}}}))
    os.utime(st, (time.time() + 5, time.time() + 5))
    got = p.read(1000.0)
    assert got["level"] is None and got["why"] == "caf?"                                       # out-of-range level is rejected
    write(st, "{broken")
    os.utime(st, (time.time() + 10, time.time() + 10))
    assert p.read(1000.0) == none
    write(st, json.dumps({"tasks": {}}))
    os.utime(st, (time.time() + 15, time.time() + 15))
    assert p.read(1000.0)["level"] is None
    write(st, json.dumps({"tasks": {"pressure_state": {"metrics": {"level": True}}}}))
    os.utime(st, (time.time() + 20, time.time() + 20))
    assert p.read(1000.0)["level"] is None                                                      # bool is not a level
    # io-only pressure: the host level is 3 but nothing waits on it, so gate_level is 0 and the Live tab colours by it
    write(st, json.dumps({"tasks": {"pressure_state": {"last_run": 990.0, "metrics": {"level": 3, "gate_level": 0, "level_name": "io stall\u00e9"}}}}))
    os.utime(st, (time.time() + 25, time.time() + 25))
    got = p.read(1000.0)
    assert (got["level"], got["gate_level"], got["level_name"]) == (3, 0, "io stall?")           # ascii-only, like why
    write(st, json.dumps({"tasks": {"pressure_state": {"metrics": {"level": 3, "gate_level": 9, "level_name": 5}}}}))
    os.utime(st, (time.time() + 30, time.time() + 30))
    got = p.read(1000.0)
    assert (got["level"], got["gate_level"], got["level_name"]) == (3, 3, None)                 # junk gate_level / name: fall back, never raise


# =========================================================================== the daemon object: schema, degraded probes, persistence
SCHEMA = {
    "schema": 1, "generated_at": None, "interval_s": None,
    "host": {"uptime_s": None, "load": None, "cores": None, "cpu_pct": None, "cpu_user": None, "cpu_sys": None, "cpu_iowait": None,
             "mem": {"total": None, "used": None, "avail": None, "cache": None, "swap_used": None, "swap_total": None},
             "psi": {"mem_some60": None, "mem_full60": None, "io_some60": None, "io_full60": None, "cpu_some60": None},
             "disk": None, "io": None, "net": {"rx_bps": None, "tx_bps": None}},
    "gpu": {"util": None, "mem_used": None, "mem_total": None, "temp": None, "power_w": None, "fan_pct": None, "stale": None},
    "sensors": {"cpu_temp": None, "gpu_temp": None, "ram_temp": None, "nvme_temp": None, "cpu_fan_rpm": None,
                "case_fan_rpm": None, "stale": None},
    "containers": {"running": None, "unhealthy": None, "top_cpu": None, "top_mem": None, "stale": None},
    "services": None,
    "io_top": {"readers": None, "window_s": None, "stale": None},
    "swap": {"used_b": None, "total_b": None, "used_pct": None, "state": None, "in_bps": None, "out_bps": None, "exhausted": None, "holders": None, "stale": None},
    "activity": {"maintenance_running": None, "check_running": None, "last_action": None},
    "pressure": {"level": None, "gate_level": None, "level_name": None, "why": None, "age_s": None},
    "history": {"t0": None, "step_s": None, **{k: None for k in SERIES}},
    "probes": None, "self": {"pid": None, "started_at": None, "ticks": None, "errors": None, "tick_ms": None, "rss_mb": None, "threads": None},
}


def assert_schema(obj, schema, path="live"):
    for k, v in schema.items():
        assert k in obj, f"{path}.{k} missing"
        if isinstance(v, dict):
            assert_schema(obj[k], v, f"{path}.{k}")


def test_live_json_has_every_block_of_the_spec3_contract_even_with_no_data(env):
    L = live.Live(quiet_cfg())                                                                  # empty fake /proc, every probe off
    out = json.loads(L.tick(wall=1_790_000_000.0, mono=10.0, write=False))
    assert_schema(out, SCHEMA)
    assert out["interval_s"] == 5.0 and out["generated_at"] == 1_790_000_000.0
    assert out["gpu"]["stale"] is True and out["sensors"]["stale"] is True and out["containers"]["stale"] is True
    assert out["services"] == [] and out["probes"] == {} and out["host"]["disk"] == []
    assert out["activity"] == {"maintenance_running": [], "check_running": False, "last_action": None}
    assert out["self"]["errors"] == 0 and out["self"]["pid"] == os.getpid()


def test_a_normal_tick_with_every_probe_fresh(env, host, monkeypatch):
    host.write()
    L = live.Live(quiet_cfg())
    clk = Clock()
    L.probes["gpu"] = live.Probe("gpu", lambda: {"util": 55, "mem_used": 1, "mem_total": 2, "temp": 61, "power_w": 120.5, "fan_pct": 40}, 10, clock=clk)
    L.probes["sensors"] = live.Probe("sensors", lambda: {"cpu_temp": 70.0, "gpu_temp": 99.0, "ram_temp": 35.0, "nvme_temp": 50.0,
                                                          "cpu_fan_rpm": 1600, "case_fan_rpm": 500}, 15, clock=clk)
    L.probes["disk"] = live.Probe("disk", lambda: [{"mount": "/", "free_b": 1, "size_b": 2, "used_pct": 50.0}], 30, clock=clk)
    ct = make_ct(env, 1, "tunarr-host-net", 0, 4 * GIB)
    L.probes["docker"] = live.Probe("docker", lambda: [ct], 10, clock=clk)
    L.probes["services"] = live.Probe("services", lambda: [{"name": "Plex", "state": "up", "detail": "2 ms", "ms": 2}], 30, clock=clk)
    for p in L.probes.values():
        p.run_once()
    L.tick(wall=1000.0, mono=1.0, write=False)
    host.cpu = [2200, 0, 1100, 17000, 1700, 0, 0, 0]
    host.write()
    set_ct(env, 1, 5_000_000, 4 * GIB)
    out = json.loads(L.tick(wall=1005.0, mono=6.0, write=False))
    assert_schema(out, SCHEMA)
    assert out["host"]["cpu_pct"] == 15.0 and out["host"]["disk"][0]["mount"] == "/"
    assert out["gpu"] == {"util": 55, "mem_used": 1, "mem_total": 2, "temp": 61, "power_w": 120.5, "fan_pct": 40, "stale": False}
    assert out["sensors"]["gpu_temp"] == 61 and out["sensors"]["cpu_temp"] == 70.0 and out["sensors"]["stale"] is False   # nvidia-smi wins
    assert out["containers"]["running"] == 1 and out["containers"]["top_cpu"][0]["cpu_pct"] == 100.0
    assert out["services"] == [{"name": "Plex", "state": "up", "detail": "2 ms", "ms": 2, "stale": False}]
    assert set(out["probes"]) == {"gpu", "sensors", "disk", "docker", "services"} and out["probes"]["gpu"]["stale"] is False
    h = out["history"]
    assert h["gpu"] == [55, 55] and h["cpu"] == [None, 15.0] and h["mem_pct"] == [75, 75] and len(h["net_rx"]) == 2
    assert out["self"]["ticks"] == 1                                                            # the count BEFORE this tick, by design


def test_stale_probes_keep_their_last_value_but_are_marked_and_leave_gaps(env, host):
    L = live.Live(quiet_cfg())
    clk = Clock()
    L.probes["gpu"] = live.Probe("gpu", lambda: {"util": 55, "mem_used": 1, "mem_total": 2, "temp": 61, "power_w": 1.0, "fan_pct": 1}, 10, clock=clk)
    L.probes["gpu"].run_once()
    first = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    assert first["gpu"]["stale"] is False and first["history"]["gpu"] == [55]
    clk.t += 31                                                                                  # three cadences without a refresh
    second = json.loads(L.tick(wall=1005.0, mono=6.0, write=False))
    assert second["gpu"]["stale"] is True and second["gpu"]["util"] == 55                        # last good value, flagged
    assert second["history"]["gpu"] == [55, None]                                                # but the graph gets a gap, not a flat line
    assert second["probes"]["gpu"] == {"age_s": 31.0, "stale": True}


def test_swap_vram_and_load_are_in_the_history_so_the_live_tab_needs_no_page_trail(env, host):
    """The Live tab draws Swap, VRAM and Load from history.swap_pct / vram_pct / load1 when present (else only from when the page opened):
    whole percents, the 1-minute load, null where there is nothing to show (no swap, a stale or missing GPU reading)."""
    L = live.Live(quiet_cfg())
    clk = Clock()
    L.probes["gpu"] = live.Probe("gpu", lambda: {"util": 55, "mem_used": 22 * GIB, "mem_total": 24 * GIB, "temp": 61, "power_w": 1.0, "fan_pct": 1}, 10, clock=clk)
    L.probes["gpu"].run_once()
    out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    h = out["history"]
    assert h["swap_pct"] == [25] and h["vram_pct"] == [92] and h["load1"] == [1.5]                  # 8 of 32 GiB swap; 22 of 24 GiB VRAM
    assert {"swap_pct", "vram_pct", "load1"} <= set(h["units"]) and h["units"]["load1"] == "" and len(h["cpu"]) == 1
    clk.t += 31                                                                                      # a stale reading is a gap, never a flat line
    host.mem_kb["SwapTotal"] = host.mem_kb["SwapFree"] = 0                                          # a host without swap: null, not 0 %
    host.load = ""                                                                                    # an unreadable loadavg
    host.write()
    h = json.loads(L.tick(wall=1005.0, mono=6.0, write=False))["history"]
    assert h["swap_pct"] == [25, None] and h["vram_pct"] == [92, None] and h["load1"] == [1.5, None]
    L2 = live.Live(quiet_cfg())                                                                       # never a GPU probe at all
    assert json.loads(L2.tick(wall=1000.0, mono=1.0, write=False))["history"]["vram_pct"] == [None]


def test_a_history_dump_from_before_these_series_still_restores_the_graph():
    """live-history.json written by the previous release has 9 series: the restart keeps those and the new three start empty."""
    now = time.time()
    old = {"v": 1, "step_s": 5.0, "t_last": now - 5, "series": {k: [1, 2, 3] for k in SERIES if k not in live.LATER_SERIES}}
    h = live.History(5.0)
    assert h.load(json.dumps(old), now) is True
    e = h.export()
    assert e["cpu"] == [1, 2, 3] and e["swap_pct"] == [None] * 3 and e["vram_pct"] == [None] * 3 and e["load1"] == [None] * 3
    gone = {**old, "series": {k: v for k, v in old["series"].items() if k != "cpu"}}                  # an OLD series missing is still a corrupt dump
    assert live.History(5.0).load(json.dumps(gone), now) is False
    junk = {**old, "series": {**old["series"], "load1": "x"}}                                         # a present but broken new series is corrupt too
    assert live.History(5.0).load(json.dumps(junk), now) is False


def test_degraded_probes_nvidia_smi_missing_exporter_down_docker_slow(env, host, monkeypatch, docker_api):
    """Everything external fails at once: the file is still produced, on time, every block present, errors stay 0."""
    hung = docker_api([], delay=5.0)                                                              # docker accepts but never answers in time
    monkeypatch.setattr(live, "DOCKER_SOCK", hung.path)
    cfg = live.Cfg(docker_timeout=0.3, http_timeout=0.3, mounts=[],
                   services=[live.clean_service({"name": "Kavita", "container": "kavita", "url": f"http://127.0.0.1:{free_port()}/"})])
    L = live.Live(cfg)
    assert L.probes["services"].after == [L.probes["docker"]]                                     # services wait for docker's first answer
    t0 = time.monotonic()
    for name in ("gpu", "sensors", "docker", "disk", "services"):
        L.probes[name].run_once()                                                                 # inline: each failure is caught by the probe
    out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    assert time.monotonic() - t0 < 3.0
    assert_schema(out, SCHEMA)
    assert out["gpu"]["stale"] is True and out["gpu"]["util"] is None                            # nvidia-smi missing (rc 127)
    assert out["sensors"]["stale"] is True and out["sensors"]["cpu_temp"] is None                # no hwmon, exporter refused
    assert out["containers"] == {"running": None, "unhealthy": [], "top_cpu": [], "top_mem": [], "stale": True}   # docker too slow
    assert out["services"][0]["state"] == "down" and out["services"][0]["detail"] == "refused"   # HTTP alone decided (docker unknown)
    assert out["services"][0]["stale"] is False                                                   # the services pass itself succeeded
    assert out["probes"]["docker"]["stale"] is True and out["probes"]["gpu"]["stale"] is True
    assert out["self"]["errors"] == 0                                                             # probe failures are data, not tick errors
    assert out["host"]["mem"]["total"] == 64 * GIB                                                # the host section needs no probe at all
    for cmd in env.sh_calls:                                                                      # and everything forked is a harmless read
        assert cmd[0] in ("nvidia-smi", "docker", "systemctl")
        assert cmd[0] != "docker" or cmd[1] == "ps"
        assert cmd[0] != "systemctl" or cmd[1] == "is-active"


def test_start_lets_the_first_probe_results_land_before_the_first_file(env, host):
    L = live.Live(quiet_cfg())
    L.probes["gpu"] = live.Probe("gpu", lambda: {"util": 7, "mem_used": 1, "mem_total": 2, "temp": 50, "power_w": 1.0, "fan_pct": 0}, 10)
    L.probes["docker"] = live.Probe("docker", lambda: (time.sleep(0.2), [])[1], 10)
    hung = threading.Event()
    L.probes["sensors"] = live.Probe("sensors", lambda: hung.wait(30), 10)                         # never answers
    stop = live.Stop()
    t0 = time.monotonic()
    L.start(stop, wait_first=0.6)
    took = time.monotonic() - t0
    try:
        assert 0.15 <= took < 1.5                                                                  # waited for docker, gave up on the hung one
        out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
        assert out["gpu"]["stale"] is False and out["gpu"]["util"] == 7 and out["containers"]["stale"] is False
        assert out["sensors"]["stale"] is True
    finally:
        hung.set()
        stop.set()


def test_self_block_reports_rss_and_threads(env, host):
    L = live.Live(quiet_cfg())
    me = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))["self"]
    assert me["rss_mb"] == round(5000 * os.sysconf("SC_PAGE_SIZE") / MIB, 1)                      # from the fake statm
    assert me["threads"] >= 1 and me["tick_ms"] == 0.0 and me["started_at"] > 0


def test_the_tick_never_waits_for_a_hung_probe(env, host):
    L = live.Live(quiet_cfg())
    release = threading.Event()
    L.probes["gpu"] = live.Probe("gpu", lambda: release.wait(30) and {}, 10)                      # blocks "forever"
    stop = live.Stop()
    th = L.probes["gpu"].start(stop)
    try:
        time.sleep(0.1)
        t0 = time.monotonic()
        out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
        assert time.monotonic() - t0 < 0.5 and out["gpu"]["stale"] is True and out["gpu"]["util"] is None
    finally:
        release.set()
        stop.set()
        th.join(2)


def test_one_broken_section_does_not_cost_the_whole_file(env, host, monkeypatch):
    L = live.Live(quiet_cfg())

    def boom(*a, **k):
        raise RuntimeError("section exploded")
    monkeypatch.setattr(L.sampler, "read_containers", boom)
    monkeypatch.setattr(L.activity, "last_action", boom)
    out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    assert out["containers"]["stale"] is True and out["activity"]["last_action"] is None
    assert out["host"]["mem"]["total"] == 64 * GIB and out["self"]["errors"] == 2


def test_a_failing_host_section_still_has_the_full_host_skeleton(env, monkeypatch):
    L = live.Live(quiet_cfg())
    monkeypatch.setattr(L.sampler, "read_host", lambda *a: (_ for _ in ()).throw(RuntimeError("proc vanished")))
    out = json.loads(L.tick(wall=1000.0, mono=1.0, write=False))
    assert_schema(out, SCHEMA)                                                                        # the web UI never meets a missing key
    assert out["host"]["cpu_pct"] is None and out["host"]["mem"]["total"] is None and out["host"]["net"]["rx_bps"] is None
    assert out["host"]["disk"] == [] and out["host"]["io"] == [] and out["history"]["cpu"] == [None]
    assert out["self"]["errors"] == 1


def test_tick_writes_live_json_atomically_0644_and_persists_history_once_a_minute(env, host):
    L = live.Live(quiet_cfg())
    L.last_persist = 0.0
    L.tick(wall=1000.0, mono=10.0)
    pub = env.state / "public" / "live.json"
    assert (pub.stat().st_mode & 0o777) == 0o644 and (env.state / "public").is_dir()
    assert json.loads(pub.read_text())["schema"] == 1
    assert not (env.state / "live-history.json").exists()                                        # < 60 s since start: not yet
    L.tick(wall=1005.0, mono=75.0)
    hist = env.state / "live-history.json"
    assert hist.exists() and (hist.stat().st_mode & 0o777) == 0o600                              # private: not in public/
    assert json.loads(hist.read_text())["series"]["mem_pct"] == [75, 75]
    mtime = hist.stat().st_mtime_ns
    L.tick(wall=1010.0, mono=80.0)
    assert hist.stat().st_mtime_ns == mtime                                                       # 5 s later: not rewritten
    assert sorted(str(p.relative_to(env.state)) for p in env.state.rglob("*") if p.is_file()) == ["live-history.json", "public/live.json"]


def test_a_failing_write_is_counted_and_the_loop_carries_on(env, host, monkeypatch):
    L = live.Live(quiet_cfg())
    monkeypatch.setattr(live, "write_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")))
    out = json.loads(L.tick(wall=1000.0, mono=1.0))
    assert L.errors == 1 and out["self"]["errors"] == 0 and L.ticks == 1


def test_restart_keeps_the_graph_and_shows_the_downtime_as_a_gap(env, host):
    now = time.time()
    a = live.Live(quiet_cfg())
    for i in range(6):
        a.tick(wall=now - 60 + 5 * i, mono=100.0 + 5 * i, write=False)
    a.persist()
    b = live.Live(quiet_cfg())
    b.start(live.Stop())                                                                          # no probes: only the recovery runs
    out = json.loads(b.tick(wall=now, mono=500.0, write=False))
    h = out["history"]
    assert h["mem_pct"][:6] == [75] * 6 and h["mem_pct"][-1] == 75                                # old points are back
    gap = h["mem_pct"][6:-1]
    assert gap and set(gap) == {None}                                                              # downtime shows as nulls
    assert h["t0"] == pytest.approx(now - 60, abs=1.0) and len(h["cpu"]) == len(h["mem_pct"])
    assert h["t0"] + (len(h["cpu"]) - 1) * 5 == pytest.approx(now, abs=3.0)                        # newest point is "now"


def test_restart_with_a_corrupt_history_file_just_starts_fresh(env, host):
    write(env.state / "live-history.json", "{truncated")
    b = live.Live(quiet_cfg())
    b.start(live.Stop())
    out = json.loads(b.tick(wall=time.time(), mono=1.0, write=False))
    assert out["history"]["mem_pct"] == [75]


def test_run_loop_ticks_until_stopped_then_persists_and_returns_zero(env, host):
    cfg = quiet_cfg(interval=0.05)
    L = live.Live(cfg)
    stop = live.Stop()
    rc = []
    th = threading.Thread(target=lambda: rc.append(L.run(stop)))
    th.start()
    pub = env.state / "public" / "live.json"
    assert wait_for(lambda: pub.exists() and json.loads(pub.read_text())["self"]["ticks"] >= 3, 5)
    stop.set()
    th.join(3)
    assert not th.is_alive() and rc == [0]
    assert (env.state / "live-history.json").exists()                                              # graceful stop persists the history


def test_run_with_duration_stops_by_itself(env, host):
    L = live.Live(quiet_cfg(interval=0.05))
    t0 = time.monotonic()
    assert L.run(live.Stop(), duration=0.3) == 0
    assert 0.25 <= time.monotonic() - t0 < 2.0 and L.ticks >= 3


def test_run_survives_a_tick_that_raises(env, host, monkeypatch):
    L = live.Live(quiet_cfg(interval=0.05))
    n = {"i": 0}
    real = L.build

    def flaky(wall, mono):
        n["i"] += 1
        if n["i"] == 2:
            raise ValueError("bad tick")
        return real(wall, mono)
    monkeypatch.setattr(L, "build", flaky)
    assert L.run(live.Stop(), duration=0.4) == 0
    assert L.errors == 1 and L.ticks >= 3


def test_next_tick_keeps_a_fixed_grid_and_resyncs_after_a_stall():
    assert live.next_tick(100.0, 100.4, 5.0) == 105.0                                       # on a grid: no drift from the tick's own duration
    assert live.next_tick(105.0, 105.01, 5.0) == 110.0
    assert live.next_tick(100.0, 104.9, 5.0) == 105.0
    assert live.next_tick(100.0, 109.9, 5.0) == 105.0                                       # one interval late: still on the grid (fires at once)
    assert live.next_tick(100.0, 130.0, 5.0) == 130.0                                       # suspended for 30 s: resync, no six catch-up ticks


# =========================================================================== the daemon as a process: SIGTERM, restart, hung services
def conf_for_daemon(env, extra=""):
    write(env.conf / "maint.toml", "[live]\ninterval_s = 1\ngpu = false\nsensors = false\ndocker = false\ndisk = false\n"
                                   "services = []\n" + extra)


def spawn(env, **more):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HOMELAB_MAINT_")}
    e.update(HOMELAB_MAINT_STATE=str(env.state), HOMELAB_MAINT_LOG=str(env.log), HOMELAB_MAINT_RUN=str(env.run),
             HOMELAB_MAINT_CONF=str(env.conf), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    e.update(more)
    return subprocess.Popen([sys.executable, "-B", "-m", "homelab_maint.live"], cwd=ROOT, env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def live_json(env):
    try:
        return json.loads((env.state / "public" / "live.json").read_text())
    except (OSError, ValueError):
        return None


def stop_proc(p, sig=signal.SIGTERM, timeout=6.0):
    t0 = time.monotonic()
    p.send_signal(sig)
    try:
        rc = p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    return rc, time.monotonic() - t0


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_daemon_exits_zero_on_signal_after_persisting_history(env, sig):
    conf_for_daemon(env)
    p = spawn(env)
    try:
        assert wait_for(lambda: (live_json(env) or {}).get("self", {}).get("ticks", 0) >= 2, 12)
        rc, took = stop_proc(p, sig)
        err = p.stderr.read()
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 0 and took < 3.0, err
    assert "sampling every 1 s" in err and "Traceback" not in err
    hist = env.state / "live-history.json"
    assert hist.exists() and (hist.stat().st_mode & 0o777) == 0o600
    assert len(json.loads(hist.read_text())["series"]["mem_pct"]) >= 2
    assert not list((env.state / "public").glob("*.tmp"))                                          # no half-written leftovers
    d = live_json(env)
    assert d is not None and d["schema"] == 1 and d["self"]["pid"] == p.pid and d["host"]["mem"]["total"] > 0


def test_a_second_run_restores_the_graph_from_the_first(env):
    conf_for_daemon(env)
    p1 = spawn(env)
    try:
        assert wait_for(lambda: (live_json(env) or {}).get("self", {}).get("ticks", 0) >= 2, 12)
        assert stop_proc(p1)[0] == 0
    finally:
        if p1.poll() is None:
            p1.kill()
    before = len(json.loads((env.state / "live-history.json").read_text())["series"]["mem_pct"])
    (env.state / "public" / "live.json").unlink()
    p2 = spawn(env)
    try:
        d = wait_for(lambda: live_json(env), 12)
        assert d and len(d["history"]["mem_pct"]) >= before + 1                                    # old points + at least the new one
        assert d["history"]["mem_pct"][0] is not None
        assert stop_proc(p2)[0] == 0
    finally:
        if p2.poll() is None:
            p2.kill()


def test_sigterm_is_prompt_even_while_a_service_probe_is_hung(env, http):
    port, hits = http({"/hang": (200, 30.0)})
    write(env.conf / "maint.toml", "[live]\ninterval_s = 1\ngpu = false\nsensors = false\ndocker = false\ndisk = false\nhttp_timeout_s = 15\n"
                                   f'[[live.services]]\nname = "Hung"\nurl = "http://127.0.0.1:{port}/hang"\n')
    p = spawn(env)
    try:
        assert wait_for(lambda: live_json(env) is not None, 12)                                    # the file appears although the probe is stuck
        assert wait_for(lambda: hits, 5)                                                           # ... which really is in flight
        assert live_json(env)["services"] == []
        rc, took = stop_proc(p)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 0 and took < 3.0                                                                  # a stuck worker thread does not hold the exit


def test_once_prints_json_and_writes_nothing(env, host, capsys):
    conf_for_daemon(env)
    assert live.main(["--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert_schema(out, SCHEMA)
    assert out["host"]["mem"]["total"] == 64 * GIB and out["history"]["mem_pct"] == [75, 75]      # two samples, so rates can exist
    assert not (env.state / "public").exists() and not (env.state / "live-history.json").exists()


# =========================================================================== command line: `homelab-maint live --once`
# Review finding: the proposed cli.py recipe `lv.add_argument("rest", nargs=argparse.REMAINDER)` rejects EVERY flag
# (`live --once`, `live --duration 5`, both: "error: unrecognized arguments", Python 3.12). The flags must be real options of the
# subparser: `live.add_args(sub.add_parser("live"))` + dispatch to `live.run_args`.
def cli_style_parser():
    """The shape of homelab_maint.cli.main(): a top-level parser, subcommands, and the documented live glue."""
    import argparse
    ap = argparse.ArgumentParser(prog="homelab-maint")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    live.add_args(sub.add_parser("live"))
    return ap


@pytest.mark.parametrize("argv,once,duration", [(["live"], False, None), (["live", "--once"], True, None),
                                               (["live", "--duration", "5"], False, 5.0),
                                               (["live", "--once", "--duration", "5"], True, 5.0),
                                               (["live", "--duration", "0.25", "--once"], True, 0.25)])
def test_the_live_flags_parse_as_a_cli_subcommand(argv, once, duration):
    a = cli_style_parser().parse_args(argv)
    assert (a.cmd, a.once, a.duration) == ("live", once, duration)


@pytest.mark.parametrize("argv", [["live", "--nope"], ["live", "--duration"], ["live", "--duration", "soon"], ["live", "stray"]])
def test_bad_live_arguments_are_refused_not_ignored(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli_style_parser().parse_args(argv)
    assert e.value.code == 2 and "error:" in capsys.readouterr().err


def test_run_args_passes_the_flags_through_and_installs_the_signal_handlers(env, monkeypatch, capsys):
    seen = {}

    class Stub:
        cfg = SimpleNamespace(interval=5.0)

        def run(self, stop, duration):
            seen.update(run=duration, stop=stop)
            return 0

        def once(self):
            seen["once"] = True
            return b'{"ok": 1}'
    monkeypatch.setattr(live, "Live", Stub)
    handlers = {}
    monkeypatch.setattr(live.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))   # keep pytest's own handlers
    assert live.run_args(cli_style_parser().parse_args(["live", "--duration", "7"])) == 0
    assert seen["run"] == 7.0 and set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert seen["stop"].is_set()                                                                # SIGTERM = graceful stop
    assert live.run_args(cli_style_parser().parse_args(["live", "--once"])) == 0
    assert seen["once"] and json.loads(capsys.readouterr().out) == {"ok": 1}
    assert live.main(["--once"]) == 0 and live.main(["--duration", "1.5"]) == 0 and seen["run"] == 1.5   # `python3 -m` form too


def _cli_has_live_subcommand(capsys) -> bool:
    from homelab_maint import cli
    try:
        cli.main(["live", "--help"])
    except SystemExit as e:
        capsys.readouterr()
        return e.code == 0
    return True


def test_homelab_maint_live_once_works_through_the_real_cli(env, monkeypatch, capsys):
    """GLUE-GATED: runs for real once cli.py has the `live` subcommand; until then it skips (the lead applies the glue)."""
    from homelab_maint import cli
    if not _cli_has_live_subcommand(capsys):
        pytest.skip("glue pending: cli.py has no `live` subcommand yet (live.add_args(sub.add_parser('live')); 'live': live.run_args)")

    class Stub:
        cfg = SimpleNamespace(interval=5.0)
        run = lambda self, stop, duration: 0 if duration == 5.0 else 9                          # noqa: E731
        once = lambda self: b'{"ok": 2}'                                                         # noqa: E731
    monkeypatch.setattr(live, "Live", Stub)
    monkeypatch.setattr(live.signal, "signal", lambda *a: None)
    for argv, code in ((["live", "--once"], 0), (["live", "--once", "--duration", "5"], 0), (["live", "--duration", "5"], 0)):
        assert cli.main(argv) == code, argv
    assert capsys.readouterr().out.count('"ok": 2') == 2                                          # printed by the two --once runs only


# =========================================================================== install.sh and the restart/config contract
# Review finding: install.sh restarts only `$WWW` when code or units change (`[[ $u == "$WWW" ]] && ((CODE_CHANGED || UNITS_CHANGED))`).
# live.py is a long-running daemon that never exits, so a re-install would leave the OLD code in memory until the next reboot.
def test_install_sh_restarts_the_live_daemon_when_code_or_units_change():
    """GLUE-GATED: skips until install.sh knows the live unit; once it does, the restart condition must include it."""
    src = (ROOT / "install.sh").read_text()
    if "homelab-maint-live" not in src:
        pytest.skip("glue pending: install.sh does not enable/start homelab-maint-live.service yet")
    conds = [ln for ln in src.splitlines() if "CODE_CHANGED || UNITS_CHANGED" in ln and not ln.lstrip().startswith("#")]
    assert conds, "install.sh lost its restart-on-change condition"
    assert any("live" in ln.lower() for ln in conds), f"the restart-on-change condition ignores the live daemon: {conds}"
    assert "ctl enable" in src and 'to_enable+=("$LIVE")' in src.replace("${LIVE}", "$LIVE")      # and it is enabled/started like $WWW


def test_config_changes_need_a_restart_and_that_is_documented_where_the_owner_looks():
    unit = (ROOT / "systemd" / "homelab-maint-live.service").read_text()
    assert "systemctl restart homelab-maint-live" in unit and "read once at start" in unit
    assert "systemctl restart homelab-maint-live" in live.__doc__
    assert "systemctl restart homelab-maint-live" in LEAD_TOML                                  # the [live] block the lead pastes into maint.toml


# =========================================================================== real host (read-only smoke tests)
real_host = pytest.mark.skipif(not Path("/proc/pressure/memory").exists() or not Path("/proc/diskstats").exists(),
                               reason="needs a Linux host with PSI")


@real_host
def test_real_host_proc_formats_parse(monkeypatch, env):
    for attr, p in (("PROC", "/proc"), ("CGROUP", "/sys/fs/cgroup"), ("SYS_BLOCK", "/sys/block"), ("SYS_NET", "/sys/class/net"),
                    ("HWMON", "/sys/class/hwmon")):
        monkeypatch.setattr(live, attr, Path(p))
    s = live.Sampler(live.Classes())
    s.read_host(time.monotonic(), None)
    time.sleep(0.4)
    h, pt = s.read_host(time.monotonic(), None)
    assert h["cores"] >= 1 and h["uptime_s"] > 0 and len(h["load"]) == 3
    assert h["mem"]["total"] > 10 ** 8 and 0 < h["mem"]["avail"] <= h["mem"]["total"] and h["mem"]["used"] >= 0
    assert 0 <= h["cpu_pct"] <= 100 and 0 <= h["cpu_user"] + h["cpu_sys"] <= 100
    assert all(v is not None for v in h["psi"].values())
    assert pt["mem_pct"] is not None and 0 <= pt["mem_pct"] <= 100
    assert all(0 <= r["util_pct"] <= 100 and r["read_bps"] >= 0 for r in h["io"])
    assert h["net"]["rx_bps"] is not None and h["net"]["rx_bps"] >= 0
    t = live.read_hwmon((1, 4), (2, 3, 5))                                                          # whatever this box has, no exception
    assert set(t) == {"cpu_temp", "ram_temp", "nvme_temp", "gpu_temp", "cpu_fan_rpm", "case_fan_rpm"}
    live.Activity().running()


# =========================================================================== guard rails and the unit file
def test_the_monitor_source_has_no_mutating_primitives():
    src = (ROOT / "homelab_maint" / "live.py").read_text()
    for forbidden in ("os.kill(", "killpg", "shutil.", "rmtree", "os.remove(", "os.rmdir", '"POST"', '"PUT"', '"DELETE"', '"PATCH"',
                      '"restart"', '"stop"', '"start"', '"kill"', '"update"', '"rm"', '"prune"', "/free", "keep_alive", "chmod -R"):
        assert forbidden not in src, forbidden
    # the only subprocesses it may start are the three read-only queries
    assert src.count("sh([") == 3 and "nvidia-smi" in src and '"is-active"' in src and '"ps"' in src


def parse_unit(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("#", "[")) and "=" in ln:
            k, _, v = ln.partition("=")
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_systemd_unit_matches_the_spec3_s4_contract():
    p = ROOT / "systemd" / "homelab-maint-live.service"
    u = parse_unit(p)
    one = lambda k: u[k][-1]                                                                          # noqa: E731
    assert one("Type") == "simple" and one("Restart") == "always" and one("RestartSec") == "5" and one("User") == "root"
    assert one("Nice") == "10" and one("IOSchedulingClass") == "idle" and one("MemoryMax") == "64M" and one("CPUQuota") == "10%"
    assert one("ProtectSystem") == "strict" and one("ReadWritePaths") == "/var/lib/homelab-maint"
    assert one("ProtectHome") in ("true", "yes") and one("PrivateTmp") in ("true", "yes") and one("NoNewPrivileges") in ("true", "yes")
    assert "PrivateDevices" not in u                                                                  # nvidia-smi needs /dev/nvidia*
    assert one("ExecStart") == "/usr/bin/python3 -B -m homelab_maint.live"
    assert "PYTHONPATH=/usr/local/lib/homelab-maint" in u["Environment"]
    assert one("WantedBy") == "multi-user.target"
    assert one("KillSignal") == "SIGTERM" and int(one("TimeoutStopSec")) >= 5
    assert one("IPAddressDeny") == "any" and one("IPAddressAllow") == "localhost"                      # it only talks to loopback and the docker socket
    assert "ProtectProc" not in u and "ProcSubset" not in u                                           # it reads /proc/stat, /proc/locks, ...
    assert one("UMask") == "0022"                                                                     # public/live.json must stay world-readable
    assert "MemoryDenyWriteExecute" not in u                                                          # the NVIDIA libraries may need W+X pages
    for dep in ("Wants", "Requires", "BindsTo", "Requisite", "PartOf", "Upholds"):
        assert dep not in u, dep                                                                       # starting the monitor never starts what it watches
    assert "docker.service" in u["After"][0]                                                           # ordering only


@pytest.mark.skipif(subprocess.run(["which", "systemd-analyze"], capture_output=True).returncode != 0, reason="no systemd-analyze")
def test_systemd_analyze_verify_finds_nothing_wrong_with_the_unit():
    r = subprocess.run(["systemd-analyze", "verify", str(ROOT / "systemd" / "homelab-maint-live.service")],
                       capture_output=True, text=True, timeout=30)
    mine = [ln for ln in (r.stdout + r.stderr).splitlines() if "homelab-maint-live" in ln]
    assert mine == [], mine


# =========================================================================== io_top and swap blocks (disk readers, swap holders)
def _fake_proc_with_swap(env, pswpin=0, pswpout=0, swap_free_kb=0, avail_kb=60 * 1024 * 1024, mem_full=0.0):
    p = env.proc
    (p / "meminfo").write_text(f"MemTotal: {94 * 1024 * 1024} kB\nMemAvailable: {avail_kb} kB\nSwapTotal: {32 * 1024 * 1024} kB\nSwapFree: {swap_free_kb} kB\n")
    (p / "vmstat").write_text(f"pswpin {pswpin}\npswpout {pswpout}\n")
    (p / "pressure").mkdir(exist_ok=True)
    for n in ("memory", "io", "cpu"):
        (p / "pressure" / n).write_text(f"some avg10=0.00 avg60={mem_full:.2f} avg300=0.00 total=0\nfull avg10=0.00 avg60={mem_full:.2f} avg300=0.00 total=0\n")
    (p / "uptime").write_text("100000.0 1.0\n")


def test_the_swap_block_names_holders_and_judges_by_churn_not_by_fullness(env):
    cid = "d" * 64
    d = env.cg / "system.slice" / f"docker-{cid}.scope"
    d.mkdir(parents=True)
    for f, v in (("memory.swap.current", str(3 * GIB)), ("memory.current", str(50 * 2**20)), ("memory.max", str(8 * GIB)), ("memory.high", "max"), ("cgroup.procs", "77\n")):
        (d / f).write_text(v + "\n")
    _fake_proc_with_swap(env, pswpin=1000, pswpout=1000, swap_free_kb=0)
    L = live.Live(quiet_cfg(swap=True))
    t = iter([0.0, 5.0, 10.0])
    L.swap_rates = live.swapwatch.RateReader(env.proc, clock=lambda: next(t))
    L.probes["swap"].fn = lambda: live.swapwatch.holders(live.CGROUP, names={cid: "open-notebook"}, top=5)
    L.probes["swap"].run_once()
    L.tick(wall=1000.0, mono=1.0, write=False)                                    # baseline reading of the counters
    out = json.loads(L.tick(wall=1005.0, mono=6.0, write=False))["swap"]          # 5 s later, the counters did not move
    assert out["state"] == "cold" and out["used_pct"] == 100.0 and out["in_bps"] == 0 and not out["exhausted"] and out["stale"] is False
    assert out["holders"] == [{"who": "open-notebook", "kind": "container", "swap_b": 3 * GIB, "resident_b": 50 * 2**20, "cap_b": 8 * GIB}]
    _fake_proc_with_swap(env, pswpin=1000 + 5 * 25 * 2**20 // 4096, pswpout=1000, swap_free_kb=0, mem_full=6.0)    # 25 MiB/s back in, memory stalls
    out = json.loads(L.tick(wall=1010.0, mono=11.0, write=False))["swap"]
    assert out["state"] == "thrashing" and out["in_bps"] == 25 * 2**20


def test_the_io_top_block_names_the_program_and_never_the_command_line(env):
    p = env.proc
    (p / "uptime").write_text("100000.0 1.0\n")
    d = p / "321"
    d.mkdir()
    d.joinpath("stat").write_text(f"321 (2.1.287) S 1 321 321 0 -1 0 0 0 0 0 1 1 0 0 20 0 1 0 {int((100000 - 4000) * 100)} 1 1 1\n")
    d.joinpath("cmdline").write_bytes(b"bfs\0-S\0dfs\0/\0--password=hunter2\0")
    d.joinpath("cgroup").write_text("0::/user.slice/vte-spawn-1.scope\n")
    d.joinpath("io").write_text("read_bytes: 0\nwrite_bytes: 0\n")
    L = live.Live(quiet_cfg(io_top=True))
    L.probes["io_top"].run_once()                                                  # baseline
    d.joinpath("io").write_text(f"read_bytes: {500 * 2**20}\nwrite_bytes: 0\n")
    L.probes["io_top"].run_once()
    raw = L.tick(wall=1000.0, mono=1.0, write=False)
    out = json.loads(raw)["io_top"]
    assert out["stale"] is False and out["readers"][0]["name"] == "bfs" and out["readers"][0]["orphan"] is True and out["readers"][0]["age_s"] == 4000
    assert out["readers"][0]["read_bps"] > 1_000_000
    assert b"hunter2" not in raw and b"password" not in raw, "a command line must never reach live.json"


def test_no_swap_at_all_is_reported_as_none(env):
    (env.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    out = json.loads(live.Live(quiet_cfg()).tick(wall=1.0, mono=1.0, write=False))["swap"]
    assert out["state"] == "none" and out["holders"] == []


def test_a_relief_in_progress_is_shown_as_relief_not_thrashing(env, monkeypatch):
    from homelab_maint import core as _core
    monkeypatch.setattr(_core, "STATE_DIR", env.state)
    _fake_proc_with_swap(env, pswpin=1000, pswpout=1000, swap_free_kb=2 * 1024 * 1024)
    L = live.Live(quiet_cfg())
    t = iter([0.0, 5.0, 10.0])
    L.swap_rates = live.swapwatch.RateReader(env.proc, clock=lambda: next(t))
    L.tick(wall=1000.0, mono=1.0, write=False)
    _fake_proc_with_swap(env, pswpin=1000 + 5 * 200 * 2**20 // 4096, pswpout=1000, swap_free_kb=2 * 1024 * 1024, mem_full=6.0)     # 200 MiB/s back in, stalls
    assert json.loads(L.tick(wall=1005.0, mono=6.0, write=False))["swap"]["state"] == "thrashing", "control: without a relief this is thrashing"
    live.swapwatch.relief_begin(proc=env.proc)
    out = json.loads(L.tick(wall=1010.0, mono=11.0, write=False))["swap"]
    assert out["state"] == "relief" and out["exhausted"] is False
