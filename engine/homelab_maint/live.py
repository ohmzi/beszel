"""homelab-maint live monitor: the 5-second heartbeat behind the website's Live tab (SPEC3 S4).

    python3 -m homelab_maint.live [--once] [--duration SECONDS]       (glue: `homelab-maint live ...`, see add_args/run_args)

A long-running daemon (systemd Type=simple). Every `interval_s` (5) it reads /proc and cgroup v2 counters, turns them into
rates, and atomically replaces STATE_DIR/public/live.json (0644). It keeps 720 points (60 min at 5 s) of history in memory
and persists them once a minute to STATE_DIR/live-history.json (0600), so a restart keeps the graph and shows the downtime as
a gap (null points). It only ever READS the host; it writes only those two files.

Efficiency (budget: < 1.5 % of one core, < 40 MB RSS, never blocks the tick > 2 s; measured by tests/bench_live.py on this
host, 74 containers, a full 720-point history: 0.5 - 0.9 % of one core depending on host load, 26 MiB RSS, 47 MiB worst case
with one nvidia-smi child alive, inside the unit's 64 MiB MemoryMax; live.json 34 KB; tick ~8 ms of CPU):
  * the tick itself is procfs/cgroup file reads (~1.5 ms for the whole host + 73 containers) and one json.dumps;
  * everything that forks, talks to a socket or can hang runs in a PROBE THREAD with its own cadence and timeout
    (nvidia-smi 10 s, hwmon temperatures 15 s, fans 60 s, docker 10 s, services 30 s, disk usage 30 s). The tick only reads
    each probe's last good value and flags it `stale` when it is older than three cadences, so a stuck docker never stalls
    the 5 s loop. Service probes use daemon threads, so a hung service cannot even delay a SIGTERM exit;
  * docker is queried over its unix socket (GET /containers/json, ~4 ms, no fork) instead of `docker ps` (~15 ms CPU), but ONLY
    while dockerd is demonstrably up (see `dockerd_ready`): on this host dockerd is socket-activated (docker.service is
    TriggeredBy=docker.socket, `dockerd -H fd://`), so a connect while the service is stopped or stopping would START it. The
    gate is a no-fork /proc check plus one `systemctl is-active docker.service` per docker_every_s (measured 2.1 ms of CPU, 0.02 % of a core; asking never
    activates anything); anything but "active" makes the probe fail (stale) and the services fall back to plain HTTP;
  * sensors come straight from hwmon files. Measured per probe (`bench_live.py --breakdown`): the nct6798 fan chip was the
    single dearest thing (~17 ms of kernel time per read, 0.27 % of a core at a 15 s cadence), so fans are read every 60 s
    (`fans_every_s`) while temperatures stay at 15 s. The sensor-exporter on :9110 is only a fallback (every GET makes it
    run `ps`/`nvidia-settings`, ~150 ms of work on its side), asked at most once a minute and only when hwmon lacks data.
  * reading memory.stat/cpu.stat of 74 cgroups costs ~1 ms per pass, so no "top-N first pass" is needed.

live.json (all sizes are BYTES, rates are per second; every block is present even when its data is missing, with nulls):
  {"schema":1,"generated_at","interval_s",
   "host":{"uptime_s","load":[1,5,15],"cores","cpu_pct","cpu_user","cpu_sys","cpu_iowait",
           "mem":{"total","used","avail","cache","swap_used","swap_total"},
           "psi":{"mem_some60","mem_full60","io_some60","io_full60","cpu_some60"},          (kernel avg60, percent)
           "disk":[{"mount","free_b","size_b","used_pct"}],                                 (watch mounts only)
           "io":[{"dev","read_bps","write_bps","util_pct"}],                                (physical block devices)
           "net":{"rx_bps","tx_bps"}},                                                      (physical interfaces only)
   "gpu":{"util","mem_used","mem_total","temp","power_w","fan_pct","stale"},
   "sensors":{"cpu_temp","gpu_temp","ram_temp","nvme_temp","cpu_fan_rpm","case_fan_rpm","stale"},
   "containers":{"running","unhealthy":[names],"top_cpu":[{"name","class","cpu_pct","mem_gib"}x8],"top_mem":[..x8],"stale"},
   "services":[{"name","state":"up|down|degraded","detail","ms","stale"}],
   "activity":{"maintenance_running":["daily","backup-system"..],"check_running":bool,"last_action":{"ts","task","action"}|null},
                                                       (tier locks held + scheduler jobs running >= 10 s; never `check`/`tick`)
   "pressure":{"level":0-5|null,"gate_level":0-5|null,"level_name","why","age_s"},   # gate_level: memory/cpu only (io/gpu-only: 0)
   "history":{"t0","step_s":5,"units":{..},"cpu","mem_pct","psi_mem","psi_io","gpu","net_rx","net_tx","disk_r","disk_w",
              "swap_pct","vram_pct","load1"},
   "io_top":{"readers":[{"pid","name","container"|null,"orphan","age_s","read_bps","write_bps"}x3],"window_s","stale"},
                                                       (processes reading/writing the disks right now, from /proc/PID/io deltas; program name only, NEVER a command line)
   "swap":{"used_b","total_b","used_pct","state":"none|idle|cold|active|thrashing|relief","in_bps","out_bps","exhausted",
           "holders":[{"who","kind","swap_b","resident_b","cap_b"|null}x5],"stale"},
                                                       (holders from cgroup memory.swap.current; state from swap-in/out rates + memory PSI, not from how full it is)
   "probes":{"<name>":{"age_s","stale"}},"self":{"pid","started_at","ticks","errors","tick_ms","rss_mb","threads"}}
Notes: `mem_gib` is a container's anonymous (non-cache) memory, the same basis as the guard samples; `cpu_pct` is percent of
ONE core (can exceed 100). History arrays are 720 points, oldest first, the newest point at t0 + (len-1)*step_s, null where the
daemon was not running; psi_* are the share of each interval the kernel counted tasks as stalled ("some", from the `total=`
counter, not the smoothed avg60); net_*/disk_* are MiB/s with one decimal; swap_pct/vram_pct are whole percents (null with no swap, no GPU
reading or a stale one); load1 is the 1-minute load average. The file is kept under 52 KB (it is ~48 KB with a full history and the io_top and swap blocks) by dropping the
oldest history points if it ever grows past that.

Config ([live] in maint.toml) is read ONCE at start: after editing it run `systemctl restart homelab-maint-live` (safe: SIGTERM
persists the history in ~20 ms and the graph shows a gap of about one restart, ~5 s).
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import select
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import core, iotop, swapwatch
from .core import load_toml, sh

# Paths are module globals read at call time, so tests can point them at fake trees / tmp dirs.
STATE_DIR, LOG_DIR, RUN_DIR, CONF_DIR = core.STATE_DIR, core.LOG_DIR, core.RUN_DIR, core.CONF_DIR
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
SYS_BLOCK = Path("/sys/block")
SYS_NET = Path("/sys/class/net")
HWMON = Path("/sys/class/hwmon")
DOCKER_SOCK = "/var/run/docker.sock"
DOCKER_PIDFILE = Path("/run/docker.pid")        # dockerd's own pidfile; READ only (a signal to it would be a mutation)
DOCKER_UNIT = "docker.service"
EXPORTER = ("127.0.0.1", 9110)
NVIDIA_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed"

POINTS = 720                     # history length
FILE_CAP = 52_000                # live.json size budget (bytes): ~35 KB with 9 series, ~48 KB with the 12 of today, the io_top and swap blocks and a full history
PERSIST_S = 60.0                 # history persistence cadence
MIB = 1024 ** 2
GIB = 1024 ** 3
SERIES = ("cpu", "mem_pct", "psi_mem", "psi_io", "gpu", "net_rx", "net_tx", "disk_r", "disk_w", "swap_pct", "vram_pct", "load1")
LATER_SERIES = ("swap_pct", "vram_pct", "load1")     # added after the first release: a history dump written before them still loads (they start empty)
UNITS = {"cpu": "%", "mem_pct": "%", "psi_mem": "%", "psi_io": "%", "gpu": "%",
         "net_rx": "MiB/s", "net_tx": "MiB/s", "disk_r": "MiB/s", "disk_w": "MiB/s", "swap_pct": "%", "vram_pct": "%", "load1": ""}
UNKNOWN: Any = object()          # "docker state is not trustworthy right now" (distinct from "no such container")

# The real services on this host (ports and endpoints verified read-only). Override with [live] services = [...].
DEFAULT_SERVICES = (
    {"name": "Plex", "unit": "snap.plexmediaserver.plexmediaserver.service", "url": "http://127.0.0.1:32400/identity"},
    {"name": "Immich", "container": "immich_server", "url": "http://127.0.0.1:2283/api/server/ping"},
    {"name": "Kavita", "container": "kavita", "url": "http://127.0.0.1:5000/api/health"},
    {"name": "Seerr", "container": "Seerr", "url": "http://127.0.0.1:5056/api/v1/status"},
    {"name": "Open WebUI", "container": "open-webui", "url": "http://127.0.0.1:4567/health"},
    {"name": "Homarr", "container": "homarr", "url": "http://127.0.0.1:7575/"},
    {"name": "Uptime Kuma", "container": "uptime-kuma", "url": "http://127.0.0.1:3011/api/entry-page"},
    {"name": "Nextcloud", "container": "nextcloud", "url": "http://127.0.0.1:8090/status.php"},
    {"name": "Tunarr", "container": "tunarr-host-net", "url": "http://127.0.0.1:8000/"},      # answers 302
    {"name": "Radarr", "container": "Radarr", "url": "http://127.0.0.1:7878/ping"},
    {"name": "Sonarr", "container": "Sonarr", "url": "http://127.0.0.1:8989/ping"},
)

_WARNED: dict[str, float] = {}


def _warn(key: str, msg: str, every: float = 300.0) -> None:
    """stderr (journald) line, at most one per `key` per `every` seconds: a broken probe must not flood the journal."""
    now = time.monotonic()
    if now - _WARNED.get(key, -1e9) >= every:
        _WARNED[key] = now
        print(f"homelab-maint-live: {msg}", file=sys.stderr, flush=True)


def _read(p: Path | str) -> str | None:
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _num(v: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    """A finite float inside [lo, hi] or None. Bool and strings are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return float(v)


def _ascii(s: Any, n: int = 80) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def _pt(x: Any) -> int | float | None:
    """A history point read back from disk: a sane finite number (ints stay ints) or None."""
    if isinstance(x, int) and not isinstance(x, bool):
        return x if abs(x) < 10 ** 12 else None
    return _num(x, -1e12, 1e12)


def _stat_sig(p: Path) -> tuple | None:
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# =========================================================================== parsers (pure; fixtures in tests)
def parse_cpu(text: str | None) -> tuple[int, ...] | None:
    """First line of /proc/stat -> (user, nice, system, idle, iowait, irq, softirq, steal) jiffies, or None.
    guest/guest_nice are already counted inside user/nice, so they are not added again."""
    first = (text or "").split("\n", 1)[0].split()
    if not first or first[0] != "cpu":
        return None
    try:
        v = [int(x) for x in first[1:9]]
    except ValueError:
        return None
    if len(v) < 5:
        return None
    return tuple(v + [0] * (8 - len(v)))


def cpu_pcts(prev: tuple, cur: tuple) -> dict[str, float] | None:
    """Busy/user/sys/iowait as percent of ALL cores' time between two readings. iowait is idle time, not busy (like top)."""
    d = [c - p for p, c in zip(prev, cur)]
    tot = sum(d)
    if tot <= 0 or any(x < 0 for x in d):          # no time passed, or counters reset (reboot / bad data)
        return None
    pct = lambda x: round(100.0 * x / tot, 1)       # noqa: E731
    return {"cpu_pct": pct(tot - d[3] - d[4]), "cpu_user": pct(d[0] + d[1]), "cpu_sys": pct(d[2] + d[5] + d[6]),
            "cpu_iowait": pct(d[4])}


def parse_meminfo(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in (text or "").splitlines():
        k, _, v = ln.partition(":")
        if k in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SReclaimable", "SwapTotal", "SwapFree"):
            try:
                out[k] = int(v.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    return out


def parse_psi(text: str | None) -> dict[str, dict[str, float]]:
    """'some avg10=0.00 avg60=0.01 avg300=0.00 total=123' -> {"some": {"avg60": .01, "total": 123.0}, "full": {...}}."""
    out: dict[str, dict[str, float]] = {}
    for ln in (text or "").splitlines():
        parts = ln.split()
        if len(parts) > 1 and parts[0] in ("some", "full"):
            d: dict[str, float] = {}
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                try:
                    d[k] = float(v)
                except ValueError:
                    pass
            out[parts[0]] = d
    return out


def parse_loadavg(text: str | None) -> list[float] | None:
    f = (text or "").split()
    try:
        return [float(x) for x in f[:3]] if len(f) >= 3 else None
    except ValueError:
        return None


def parse_diskstats(text: str | None, is_disk: Callable[[str], bool]) -> dict[str, tuple[int, int, int]]:
    """{dev: (sectors_read, sectors_written, ms_doing_io)} for whole physical disks. Sectors are always 512 bytes."""
    out: dict[str, tuple[int, int, int]] = {}
    for ln in (text or "").splitlines():
        f = ln.split()
        if len(f) >= 13 and is_disk(f[2]):
            try:
                out[f[2]] = (int(f[5]), int(f[9]), int(f[12]))
            except ValueError:
                pass
    return out


def parse_netdev(text: str | None, is_phys: Callable[[str], bool]) -> dict[str, tuple[int, int]]:
    """{iface: (rx_bytes, tx_bytes)} for physical interfaces."""
    out: dict[str, tuple[int, int]] = {}
    for ln in (text or "").splitlines()[2:]:
        name, _, rest = ln.partition(":")
        name, f = name.strip(), rest.split()
        if name and len(f) >= 9 and is_phys(name):
            try:
                out[name] = (int(f[0]), int(f[8]))
            except ValueError:
                pass
    return out


def parse_flock_keys(text: str | None) -> set[str]:
    """'maj:min:inode' of every file that currently holds a FLOCK (the runner's tier locks are flock()s).
    Blocked waiters ('->' lines) do not hold anything. Same key format as `_lock_key`."""
    held: set[str] = set()
    for ln in (text or "").splitlines():
        t = ln.split()
        if len(t) >= 6 and t[1] == "FLOCK" and re.fullmatch(r"[0-9a-f]+:[0-9a-f]+:\d+", t[5]):
            held.add(t[5])
    return held


def _lock_key(p: Path) -> str | None:
    try:
        st = os.stat(p)
    except OSError:
        return None
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def parse_mounts(text: str | None) -> set[str]:
    """Mount points from /proc/self/mountinfo (field 5, with \\040-style octal escapes decoded)."""
    out: set[str] = set()
    for ln in (text or "").splitlines():
        f = ln.split()
        if len(f) > 4:
            out.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[4]))
    return out


# =========================================================================== configuration
@dataclass
class Cfg:
    interval: float = 5.0
    gpu: bool = True
    sensors: bool = True
    docker: bool = True
    disk: bool = True
    io_top: bool = True
    swap: bool = True
    gpu_every: float = 10.0
    sensors_every: float = 15.0
    fans_every: float = 60.0          # the fan chip (Super-I/O) is the dearest sensor read: ~17 ms of kernel time each
    docker_every: float = 10.0
    services_every: float = 30.0
    disk_every: float = 30.0
    io_top_every: float = 10.0       # /proc/PID/io of every process: ~700 tiny reads, ~10 ms
    swap_every: float = 30.0         # cgroup walk for memory.swap.current
    exporter_every: float = 60.0
    http_timeout: float = 5.0
    docker_timeout: float = 3.0
    slow_ms: int = 2000
    cpu_fans: tuple = (1, 4)          # nct6798 headers, same grouping as sensor-exporter
    case_fans: tuple = (2, 3, 5)
    mounts: list = field(default_factory=lambda: ["/"])
    services: list = field(default_factory=list)


def _loopback_addr(url: str) -> tuple[str, int, str] | None:
    """(host, port, path) of an http URL that points at this machine, else None. Probes never leave the host."""
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        if u.scheme != "http" or host not in ("127.0.0.1", "localhost", "::1"):
            return None
        return host, u.port or 80, (u.path or "/") + (f"?{u.query}" if u.query else "")
    except ValueError:
        return None


def clean_service(d: Any) -> dict | None:
    """Validated service spec or None. A bad URL keeps the entry (it will read "degraded: bad url") instead of hiding it."""
    if not isinstance(d, dict):
        return None
    name = _ascii(str(d.get("name", "")).strip(), 32)
    container = _ascii(str(d.get("container", "")).strip(), 64)
    unit = _ascii(str(d.get("unit", "")).strip(), 96)
    url = str(d.get("url", "")).strip()
    if not name or not (container or unit or url):
        return None
    addr = _loopback_addr(url) if url else None
    ex = d.get("expect") if isinstance(d.get("expect"), list) else []
    expect = [x for x in ex if isinstance(x, int) and not isinstance(x, bool) and 100 <= x <= 599]
    return {"name": name, "container": container, "unit": unit, "addr": addr, "bad_url": bool(url) and addr is None,
            "expect": expect, "slow_ms": _num(d.get("slow_ms"), 50, 60000)}


def load_cfg(raw: dict | None = None) -> Cfg:
    """Config from [live] in maint.toml (+ [tasks.disk_forecast].watch for the disk list). Anything malformed falls back to
    the default; a missing file is fine (the defaults describe this host)."""
    try:
        raw = raw if raw is not None else load_toml(CONF_DIR / "maint.toml")
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop the monitor
        _warn("cfg", f"config unreadable ({type(exc).__name__}); using defaults")
        raw = {}
    live = raw.get("live", {}) if isinstance(raw.get("live"), dict) else {}

    def num(key: str, default: float, lo: float, hi: float) -> float:
        v = _num(live.get(key), lo, hi)
        return default if v is None else v

    c = Cfg()
    c.interval = num("interval_s", c.interval, 1, 60)
    for key, attr in (("gpu_every_s", "gpu_every"), ("sensors_every_s", "sensors_every"), ("fans_every_s", "fans_every"), ("docker_every_s", "docker_every"),
                      ("services_every_s", "services_every"), ("disk_every_s", "disk_every"),
                      ("io_top_every_s", "io_top_every"), ("swap_every_s", "swap_every")):
        setattr(c, attr, num(key, getattr(c, attr), 2, 3600))
    c.http_timeout = num("http_timeout_s", c.http_timeout, 0.5, 15)
    c.slow_ms = int(num("slow_ms", c.slow_ms, 50, 60000))
    for flag in ("gpu", "sensors", "docker", "disk", "io_top", "swap"):
        if isinstance(live.get(flag), bool):
            setattr(c, flag, live[flag])
    for key, attr in (("cpu_fans", "cpu_fans"), ("case_fans", "case_fans")):
        v = live.get(key)
        if isinstance(v, list) and all(isinstance(x, int) and 1 <= x <= 16 for x in v):
            setattr(c, attr, tuple(v))
    watch = raw.get("tasks", {}).get("disk_forecast", {}).get("watch") if isinstance(raw.get("tasks"), dict) else None
    mounts = live.get("mounts", watch)
    if isinstance(mounts, list) and all(isinstance(m, str) for m in mounts):
        c.mounts = [m for m in mounts if m.startswith("/")][:24]
    specs = live.get("services") if isinstance(live.get("services"), list) else list(DEFAULT_SERVICES)
    c.services = [s for s in (clean_service(x) for x in specs) if s][:40]
    return c


class Classes:
    """Container name -> P0..P3 from CONF_DIR/classes.toml (`[classes] P0 = [regex, ...]`). Most protective match wins;
    unknown names are P2 (batch), as SPEC3 says. Reloaded when the file changes (checked once a minute)."""
    ORDER = ("P0", "P1", "P2", "P3")

    def __init__(self) -> None:
        self.rules: list[tuple[str, list[re.Pattern]]] = []
        self.cache: dict[str, str] = {}
        self.sig: Any = "unset"
        self.checked = -1e9

    def refresh(self, mono: float) -> None:
        if mono - self.checked < 60:
            return
        self.checked = mono
        p = CONF_DIR / "classes.toml"
        sig = _stat_sig(p)
        if sig == self.sig:
            return
        self.sig, self.cache, self.rules = sig, {}, []
        if sig is None:
            return
        try:
            cls = load_toml(p).get("classes", {})
        except Exception as exc:  # noqa: BLE001
            _warn("classes", f"classes.toml unreadable ({type(exc).__name__}); everything is P2")
            return
        for c in self.ORDER:
            rx = []
            for pat in cls.get(c, []) if isinstance(cls, dict) and isinstance(cls.get(c, []), list) else []:
                try:
                    rx.append(re.compile(str(pat), re.I))
                except re.error:
                    pass
            self.rules.append((c, rx))

    def of(self, name: str) -> str:
        c = self.cache.get(name)
        if c is None:
            c = next((k for k, rx in self.rules if any(r.search(name) for r in rx)), "P2")
            self.cache[name] = c
        return c


# =========================================================================== probes (slow things, run off the tick)
class Probe:
    """One slow data source with its own cadence. `run_once()` calls fn and keeps the LAST GOOD value; `get()` returns
    (value, stale). A failure (exception) keeps the old value and counts as no refresh; after 3 cadences without a success the
    value is flagged stale. The tick never waits for a probe."""

    def __init__(self, name: str, fn: Callable[[], Any], every: float, clock: Callable[[], float] = time.monotonic):
        self.name, self.fn, self.every, self.clock = name, fn, every, clock
        self.stale_after = max(3 * every, 15.0)
        self._v: tuple[Any, float | None] = (None, None)      # replaced as a unit: readers never see a torn pair
        self.fails = 0
        self.first = threading.Event()                        # set after the first attempt, success or not
        self.after: list[Probe] = []                          # probes whose first answer this one waits for (bounded)

    def run_once(self) -> None:
        try:
            self._v = (self.fn(), self.clock())
            self.fails = 0
        except Exception as exc:  # noqa: BLE001 - a probe failing is data (stale), not an error
            self.fails += 1
            _warn(f"probe-{self.name}", f"probe {self.name} failed: {type(exc).__name__}: {_ascii(exc, 100)}")
        finally:
            self.first.set()

    def get(self) -> tuple[Any, bool]:
        v, at = self._v
        return v, at is None or self.clock() - at > self.stale_after

    def age(self) -> float | None:
        at = self._v[1]
        return None if at is None else round(self.clock() - at, 1)

    def start(self, stop: Any) -> threading.Thread:
        def loop() -> None:
            for dep in self.after:                            # e.g. services wait for docker, so the first file has both
                dep.first.wait(3.0)
            while not stop.is_set():
                self.run_once()
                if stop.wait(self.every):
                    break
        t = threading.Thread(target=loop, name=f"probe-{self.name}", daemon=True)
        t.start()
        return t


def _f(s: str) -> float | None:
    try:
        return _num(float(s.strip()))
    except ValueError:
        return None                                   # "[N/A]", "N/A", ""


def parse_gpu(text: str) -> dict:
    """First GPU line of nvidia-smi csv (util %, mem used MiB, mem total MiB, temp C, power W, fan %) -> live.json gpu block."""
    line = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    v = [_f(x) for x in (line.split(",") + [""] * 6)[:6]]
    if all(x is None for x in v):
        raise ValueError("nvidia-smi gave no usable values")
    util, used, total, temp, power, fan = v
    mb = lambda x: None if x is None else int(x * MIB)   # noqa: E731
    return {"util": None if util is None else int(round(util)), "mem_used": mb(used), "mem_total": mb(total),
            "temp": None if temp is None else int(round(temp)), "power_w": _r(power), "fan_pct": None if fan is None else int(round(fan))}


def read_gpu() -> dict:
    r = sh(["nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"], timeout=4)
    if r.returncode != 0:                              # 127 = not installed, 124 = hung, else driver trouble
        raise RuntimeError(f"nvidia-smi rc={r.returncode}")
    return parse_gpu(r.stdout)


def _milli(p: Path) -> float | None:
    s = _read(p)
    try:
        return int(s) / 1000 if s else None
    except ValueError:
        return None


def read_hwmon(cpu_fans: tuple, case_fans: tuple, fans: bool = True) -> dict:
    """Temperatures and fan speeds from sysfs hwmon, no forks. The whole tree (~25 chips) is rescanned every call: it costs
    well under a millisecond and survives hwmonN renumbering. Same sources as metrics_ring: coretemp 'Package id 0', DIMM
    spd5118 mean, nvme 'Composite' (hottest), nct67xx tachs (mean of the fans that turn). `fans=False` skips the Super-I/O
    chip (fan values come back None): the first fan read after its 1.5 s driver cache expires makes the kernel re-read the whole
    chip (measured ~17 ms CPU on this board), by far the dearest thing the monitor does, and fans change slowly."""
    cpu = nvme = None
    dimm: list[float] = []
    nvmes: list[float] = []
    rpm: dict[int, int] = {}
    try:
        chips = sorted(HWMON.glob("hwmon*"))
    except OSError:
        chips = []
    for d in chips:
        name = (_read(d / "name") or "").strip()
        if name == "coretemp" and cpu is None:
            for lp in d.glob("temp*_label"):
                if (_read(lp) or "").strip() == "Package id 0":
                    cpu = _milli(lp.with_name(lp.name.replace("_label", "_input")))
                    break
        elif name == "spd5118":
            v = _milli(d / "temp1_input")
            if v is not None:
                dimm.append(v)
        elif name.startswith("nvme"):
            comp = next((lp for lp in d.glob("temp*_label") if (_read(lp) or "").strip() == "Composite"), None)
            v = _milli(comp.with_name(comp.name.replace("_label", "_input"))) if comp else _milli(d / "temp1_input")
            if v is not None:
                nvmes.append(v)
        elif fans and name.startswith("nct67") and not rpm:
            for i in range(1, 8):
                s = (_read(d / f"fan{i}_input") or "").strip()
                if s.isdigit():
                    rpm[i] = int(s)

    def mean_turning(idx: tuple) -> float | None:
        if not rpm:
            return None                                # no fan chip at all: unknown, not zero
        v = [rpm[i] for i in idx if rpm.get(i)]
        return sum(v) / len(v) if v else 0.0

    def temp(x: float | None) -> float | None:
        return _r(_num(x, -40, 150))
    return {"cpu_temp": temp(cpu), "ram_temp": temp(sum(dimm) / len(dimm) if dimm else None),
            "nvme_temp": temp(max(nvmes) if nvmes else None), "gpu_temp": None,
            "cpu_fan_rpm": _fan(mean_turning(cpu_fans)), "case_fan_rpm": _fan(mean_turning(case_fans))}


def _fan(v: float | None) -> int | None:
    v = _num(v, 0, 30000)
    return None if v is None else int(round(v))


def read_exporter(timeout: float = 2.0) -> dict:
    """GET the sensor-exporter (fallback only; see module docstring). Raises on any failure."""
    c = http.client.HTTPConnection(EXPORTER[0], EXPORTER[1], timeout=timeout)
    try:
        c.request("GET", "/")
        r = c.getresponse()
        d = json.loads(r.read(65536)) if r.status == 200 else {}
    finally:
        c.close()
    if not isinstance(d, dict):
        raise ValueError("exporter returned a non-object")
    return d


def make_sensors_reader(cfg: Cfg, clock: Callable[[], float] = time.monotonic) -> Callable[[], dict]:
    """Temperatures every call; fan speeds only every `fans_every` s (the last reading is reused in between); the
    sensor-exporter only as a rare fallback for whatever hwmon cannot give."""
    cache: dict[str, Any] = {"t": -1e9, "d": {}}
    fan: dict[str, Any] = {"t": -1e9, "v": (None, None)}
    fills = {"cpu_temp": "cpu_temp", "nvme_temp": "nvme_temp", "cpu_fan_rpm": "cpu_fan", "case_fan_rpm": "case_fan"}

    def read() -> dict:
        now = clock()
        due = now - fan["t"] >= cfg.fans_every
        out = read_hwmon(cfg.cpu_fans, cfg.case_fans, fans=due)
        if due:
            fan["t"], fan["v"] = now, (out["cpu_fan_rpm"], out["case_fan_rpm"])
        else:
            out["cpu_fan_rpm"], out["case_fan_rpm"] = fan["v"]
        if any(out[k] is None for k in fills):
            if now - cache["t"] >= cfg.exporter_every:           # the exporter is expensive: rarely, and only if needed
                cache["t"] = now
                try:
                    cache["d"] = read_exporter()
                except Exception as exc:  # noqa: BLE001 - exporter down: hwmon values stand, the rest stay null
                    cache["d"] = {}
                    _warn("exporter", f"sensor-exporter unavailable ({type(exc).__name__})")
            ex = cache["d"]
            for k, ek in fills.items():
                if out[k] is None:
                    v = _num(ex.get(ek), -40, 30000)
                    out[k] = None if v is None else (_r(v) if "temp" in k else int(round(v)))
            out["gpu_temp"] = _r(_num(ex.get("gpu_temp"), -40, 150))
        if all(v is None for v in out.values()):
            raise RuntimeError("no sensor data from hwmon or the exporter")
        return out
    return read


class _UnixConn(http.client.HTTPConnection):
    """http.client over the docker unix socket."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


_CID = re.compile(r"[0-9a-f]{12,64}")
_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")


def norm_container(c: Any) -> dict | None:
    """One row of GET /containers/json (or of the CLI fallback, already shaped alike) -> {name,id,state,health}."""
    try:
        names = c.get("Names") or []
        name = str(names[0] if names else "").lstrip("/")
        cid = str(c["Id"])
        state = str(c.get("State", "")).lower()
        m = _HEALTH.search(str(c.get("Status", "")))
    except (AttributeError, KeyError, IndexError, TypeError):
        return None
    if not name or not _CID.fullmatch(cid):
        return None
    health = {"healthy": "healthy", "unhealthy": "unhealthy", "health: starting": "starting"}[m.group(1)] if m else None
    return {"name": _ascii(name, 64), "id": cid, "state": state, "health": health}


def docker_list_api(timeout: float) -> list:
    c = _UnixConn(DOCKER_SOCK, timeout)
    try:
        c.request("GET", "/containers/json?all=1", headers={"Host": "docker", "Connection": "close"})
        r = c.getresponse()
        if r.status != 200:
            raise RuntimeError(f"docker api HTTP {r.status}")
        data = json.loads(r.read(16 * MIB))
    finally:
        c.close()
    if not isinstance(data, list):
        raise ValueError("docker api returned a non-list")
    return data


def docker_list_cli() -> list:
    r = sh(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}|{{.Names}}|{{.State}}|{{.Status}}"], timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps rc={r.returncode}")
    rows = []
    for ln in r.stdout.splitlines():
        f = (ln.split("|", 3) + ["", "", ""])[:4]
        rows.append({"Id": f[0], "Names": [f[1].split(",")[0]], "State": f[2], "Status": f[3]})
    return rows


def cg_dir(cid: str) -> Path | None:
    for rel in (f"system.slice/docker-{cid}.scope", f"docker/{cid}"):     # systemd cgroup driver first, then cgroupfs
        p = CGROUP / rel
        if p.is_dir():
            return p
    return None


class DockerDown(RuntimeError):
    """dockerd is not running (or is stopping/starting): the probe must NOT touch the socket. The probe just fails, so the
    container data goes stale and the services fall back to plain HTTP."""


def unit_state(unit: str) -> str:
    """`systemctl is-active` word: active | inactive | deactivating | activating | failed | ... | "unknown" when systemctl is
    missing or hung (rc 127 / 124 and empty output). Read-only; asking never activates a socket-activated service."""
    return sh(["systemctl", "is-active", unit], timeout=3).stdout.strip() or "unknown"


def _is_dockerd(pid: int) -> bool:
    return (_read(PROC / str(pid) / "comm") or "").strip() == "dockerd"


def dockerd_process_alive() -> bool:
    """A running dockerd process, found without forking and without touching the socket: the pidfile first (the process
    must be named `dockerd`, so a stale file with a recycled pid proves nothing), then docker.service's cgroup."""
    txt = (_read(DOCKER_PIDFILE) or "").strip()
    if txt.isdigit() and len(txt) <= 10 and int(txt) > 0 and _is_dockerd(int(txt)):
        return True
    procs = (_read(CGROUP / "system.slice" / DOCKER_UNIT / "cgroup.procs") or "").split()
    return any(p.isdigit() and len(p) <= 10 and _is_dockerd(int(p)) for p in procs[:64])


def dockerd_ready() -> None:
    """Return only when it is safe to connect to the docker socket, else raise DockerDown. Docker here is socket-activated
    (docker.service is TriggeredBy=docker.socket, `dockerd -H fd://`; the socket stays active when only the service is stopped),
    so ANY connect while the service is stopped, and also while it is still stopping, makes systemd (re)start dockerd and all the
    restart=always containers in the middle of the owner's maintenance. Two independent checks, both must pass:
      1. a live `dockerd` process (no fork), which already covers a cleanly stopped service, and
      2. `systemctl is-active docker.service` == "active": dockerd only removes its pidfile AFTER it has stopped every container,
         so during `systemctl stop docker` (seconds to a minute with 70+ containers) the process is still there while a connect
         would queue on the socket and bring the service back as soon as the stop finishes.
    Anything else (inactive, deactivating, activating, failed, unknown, systemctl missing or hung) fails closed. What is left is
    a window of a few milliseconds between this check and the connect."""
    if not dockerd_process_alive():
        raise DockerDown("dockerd process not found")
    state = unit_state(DOCKER_UNIT)
    if state != "active":
        raise DockerDown(f"{DOCKER_UNIT} is {_ascii(state, 20)}")


def read_docker(timeout: float = 3.0) -> list[dict]:
    """Every container with its state and, if running, its cgroup dir. Socket first; the CLI only when the socket itself is
    unavailable (a TIMEOUT is not retried through the CLI: hammering a struggling daemon twice helps nobody). Neither is ever
    tried while dockerd is down (`dockerd_ready`): the CLI would connect to the very same activating socket."""
    dockerd_ready()
    try:
        raw = docker_list_api(timeout)
    except (FileNotFoundError, PermissionError, ConnectionRefusedError):
        if not dockerd_process_alive():                       # it exited since the check above: the CLI would wake it too
            raise DockerDown("dockerd process not found") from None
        raw = docker_list_cli()
    rows = [r for r in (norm_container(c) for c in raw) if r]
    for r in rows:
        r["cg"] = cg_dir(r["id"]) if r["state"] == "running" else None
    return rows


def http_get(host: str, port: int, path: str, timeout: float) -> tuple[int | None, int, str]:
    """(status, ms, error). Loopback only (callers validate), no redirects, reads at most 512 bytes, never sends credentials."""
    t0 = time.perf_counter()
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    ms = lambda: int((time.perf_counter() - t0) * 1000)   # noqa: E731
    try:
        c.request("GET", path, headers={"User-Agent": "homelab-maint-live", "Connection": "close", "Accept": "*/*"})
        r = c.getresponse()
        r.read(512)
        return r.status, ms(), ""
    except (socket.timeout, TimeoutError):
        return None, ms(), "timeout"
    except ConnectionRefusedError:
        return None, ms(), "refused"
    except (OSError, http.client.HTTPException):
        return None, ms(), "error"
    finally:
        c.close()


def service_state(spec: dict, ct: Any, http_res: tuple | None, unit: str | None, slow_ms: float) -> tuple[str, str, int | None]:
    """(state, detail, ms) for one service.
    down     = the user cannot use it: container missing/stopped, unit inactive, port not answering, HTTP 5xx.
    degraded = answering but not right: unhealthy/starting container, unexpected HTTP code, slow answer, bad URL in config.
    ct: docker row | None (docker answered: no such container) | UNKNOWN (docker data not trusted: HTTP alone decides)."""
    notes: list[str] = []
    state = "up"
    if spec["container"]:
        if ct is UNKNOWN:
            notes.append("docker state unknown")
        elif ct is None:
            return "down", "container missing", None
        elif ct["state"] != "running":
            return "down", f"container {_ascii(ct['state'] or 'stopped', 20)}", None
        elif ct["health"] == "unhealthy":
            state = "degraded"
            notes.append("unhealthy")
        elif ct["health"] == "starting":
            state = "degraded"
            notes.append("starting")
        elif ct["health"] == "healthy":
            notes.append("healthy")
    if spec.get("bad_url"):
        return "degraded", "bad url in config", None
    ms = None
    if http_res is not None:
        code, ms, err = http_res
        if err:
            if unit not in (None, "active"):
                return "down", f"unit {_ascii(unit, 20)}", None
            return "down", err, None
        if code >= 500:
            return "down", f"HTTP {code}", ms
        expected = code in spec["expect"] if spec["expect"] else 200 <= code < 400
        if not expected:
            state = "degraded"
            notes.append(f"HTTP {code}")
        elif ms > (spec.get("slow_ms") or slow_ms):
            state = "degraded"
            notes.append(f"slow {ms} ms")
        else:
            notes.append(f"{ms} ms")
    elif spec["unit"]:
        if unit != "active":
            return "down", f"unit {_ascii(unit or 'unknown', 20)}", None
        notes.append("unit active")
    return state, ", ".join(notes) or "ok", ms


def make_services_reader(cfg: Cfg, docker_get: Callable[[], tuple[Any, bool]], grace: float = 6.0) -> Callable[[], list[dict]]:
    """One pass over the configured services. At most 8 DAEMON worker threads (so a hung service neither holds up the others
    nor delays a SIGTERM exit); the whole pass is bounded by http_timeout + `grace` (6) s and a service that has not answered by then
    reads "degraded: probe timed out" instead of blocking the list."""

    def one(spec: dict, by_name: dict | None) -> dict:
        ct: Any = UNKNOWN if by_name is None else by_name.get(spec["container"]) if spec["container"] else None
        res = http_get(*spec["addr"], cfg.http_timeout) if spec["addr"] else None
        unit = None
        if spec["unit"] and (res is None or res[2]):                        # only ask systemd when HTTP failed or is absent
            unit = unit_state(spec["unit"])
        state, detail, ms = service_state(spec, ct, res, unit, cfg.slow_ms)
        return {"name": spec["name"], "state": state, "detail": detail, "ms": ms}

    def read() -> list[dict]:
        dk, stale = docker_get()
        by_name = None if dk is None or stale else {c["name"]: c for c in dk}
        specs = cfg.services
        out: list[dict | None] = [None] * len(specs)
        todo, lock = iter(range(len(specs))), threading.Lock()

        def worker() -> None:
            while True:
                with lock:
                    i = next(todo, None)
                if i is None:
                    return
                try:
                    out[i] = one(specs[i], by_name)
                except Exception as exc:  # noqa: BLE001 - one broken service must not blank the whole list
                    _warn(f"svc-{specs[i]['name']}", f"service {specs[i]['name']}: {type(exc).__name__}: {_ascii(exc, 80)}")
                    out[i] = {"name": specs[i]["name"], "state": "degraded", "detail": "probe error", "ms": None}

        threads = [threading.Thread(target=worker, name=f"svc-{n}", daemon=True) for n in range(min(8, len(specs)))]
        for t in threads:
            t.start()
        end = time.monotonic() + cfg.http_timeout + grace
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        return [o or {"name": sp["name"], "state": "degraded", "detail": "probe timed out", "ms": None}
                for o, sp in zip(out, specs)]
    return read


def read_disks(mounts: list[str], statvfs: Callable = os.statvfs) -> list[dict]:
    """Usage of the configured (watch) mounts that are actually mounted, df-style (used / (used + available))."""
    mounted = parse_mounts(_read(PROC / "self" / "mountinfo"))
    out = []
    for m in mounts:
        if m not in mounted:
            continue
        try:
            s = statvfs(m)
        except OSError:
            continue
        size, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
        used = (s.f_blocks - s.f_bfree) * s.f_frsize
        if size > 0:
            out.append({"mount": _ascii(m, 96), "free_b": int(free), "size_b": int(size),
                        "used_pct": round(100.0 * used / (used + free), 1) if used + free else 0.0})
    return out


# =========================================================================== history ring
class History:
    """720 points of 12 series on a fixed `step` grid. Missing time (daemon down, long stall) becomes null points, so the
    arrays always map linearly onto time: point i is at t0 + i*step."""

    def __init__(self, step: float = 5.0, points: int = POINTS):
        self.step, self.n = float(step), points
        self.s: dict[str, deque] = {k: deque(maxlen=points) for k in SERIES}
        self.t_last: float | None = None                # grid time of the newest point

    def clear(self) -> None:
        for d in self.s.values():
            d.clear()
        self.t_last = None

    def push(self, t: float, vals: dict) -> None:
        if self.t_last is not None:
            dt = t - self.t_last
            if dt < -2 * self.step:                      # the clock stepped back: the old points are on another timeline
                self.clear()
            else:
                gap = max(round(dt / self.step), 1)       # jitter (+-half a step) still means "next point"
                if gap > self.n:
                    self.clear()
                else:
                    for _ in range(gap - 1):
                        for d in self.s.values():
                            d.append(None)
                    self.t_last += gap * self.step
        if self.t_last is None:
            self.t_last = t
        for k in SERIES:
            self.s[k].append(vals.get(k))

    def export(self) -> dict:
        n = len(self.s[SERIES[0]])
        t0 = None if self.t_last is None or not n else round(self.t_last - (n - 1) * self.step, 1)
        return {"t0": t0, "step_s": self.step, "units": UNITS, **{k: list(self.s[k]) for k in SERIES}}

    def dump(self) -> bytes:
        return json.dumps({"v": 1, "step_s": self.step, "t_last": self.t_last,
                           "series": {k: list(d) for k, d in self.s.items()}}, separators=(",", ":")).encode()

    def load(self, text: str | None, now: float) -> bool:
        """Restore from a dump. False (and nothing changes) if it is missing, corrupt, from another step size, from the
        future, or older than the whole window."""
        try:
            obj = json.loads(text or "")
        except ValueError:
            return False
        if not isinstance(obj, dict) or obj.get("v") != 1 or obj.get("step_s") != self.step:
            return False
        t_last, series = _num(obj.get("t_last")), obj.get("series")
        if t_last is None or not isinstance(series, dict) or t_last > now + 2 * self.step \
                or now - t_last > self.n * self.step:
            return False
        cols = {}
        base = next((len(series[k]) for k in SERIES if k not in LATER_SERIES and isinstance(series.get(k), list)), 0)
        for k in SERIES:
            a = series.get(k)
            if a is None and k in LATER_SERIES:
                a = [None] * base                        # a dump from before this series existed: its history starts now
            if not isinstance(a, list):
                return False
            # keep ints as ints (a float 27.0 is two bytes longer, x 6480 points), drop anything that is not a finite number
            cols[k] = [_pt(x) for x in a][-self.n:]
        n = min(len(c) for c in cols.values())
        for k in SERIES:
            self.s[k] = deque(cols[k][len(cols[k]) - n:], maxlen=self.n)
        self.t_last = t_last
        return n > 0


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """tmp + os.replace in the same directory: readers see the old file or the new one, never half of one. The mode is set
    with fchmod so the umask cannot change it."""
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode & 0o004:
            os.chmod(path.parent, 0o755)                  # a dir we create for a world-readable file must be traversable (umask 077)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def encode(payload: dict, cap: int = FILE_CAP) -> bytes:
    """Compact JSON, guaranteed <= cap: if it ever grows past the budget the OLDEST history points are dropped (t0 follows)."""
    data = json.dumps(payload, separators=(",", ":")).encode()
    h = payload.get("history") or {}
    for _ in range(4):
        n = len(h.get(SERIES[0], []))
        if len(data) <= cap or n <= 1:
            break
        per_point = max(len(json.dumps({k: h[k] for k in SERIES}, separators=(",", ":"))) / n, 1)
        drop = min(n - 1, int((len(data) - cap) / per_point * 1.15) + 1)
        for k in SERIES:
            h[k] = h[k][drop:]
        if h.get("t0") is not None:
            h["t0"] = round(h["t0"] + drop * h["step_s"], 1)
        data = json.dumps(payload, separators=(",", ":")).encode()
    return data


# =========================================================================== host sampling
def empty_host(cores: int | None = None) -> dict:
    """The host block with every key present and every value unknown (what the file shows if sampling /proc itself fails)."""
    return {"uptime_s": None, "load": None, "cores": cores or os.cpu_count() or 1, "cpu_pct": None, "cpu_user": None,
            "cpu_sys": None, "cpu_iowait": None,
            "mem": {"total": None, "used": None, "avail": None, "cache": None, "swap_used": None, "swap_total": None},
            "psi": {"mem_some60": None, "mem_full60": None, "io_some60": None, "io_full60": None, "cpu_some60": None},
            "disk": [], "io": [], "net": {"rx_bps": None, "tx_bps": None}}


class Sampler:
    """Reads the host each tick and keeps the previous counters to turn them into rates. Never raises: an unreadable file
    gives nulls for what depends on it."""

    def __init__(self, classes: Classes):
        self.classes = classes
        self.prev: dict | None = None
        self.prev_ct: dict[str, tuple] = {}
        self.prev_ct_t: float | None = None
        self._disk_ok: dict[str, bool] = {}
        self._net_ok: dict[str, bool] = {}
        self.cores = os.cpu_count() or 1

    # whole physical disks have /sys/block/<dev>/device; partitions, loop, dm, md, ram, zram do not (or are filtered by name)
    def _is_disk(self, dev: str) -> bool:
        v = self._disk_ok.get(dev)
        if v is None:
            v = not dev.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")) and (SYS_BLOCK / dev / "device").exists()
            self._disk_ok[dev] = v
        return v

    # physical NICs have /sys/class/net/<if>/device; lo, bridges, veth*, docker0, tailscale0 do not
    def _is_phys(self, ifc: str) -> bool:
        v = self._net_ok.get(ifc)
        if v is None:
            v = ifc != "lo" and (SYS_NET / ifc / "device").exists()
            self._net_ok[ifc] = v
        return v

    def read_host(self, mono: float, disks: list[dict] | None) -> tuple[dict, dict]:
        """(host block, history point). `disks` is the disk probe's last good value."""
        cpu = parse_cpu(_read(PROC / "stat"))
        mem = parse_meminfo(_read(PROC / "meminfo"))
        psi = {n: parse_psi(_read(PROC / "pressure" / n)) for n in ("memory", "io", "cpu")}
        cur = {"m": mono, "cpu": cpu, "disk": parse_diskstats(_read(PROC / "diskstats"), self._is_disk),
               "net": parse_netdev(_read(PROC / "net" / "dev"), self._is_phys),
               "stall": {"mem": psi["memory"].get("some", {}).get("total"), "io": psi["io"].get("some", {}).get("total")}}
        prev, self.prev = self.prev, cur
        dt = cur["m"] - prev["m"] if prev else 0.0
        rates = prev is not None and dt > 0

        pc = cpu_pcts(prev["cpu"], cpu) if rates and prev["cpu"] and cpu else None
        io = []
        r_tot = w_tot = 0.0
        if rates:
            for dev, (rs, ws, ms) in cur["disk"].items():
                p = prev["disk"].get(dev)
                if p and rs >= p[0] and ws >= p[1] and ms >= p[2]:
                    rb, wb = (rs - p[0]) * 512 / dt, (ws - p[1]) * 512 / dt
                    r_tot, w_tot = r_tot + rb, w_tot + wb
                    io.append({"dev": dev, "read_bps": int(rb), "write_bps": int(wb),
                               "util_pct": round(min(100.0, (ms - p[2]) / (dt * 10)), 1)})   # ms / (dt s * 1000 ms) * 100
        io.sort(key=lambda r: r["dev"])
        rx = tx = None
        if rates:
            rx = tx = 0.0
            for ifc, (a, b) in cur["net"].items():
                p = prev["net"].get(ifc)
                if p and a >= p[0] and b >= p[1]:           # a reset counter (interface bounce) drops that interface for this tick
                    rx, tx = rx + (a - p[0]) / dt, tx + (b - p[1]) / dt

        def stall(kind: str) -> float | None:
            a, b = (prev or {}).get("stall", {}).get(kind), cur["stall"][kind]
            if not rates or a is None or b is None or b < a:
                return None
            return round(min(100.0, (b - a) / (dt * 1e6) * 100), 1)              # `total=` is stall time in microseconds

        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        used = total - avail if total is not None and avail is not None else None
        a60 = lambda f, k: _r(psi[f].get(k, {}).get("avg60"), 2)                  # noqa: E731
        up = _read(PROC / "uptime")
        try:
            uptime = int(float((up or "").split()[0]))
        except (IndexError, ValueError):
            uptime = None
        host = {"uptime_s": uptime, "load": parse_loadavg(_read(PROC / "loadavg")), "cores": self.cores,
                "cpu_pct": pc and pc["cpu_pct"], "cpu_user": pc and pc["cpu_user"], "cpu_sys": pc and pc["cpu_sys"],
                "cpu_iowait": pc and pc["cpu_iowait"],
                "mem": {"total": total, "used": used, "avail": avail,
                        "cache": sum(mem.get(k, 0) for k in ("Buffers", "Cached", "SReclaimable")) if mem else None,
                        "swap_used": mem["SwapTotal"] - mem["SwapFree"] if "SwapTotal" in mem and "SwapFree" in mem else None,
                        "swap_total": mem.get("SwapTotal")},
                "psi": {"mem_some60": a60("memory", "some"), "mem_full60": a60("memory", "full"),
                        "io_some60": a60("io", "some"), "io_full60": a60("io", "full"), "cpu_some60": a60("cpu", "some")},
                "disk": disks or [], "io": io[:24],
                "net": {"rx_bps": None if rx is None else int(rx), "tx_bps": None if tx is None else int(tx)}}
        mib = lambda x: None if x is None or not rates else round(x / MIB, 1)    # noqa: E731
        sw_t, sw_u = host["mem"]["swap_total"], host["mem"]["swap_used"]
        point = {"cpu": pc and pc["cpu_pct"], "mem_pct": None if not total or used is None else int(round(100.0 * used / total)),
                 "swap_pct": int(round(100.0 * sw_u / sw_t)) if sw_t and sw_u is not None else None,       # no swap at all: null, not 0
                 "load1": _r((host["load"] or [None])[0], 2),
                 "psi_mem": stall("mem"), "psi_io": stall("io"),
                 "net_rx": mib(rx), "net_tx": mib(tx), "disk_r": mib(r_tot) if rates else None,
                 "disk_w": mib(w_tot) if rates else None}
        return host, point

    def read_containers(self, mono: float, dk: list[dict] | None, stale: bool) -> dict:
        """Per-container CPU% (cgroup cpu.stat usage_usec delta) and anonymous memory (memory.stat anon) for every running
        container, ~1 ms for 73 of them; then the top 8 of each. Container list comes from the docker probe."""
        self.classes.refresh(mono)
        if dk is None:
            return {"running": None, "unhealthy": [], "top_cpu": [], "top_mem": [], "stale": True}
        cur: dict[str, tuple] = {}
        for c in dk:
            d = c.get("cg")
            if c["state"] != "running" or d is None:
                continue
            base = os.fspath(d)                                # str concat, not Path.__truediv__: 3x cheaper x 150 reads
            cpu, mem = _read(base + "/cpu.stat"), _read(base + "/memory.stat")
            if cpu is None or mem is None:
                continue                                  # exited between the docker refresh and now
            usec = anon = None
            for ln in cpu.splitlines():
                if ln.startswith("usage_usec "):
                    usec = int(ln[11:])
                    break
            for ln in mem.splitlines():
                if ln.startswith("anon "):
                    anon = int(ln[5:])
                    break
            if usec is not None and anon is not None:
                cur[c["name"]] = (c["id"], usec, anon)
        dt = None if self.prev_ct_t is None else mono - self.prev_ct_t
        rows = []
        for name, (cid, usec, anon) in cur.items():
            p = self.prev_ct.get(name)
            pct = None
            if dt and dt > 0 and p and p[0] == cid and usec >= p[1]:    # new id or a lower counter = restarted container
                pct = round((usec - p[1]) / (dt * 1e6) * 100, 1)
            rows.append({"name": name, "class": self.classes.of(name), "cpu_pct": pct, "mem_gib": round(anon / GIB, 2)})
        self.prev_ct, self.prev_ct_t = cur, mono
        top_cpu = sorted((r for r in rows if r["cpu_pct"] is not None and r["cpu_pct"] >= 0.05),
                         key=lambda r: (-r["cpu_pct"], r["name"]))[:8]
        top_mem = sorted(rows, key=lambda r: (-r["mem_gib"], r["name"]))[:8]
        bad = sorted(c["name"] for c in dk if c["state"] == "restarting" or (c["state"] == "running" and c["health"] == "unhealthy"))
        return {"running": sum(1 for c in dk if c["state"] == "running"), "unhealthy": bad[:20],
                "top_cpu": top_cpu, "top_mem": top_mem, "stale": stale}


# =========================================================================== activity / pressure (cheap, file based)
def _parse_ts(s: Any) -> float | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


class Activity:
    """What the maintenance runner is doing right now and the newest REAL action.

    Running = RUN_DIR/<tier>.lock files that are flock()ed (the files persist, so 'held' is read from /proc/locks: probing with
    flock ourselves could make a starting timer skip its run) PLUS the jobs the scheduler has detached (STATE_DIR/sched.json,
    jobs.<name>.running, pid alive, running for >= MIN_JOB_S so a one-second plumbing job does not flicker on a 5 s display).
    `check` (the 15-min read-only check) is reported separately and `tick` (the scheduler's own once-a-minute evaluation) is not
    maintenance at all. Last action = newest audit record with outcome "done", re-read only when the file changes."""
    QUIET = ("check", "tick")
    MIN_JOB_S = 10.0

    def __init__(self) -> None:
        self.sig: Any = None
        self.last: dict | None = None
        self.sched_sig: Any = "unset"
        self.sched: list[tuple[str, int, float]] = []      # (job, supervisor pid, started) from the last sched.json read

    def jobs(self, now: float) -> list[str]:
        p = STATE_DIR / "sched.json"
        sig = _stat_sig(p)
        if sig != self.sched_sig:                          # parse only when the scheduler rewrote it (about once a minute)
            self.sched_sig, self.sched = sig, []
            try:
                jobs = json.loads(p.read_text()).get("jobs")
                for name, j in (jobs.items() if isinstance(jobs, dict) else ()):
                    r = j.get("running") if isinstance(j, dict) else None
                    pid, started = (r.get("pid"), _num(r.get("started"))) if isinstance(r, dict) else (None, None)
                    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and started is not None:
                        self.sched.append((_ascii(name, 40), pid, started))
            except (OSError, ValueError, AttributeError, TypeError):
                self.sched = []
        return [n for n, pid, started in self.sched if now - started >= self.MIN_JOB_S and (PROC / str(pid)).exists()]

    def running(self, now: float | None = None) -> tuple[list[str], bool]:
        names: list[str] = []
        check = False
        try:
            locks = sorted(RUN_DIR.glob("*.lock"))
        except OSError:
            locks = []
        if locks:
            held = parse_flock_keys(_read(PROC / "locks"))
            on = [p.stem for p in locks if _lock_key(p) in held]
            check = "check" in on
            names = [_ascii(n, 40) for n in on if n not in self.QUIET]
        names += self.jobs(time.time() if now is None else now)
        return sorted(set(names))[:8], check

    def last_action(self) -> dict | None:
        p = LOG_DIR / "audit.jsonl"
        sig = _stat_sig(p)
        if sig == self.sig:
            return self.last
        self.sig, self.last = sig, None
        try:
            with open(p, "rb") as f:
                f.seek(max(0, (sig or (0, 0))[1] - 131072))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("outcome") == "done" and r.get("task") != "notify":
                ts = _parse_ts(r.get("ts"))
                if ts is not None:
                    self.last = {"ts": ts, "task": _ascii(r.get("task"), 40), "action": _ascii(r.get("action"), 60)}
                    break
        return self.last


class PressureSrc:
    """The spike manager's current level, from status.json (tasks.pressure_state), re-read only when the file changes."""

    def __init__(self) -> None:
        self.sig: Any = None
        self.val: dict = {"level": None, "gate_level": None, "level_name": None, "why": "", "ran": None}

    def read(self, now: float) -> dict:
        p = STATE_DIR / "status.json"
        sig = _stat_sig(p)
        if sig != self.sig:
            self.sig, self.val = sig, {"level": None, "gate_level": None, "level_name": None, "why": "", "ran": None}
            try:
                e = (json.loads(p.read_text()).get("tasks") or {}).get("pressure_state") or {}
                m = e.get("metrics") if isinstance(e.get("metrics"), dict) else {}
                lvl, gate = (self._lvl(m.get(k)) for k in ("level", "gate_level"))
                name = m.get("level_name")
                self.val = {"level": lvl, "gate_level": lvl if gate is None else gate,      # io/gpu-only pressure: level 3, gate_level 0
                            "level_name": _ascii(name, 40) if isinstance(name, str) and name else None,
                            "why": _ascii(m.get("why") or e.get("summary") or "", 140), "ran": _num(e.get("last_run"))}
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        ran = self.val["ran"]
        return {"level": self.val["level"], "gate_level": self.val["gate_level"], "level_name": self.val["level_name"],
                "why": self.val["why"], "age_s": None if ran is None else int(max(now - ran, 0))}

    @staticmethod
    def _lvl(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 5 else None


# =========================================================================== the daemon
class Stop:
    """Stop flag that is safe to set from a signal handler (a pipe write: no locks, so it cannot deadlock the way
    threading.Event.set can when it interrupts the thread inside wait()) and cheap to wait on from any number of threads."""

    def __init__(self) -> None:
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        os.set_blocking(self.w, False)
        self._flag = False

    def set(self) -> None:
        self._flag = True
        try:
            os.write(self.w, b"x")
        except OSError:
            pass                                      # pipe full or closed: the flag is already set

    def is_set(self) -> bool:
        return self._flag

    def wait(self, timeout: float | None = None) -> bool:
        if not self._flag:
            p = select.poll()                         # one poll object per call (they refuse concurrent use); poll, not
            p.register(self.r, select.POLLIN)         # select(): select() raises for descriptors >= 1024
            p.poll(None if timeout is None else max(0.0, timeout) * 1000)   # the byte is never consumed, so every waiter wakes
        return self._flag


def next_tick(nxt: float, now: float, interval: float) -> float:
    """When the tick after the one scheduled at `nxt` is due. A fixed grid (no drift from tick duration); if we fell more than
    one interval behind (suspend, long stall) resync to `now` instead of firing a burst of catch-up ticks."""
    nxt += interval
    return now if nxt < now - interval else nxt


class Live:
    def __init__(self, cfg: Cfg | None = None):
        self.cfg = cfg or load_cfg()
        c = self.cfg
        self.classes = Classes()
        self.sampler = Sampler(self.classes)
        self.hist = History(c.interval)
        self.activity, self.pressure = Activity(), PressureSrc()
        self.probes: dict[str, Probe] = {}
        if c.gpu:
            self.probes["gpu"] = Probe("gpu", read_gpu, c.gpu_every)
        if c.sensors:
            self.probes["sensors"] = Probe("sensors", make_sensors_reader(c), c.sensors_every)
        if c.docker:
            self.probes["docker"] = Probe("docker", lambda: read_docker(c.docker_timeout), c.docker_every)
        if c.disk:
            self.probes["disk"] = Probe("disk", lambda: read_disks(c.mounts), c.disk_every)
        self.io_sampler, self.swap_rates = iotop.IoSampler(PROC), swapwatch.RateReader(PROC)      # PROC/CGROUP are module globals so tests can point them at fake trees
        if c.io_top:
            self.probes["io_top"] = Probe("io_top", lambda: self.io_sampler.sample(self._docker_names()), c.io_top_every)
        if c.swap:
            self.probes["swap"] = Probe("swap", lambda: swapwatch.holders(CGROUP, names=self._docker_names(), top=5), c.swap_every)
        if c.services:
            self.probes["services"] = Probe("services", make_services_reader(c, lambda: self.get("docker")), c.services_every)
            if "docker" in self.probes:
                self.probes["services"].after = [self.probes["docker"]]
        for n in ("io_top", "swap"):                              # they name containers from the docker probe's list: wait for its first answer
            if n in self.probes and "docker" in self.probes:
                self.probes[n].after = [self.probes["docker"]]
        self.started = time.time()
        self.ticks = self.errors = 0
        self.tick_ms = 0.0
        self.last_persist = time.monotonic()

    def get(self, name: str) -> tuple[Any, bool]:
        p = self.probes.get(name)
        return p.get() if p else (None, True)

    def _docker_names(self) -> dict[str, str]:
        """{container id: name} from the docker probe's last good list (names a cgroup or a process to its container)."""
        dk, _ = self.get("docker")
        return {c["id"]: c["name"] for c in dk or [] if isinstance(c, dict) and "id" in c and "name" in c}

    # -- one sample ------------------------------------------------------------------------------------------------
    def build(self, wall: float, mono: float) -> dict:
        def safe(name: str, fn: Callable, default: Any) -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - one broken section must not cost the whole file
                self.errors += 1
                _warn(f"build-{name}", f"{name} failed: {type(exc).__name__}: {_ascii(exc, 100)}")
                return default

        disks, _ = self.get("disk")
        host, point = safe("host", lambda: self.sampler.read_host(mono, disks), (empty_host(), {}))
        gpu_v, gpu_stale = self.get("gpu")
        gpu = {"util": None, "mem_used": None, "mem_total": None, "temp": None, "power_w": None, "fan_pct": None,
               **(gpu_v or {}), "stale": gpu_stale}
        point["gpu"] = None if gpu_stale else gpu["util"]
        vt, vu = gpu["mem_total"], gpu["mem_used"]
        point["vram_pct"] = int(round(100.0 * vu / vt)) if not gpu_stale and vt and vu is not None else None
        sens_v, sens_stale = self.get("sensors")
        sensors = {"cpu_temp": None, "gpu_temp": None, "ram_temp": None, "nvme_temp": None, "cpu_fan_rpm": None,
                   "case_fan_rpm": None, **(sens_v or {}), "stale": sens_stale}
        if gpu["temp"] is not None and not gpu_stale:
            sensors["gpu_temp"] = gpu["temp"]               # nvidia-smi is the primary source for the GPU temperature
        dk, dk_stale = self.get("docker")
        containers = safe("containers", lambda: self.sampler.read_containers(mono, dk, dk_stale),
                          {"running": None, "unhealthy": [], "top_cpu": [], "top_mem": [], "stale": True})
        sv, sv_stale = self.get("services")
        services = [{**s, "stale": sv_stale} for s in (sv or [])]
        running, check = safe("activity", self.activity.running, ([], False))
        io_v, io_stale = self.get("io_top")
        io_top = {"readers": [], "window_s": None, **(io_v or {}), "stale": io_stale}
        sw_v, sw_stale = self.get("swap")
        swap = safe("swap", lambda: self._swap_block(host, sw_v or [], sw_stale), {"state": "none", "stale": True})
        self.hist.push(wall, point)
        return {"schema": 1, "generated_at": round(wall, 2), "interval_s": self.cfg.interval, "host": host, "gpu": gpu,
                "sensors": sensors, "containers": containers, "services": services, "io_top": io_top, "swap": swap,
                "activity": {"maintenance_running": running, "check_running": check,
                             "last_action": safe("audit", self.activity.last_action, None)},
                "pressure": safe("pressure", lambda: self.pressure.read(wall),
                                {"level": None, "gate_level": None, "level_name": None, "why": "", "age_s": None}),
                "history": self.hist.export(),
                "probes": {n: {"age_s": p.age(), "stale": p.get()[1]} for n, p in self.probes.items()},
                "self": {"pid": os.getpid(), "started_at": round(self.started, 1), "ticks": self.ticks, "errors": self.errors,
                         "tick_ms": round(self.tick_ms, 1), "rss_mb": _rss_mb(), "threads": threading.active_count()}}

    def _swap_block(self, host: dict, hold: list, stale: bool) -> dict:
        """Swap state from the kernel's own signals (swap-in/out rate, memory stall), holders from the cgroup probe."""
        m = host["mem"]
        total, used = m.get("swap_total"), m.get("swap_used")
        rates = self.swap_rates.read()
        if not total:
            return {"used_b": 0, "total_b": 0, "used_pct": None, "state": "none", "in_bps": None, "out_bps": None, "exhausted": False,
                    "holders": [], "stale": stale}
        an = swapwatch.analyse({"MemTotal": m.get("total") or 0, "MemAvailable": m.get("avail") or 0, "SwapTotal": total,
                                "SwapFree": max(total - (used or 0), 0)}, rates, {"full60": host["psi"].get("mem_full60")})
        relief = swapwatch.relief_active()                       # emptied on purpose by `homelab-maint swap relieve`: not thrashing
        return {"used_b": an["used_b"], "total_b": an["total_b"], "used_pct": an["used_pct"], "state": "relief" if relief else an["state"],
                "in_bps": None if rates["in_bps"] is None else int(rates["in_bps"]), "out_bps": None if rates["out_bps"] is None else int(rates["out_bps"]),
                "exhausted": an["exhausted"] and not relief,
                "holders": [{"who": _ascii(h["who"], 40), "kind": h["kind"], "swap_b": h["swap_b"], "resident_b": h["current_b"], "cap_b": h["max_b"]} for h in hold[:5]],
                "stale": stale}

    def tick(self, wall: float | None = None, mono: float | None = None, write: bool = True) -> bytes:
        t0 = time.perf_counter()
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        data = encode(self.build(wall, mono))
        if write:
            try:
                write_atomic(STATE_DIR / "public" / "live.json", data, 0o644)
            except OSError as exc:
                self.errors += 1
                _warn("write", f"cannot write live.json: {exc}")
        self.ticks += 1
        self.tick_ms = (time.perf_counter() - t0) * 1000
        if write and mono - self.last_persist >= PERSIST_S:
            self.persist()
        return data

    def persist(self) -> None:
        self.last_persist = time.monotonic()
        try:
            write_atomic(STATE_DIR / "live-history.json", self.hist.dump(), 0o600)
        except OSError as exc:
            _warn("persist", f"cannot persist history: {exc}")

    # -- lifecycle -------------------------------------------------------------------------------------------------
    def start(self, stop: Any, wait_first: float = 2.0) -> None:
        self.hist.load(_read(STATE_DIR / "live-history.json"), time.time())      # restart recovery
        for p in self.probes.values():
            p.start(stop)
        end = time.monotonic() + wait_first                  # let the first probe results land so the first file is full
        for p in self.probes.values():
            p.first.wait(max(0.0, end - time.monotonic()))

    def run(self, stop: Any, duration: float | None = None) -> int:
        self.start(stop)
        t_end = None if duration is None else time.monotonic() + duration
        nxt = time.monotonic()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.errors += 1
                _warn("tick", f"tick failed: {type(exc).__name__}: {_ascii(exc, 120)}")
            now = time.monotonic()
            if t_end is not None and now >= t_end:
                break
            nxt = next_tick(nxt, now, self.cfg.interval)
            if stop.wait(max(0.0, nxt - now)):
                break
        self.persist()                                       # graceful stop: the graph survives the restart
        return 0

    def once(self) -> bytes:
        """Two samples one second apart (so rates exist), probes run inline; prints nothing, writes nothing."""
        for p in self.probes.values():
            p.run_once()
        self.tick(write=False)
        time.sleep(1.0)
        return self.tick(write=False)


def _rss_mb() -> float | None:
    try:
        return round(int((_read(PROC / "self" / "statm") or "").split()[1]) * os.sysconf("SC_PAGE_SIZE") / MIB, 1)
    except (IndexError, ValueError):
        return None


def add_args(ap: argparse.ArgumentParser) -> None:
    """The command line of the monitor. Declared as real options so `homelab-maint live --once` works as a cli.py subcommand
    (`live.add_args(sub.add_parser("live"))`); a REMAINDER positional rejects every flag."""
    ap.add_argument("--once", action="store_true", help="sample twice, print the JSON, write nothing")
    ap.add_argument("--duration", type=float, help="exit after N seconds (benchmarks, tests)")


def run_args(a: argparse.Namespace) -> int:
    """Run (or `--once`) the monitor for parsed arguments. Used by main() and by the cli.py `live` subcommand."""
    mon = Live()
    if a.once:
        print(json.dumps(json.loads(mon.once()), indent=1))
        return 0
    stop = Stop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    print(f"homelab-maint-live: sampling every {mon.cfg.interval:g} s into {STATE_DIR / 'public' / 'live.json'}",
          file=sys.stderr, flush=True)
    return mon.run(stop, a.duration)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="homelab-maint live", description=__doc__.split("\n\n")[0])
    add_args(ap)
    return run_args(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
