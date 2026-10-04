"""Who is reading the disk, and which filesystem scans have outlived their purpose (stdlib, read-only, /proc only).

Two users share this module:
  * the live monitor (live.py) asks `IoSampler.sample()` every ~10 s for the top disk readers/writers, so the Disk I/O tile can say
    "bfs 4.8 MiB/s" next to "sdg 96% busy" instead of leaving the owner to guess;
  * the `stuck_scans` check (tasks/scans.py) asks `find_scans()` for find/bfs/du/... processes and judges them against their age,
    their parent and what they cost.

Numbers are the kernel's own per-process counters (/proc/PID/io `read_bytes` / `write_bytes` = bytes that really went to the block
layer, not page-cache hits), turned into rates between two samples, the way iotop does.

PRIVACY: everything returned here may end up in live.json / status JSON, which the website serves. So a row carries the program name
(argv[0] basename, the kernel `comm` when argv[0] is unreadable), a pid, an age, the container it belongs to, and for a scan tool the
single directory it was pointed at. NEVER a command line: arguments can hold passwords, patterns and tokens.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable

PROC = Path("/proc")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# Programs that walk the filesystem and can run for hours without producing anything the owner is waiting for.
SCAN_TOOLS = frozenset({"find", "bfs", "fd", "fdfind", "locate", "mlocate", "plocate", "updatedb", "updatedb.mlocate", "du", "ncdu", "tree"})
# Recursive by default (rg, ag, ack) or only with -r/-R (grep).
GREP_LIKE = frozenset({"rg", "ag", "ack", "grep", "egrep", "fgrep"})
_DOCKER_ID = re.compile(r"docker-([0-9a-f]{64})\.scope|/docker/([0-9a-f]{64})")
_SAFE = re.compile(r"[^A-Za-z0-9._+@:/-]")


def _read(p: Path | str) -> str | None:
    try:
        with open(p, "rb") as f:
            return f.read(65536).decode("utf-8", "replace")
    except OSError:
        return None


def clean(s: Any, n: int = 24) -> str:
    """ASCII-only, no whitespace or shell characters, capped: safe to put in JSON that a website serves."""
    return _SAFE.sub("?", str(s))[:n]


def read_io(pid: int, proc: Path = PROC) -> tuple[int, int] | None:
    """(read_bytes, write_bytes) of one process, or None when it is gone or unreadable."""
    txt = _read(proc / str(pid) / "io")
    if txt is None:
        return None
    r = w = None
    for ln in txt.splitlines():
        if ln.startswith("read_bytes:"):
            r = int(ln.split()[1])
        elif ln.startswith("write_bytes:"):
            w = int(ln.split()[1])
    return None if r is None or w is None else (r, w)


def parse_stat(txt: str | None) -> dict | None:
    """/proc/PID/stat -> {comm, ppid, tty, start_ticks}. `comm` may contain spaces and parentheses: split on the LAST ')'."""
    if not txt or "(" not in txt or ")" not in txt:
        return None
    a, b = txt.index("("), txt.rindex(")")
    f = txt[b + 1:].split()
    try:
        return {"comm": txt[a + 1:b], "ppid": int(f[1]), "tty": int(f[4]), "start_ticks": int(f[19])}
    except (IndexError, ValueError):
        return None


def argv_of(pid: int, proc: Path = PROC) -> list[str]:
    txt = _read(proc / str(pid) / "cmdline")
    return [x for x in (txt or "").split("\0") if x != ""] if txt else []


def _uptime(proc: Path) -> float | None:
    try:
        return float((_read(proc / "uptime") or "").split()[0])
    except (IndexError, ValueError):
        return None


def describe(pid: int, proc: Path = PROC, up: float | None = None, names: dict[str, str] | None = None) -> dict | None:
    """One process as a safe, small row: name, pid, age_s, container, orphan, unit. Never the arguments."""
    st = parse_stat(_read(proc / str(pid) / "stat"))
    if st is None:
        return None
    argv = argv_of(pid, proc)
    exe = os.path.basename(argv[0]) if argv and argv[0] else ""
    name = clean(exe or st["comm"])
    cg = _read(proc / str(pid) / "cgroup") or ""
    unit = cg.strip().splitlines()[-1].rsplit("/", 1)[-1] if cg.strip() else ""
    container = None
    m = _DOCKER_ID.search(cg)
    if m:
        cid = m.group(1) or m.group(2)
        container = clean((names or {}).get(cid) or (names or {}).get(cid[:12]) or cid[:12], 40)
    parent = parse_stat(_read(proc / str(st["ppid"]) / "stat")) if st["ppid"] > 1 else None
    # Orphaned = nobody is waiting for its output: re-parented to init or to a `systemd --user` manager, and not a unit's own job
    # (a .service such as the nightly plocate update is supervised and expected to run unattended).
    reparented = st["ppid"] <= 1 or (parent is not None and parent["comm"] == "systemd")
    orphan = bool(reparented and not unit.endswith(".service") and container is None)
    up = _uptime(proc) if up is None else up
    age = None if up is None else max(0.0, up - st["start_ticks"] / CLK_TCK)
    return {"pid": pid, "name": name, "argv": argv, "ppid": st["ppid"], "tty": st["tty"] != 0, "age_s": None if age is None else int(age),
            "container": container, "unit": clean(unit, 60), "orphan": orphan}


def public_row(d: dict, **extra: Any) -> dict:
    """The part of `describe()` that may be published (no argv)."""
    return {"pid": d["pid"], "name": d["name"], "age_s": d["age_s"], "container": d["container"], "orphan": d["orphan"], **extra}


# --------------------------------------------------------------------------- top readers (live monitor)
class IoSampler:
    """Rates from consecutive samples of every process's /proc/PID/io. ~700 small reads per pass (about 10 ms)."""

    def __init__(self, proc: Path = PROC, clock: Callable[[], float] = time.monotonic):
        self.proc, self.clock = proc, clock
        self.prev: dict[int, tuple[int, int]] = {}
        self.prev_t: float | None = None

    def _all(self) -> dict[int, tuple[int, int]]:
        cur: dict[int, tuple[int, int]] = {}
        try:
            with os.scandir(self.proc) as it:
                for e in it:
                    if e.name.isdigit():
                        v = read_io(int(e.name), self.proc)
                        if v is not None:
                            cur[int(e.name)] = v
        except OSError:
            pass
        return cur

    def sample(self, names: dict[str, str] | None = None, top: int = 3, min_bps: float = 64 * 1024) -> dict:
        """{"readers":[{pid,name,container,orphan,age_s,read_bps,write_bps}], "window_s"}; empty `readers` on the first call.
        A counter that went backwards (pid reused) drops that process for this window."""
        now, cur = self.clock(), self._all()
        prev, pt, self.prev, self.prev_t = self.prev, self.prev_t, cur, now
        if pt is None or now <= pt:
            return {"readers": [], "window_s": None}
        dt = now - pt
        cand = []
        for pid, (r, w) in cur.items():
            p = prev.get(pid)
            if p and r >= p[0] and w >= p[1] and (r - p[0]) + (w - p[1]) >= min_bps * dt:
                cand.append(((r - p[0]) + (w - p[1]), pid, (r - p[0]) / dt, (w - p[1]) / dt))
        cand.sort(reverse=True)
        up, rows = _uptime(self.proc), []
        for _tot, pid, rb, wb in cand[:top]:
            d = describe(pid, self.proc, up, names)
            if d is not None:
                rows.append(public_row(d, read_bps=int(rb), write_bps=int(wb)))
        return {"readers": rows, "window_s": round(dt, 1)}


# --------------------------------------------------------------------------- filesystem scans (stuck_scans check)
def _scan_root(argv: list[str]) -> str:
    """The one directory a scan tool was pointed at: the first absolute-path argument ("/" for find / ...), else the cwd-relative default.
    Only that, never the patterns or any other argument."""
    for a in argv[1:]:
        if a.startswith("/") and "\0" not in a and not a.startswith("//"):
            return clean(a, 60)
    return "."


def _is_scan(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe in SCAN_TOOLS:
        return True
    if exe in GREP_LIKE:
        if exe in ("rg", "ag", "ack"):
            return True
        return any(a in ("-r", "-R", "--recursive", "--dereference-recursive") or (a.startswith("-") and not a.startswith("--") and ("r" in a[1:] or "R" in a[1:]))
                   for a in argv[1:] if a.startswith("-"))
    return False


def find_scans(proc: Path = PROC, names: dict[str, str] | None = None) -> list[dict]:
    """Every running filesystem scan, as describe() rows plus `root` and `scan_tool`. Reads each process's cmdline once."""
    out, up = [], _uptime(proc)
    try:
        with os.scandir(proc) as it:
            pids = [int(e.name) for e in it if e.name.isdigit()]
    except OSError:
        return []
    for pid in pids:
        argv = argv_of(pid, proc)
        if not _is_scan(argv):
            continue
        d = describe(pid, proc, up, names)
        if d is not None and d["container"] is None:         # a scan inside a container is that application's business
            d["root"] = _scan_root(argv)
            out.append(d)
    return sorted(out, key=lambda d: (-(d["age_s"] or 0), d["pid"]))


def psi_io(proc: Path = PROC) -> dict[str, float | None]:
    """Kernel I/O pressure averages: {"some60","full60"} in percent (None when unreadable)."""
    res: dict[str, float | None] = {"some60": None, "full60": None}
    for ln in (_read(proc / "pressure" / "io") or "").splitlines():
        kind, _, rest = ln.partition(" ")
        if kind in ("some", "full"):
            for kv in rest.split():
                if kv.startswith("avg60="):
                    try:
                        res[f"{kind}60"] = float(kv[6:])
                    except ValueError:
                        pass
    return res
