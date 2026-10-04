"""Swap: how full it is, WHO holds it, whether anything is waiting on it, and a relief that cannot hurt work in progress (stdlib).

A full swap is not a fault by itself. The kernel parks pages nobody has touched for a while there (an idle model server, a closed IDE)
to keep RAM for the page cache; they stay until something reads them. What matters is

  * CHURN: swap-in (a task waits for a page to come back) and swap-out (the kernel is pushing pages out right now). Idle and full is
    fine; busy and full is thrashing. This is the kernel's own signal (/proc/vmstat pswpin/pswpout) plus memory PSI.
  * HEADROOM: 0 bytes of free swap means the next real spike has no safety valve and ends in the OOM killer instead of a slowdown.

WHO holds it comes from cgroup v2 `memory.swap.current` of every cgroup that has processes (exact). Summing per-process VmSwap is not
used: forked workers share pages and each of them counts the same page again (it reported 39.9 GiB on a 32 GiB swapfile).

RELIEF = `swapoff` then `swapon` of the same area: every page returns to RAM, the area comes back empty, and nothing is killed because
`swapoff` only moves pages. It is only safe when ALL of this holds, and `relief_plan()` checks each one and names what blocks it:
  1. enough free RAM for the pages (MemAvailable - swap used >= headroom);
  2. no workload the busy gates protect is working (ComfyUI queue, Ollama, backups, Plex, builds, Immich ...);
  3. the disk the swap lives on is not busy (the page-in reads from it);
  4. every cgroup that holds swap has a memory limit that fits resident + swapped (pages swapped back in are charged to the group's
     memory.max: a cap that is too small would make the swap-in itself an OOM kill);
  5. memory PSI is calm.
`swapoff` aborts cleanly on a signal (the area stays in service), which is how the guarded CLI stops it the moment memory turns tight.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import core

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
MIB = 1024 ** 2
GIB = 1024 ** 3
PAGE = 4096

# Verdict thresholds (the shipped values; [tasks.swap_audit] overrides them).
DEFAULTS: dict[str, float] = {
    "active_in_mib_s": 1.0,          # swap-IN above this: something is reading pages back, so the swap is in use
    "thrash_in_mib_s": 16.0,         # ... and above this together with memory stall: thrashing
    "thrash_total_mib_s": 64.0,      # in + out, whatever the stall says
    "thrash_psi_full": 3.0,          # memory "full" PSI avg60 (percent) that makes busy swap hurt
    "full_pct": 90.0,                # "full" for the wording
    "min_free_swap_pct": 5.0,        # below this, with scarce RAM, there is no safety valve
    "scarce_ram_pct": 20.0,          # MemAvailable below this share of RAM counts as scarce
    "relief_min_used_gib": 1.0,      # nothing worth relieving below this
    "relief_headroom_gib": 16.0,     # RAM that must stay free after the pages are back (or 15 % of RAM, the larger): above memory_health's 10 GiB warn
    "relief_cap_margin": 0.9,        # resident + swapped must stay under this share of a group's memory.max
    "relief_disk_busy_pct": 40.0,    # the swap's own disk must be quieter than this
    "relief_psi_full": 1.0,          # memory full PSI avg60 must be below this
}


def _read(p: Path | str) -> str | None:
    try:
        with open(p, "rb") as f:
            return f.read(1 << 20).decode("utf-8", "replace")
    except OSError:
        return None


def human(b: float | None) -> str:
    if b is None:
        return "?"
    return f"{b / GIB:.1f} GiB" if b >= GIB else f"{b / MIB:.0f} MiB"


# --------------------------------------------------------------------------- raw readings
def meminfo(proc: Path = PROC) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in (_read(proc / "meminfo") or "").splitlines():
        k, _, v = ln.partition(":")
        f = v.split()
        if f and f[0].isdigit():
            out[k] = int(f[0]) * 1024
    return out


def swap_counters(proc: Path = PROC) -> tuple[int, int] | None:
    """(pswpin, pswpout) in pages since boot."""
    d = {}
    for ln in (_read(proc / "vmstat") or "").splitlines():
        k, _, v = ln.partition(" ")
        if k in ("pswpin", "pswpout") and v.strip().isdigit():
            d[k] = int(v)
    return (d["pswpin"], d["pswpout"]) if len(d) == 2 else None


def psi_mem(proc: Path = PROC) -> dict[str, float | None]:
    res: dict[str, float | None] = {"some60": None, "full60": None}
    for ln in (_read(proc / "pressure" / "memory") or "").splitlines():
        kind, _, rest = ln.partition(" ")
        if kind in ("some", "full"):
            for kv in rest.split():
                if kv.startswith("avg60="):
                    try:
                        res[f"{kind}60"] = float(kv[6:])
                    except ValueError:
                        pass
    return res


def swap_areas(proc: Path = PROC) -> list[dict]:
    """/proc/swaps -> [{path,type,size_b,used_b,prio}]."""
    rows = []
    for ln in (_read(proc / "swaps") or "").splitlines()[1:]:
        f = ln.split()
        if len(f) >= 5 and f[2].isdigit() and f[3].isdigit():
            rows.append({"path": f[0].replace("\\040", " "), "type": f[1], "size_b": int(f[2]) * 1024, "used_b": int(f[3]) * 1024, "prio": f[4]})
    return rows


class RateReader:
    """swap-in/out bytes per second between two calls (the first call has no rates)."""

    def __init__(self, proc: Path = PROC, clock: Callable[[], float] = time.monotonic):
        self.proc, self.clock, self.prev = proc, clock, None

    def read(self) -> dict[str, float | None]:
        now, cur = self.clock(), swap_counters(self.proc)
        prev, self.prev = self.prev, (now, cur)
        if prev is None or cur is None or prev[1] is None or now <= prev[0] or cur[0] < prev[1][0] or cur[1] < prev[1][1]:
            return {"in_bps": None, "out_bps": None}
        dt = now - prev[0]
        return {"in_bps": (cur[0] - prev[1][0]) * PAGE / dt, "out_bps": (cur[1] - prev[1][1]) * PAGE / dt}


# --------------------------------------------------------------------------- who holds it
def _cg_int(p: Path) -> int | None:
    """An integer cgroup file; None for 'max', a missing file or garbage."""
    t = (_read(p) or "").strip()
    return int(t) if t.isdigit() else None


def _stat_val(p: Path, key: str) -> int | None:
    """One counter of a cgroup memory.stat (None when the file or the key is missing)."""
    for ln in (_read(p) or "").splitlines():
        if ln.startswith(key + " "):
            v = ln.split()[1]
            return int(v) if v.isdigit() else None
    return None


def _unescape(s: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def classify(base: str, names: dict[str, str] | None = None) -> tuple[str, str]:
    """(kind, friendly name) of a cgroup directory name."""
    m = re.fullmatch(r"docker-([0-9a-f]{64})\.scope", base)
    if m:
        cid, nm = m.group(1), names or {}
        return "container", nm.get(cid) or nm.get(cid[:12]) or cid[:12]
    if base.endswith(".service"):
        return "service", _unescape(base[:-8])[:40]
    if base.startswith("app-") and base.endswith(".scope"):
        core = re.sub(r"-\d+$", "", _unescape(base[4:-6]))
        core = re.sub(r"^(gnome|flatpak|snap)-", "", core)
        return "app", core[:40]
    if base.startswith("session-") and base.endswith(".scope"):
        return "session", "login " + base[8:-6]
    if base.endswith(".scope"):
        return "scope", _unescape(base[:-6])[:40]
    return "cgroup", _unescape(base)[:40]


def holders(cg: Path = CGROUP, names: dict[str, str] | None = None, min_b: int = 32 * MIB, top: int | None = None) -> list[dict]:
    """Every cgroup that has processes and at least min_b swapped out, biggest first:
    {who, kind, swap_b, current_b, max_b|None, high_b|None, procs}. Cheap: one walk, three small reads per group."""
    rows = []
    root = str(cg)
    anc_cache: dict[str, tuple[int | None, int]] = {}

    def ancestors(d: Path) -> list[dict]:
        """Ancestor cgroups that carry a memory.max (their limit also bounds this holder's swap-in; hierarchical current includes it)."""
        out, p = [], d.parent
        while str(p) != root and str(p).startswith(root) and len(str(p)) > len(root):
            k = str(p)
            if k not in anc_cache:
                anc_cache[k] = (_cg_int(p / "memory.max"), _cg_int(p / "memory.current") or 0)
            mx, cur = anc_cache[k]
            if mx:
                out.append({"path": k[len(root):], "max_b": mx, "current_b": cur})
            p = p.parent
        return out

    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count("/") - root.count("/") > 12:
            dirnames[:] = []
            continue
        if "memory.swap.current" not in filenames:
            continue
        d = Path(dirpath)
        sw = _cg_int(d / "memory.swap.current")
        if not sw or sw < min_b:
            continue
        procs = (_read(d / "cgroup.procs") or "").split()
        if not procs:
            continue
        kind, who = classify(d.name, names)
        rows.append({"who": who, "kind": kind, "path": dirpath[len(root):], "swap_b": sw, "current_b": _cg_int(d / "memory.current") or 0,
                     "max_b": _cg_int(d / "memory.max"), "high_b": _cg_int(d / "memory.high"), "procs": len(procs),
                     "refault_anon": _stat_val(d / "memory.stat", "workingset_refault_anon"), "anc": ancestors(d)})
    rows.sort(key=lambda r: (-r["swap_b"], r["who"]))
    return rows if top is None else rows[:top]


# --------------------------------------------------------------------------- verdict
def analyse(mem: dict[str, int], rates: dict[str, float | None], psi: dict[str, float | None], cfg: dict | None = None) -> dict:
    """The one-word state and the numbers behind it.

    state: none (no swap) | idle (little used) | cold (lots used, nothing reads it) | active (pages come back) | thrashing.
    exhausted: no free swap AND scarce RAM, i.e. no safety valve when it is needed."""
    c = {**DEFAULTS, **(cfg or {})}
    total, free = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    if not total:
        return {"state": "none", "used_b": 0, "total_b": 0, "used_pct": None, "in_mib_s": None, "out_mib_s": None, "exhausted": False, "free_b": 0}
    used = max(total - free, 0)
    pct = 100.0 * used / total
    ib, ob = rates.get("in_bps"), rates.get("out_bps")
    in_m = None if ib is None else ib / MIB
    out_m = None if ob is None else ob / MIB
    full60 = psi.get("full60")
    avail, ram = mem.get("MemAvailable", 0), mem.get("MemTotal", 0) or 1
    scarce = avail < ram * c["scarce_ram_pct"] / 100
    exhausted = 100.0 * free / total < c["min_free_swap_pct"] and scarce
    if in_m is None:
        state = "idle" if pct < c["full_pct"] / 2 else "cold"        # no rates yet: do not claim activity we did not see
    elif (in_m >= c["thrash_in_mib_s"] and (full60 or 0) >= c["thrash_psi_full"]) or (in_m + (out_m or 0)) >= c["thrash_total_mib_s"]:
        state = "thrashing"
    elif in_m >= c["active_in_mib_s"]:
        state = "active"
    elif pct >= c["full_pct"] / 2:
        state = "cold"
    else:
        state = "idle"
    return {"state": state, "used_b": used, "total_b": total, "free_b": free, "used_pct": round(pct, 1),
            "in_mib_s": None if in_m is None else round(in_m, 2), "out_mib_s": None if out_m is None else round(out_m, 2),
            "exhausted": bool(exhausted), "full": pct >= c["full_pct"]}


RELIEF_KEYS = ("relief_min_used_gib", "relief_headroom_gib", "relief_cap_margin", "relief_disk_busy_pct", "relief_psi_full")
# (low, high) a value must sit inside to be honoured: 9 (meaning 0.9) as a cap margin, or 100 as a PSI limit, would silently switch a gate off
RELIEF_BOUNDS = {"relief_min_used_gib": (0.01, 4096.0), "relief_headroom_gib": (1.0, 4096.0), "relief_cap_margin": (0.1, 1.0),
                 "relief_disk_busy_pct": (1.0, 100.0), "relief_psi_full": (0.1, 10.0)}


def relief_cfg(cfg: dict | None) -> dict:
    """The relief's safety limits, from [tasks.swap_audit] (their documented home), for EVERY caller that plans or runs a relief: swap_audit's
    feasibility, swap_auto_relief and `homelab-maint swap relieve`. A missing, non-numeric, non-finite or out-of-range (RELIEF_BOUNDS) value
    falls back to the shipped default: a typo can tighten a gate but never switch one off."""
    t = ((cfg or {}).get("tasks") or {}).get("swap_audit") or {}
    out: dict[str, float] = {}
    for k in RELIEF_KEYS:
        v = t.get(k)
        lo, hi = RELIEF_BOUNDS[k]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and lo <= v <= hi:
            out[k] = float(v)
    return out


def _loaded_cfg() -> dict:
    try:
        return core.load_config()
    except Exception:  # noqa: BLE001 - an unreadable config must not stop a relief plan: the shipped defaults are the safe ones
        return {}


def disk_of(path: str, sys_dev: Path = Path("/sys/dev/block")) -> str | None:
    """Whole-disk name (nvme0n1, sda, dm-0) that backs a swap file or partition, or None."""
    import stat as _stat
    try:
        st = os.stat(path)
        dev = st.st_rdev if _stat.S_ISBLK(st.st_mode) else st.st_dev
        real = os.path.realpath(sys_dev / f"{os.major(dev)}:{os.minor(dev)}")
    except OSError:
        return None
    if os.path.exists(os.path.join(real, "partition")):          # a partition: its parent directory is the disk
        return os.path.basename(os.path.dirname(real)) or None
    return os.path.basename(real) or None


def disk_busy_pct(dev: str, seconds: float = 2.0, proc: Path = PROC, sleep: Callable[[float], None] = time.sleep) -> float | None:
    """io_ticks share of one disk over a short window (None when unreadable)."""
    def ticks() -> int | None:
        for ln in (_read(proc / "diskstats") or "").splitlines():
            f = ln.split()
            if len(f) > 12 and f[2] == dev:
                return int(f[12])
        return None
    a = ticks()
    if a is None:
        return None
    sleep(seconds)
    b = ticks()
    return None if b is None or b < a else round(min(100.0, (b - a) / (seconds * 10)), 1)


# --------------------------------------------------------------------------- relief
def relief_plan(mem: dict[str, int], hold: list[dict], psi: dict[str, float | None], areas: list[dict], busy: tuple[bool, str] | None,
                disk_busy: float | None, cfg: dict | None = None) -> dict:
    """Can the swap be emptied right now without hurting anything running? {safe, blockers[], steps[], need_b, headroom_b, eta_s}.

    `busy` is gates.busy("any") ((False, ...) = idle); None means "not checked", which is a blocker. `disk_busy` is the swap disk's
    busy percent (None = unknown = blocker)."""
    c = {**DEFAULTS, **(cfg or {})}
    used = sum(a["used_b"] for a in areas)
    ram, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    headroom = max(c["relief_headroom_gib"] * GIB, 0.15 * ram)
    blockers: list[str] = []
    if used < c["relief_min_used_gib"] * GIB:
        return {"safe": False, "needed": False, "blockers": [], "steps": [], "need_b": used, "headroom_b": headroom, "eta_s": 0,
                "why": f"only {human(used)} in swap: nothing worth relieving"}
    if avail - used < headroom:
        blockers.append(f"not enough free RAM: {human(avail)} available, {human(used)} to bring back, {human(headroom)} must stay free")
    if busy is None:
        blockers.append("busy gates were not checked")
    elif busy[0]:
        blockers.append(f"a protected workload is working ({busy[1]})")
    if disk_busy is None:
        blockers.append("could not read how busy the swap disk is")
    elif disk_busy >= c["relief_disk_busy_pct"]:
        blockers.append(f"the swap disk is {disk_busy:.0f}% busy")
    f60 = psi.get("full60")
    if f60 is None or f60 >= c["relief_psi_full"]:
        blockers.append("memory is under pressure (PSI full " + ("unknown" if f60 is None else f"{f60:.1f}%") + ")")
    # Pages swapped back in are charged to the holder's cgroup AND to every ancestor: memory.max would OOM-kill, memory.high would throttle
    # and push the pages straight back out. `hold` must be EVERY cgroup with swap (snapshot()["all_holders"]), not just the big ones.
    agg: dict[str, list[int]] = {}
    for h in hold:
        mx, hi = h.get("max_b"), h.get("high_b")
        if mx and h["current_b"] + h["swap_b"] > mx * c["relief_cap_margin"]:
            blockers.append(f"{h['who']}: memory cap {human(mx)} is too small to take back its {human(h['swap_b'])} "
                            f"(now {human(h['current_b'])} resident): the swap-in would OOM-kill it")
        elif hi and h["current_b"] + h["swap_b"] > hi * c["relief_cap_margin"]:
            blockers.append(f"{h['who']}: memory.high {human(hi)} is below its {human(h['current_b'])} resident plus {human(h['swap_b'])} swapped: "
                            f"it would be throttled and pushed back out")
        for a in h.get("anc") or []:
            row = agg.setdefault(a["path"], [a["max_b"], a["current_b"], 0])
            row[2] += h["swap_b"]
    for path, (mx, cur, swp) in sorted(agg.items()):
        if cur + swp > mx * c["relief_cap_margin"]:
            blockers.append(f"{path or '/'}: its memory limit {human(mx)} cannot take back {human(swp)} of swap on top of {human(cur)} in use (the swap-in would OOM-kill inside it)")
    steps = []
    if not blockers:
        for a in areas:
            if a["used_b"] > 0:
                steps += [f"ionice -c3 nice -n19 swapoff {a['path']}      # pages return to RAM; nothing is killed, work continues",
                          f"swapon {a['path']}                              # back in service, empty"]
    return {"safe": not blockers, "needed": True, "blockers": blockers, "steps": steps, "need_b": used, "headroom_b": headroom,
            "eta_s": int(used / (150 * MIB)) + 15, "why": ""}      # measured 2026-10-03: 31.3 GiB took ~3 min at idle I/O priority (starts near 50 MiB/s, speeds up)


# --------------------------------------------------------------------------- the relief ledger: a relief the owner ran on purpose is not an emergency
# While `swapoff` runs, swap-in is huge by design (31 GiB came back at 50-250 MiB/s). Everything that judges swap-in (memory_health, the
# guard's pressure verdict, the pressure ladder, swap_audit, the Live tile) must not read that as trouble, and the 15-minute AVERAGES must not
# keep counting those pages in the next run either. So the relief records itself here, and `discount()` removes exactly its pages.
def _ledger_path() -> Path:
    return core.STATE_DIR / "swap-relief.json"


def _ledger() -> dict:
    d = core.read_json(_ledger_path(), {}) or {}
    return d if isinstance(d, dict) else {}


@contextlib.contextmanager
def _ledger_lock():
    """Serialises the ledger's read-modify-write (the owner's relief, the unit, its refusal rows): a short blocking flock, unrelated to the
    relief lock below (which is held for the whole relief)."""
    path = core.STATE_DIR / "swap-relief-ledger.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _active_marker(now: float | None = None, max_age_s: float = 3600.0) -> dict | None:
    """The ledger's `active` entry, but only while it is real: written recently by a process that is still alive. A crashed relief leaves a
    marker behind; it must neither count as "running" nor keep discounting real swap-in."""
    a = _ledger().get("active")
    if not isinstance(a, dict) or not isinstance(a.get("t0"), (int, float)) or not _alive(a.get("pid")):
        return None
    t = time.time() if now is None else now
    return a if 0 <= t - a["t0"] <= max_age_s else None


def relief_begin(pid: int | None = None, now: float | None = None, proc: Path | None = None, by: str = "owner") -> bool:
    """Mark a relief as running. False (nothing written) when another live relief already holds the marker."""
    me = os.getpid() if pid is None else pid
    with _ledger_lock():
        other = _active_marker(now)
        if other is not None and other.get("pid") != me:
            return False
        c = swap_counters(proc or PROC)                      # PROC resolved at call time: tests can point it at a fake tree
        d = _ledger()
        d["active"] = {"pid": me, "t0": time.time() if now is None else now, "pin0": None if c is None else c[0], "by": "auto" if by == "auto" else "owner"}
        core.write_json_atomic(_ledger_path(), d, 0o644)
    return True


def _record_done(d: dict, row: dict) -> None:
    done = [x for x in d.get("done", []) if isinstance(x, dict)][-19:]
    done.append(row)
    d["done"] = done


def relief_end(now: float | None = None, proc: Path | None = None, ok: bool = True, note: str = "") -> None:
    """Close the running relief and keep its page count and OUTCOME (ok False = refused, aborted or failed) for whoever launched it."""
    with _ledger_lock():
        d = _ledger()
        a = d.pop("active", None)
        c = swap_counters(proc or PROC)
        if isinstance(a, dict) and isinstance(a.get("t0"), (int, float)):
            pages = max((c[0] - a["pin0"]) if c and isinstance(a.get("pin0"), int) and c[0] >= a["pin0"] else 0, 0)
            _record_done(d, {"t0": a["t0"], "t1": time.time() if now is None else now, "pages_in": pages, "by": a.get("by", "owner"), "ok": bool(ok), "note": str(note)[:120]})
        core.write_json_atomic(_ledger_path(), d, 0o644)


def relief_refused(by: str, reason: str, now: float | None = None) -> None:
    """A relief that was asked for but never started (not safe, paused, not in apply mode): a row with ok False and no pages, so the task that
    launched it can tell "refused" from "still starting" and back off, instead of mistaking a launch for a completed relief."""
    t = time.time() if now is None else now
    with _ledger_lock():
        d = _ledger()
        _record_done(d, {"t0": t, "t1": t, "pages_in": 0, "by": "auto" if by == "auto" else "owner", "ok": False, "note": "refused: " + str(reason)[:100]})
        core.write_json_atomic(_ledger_path(), d, 0o644)


def relief_outcome(since: float, by: str | None = None) -> dict | None:
    """The newest finished (or refused) relief that started at or after `since` (optionally only by "auto"/"owner"), else None."""
    best = None
    for x in _ledger().get("done", []):
        if isinstance(x, dict) and isinstance(x.get("t0"), (int, float)) and x["t0"] >= since and (by is None or x.get("by") == by):
            if best is None or x["t0"] >= best["t0"]:
                best = x
    return best


def relief_active(now: float | None = None, max_age_s: float = 3600.0) -> bool:
    """A relief is running right now: its marker is recent and the process that wrote it is alive. A stale marker (crash) is ignored."""
    return _active_marker(now, max_age_s) is not None


def relief_pages(t_from: float, t_to: float, proc: Path | None = None) -> int:
    """Pages swapped in by owner-run reliefs inside [t_from, t_to] (a relief that straddles the window counts in proportion to its overlap)."""
    if t_to <= t_from:
        return 0
    d, total = _ledger(), 0.0
    spans = [(x["t0"], x["t1"], x["pages_in"]) for x in d.get("done", []) if isinstance(x, dict) and all(isinstance(x.get(k), (int, float)) for k in ("t0", "t1", "pages_in"))]
    a = _active_marker(t_to)                                  # a dead marker's pages are not a deliberate relief's: do not discount them
    if a is not None and isinstance(a.get("pin0"), int):
        c = swap_counters(proc or PROC)
        if c is not None and c[0] >= a["pin0"]:
            spans.append((a["t0"], max(t_to, a["t0"]), c[0] - a["pin0"]))
    for t0, t1, pages in spans:
        width = max(t1 - t0, 1e-6)
        overlap = max(0.0, min(t1, t_to) - max(t0, t_from))
        total += pages * min(1.0, overlap / width)
    return int(total)


@contextlib.contextmanager
def relief_lock():
    """Mutual exclusion between reliefs (the owner's and the automatic one): an exclusive non-blocking flock for the whole relief.
    Yields False when another relief holds it. The lock dies with its process, so a crash never leaves it stuck."""
    path = core.STATE_DIR / "swap-relief.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        f.close()


def discount(pages: float, t_from: float, t_to: float, proc: Path | None = None) -> float:
    """`pages` swapped in over [t_from, t_to] minus the ones a deliberate relief caused (never below 0)."""
    return max(pages - relief_pages(t_from, t_to, proc), 0.0)


# --------------------------------------------------------------------------- automatic relief: judge each holder, then decide when waiting has gone on too long
# The standard (systemd-oomd, Meta's oomd, the TMO proactive-reclaim work) gives how full swap is NO verdict: it judges pressure and refaults.
# A holder is therefore
#   cold      holds swap and is not refaulting it: idle, wasteful in space but harmless, exactly what swap is for;
#   active    refaults it: in use, leave it alone;
#   thrashing refaults AND stalls (judged system-wide by analyse()): needs a cap or RAM, never a relief.
# `auto_decide` turns that into "has a quiet, mostly-cold, nearly-full swap hung around long enough to clear?", with hysteresis (start at
# trigger_pct, keep watching down to release_pct), a cooldown, and a circuit breaker for a swap that refills right after every relief.
AUTO_DEFAULTS: dict[str, float] = {
    "trigger_pct": 80.0,             # swap at least this full ...
    "release_pct": 60.0,             # ... and the watch only ends below this (hysteresis, no flapping around the trigger)
    "persist_hours": 2.0,            # ... for this long ("hangs around too long")
    "min_cold_share": 0.7,           # at least this share of the swap is held by cold holders (idle pages, not working memory)
    "cold_refault_pps": 5.0,         # a holder refaulting fewer anonymous pages per second than this is cold
    "min_interval_hours": 12.0,      # at most one automatic relief per this long
    "refill_hours": 6.0,             # swap back above trigger_pct within this after a relief is a "quick refill"
    "breaker_after_refills": 2.0,    # this many quick refills in a row stop the automation ...
    "breaker_days": 3.0,             # ... for this long, and the owner is told
    "max_defer_hours": 24.0,         # relief blocked this long: say so (a visible warning, never a page)
    "quiet_out_mib_s": 8.0,          # "quiet" also means the kernel is not pushing pages OUT faster than this
    "window_start_hour": 2.0,        # unattended reliefs only inside this local-time window (swapoff can stall a process that maps memory)
    "window_end_hour": 6.0,          # (start == end means any time; a window may wrap midnight, e.g. 22 -> 6)
    "max_gap_hours": 0.75,           # a longer silence between two observations (host down, PAUSE, missed runs) restarts the persistence timer
    "retry_hours": 1.0,              # after a relief that was refused, aborted or left no outcome: wait this long, doubling, at most 24 h
    "max_failed_attempts": 3.0,      # this many failures in a row make the failure visible (a warning, never a page)
}


def track(state: dict, hold: list[dict], now: float, cold_pps: float = 5.0) -> list[dict]:
    """Annotate holders with `activity` (cold | active | unknown), `refault_pps` and `cold_h` (hours continuously cold).

    `state` is the caller's persisted dict; this keeps state["holders"] = {kind:who: {r, t, cold_since}}. The first sighting of a holder, a
    missing counter or a counter that went backwards (restart) is "unknown" and never counts as cold."""
    prev = state.get("holders") if isinstance(state.get("holders"), dict) else {}
    cur: dict[str, dict] = {}
    for h in hold:
        key = h.get("path") or f"{h['kind']}:{h['who']}"        # the cgroup path: stable when a display name (a docker name) is not, and unique
        r, p = h.get("refault_anon"), prev.get(key)
        act, cold_since, pps = "unknown", None, None
        if isinstance(r, int) and isinstance(p, dict) and isinstance(p.get("r"), int) and isinstance(p.get("t"), (int, float)) and now > p["t"] and r >= p["r"]:
            pps = (r - p["r"]) / (now - p["t"])
            act = "cold" if pps < cold_pps else "active"
            if act == "cold":
                cs = p.get("cold_since")
                cold_since = cs if isinstance(cs, (int, float)) else p["t"]
        h["activity"], h["refault_pps"] = act, None if pps is None else round(pps, 2)
        h["cold_h"] = None if cold_since is None else round((now - cold_since) / 3600, 1)
        cur[key] = {"r": r, "t": now, "cold_since": cold_since}
    state["holders"] = cur
    return hold


def cold_share(hold: list[dict]) -> float:
    """Share of the swap held by cold holders (0.0 when nothing is known to be cold)."""
    tot = sum(h["swap_b"] for h in hold)
    return 0.0 if tot <= 0 else sum(h["swap_b"] for h in hold if h.get("activity") == "cold") / tot


def in_window(now: float, start_h: float, end_h: float) -> bool:
    """Is `now` (local time) inside [start_h, end_h) hours? start == end means always; the window may wrap midnight."""
    if start_h == end_h:
        return True
    lt = time.localtime(now)
    h = lt.tm_hour + lt.tm_min / 60.0
    return start_h <= h < end_h if start_h < end_h else (h >= start_h or h < end_h)


def auto_decide(st: dict, an: dict, hold: list[dict], plan: dict | None, cfg: dict | None, now: float) -> dict:
    """What the automatic relief should do on this run. Mutates `st` (the observation timers and the breaker); the CALLER records
    last_relief_at only after the relief is CONFIRMED to have finished (the ledger's outcome), never at launch. Returns {action, reason}:
      idle      nothing to watch (swap below the watch level, not quiet, or not cold enough)
      watching  over the line, waiting out persist_hours (or the cold share dipped meanwhile)
      wait      persisted, but not yet allowed: cooldown, retry backoff after a failed attempt, or outside the maintenance window
      blocked   persisted and allowed, but a precondition of the relief fails (`reasons`); retried every run, never forced
      breaker   swap kept refilling right after reliefs: automation is paused, the owner is told
      relieve   go (the relief itself re-checks everything, and honours the kill switch)
    `plan` may be None until the caller needed it (it is only computed once the timers say "relieve")."""
    c = {**AUTO_DEFAULTS, **(cfg or {})}
    pct = an.get("used_pct")
    # Observation continuity: time we did not watch (host down, PAUSE, missed runs, a clock that jumped) is not time spent "hanging around".
    last = st.get("last_seen")
    if isinstance(last, (int, float)) and not (0 <= now - last <= c["max_gap_hours"] * 3600):
        st["over_since"], st["blocked_since"] = None, None
    st["last_seen"] = now
    if an.get("state") == "none" or pct is None:
        st["over_since"] = None
        return {"action": "idle", "reason": "no swap configured"}
    # quick-refill bookkeeping for the last CONFIRMED automatic relief (circuit breaker); old refills age out
    lr = st.get("last_relief_at")
    if isinstance(lr, (int, float)) and not st.get("refill_checked"):
        if pct >= c["trigger_pct"] and now - lr <= c["refill_hours"] * 3600:
            st["refills"], st["refill_checked"], st["last_refill_at"] = int(st.get("refills", 0)) + 1, True, now
            if st["refills"] >= c["breaker_after_refills"]:
                st["breaker_until"], st["refills"] = now + c["breaker_days"] * 86400, 0
        elif now - lr > c["refill_hours"] * 3600:
            st["refills"], st["refill_checked"] = 0, True                  # it stayed down for the whole window: healthy
    lf = st.get("last_refill_at")
    if st.get("refills") and isinstance(lf, (int, float)) and now - lf > c["breaker_days"] * 86400:
        st["refills"] = 0                                                  # one quick refill long ago is not "in a row"
    bu = st.get("breaker_until")
    if isinstance(bu, (int, float)) and now < bu:
        return {"action": "breaker", "until": bu,
                "reason": f"swap back at {c['trigger_pct']:.0f}%+ within {c['refill_hours']:.0f} h of {int(c['breaker_after_refills'])} reliefs; "
                          f"auto relief paused until {time.strftime('%a %d %b %H:%M', time.localtime(bu))}"}
    quiet = an.get("state") in ("cold", "idle") and (an.get("out_mib_s") or 0.0) < c["quiet_out_mib_s"]
    share = cold_share(hold)
    keep_share = max(c["min_cold_share"] - 0.2, 0.3)                       # the watch survives small dips in the cold share, but not a real change of character
    start = pct >= c["trigger_pct"] and quiet and share >= c["min_cold_share"]
    keep = pct >= c["release_pct"] and quiet and share >= keep_share
    if st.get("over_since") is None:
        if start:
            st["over_since"] = now
    elif not keep:
        st["over_since"], st["blocked_since"] = None, None
    if st.get("over_since") is None:
        why = (f"swap {pct:.0f}% is below the {c['trigger_pct']:.0f}% watch level" if pct < c["trigger_pct"] else
               "swap is in use (not quiet)" if not quiet else f"only {share * 100:.0f}% of it is cold (needs {c['min_cold_share'] * 100:.0f}%)")
        return {"action": "idle", "reason": why}
    held_h = (now - st["over_since"]) / 3600
    if held_h < c["persist_hours"]:
        return {"action": "watching", "reason": f"swap {pct:.0f}% and quiet for {held_h:.1f} h; clears after {c['persist_hours']:g} h", "held_h": held_h}
    if share < c["min_cold_share"]:
        return {"action": "watching", "reason": f"cold share dipped to {share * 100:.0f}% (needs {c['min_cold_share'] * 100:.0f}% to act); waiting", "held_h": held_h}
    if isinstance(lr, (int, float)) and now - lr < c["min_interval_hours"] * 3600:
        return {"action": "wait", "reason": f"last automatic relief {(now - lr) / 3600:.1f} h ago (cooldown {c['min_interval_hours']:g} h)", "held_h": held_h}
    ra = st.get("retry_after")
    if isinstance(ra, (int, float)) and now < ra:
        return {"action": "wait", "reason": f"last attempt failed; retrying in {max(1, round((ra - now) / 60))} min", "held_h": held_h}
    if not in_window(now, c["window_start_hour"], c["window_end_hour"]):
        return {"action": "wait", "reason": f"outside the maintenance window ({c['window_start_hour']:g}:00-{c['window_end_hour']:g}:00)", "held_h": held_h}
    if plan is None:
        return {"action": "relieve", "reason": f"swap {pct:.0f}%, quiet and {share * 100:.0f}% cold for {held_h:.1f} h", "held_h": held_h, "need_plan": True}
    if not plan.get("safe"):
        if st.get("blocked_since") is None:                                # (setdefault would keep a stored None and crash below)
            st["blocked_since"] = now
        return {"action": "blocked", "reason": "; ".join(plan.get("blockers") or ["not safe"]), "held_h": held_h,
                "blocked_h": (now - st["blocked_since"]) / 3600}
    st["blocked_since"] = None
    return {"action": "relieve", "reason": f"swap {pct:.0f}%, quiet and {share * 100:.0f}% cold for {held_h:.1f} h", "held_h": held_h}


def swap_expected_but_off(proc: Path = PROC, fstab: Path = Path("/etc/fstab")) -> bool:
    """True when /etc/fstab lists swap but nothing is swapped on and no relief is running: an interrupted relief (or a failed boot-time swapon)."""
    if swap_areas(proc) or relief_active():
        return False
    for ln in (_read(fstab) or "").splitlines():
        f = ln.split("#", 1)[0].split()
        if len(f) >= 3 and f[2] == "swap":
            return True
    return False


# --------------------------------------------------------------------------- the CLI: homelab-maint swap [relieve [--apply]]
def snapshot(proc: Path = PROC, cg: Path = CGROUP, names: dict[str, str] | None = None, sample_s: float = 3.0,
             sleep: Callable[[float], None] = time.sleep) -> dict:
    """One full reading: memory, churn over `sample_s`, psi, holders, areas."""
    rr = RateReader(proc)
    rr.read()
    sleep(sample_s)
    rates = rr.read()
    mem = meminfo(proc)
    every = holders(cg, names, min_b=1)                     # ALL cgroups with swap: the cap checks must see the small ones too
    return {"mem": mem, "rates": rates, "psi": psi_mem(proc), "holders": [h for h in every if h["swap_b"] >= 32 * MIB], "all_holders": every,
            "areas": swap_areas(proc)}


def container_names() -> dict[str, str]:
    """{full id: name} of running containers (read-only `docker ps`); {} when docker cannot be asked."""
    try:
        from .tasks import gates
        info = gates.container_info() or {}
    except Exception:  # noqa: BLE001
        return {}
    return {c.id: n for n, c in info.items()}


def _eta(s: int) -> str:
    return f"{s} s" if s < 90 else f"{round(s / 60)} min"


def render(snap: dict, an: dict, plan: dict | None) -> str:
    lines = [f"swap: {human(an['used_b'])} of {human(an['total_b'])} used ({an['used_pct']}%), state {an['state'].upper()}"
             + (f"; swap-in {an['in_mib_s']} MiB/s, swap-out {an['out_mib_s']} MiB/s" if an["in_mib_s"] is not None else "")]
    m = snap["mem"]
    lines.append(f"RAM: {human(m.get('MemAvailable'))} available of {human(m.get('MemTotal'))}; memory stall (PSI full avg60) {snap['psi'].get('full60')}%")
    if an["exhausted"]:
        lines.append("WARNING: swap is exhausted and RAM is scarce: the next spike has no safety valve.")
    lines.append("")
    lines.append("who holds it (cgroup memory.swap.current, exact):")
    for h in snap["holders"][:12]:
        cap = "no cap" if not h["max_b"] else f"cap {human(h['max_b'])}"
        lines.append(f"  {human(h['swap_b']):>9}  {h['who']:<30} {h['kind']:<9} resident {human(h['current_b'])}, {cap}")
    if not snap["holders"]:
        lines.append("  (nothing above 32 MiB)")
    if plan is not None:
        lines.append("")
        if not plan.get("needed"):
            lines.append("relief: " + plan["why"])
        elif plan["safe"]:
            lines.append(f"relief: SAFE now. {human(plan['need_b'])} would return to RAM in about {_eta(plan['eta_s'])}; nothing is killed.")
            lines.append("  preview: homelab-maint swap relieve        (dry run)")
            lines.append("  do it:   sudo homelab-maint swap relieve --apply")
        else:
            lines.append("relief: NOT safe right now:")
            lines += [f"  - {b}" for b in plan["blockers"]]
    return "\n".join(lines)


def swap_main(argv: list[str] | None = None) -> int:
    """homelab-maint swap [status]            who holds the swap, what it is doing, whether it could be emptied safely
       homelab-maint swap relieve [--apply]   dry run of the safe relief; --apply (root) really does swapoff + swapon, with guards"""
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a and not a[0].startswith("-") else "status"
    flags = a[1:] if a and not a[0].startswith("-") else a
    if cmd not in ("status", "relieve") or any(f not in ("--apply", "--json", "--auto") for f in flags):
        print(swap_main.__doc__, file=sys.stderr)
        return 2
    names = container_names()
    snap = snapshot(names=names)
    an = analyse(snap["mem"], snap["rates"], snap["psi"])
    if an["state"] == "none":
        print("no swap configured")
        return 0
    plan = None
    if cmd == "relieve" or an["used_b"] > 0:
        busy: tuple[bool, str] | None
        try:
            from .tasks import gates
            busy = gates.busy("any")
        except Exception as exc:  # noqa: BLE001
            busy = (True, f"gate check failed: {type(exc).__name__}")
        areas = snap["areas"]
        disk = disk_of(areas[0]["path"]) if areas else None
        plan = relief_plan(snap["mem"], snap.get("all_holders", snap["holders"]), snap["psi"], areas, busy, disk_busy_pct(disk) if disk else None, relief_cfg(_loaded_cfg()))
    print(render(snap, an, plan))
    if cmd != "relieve" or "--apply" not in flags:
        return 0
    auto = "--auto" in flags
    if os.geteuid() != 0:
        print("error: --apply needs root (sudo homelab-maint swap relieve --apply)", file=sys.stderr)
        return 1
    if auto:                                    # the unattended path obeys the same switches as the task that launched it
        ok, why = _auto_allowed()
        if not ok:
            print(f"refusing: {why}. Nothing was changed.", file=sys.stderr)
            relief_refused("auto", why)
            return 1
    if not plan or not plan["safe"]:
        print("refusing: not safe right now (see above). Nothing was changed.", file=sys.stderr)
        if auto:
            relief_refused("auto", "; ".join((plan or {}).get("blockers") or ["not safe"]))
        return 1
    return _apply(snap["areas"], by="auto" if auto else "owner")


def _auto_revoked() -> str:
    """Why an unattended relief that is ALREADY RUNNING must stop now, or "" (an unreadable config does not stop it: the abort is only ever safe, but
    a config glitch is not worth a failed attempt)."""
    if core.paused("swap_auto_relief"):
        return "kill switch (PAUSE) set"
    try:
        t = (core.load_config().get("tasks") or {}).get("swap_auto_relief") or {}
    except Exception:  # noqa: BLE001
        return ""
    return "" if t.get("mode") == "apply" else "swap_auto_relief is no longer in apply mode"


def _auto_allowed() -> tuple[bool, str]:
    """May an UNATTENDED relief run right now? Only in apply mode and while the kill switch is absent (the owner's own relief is not asked)."""
    try:
        t = (core.load_config().get("tasks") or {}).get("swap_auto_relief") or {}
    except Exception:  # noqa: BLE001
        return False, "config unreadable"
    if t.get("mode") != "apply":
        return False, "swap_auto_relief is not in apply mode"
    if core.paused("swap_auto_relief"):
        return False, "paused (kill switch)"
    return True, ""


def _apply(areas: list[dict], by: str = "owner") -> int:
    """swapoff + swapon each used area, one at a time, aborting (signal -> the kernel keeps the area in service) the moment memory
    gets tight, and turning the area back on however this function ends (an exception, Ctrl-C and SIGTERM included; for a SIGKILL the
    systemd unit that the automation uses runs `swapon -a` afterwards). One relief at a time: the lock is held for the whole run."""
    import subprocess
    with relief_lock() as got:
        if not got or not relief_begin(by=by):
            print("another swap relief is running: nothing was changed", file=sys.stderr)
            relief_refused(by, "another relief is running")
            return 1
        rc, t0, used0 = 1, time.time(), sum(a["used_b"] for a in areas)

        def _term(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt                         # `systemctl stop`, RuntimeMaxSec: take the same safe path as Ctrl-C
        old = signal.signal(signal.SIGTERM, _term)
        try:
            rc = _apply_areas(areas, subprocess, by)
        finally:
            signal.signal(signal.SIGTERM, old)
            relief_end(ok=(rc == 0), note="" if rc == 0 else "aborted or failed (the swap stayed in service or was switched back on)")
        _journal(by, rc, used0, time.time() - t0)
        return rc


def _journal(by: str, rc: int, used_b: int, secs: float) -> None:
    """One line in the maintenance journal (the website's "what was done and when"); never raises."""
    try:
        from .routine import journal_note
        journal_note(("Swap relieved automatically" if by == "auto" else "Swap relieved by the owner") if rc == 0 else "Swap relief stopped early",
                     f"{human(used_b)} returned to RAM in {int(secs)} s (swapoff + swapon, nothing killed)" if rc == 0 else
                     "memory got tight or swapoff failed, so the swap stayed in service; see: journalctl -u homelab-maint-swap-relief")
    except Exception:  # noqa: BLE001
        pass


def _wait(p: Any) -> int | None:
    """Wait for a child however long it takes (swapoff in the kernel cannot be hurried); never give up while it is alive."""
    import subprocess
    while True:
        try:
            return p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            continue


def _apply_areas(areas: list[dict], subprocess: Any, by: str = "owner") -> int:
    rc = 0
    for area in [x for x in areas if x["used_b"] > 0]:
        path = area["path"]
        print(f"swapoff {path} ({human(area['used_b'])} to move back) ...", flush=True)
        p = subprocess.Popen(["ionice", "-c3", "nice", "-n19", "swapoff", path], stderr=subprocess.PIPE, text=True)
        t0, aborted, tick = time.monotonic(), None, 0
        try:
            while p.poll() is None:
                time.sleep(1.0)
                tick += 1
                mem, ps = meminfo(), psi_mem()
                floor = max(8 * GIB, 0.08 * mem.get("MemTotal", 0))        # clear of memory_health's warn/crit bands
                if mem.get("MemAvailable", 1 << 60) < floor:
                    aborted = f"available RAM fell under {human(floor)}"
                elif (ps.get("full60") or 0) >= 10:
                    aborted = "memory stall rose above 10%"
                elif time.monotonic() - t0 > 1800:
                    aborted = "took longer than 30 minutes"
                elif by == "auto" and tick % 5 == 0 and (revoked := _auto_revoked()):
                    aborted = revoked
                if aborted:
                    p.terminate()                      # swapoff returns EINTR and leaves the area in service
                    break
                left = next((x["used_b"] for x in swap_areas() if x["path"] == path), 0)
                print(f"  {human(left)} left in swap", flush=True)
            _wait(p)
        except KeyboardInterrupt:
            p.terminate()
            aborted = "interrupted"
            _wait(p)
        finally:
            held: set = set()
            try:
                held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})     # nothing may cut this short (a late signal waits here)
            except KeyboardInterrupt:                    # a signal whose handler was already pending ran inside the call: block again and carry on
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            try:
                if p.poll() is None:                    # never decide about swapon while swapoff is still alive
                    _wait(p)
                on = any(x["path"] == path for x in swap_areas())
                if not on:
                    r = subprocess.CompletedProcess([], 1, "", "")
                    for attempt in range(3):            # a transient failure must not leave the box without swap
                        r = subprocess.run(["swapon", path], capture_output=True, text=True)
                        if r.returncode == 0:
                            break
                        time.sleep(2.0)
                    print(f"swapon {path}: " + ("done, back in service empty" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}  <-- run: sudo swapon {path}"), flush=True)
                    rc = rc or r.returncode
                elif aborted or p.returncode:
                    print(f"{path} stayed in service" + (f" ({aborted})" if aborted else ""), flush=True)
            finally:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, held)                                  # a held signal is delivered HERE ...
                except KeyboardInterrupt:                                                             # ... after the swap is back: it must not turn a finished relief into a failed one
                    print("stop signal received while finishing: the swap is already back in service", flush=True)
        if aborted or p.returncode not in (0, None):
            rc = rc or 1
            if p.stderr:
                err = p.stderr.read().strip()
                if err:
                    print("swapoff said: " + err)
            break
    return rc
