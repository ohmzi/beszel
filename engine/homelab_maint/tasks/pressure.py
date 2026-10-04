"""pressure: spike management that never harms users (SPEC3 section 2, stream "spike").

pressure_state    C0 check   current LEVEL 0-5 from PSI, MemAvailable, swap-in, GPU memory (load is shown, not scored),
                             with hysteresis, plus a ledger of spike events (STATE_DIR/spikes.jsonl)
pressure_response C1 check   walks the response ladder for the current level, one gated, budgeted, audited rung at a time
qos_classes       C1 daily   baseline cpu-shares per class (report-only by default)
bulkhead_check    C0 weekly  drift of agreed concurrency limits (Ollama, ComfyUI, Open Notebook, Plex), read-only
export()                     JSON for the website (pressure.json)

The ladder (each rung has its own mode, in [tasks.pressure_response]: "apply" | "report" | "off"):
    L0 normal     nothing
    L1 annotate   note the spike and who caused it (the log row and pressure_state's alert; never an action)
    L2 reclaim    unload IDLE Ollama models, ComfyUI /free only with an empty queue      (default: apply)
    L3 slow batch lower cpu-shares of P3 then P2 batch containers, restore at level 0   (default: report)
    L4 restart    ONE proven-stuck, unprotected, non-database P2/P3 container, backoff + budget (default: report)
    L5 emergency  stop containers from the explicit `emergency_stop` list, one per run   (default: report)
Run with `--apply` for anything but "would" rows: without it every rung is report-only, and `mode = "report"` on the
task forces the same. PAUSE stops every new action and releases what the ladder changed: throttles at once, emergency-
stopped containers once memory is below L4 (or after max_stop_min), so "paused" never starts a hog back into a live stall.

Decisions that come from what this host actually does (all measured read-only on 2026-10-01):
  * Saturation per RESOURCE, not one number. Memory, io, cpu and gpu are leveled separately (hysteresis per resource)
    and a rung only fires for the resource it can relieve: reclaim needs memory or GPU pressure, restart and
    emergency need memory pressure, io and cpu are capped at L3 and answered by slowing batch work. Right now the
    host has io PSI full ~75% (a `find` on a spinning disk and an NTFS mount) with memory PSI ~0: that must never
    unload a model or restart a container.
  * Load average is reported but does not set the level: that IO storm shows load 33 on 24 cores with 87% CPU idle,
    because the 30 tasks in D state count towards the load.
  * blkio-weight is a no-op here (disks run `none`/`mq-deadline`, io.cost is off, no io.bfq.weight), so it is only
    used where a block device runs bfq or io.cost.qos is enabled. cpu-shares work, but runc 1.2.5 maps them LINEARLY
    to cpu.weight (1024 -> 39 < the unset default 100; runc >= 1.3: 1024 -> 100), and `docker update --cpu-shares 0`
    changes nothing, so "restore the default" cannot be a constant: the throttle records the container's real
    cpu.weight and restore() tries candidate share values until the cgroup reads that weight again.
  * An existing comfyui-idle-vram.timer restarts an idle ComfyUI that holds > 3 GB of VRAM, and mem-guard.py is
    dry-run only. L2 never restarts anything; ComfyUI's /free is the gentler attempt and runs only with an empty queue.
  * Ollama here runs OLLAMA_KEEP_ALIVE=60s, so most idle models vanish by themselves; L2 matters for clients that
    send a long keep_alive. A model whose expires_at is already in the past is being used (or is unloading): skipped.
  * Two levels are published. `level` (max over all four resources) is for the dashboard, labelled by what drives it
    ("io stall", not "slow batch"). `gate_level` = max(mem, cpu) is what every OTHER consumer must read (scheduler,
    routine pressure gate, live page, SLO): this host holds io PSI at L3 for hours every night (a `find` on a spinning
    disk) and nobody waits on that, so io-only or GPU-only pressure is status "info" and gate_level 0. Measured on
    9.5 h of real samples: level 3 for 32 of 39 runs, which as "warn" burnt a 99% SLO to 15% and deferred every
    P2/P3 job and backup.
  * What a rung changed is undone by the RESOURCE it answered, not by the host level (which io and gpu keep high):
    throttles are released once memory and cpu are below L3 (blkio weights: once io is), emergency stops once memory
    is below L4, and no stop lasts longer than `max_stop_min` (then a cooldown keeps L5 from repeating it). A throttle
    that cannot be restored, or a stopped container that cannot be started, is a visible failure (row, warn, alert);
    a stopped container that no longer exists, was recreated, or was started by its owner is dropped from the books.
    Flipping the throttle rung to report, or PAUSE while memory is still L4+, never starts an emergency-stopped container.
  * "Unknown => P2" is only for display. Every rung that acts (L3 slow, L4 restart, L5 stop, qos baselines) requires an
    EXPLICIT class match in classes.toml; an unclassified container is reported (pressure_state metrics, pressure.json,
    the weekly bulkhead_check) and at L4 it is alert-only.

Files written (the tool's own state, never the host): STATE_DIR/spikes.jsonl holds ONE line per finished spike (the
spike in progress lives in tasks/pressure_state.json and is added by export()); STATE_DIR/pressure-log.jsonl holds one
row per ladder action ({"ts","level","rung","action","target","class","outcome"}), explanatory rows once per spike.

Every mutation goes through ctx.act (audit, protected.toml, caps, PAUSE). Two deliberate exceptions, both only
UNDOING our own recorded change: releasing throttles and starting back containers we stopped, because ctx.act refuses
while PAUSE exists and "paused" must not mean "left slowed". Those honour --dry-run (no --apply, no change).
The only extra read-only calls are `docker inspect --type container` (is a stopped container still there?).
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import time
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import core, swapwatch
from ..core import GIB, CapExceeded, Ctx, Result, audit, human, read_json, sh, task
from . import gates, guard

CLASSES = ("P0", "P1", "P2", "P3")
DIMS = ("mem", "io", "cpu", "gpu")
LEVEL_NAMES = ("normal", "annotate", "reclaim", "slow batch", "restart", "emergency")
RUNG_DEFAULTS = {"reclaim": "apply", "throttle": "report", "restart": "report", "emergency": "report"}
MAX_PER_DAY = {"reclaim": 12, "throttle": 24, "restart": 4, "emergency": 3}      # [ladder.max_per_day] overrides
LADDER_TEXT = (
    (0, "normal", "Nothing to do."),
    (1, "annotate", "Note the spike and who caused it. Only memory pressure sends an alert."),
    (2, "reclaim", "Unload idle Ollama models and free ComfyUI VRAM when its queue is empty. Nothing else is dropped."),
    (3, "slow batch", "Lower CPU priority of best-effort (P3) then batch (P2) containers. Nothing is paused or killed."),
    (4, "restart", "Restart ONE proven-stuck, unprotected P2/P3 container, with backoff and a budget of 2 per 6 h."),
    (5, "emergency", "Stop best-effort containers from an explicit list, one per run, only after a long memory stall."),
)
POLICY_DEFAULT = {
    "P0": "Platform. Never throttled, restarted or stopped by the ladder.",
    "P1": "Interactive and serving. Protected, highest CPU weight, never slowed, restarted or stopped.",
    "P2": "Batch and background. Slowed second, restarted only when proven stuck and unprotected.",
    "P3": "Best effort. Slowed first, the only class an emergency list may stop.",
}
# (dim, direction, enter thresholds for L1..L5, leave thresholds, highest level this signal can reach). "hi": larger
# is worse. Leave thresholds are less strict than enter ones, which is the hysteresis band. Overridable per signal
# under [ladder.signals.<name>] in classes.toml.
SIGNALS: dict[str, tuple[str, str, list[float], list[float], int]] = {
    "mem_full60": ("mem", "hi", [1, 3, 8, 15, 30], [0.5, 2, 5, 10, 20], 5),
    "mem_some60": ("mem", "hi", [10, 20, 35, 50, 70], [6, 14, 25, 40, 55], 3),
    "mem_avail_gib": ("mem", "lo", [20, 12, 8, 5, 3], [24, 15, 10, 7, 4], 5),
    "swap_in_pps": ("mem", "hi", [300, 1000, 2500, 6000, 15000], [150, 600, 1500, 4000, 10000], 3),
    "io_full60": ("io", "hi", [10, 25, 45, 65, 85], [6, 15, 30, 45, 65], 3),
    "io_some60": ("io", "hi", [30, 50, 70, 85, 95], [20, 40, 60, 75, 85], 2),
    "cpu_some60": ("cpu", "hi", [30, 50, 70, 85, 95], [20, 35, 55, 75, 85], 3),
    "gpu_vram_pct": ("gpu", "hi", [85, 92, 97, 99, 101], [80, 88, 94, 97, 99], 2),
}
_SIG_TEXT = {"mem_full60": "memory stall {:.1f}%", "mem_some60": "memory wait {:.0f}%", "mem_avail_gib": "{:.1f} GiB avail",
             "swap_in_pps": "swap-in {:.0f}/s", "io_full60": "io stall {:.0f}%", "io_some60": "io wait {:.0f}%",
             "cpu_some60": "cpu wait {:.0f}%", "gpu_vram_pct": "vram {:.0f}%"}

# Indirections so tests never sleep, touch the network or look at the real host.
_sleep = time.sleep
SYSBLOCK = Path("/sys/block")
STATE_FMT = "{{.Id}}|{{.State.Running}}"       # `docker inspect --type container` answer for an existence/identity check
MAX_STOP_MIN, STOP_COOLDOWN_MIN, START_TRIES = 60, 360, 3      # [ladder] max_stop_min / stop_cooldown_min override the first two


def gate_level(eff: dict) -> int:
    """The level other streams gate on: memory and cpu only. An io stall or a full GPU is real but no lever of ours
    relieves it and nobody waits on it, so it must not defer maintenance, hold the routine or burn the SLO."""
    return max(int(eff.get("mem", 0) or 0), int(eff.get("cpu", 0) or 0))


def level_label(level: int, eff: dict) -> str:
    """Name of a level. When only io/gpu drive it, name the resource instead of a rung that cannot answer it."""
    if level <= 0:
        return LEVEL_NAMES[0]
    if gate_level(eff) >= level:
        return LEVEL_NAMES[min(level, 5)]
    parts = (["io stall" if level >= 3 else "io wait"] if int(eff.get("io", 0) or 0) >= level else []) \
        + (["vram full"] if int(eff.get("gpu", 0) or 0) >= level else [])
    return " + ".join(parts) or LEVEL_NAMES[min(level, 5)]


def _cores() -> int:
    return os.cpu_count() or 1


def _a(s: Any, n: int = 140) -> str:
    """ASCII only and short: summaries may become an SMS, rows go to a public page."""
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def _num(v: Any, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


# =========================================================================== class map (etc/classes.toml)
_REPO_ETC = Path(__file__).resolve().parents[2] / "etc"


class ClassMap:
    """classes.toml, compiled. `ok` is False when the file is missing/unreadable or any pattern is broken: the
    destructive rungs (L3-L5) then refuse to run, because without a trustworthy map "unknown => P2" would put
    P0/P1 containers in reach of a throttle."""

    def __init__(self, raw: dict | None):
        self.raw = raw if isinstance(raw, dict) else {}
        self.ok = isinstance(self.raw.get("classes"), dict) and bool(self.raw.get("classes"))
        self._rx: dict[str, dict[str, list]] = {"classes": {}, "units": {}}
        for sect in self._rx:
            tbl = self.raw.get(sect)
            tbl = tbl if isinstance(tbl, dict) else {}
            for c in CLASSES:
                rxs = []
                for p in tbl.get(c, []) if isinstance(tbl.get(c, []), list) else []:
                    try:
                        rxs.append(re.compile(p, re.I))
                    except (re.error, TypeError):
                        self.ok = False
                self._rx[sect][c] = rxs
        lad = self.raw.get("ladder")
        self.lad: dict = lad if isinstance(lad, dict) else {}

    def of(self, name: str, kind: str = "classes") -> tuple[str, bool]:
        """(class, matched explicitly). Unknown => P2. First match wins in P0, P1, P2, P3 order."""
        for c in CLASSES:
            if any(r.search(name or "") for r in self._rx[kind][c]):
                return c, True
        return "P2", False

    def cls(self, name: str, kind: str = "classes") -> str:
        return self.of(name, kind)[0]

    def n(self, key: str, default: float) -> float:
        return _num(self.lad.get(key), default)

    def sub(self, table: str, key: str, default: float) -> float:
        t = self.lad.get(table)
        return _num(t.get(key), default) if isinstance(t, dict) else default

    def section(self, name: str) -> dict:
        v = self.raw.get(name)
        return v if isinstance(v, dict) else {}


def load_classes() -> ClassMap:
    """CONF_DIR/classes.toml, else the copy shipped next to the source tree (not installed yet), else an empty map."""
    for p in (core.CONF_DIR / "classes.toml", _REPO_ETC / "classes.toml"):
        try:
            raw = core.load_toml(p)
        except Exception:  # noqa: BLE001  - a syntax error must not crash the runner
            raw = {}
        if raw:
            return ClassMap(raw)
    return ClassMap({})


# =========================================================================== levels and hysteresis (pure)
def signals_cfg(cm: ClassMap) -> dict[str, tuple[str, str, list[float], list[float], int]]:
    """SIGNALS with valid per-signal overrides applied. An invalid override (wrong length, not monotonic, leave
    stricter than enter) is ignored, never half-applied."""
    out = dict(SIGNALS)
    ov = cm.lad.get("signals")
    for name, o in (ov.items() if isinstance(ov, dict) else []):
        if name not in SIGNALS or not isinstance(o, dict):
            continue
        dim, kind, enter, leave, mx = SIGNALS[name]
        e, l_, m = o.get("enter", enter), o.get("leave", leave), int(_num(o.get("max"), mx))
        if not all(isinstance(x, list) and len(x) == 5 and all(isinstance(v, (int, float)) for v in x) for x in (e, l_)):
            continue
        asc = kind == "hi"
        mono = all((a <= b) if asc else (a >= b) for a, b in zip(e, e[1:]))
        band = all((lv <= ev) if asc else (lv >= ev) for ev, lv in zip(e, l_))
        if mono and band and 1 <= m <= 5:
            out[name] = (dim, kind, list(e), list(l_), m)
    return out


def raw_levels(sigs: dict, vals: dict[str, float | None], caps: dict[str, int]) -> dict[str, dict]:
    """Per resource: {"enter": L, "leave": L, "why": [(level, signal, value)]}. `enter` is what the signals reach with
    the enter thresholds, `leave` what they still hold with the lower leave thresholds (always >= enter). A signal that
    could not be read is skipped: unknown is not pressure."""
    out = {d: {"enter": 0, "leave": 0, "why": []} for d in DIMS}
    for name, (dim, kind, enter, leave, mx) in sigs.items():
        v = vals.get(name)
        if v is None:
            continue
        hit = (lambda t, v=v: v >= t) if kind == "hi" else (lambda t, v=v: v <= t)
        le = min(sum(1 for t in enter if hit(t)), mx, caps[dim])
        ll = max(min(sum(1 for t in leave if hit(t)), mx, caps[dim]), le)
        o = out[dim]
        o["enter"], o["leave"] = max(o["enter"], le), max(o["leave"], ll)
        if le:
            o["why"].append((le, name, v))
    return out


def hysteresis(st: dict, enter_raw: int, leave_raw: int, enter_runs: int, leave_runs: int) -> int:
    """Advance one resource's state by one run and return its effective level.
    Up: the level becomes min(last `enter_runs` enter-levels) once they are all above the current level.
    Down: only when the last `leave_runs` leave-levels are all BELOW the current level, and then to the highest of them.
    The two bands differ, so a signal hovering at a threshold cannot flap the level."""
    lvl = int(st.get("level", 0))
    eh = (list(st.get("eh", [])) + [enter_raw])[-max(enter_runs, 1):]
    lh = (list(st.get("lh", [])) + [leave_raw])[-max(leave_runs, 1):]
    if len(eh) >= enter_runs and min(eh) > lvl:
        lvl = min(eh)
    if lvl > 0 and len(lh) >= leave_runs and max(lh) < lvl:
        lvl = max(lh)
    st.update(level=lvl, eh=eh, lh=lh)
    return lvl


# =========================================================================== host readers
def _gpu_mem() -> tuple[float, float] | None:
    """(used MiB, total MiB) summed over GPUs, None when nvidia-smi is missing or fails (unknown is not pressure)."""
    r = sh(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"], timeout=8)
    if r.returncode != 0:
        return None
    used = total = 0.0
    for ln in r.stdout.splitlines():
        try:
            u, t = (float(x.strip()) for x in ln.split(","))
        except ValueError:
            continue
        used, total = used + u, total + t
    return (used, total) if total > 0 else None


def read_host(ctx: Ctx) -> dict | None:
    """Everything the level needs, from /proc (gates.PROC) and nvidia-smi. None when PSI is unreadable. Updates the
    swap/oom baseline in ctx.state["vm"]; the swap-in rate is the average since the previous run."""
    P = gates.PROC
    psi = {k: guard.parse_psi(gates.read_text(P / "pressure" / k)) for k in ("memory", "io", "cpu")}
    if "some" not in psi["memory"] or "some" not in psi["io"]:
        return None
    mi, vm = guard._meminfo(), guard._vmstat()
    g = lambda r, kind, key="avg60": float(psi[r].get(kind, {}).get(key, 0.0))      # noqa: E731
    pin, oom = vm.get("pswpin"), vm.get("oom_kill")
    prev = ctx.state.get("vm") or {}
    rate = None
    if pin is not None and isinstance(prev.get("t"), (int, float)) and 60 <= ctx.now - prev["t"] <= 3 * 3600 \
            and pin >= prev.get("pswpin", 0):
        rate = swapwatch.discount(pin - prev["pswpin"], prev["t"], ctx.now) / (ctx.now - prev["t"])     # a relief the owner ran on purpose is not pressure
    ctx.state["vm"] = {"t": ctx.now, "pswpin": pin, "oom_kill": oom}
    try:
        load1 = float((gates.read_text(P / "loadavg") or "").split()[0])
    except (IndexError, ValueError):
        load1 = None
    gm = _gpu_mem()
    avail = mi.get("MemAvailable")
    return {"vals": {"mem_full60": g("memory", "full"), "mem_some60": g("memory", "some"),
                     "mem_avail_gib": None if avail is None else avail / GIB, "swap_in_pps": rate,
                     "io_full60": g("io", "full"), "io_some60": g("io", "some"), "cpu_some60": g("cpu", "some"),
                     "gpu_vram_pct": None if gm is None else gm[0] / gm[1] * 100},
            "full10": g("memory", "full", "avg10"), "load1": load1,
            "load_ratio": None if load1 is None else round(load1 / _cores(), 2), "oom": oom, }


def _d_rows() -> list[tuple[str, int]]:
    """[(name, count)] of tasks in uninterruptible IO wait, most numerous first, e.g. [("find", 2)]: WHAT an io stall is
    when no container is doing the io. [] when /proc is unreadable."""
    try:
        cnt: dict[str, int] = {}
        for p in gates.scan_procs():
            if p.state == "D":
                cnt[p.exe[:24]] = cnt.get(p.exe[:24], 0) + 1
    except OSError:
        return []
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:3]


def _d_state() -> list[str]:
    return [f"{n} x{c}" for n, c in _d_rows()]


def running_units() -> list[str]:
    r = sh(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"], timeout=15)
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.split()] if r.returncode == 0 else []


def unclassified(cm: ClassMap, info: dict | None) -> list[str]:
    """Running containers no classes.toml pattern names. They show as P2 on the class table, but no rung acts on them."""
    return sorted((n for n in (info or {}) if not cm.of(n)[1]), key=str.lower)


def members(cm: ClassMap, info: dict | None = None) -> dict[str, list[str]] | None:
    """{class: [running container names and service units]} for the website's class table; None if docker is down.
    `info` (gates.container_info()) may be passed in by a caller that already asked docker."""
    info = gates.container_info() if info is None else info
    if info is None:
        return None
    out: dict[str, list[str]] = {c: [] for c in CLASSES}
    for n in sorted(info, key=str.lower):
        out[cm.cls(n)].append(n)
    for u in sorted(running_units()):
        out[cm.cls(u, "units")].append(u)
    return {c: v[:80] for c, v in out.items()}


# =========================================================================== who is causing it
def activity(now: float, window_s: float = 3000) -> dict[str, dict]:
    """Per running container from the guard samples: anon/swap, cpu %, io MiB/s and anon+swap growth GiB/h over the
    window (cpu/io/growth are 0 when only one sample or counters went backwards, i.e. unknown)."""
    recs = [r for r in guard.read_samples(window_s, now) if isinstance(r.get("c"), dict) and r["c"]]
    if not recs:
        return {}
    first, last = recs[0], recs[-1]
    dt = last["t"] - first["t"]
    out: dict[str, dict] = {}
    for name, c in last["c"].items():
        row = {"anon": int(c.get("anon", 0)), "swap": int(c.get("swap", 0)), "cpu_pct": 0.0, "io_mib_s": 0.0,
               "growth": 0.0}
        a = first["c"].get(name) if first is not last else None
        if a and dt >= 60:
            try:
                if c["cpu_us"] >= a["cpu_us"]:
                    row["cpu_pct"] = (c["cpu_us"] - a["cpu_us"]) / (dt * 1e6) * 100
                if c["io_b"] >= a["io_b"]:
                    row["io_mib_s"] = (c["io_b"] - a["io_b"]) / (1024 ** 2) / dt
                row["growth"] = ((c["anon"] + c.get("swap", 0)) - (a["anon"] + a.get("swap", 0))) / GIB / (dt / 3600)
            except (KeyError, TypeError):
                pass
        out[name] = row
    out["\0host"] = {"p": last.get("p") or []}                       # host (non-container) top processes
    return out


def contributors(act: dict, cm: ClassMap, eff: dict[str, int], n: int = 5,
                 d_rows: list[tuple[str, int]] | None = None) -> list[dict]:
    """Top contributors by the resource that is under pressure: [{name, class, anon_gib, cpu_pct}] (spec keys only).
    Memory pressure: fastest growth, then largest. IO: the containers moving data; when none is, the host tasks stuck
    in IO wait (class "host", e.g. a `find` or `sha256sum` on a spinning disk). CPU: the busiest."""
    rows = {k: v for k, v in act.items() if not k.startswith("\0")}
    pick: list[str] = []

    def top(key, k, floor):
        pick.extend(n_ for n_, v in sorted(rows.items(), key=lambda kv: (-key(kv[1]), kv[0]))[:k]
                    if key(v) > floor and n_ not in pick)

    if eff.get("mem", 0) >= 1:
        top(lambda v: v["growth"], 3, 0.5)
        top(lambda v: v["anon"] + v["swap"], 2, 1 * GIB)
    io_found = False
    if eff.get("io", 0) >= 1:
        before = len(pick)
        top(lambda v: v["io_mib_s"], 2, 1.0)
        io_found = len(pick) > before
    if eff.get("cpu", 0) >= 1:
        top(lambda v: v["cpu_pct"], 2, 25.0)
    if not pick and eff.get("mem", 0) >= 1:          # memory pressure but nothing growing or big: the largest, for context
        top(lambda v: v["anon"], 3, 0)
    out = [{"name": k[:40], "class": cm.cls(k) if cm.of(k)[1] else "unclassified", "anon_gib": round(rows[k]["anon"] / GIB, 1),
            "cpu_pct": round(rows[k]["cpu_pct"], 1)} for k in pick[:n]]
    host = (act.get("\0host") or {}).get("p") or []
    if eff.get("mem", 0) >= 1 and host and isinstance(host[0], list) and host[0][2] * 1024 >= 1 * GIB:
        out.append({"name": str(host[0][0])[:40], "class": "host", "anon_gib": round(host[0][2] * 1024 / GIB, 1),
                    "cpu_pct": 0.0})
    if eff.get("io", 0) >= 1 and not io_found:
        out.extend({"name": nm[:40], "class": "host", "anon_gib": 0.0, "cpu_pct": 0.0} for nm, _c in (d_rows or [])[:2])
    return out[:n + 1]


# =========================================================================== small jsonl stores
def _append_jsonl(path: Path, rec: dict, max_bytes: int = 300_000, keep: int = 500) -> None:
    """Append one compact line; past `max_bytes` rewrite (atomically) keeping the newest `keep` lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    try:
        if path.stat().st_size > max_bytes:
            lines = path.read_text().splitlines()[-keep:]
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, path)
    except OSError:
        pass


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for ln in path.read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    except OSError:
        pass
    return out


def spikes_path() -> Path:
    return core.STATE_DIR / "spikes.jsonl"


def log_path() -> Path:
    return core.STATE_DIR / "pressure-log.jsonl"


def _task_state(name: str) -> dict:
    d = read_json(core.STATE_DIR / "tasks" / f"{name}.json", {})
    return d if isinstance(d, dict) else {}


# =========================================================================== pressure_state
def _outcome(start: float, end: float) -> dict:
    """What the ladder did during a spike, from pressure-log.jsonl: counts and a plain sentence."""
    rows = [r for r in _read_jsonl(log_path()) if start <= r.get("ts", 0) <= end + 1]
    done = [r for r in rows if r.get("outcome") == "done"]
    restarted = [r["target"] for r in done if r.get("rung") == "L4"]
    stopped = [r["target"] for r in done if r.get("rung") == "L5"]
    reclaimed = sum(1 for r in done if r.get("rung") == "L2")
    throttled = sum(1 for r in done if r.get("rung") == "L3")
    would = sum(1 for r in rows if r.get("outcome") == "would")
    if restarted or stopped:
        text = "handled: " + ", ".join([f"restarted {', '.join(restarted)}"] * bool(restarted)
                                        + [f"stopped {', '.join(stopped)}"] * bool(stopped))
    elif throttled or reclaimed:
        text = "handled: " + ", ".join([f"reclaimed {reclaimed} idle item(s)"] * bool(reclaimed)
                                        + [f"slowed {throttled} batch container(s)"] * bool(throttled)) \
            + ", nothing killed"
    else:
        text = "resolved by itself, nothing killed" + (f" ({would} action(s) logged as would-do)" if would else "")
    return {"outcome": _a(text, 160), "restarted": restarted, "stopped": stopped, "reclaimed": reclaimed,
            "throttled": throttled, "would": would}


def _spike_record(sp: dict, state: str, end: float | None, now: float, oom_now: int | None) -> dict:
    rec = {"t": sp["start"], "kind": "spike", "id": sp["id"], "state": state, "end": end,
           "peak_level": sp["peak"], "gate_peak": gate_level(sp["dims"]),      # 0: only io/gpu were under pressure
           "duration_s": int((end or now) - sp["start"]), "dims": sp["dims"],
           "psi": sp["psi"], "contributors": sp["contrib"], "oom_kills": max((oom_now or 0) - (sp.get("oom0") or 0), 0)}
    if state == "closed":
        rec.update(_outcome(sp["start"], end or now))
        rec["nothing_killed"] = not rec["restarted"] and not rec["stopped"] and rec["oom_kills"] == 0
    else:
        rec.update(outcome="in progress", nothing_killed=True, restarted=[], stopped=[], reclaimed=0, throttled=0)
    return rec


def _why(rl: dict, eff: dict) -> str:
    parts = []
    for d in DIMS:
        if eff[d] > 0:
            top = max(rl[d]["why"], key=lambda w: w[0], default=None)
            parts.append(_SIG_TEXT[top[1]].format(top[2]) if top else f"{d} holding")
    return _a(", ".join(parts) or "no pressure", 100)


@task("pressure_state", klass="C0", tier="check", title="Load pressure level", timeout=60)
def pressure_state(ctx: Ctx) -> Result:
    cm = load_classes()
    h = read_host(ctx)
    if h is None:
        return Result("error", "cannot read /proc/pressure (memory/io): pressure level unknown")
    caps = {"mem": 5, "io": int(cm.n("io_max_level", 3)), "cpu": int(cm.n("cpu_max_level", 3)),
            "gpu": int(cm.n("gpu_max_level", 2))}
    rl = raw_levels(signals_cfg(cm), h["vals"], caps)
    er, lr = int(cm.n("enter_runs", 2)), int(cm.n("leave_runs", 3))
    ds = ctx.state.setdefault("dims", {})
    eff = {d: hysteresis(ds.setdefault(d, {}), rl[d]["enter"], rl[d]["leave"], er, lr) for d in DIMS}
    level = max(eff.values())
    gate = gate_level(eff)                       # what the scheduler, routine, live page and SLO must read (see docstring)
    label = level_label(level, eff)
    if level != ctx.state.get("level"):
        ctx.state["since"] = ctx.now
    ctx.state["level"] = level
    ctx.state["gate_level"] = gate
    ctx.state["level_name"] = label
    ctx.state["t"] = ctx.now
    # host-level stall clock for L5: how long the memory resource has been continuously at L4 or worse
    if eff["mem"] >= 4:
        ctx.state.setdefault("mem4_since", ctx.now)
    else:
        ctx.state.pop("mem4_since", None)
    stall_min = round((ctx.now - ctx.state["mem4_since"]) / 60, 1) if "mem4_since" in ctx.state else 0.0
    ctx.state["stall_min"] = stall_min
    hist = [x for x in ctx.state.get("history", []) if isinstance(x, dict) and ctx.now - x.get("t", 0) < 86400]
    ctx.state["history"] = (hist + [{"t": round(ctx.now), "level": level}])[-100:]

    # who: contributors always while pressured, members of each class refreshed every run
    info = gates.container_info()
    mem = members(cm, info) if info is not None else None
    if mem is not None:
        ctx.state["members"] = mem
        ctx.state["unclassified"] = unclassified(cm, info)[:40]
    unc = list(ctx.state.get("unclassified") or [])
    d_rows = _d_rows() if rl["io"]["enter"] >= 1 else []
    d_state = [f"{n_} x{c}" for n_, c in d_rows]
    contrib = contributors(activity(ctx.now), cm, eff, d_rows=d_rows) if level >= 1 else []
    v = h["vals"]

    # ---- spike ledger: open at the first confirmed pressure, close when the level is back to 0
    sp = ctx.state.get("spike")
    if sp and ctx.now - sp["start"] >= cm.n("spike_max_hours", 6) * 3600:
        # An episode that lasts for hours (this host sits at io PSI 50% while a scan runs) is recorded in slices, so
        # the daily report does not have to wait for it to end. A fresh spike opens right below if pressure continues.
        _append_jsonl(spikes_path(), _spike_record(sp, "closed", ctx.now, ctx.now, h["oom"]))
        ctx.state.pop("spike", None)
        sp = None
    if level >= 1:
        if not sp:
            sp = {"id": int(ctx.now), "start": ctx.now, "peak": level, "dims": {}, "psi": {}, "contrib": contrib,
                  "oom0": (ctx.state.get("oom_prev") if isinstance(ctx.state.get("oom_prev"), int) else h["oom"])}
        if level >= sp["peak"]:
            sp["peak"], sp["contrib"] = level, contrib
        for d in DIMS:
            sp["dims"][d] = max(sp["dims"].get(d, 0), eff[d])
        for k in ("mem_full60", "mem_some60", "io_full60", "io_some60", "cpu_some60"):
            if v.get(k) is not None:
                sp["psi"][k] = max(sp["psi"].get(k, 0), round(v[k], 1))
        ctx.state["spike"] = sp                        # an open spike lives in the task state; export() shows it
    elif sp:
        # ONE line per spike, written when it closes: reports.py counts lines of spikes.jsonl and does not dedupe
        _append_jsonl(spikes_path(), _spike_record(sp, "closed", ctx.now, ctx.now, h["oom"]))
        ctx.state.pop("spike", None)
    ctx.state["oom_prev"] = h["oom"]

    # status follows the gate level: io-only or gpu-only pressure is "info" (it can still be seen as `level`), because
    # history.jsonl, the SLO and the incident ledger only see the status word, not the alert flag
    status = "crit" if gate >= 4 else "warn" if gate >= 2 else "info" if level >= 1 else "ok"
    why = _why(rl, eff)
    metrics = {"level": level, "gate_level": gate, "level_name": label, "why": why, "dims": eff, "stall_min": stall_min,
               "psi_mem_some60": v["mem_some60"], "psi_mem_full60": v["mem_full60"], "psi_mem_full10": h["full10"],
               "psi_io_some60": v["io_some60"], "psi_io_full60": v["io_full60"], "psi_cpu_some60": v["cpu_some60"],
               "mem_avail_gib": None if v["mem_avail_gib"] is None else round(v["mem_avail_gib"], 1),
               "swap_in_pps": None if v["swap_in_pps"] is None else round(v["swap_in_pps"]),
               "load1": h["load1"], "load_ratio": h["load_ratio"],
               "gpu_vram_pct": None if v["gpu_vram_pct"] is None else round(v["gpu_vram_pct"]),
               "d_state": d_state, "top_contributors": contrib, "spike_open": bool(ctx.state.get("spike")),
               "unclassified": unc[:8], "unclassified_n": len(unc), "enter_runs": er, "leave_runs": lr}
    if level == 0:
        av = metrics["mem_avail_gib"]
        summary = f"L0 normal: mem stall {v['mem_full60']:.1f}%, io wait {v['io_some60']:.0f}%" \
                  + (f", {av} GiB avail" if av is not None else "")
    else:
        who = f"; top: {contrib[0]['name']} {contrib[0]['class']}" if contrib else ""
        summary = f"L{level} {label}{' (info only)' if gate == 0 else ''}: {why}{who}" \
            + (f" (io: {', '.join(d_state)})" if d_state else "")
    # Only memory pressure pages. IO/CPU saturation has no lever a person could pull in minutes and is on the live
    # page; GPU memory full is routine with ComfyUI + Plex + Immich ML sharing one card.
    return Result(status, _a(summary), metrics, [dict(c) for c in contrib[:8]], alert=eff["mem"] >= 2)


# =========================================================================== rows, effects, cgroup weights
def _row(ts: float, level: int, rung: str, action: str, target: str, cls: str, outcome: str) -> dict:
    return {"ts": round(ts, 1), "level": level, "rung": rung, "action": _a(action, 80), "target": _a(target, 60),
            "class": cls, "outcome": _a(outcome, 100)}


def io_weights_effective() -> bool:
    """True only when some block device runs bfq or io.cost.qos is enabled; otherwise io.weight/blkio-weight is ignored."""
    try:
        for p in SYSBLOCK.glob("*/queue/scheduler"):
            if re.search(r"\[bfq\]", gates.read_text(p) or ""):
                return True
    except OSError:
        pass
    return bool(re.search(r"\benable=1\b", gates.read_text(gates.CGROUP / "io.cost.qos") or ""))


def mem_reservation_effective() -> bool:
    """memory.low of a container only protects it if the parent slice has a memory.low too (cgroup-v2 hierarchy)."""
    t = (gates.read_text(gates.CGROUP / "system.slice" / "memory.low") or "0").strip()
    return t == "max" or (t.isdigit() and int(t) > 0)


def weight_linear(shares: int) -> int:
    """cpu-shares -> cgroup-v2 cpu.weight as runc <= 1.2.5 (installed here) converts it."""
    return 1 + ((shares - 2) * 9999) // 262142


def weight_quadratic(shares: int) -> int:
    """... as runc >= 1.3 converts it (default 1024 -> 100)."""
    l = math.log2(max(shares, 2))
    return math.ceil(10 ** ((l * l + 125 * l) / 612 - 7 / 34))


def _shares_for_weight_linear(w: int) -> int:
    return 2 + -(-(w - 1) * 262142 // 9999)


INSPECT_FMT = ("{{.Name}}|{{.Id}}|{{.HostConfig.CpuShares}}|{{.HostConfig.BlkioWeight}}|"
               "{{.HostConfig.MemoryReservation}}|{{.HostConfig.Memory}}")


def inspect_hc(names: list[str]) -> dict[str, dict] | None:
    """{name: {id, shares, blkio, resv, mem}} from `docker inspect` (read-only); partial output is accepted."""
    out: dict[str, dict] = {}
    for i in range(0, len(names), 100):
        r = sh(["docker", "inspect", "--format", INSPECT_FMT, *names[i:i + 100]], timeout=60)
        if r.returncode != 0 and not r.stdout.strip():
            return None
        for ln in r.stdout.splitlines():
            f = ln.strip().split("|")
            if len(f) == 6 and all(x.lstrip("-").isdigit() for x in f[2:]):
                out[f[0].lstrip("/")] = {"id": f[1], "shares": int(f[2]), "blkio": int(f[3]), "resv": int(f[4]),
                                         "mem": int(f[5])}
    return out


def cpu_weight(cid: str) -> int | None:
    d = gates.cg_dir(cid)
    return gates.read_int(d / "cpu.weight") if d else None


def docker_update(name: str, flags: list[str]) -> None:
    r = sh(["docker", "update", *flags, name], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"docker update {name} rc={r.returncode}: {(r.stderr or r.stdout).strip()[-80:]}")


def _never_throttle(name: str, image: str = "", service: str = "") -> bool:
    """Databases and build daemons (by name, image or compose service): never slowed, restarted or stopped."""
    return bool(guard._NEVER_RESTART.search(f"{name} {image} {service}"))


def _act(ctx: Ctx, apply_: bool, what: str, target: str, fn: Callable[[], Any], exempt: list[str] | None = None,
         protect: tuple[str, ...] = ()) -> str:
    """ctx.act with a per-rung apply flag (the task-level Ctx.apply cannot express four modes) and an optional,
    explicit protected.toml exemption for non-destructive weight changes. Size is always 0: a model or a CPU weight
    is not disk space and must not land in the "space reclaimed" totals.
    Returns "done", "dry-run" (would run in apply), "protected" (protected.toml names it, in either mode, so a report
    predicts what apply would refuse) or "refused" (PAUSE appeared, or an empty target)."""
    old_apply, old_tcfg = ctx.apply, ctx.tcfg
    ctx.apply = bool(apply_)                    # a PAUSE that appears now is caught (and audited) by ctx.act itself
    if exempt:
        ctx.tcfg = {**ctx.tcfg, "unprotect": [*(ctx.tcfg.get("unprotect") or []), *exempt]}
    try:
        prot = ctx.is_protected(target, *protect)
        if ctx.act(what, target, 0, fn, protect_names=protect):
            return "done"
        return "protected" if prot else "dry-run" if not apply_ else "refused"
    finally:
        ctx.apply, ctx.tcfg = old_apply, old_tcfg


# =========================================================================== HTTP (Ollama, ComfyUI)
def _loopback(url: str) -> bool:
    return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")


def _http_get(url: str, timeout: float = 3.0) -> Any:
    if not _loopback(url):
        raise ValueError("only loopback URLs are probed")
    return gates.http_json(url, timeout)


def _http_post(url: str, payload: dict, timeout: float = 8.0) -> None:
    if not _loopback(url):
        raise ValueError("only loopback URLs are used")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as resp:
        resp.read(65536)
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


def _gate(name: str, cfg: dict) -> tuple[bool, str]:
    """gates.busy with a fail-closed wrapper. Tests patch this."""
    try:
        b, why = gates.busy(name, cfg)
        return bool(b), str(why)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, getattr(core, "_Timeout", ())):
            raise
        return True, f"gate error: {type(exc).__name__}"


def _parse_ts(s: Any) -> float | None:
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?", str(s or ""))
    if not m:
        return None
    tz = "+00:00" if m.group(3) in (None, "Z") else m.group(3)
    try:
        return datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + tz).timestamp()
    except ValueError:
        return None


def _ollama_models(base: str) -> list[dict] | None:
    try:
        ms = _http_get(base + "/api/ps")["models"]
        if not isinstance(ms, list):
            return None
    except Exception as exc:  # noqa: BLE001  - unreachable/odd answer: nothing is touched
        if isinstance(exc, getattr(core, "_Timeout", ())):
            raise
        return None
    return [m for m in ms if isinstance(m, dict) and isinstance(m.get("name"), str)]


def _comfy_queue(base: str) -> int | None:
    """running + pending jobs, None if ComfyUI cannot be asked (stopped container, bad answer)."""
    try:
        q = _http_get(base + "/queue")
        return len(q["queue_running"]) + len(q["queue_pending"])
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, getattr(core, "_Timeout", ())):
            raise
        return None


# =========================================================================== rungs
def _modes(ctx: Ctx) -> dict[str, str]:
    """Per-rung mode. Unknown values fall back to report (never to apply). Master switch: run without --apply, or
    `mode = "report"` on the task, forces report for every rung."""
    master = ctx.global_apply and ctx.opt("mode", "apply") == "apply" and not core.paused(ctx.name)
    out = {}
    for r, d in RUNG_DEFAULTS.items():
        m = ctx.opt(r, d)
        m = m if m in ("apply", "report", "off") else "report"
        out[r] = "report" if (m == "apply" and not master) else m
    return out


def _budget(ctx: Ctx, cm: ClassMap, rung: str) -> bool:
    """max_per_day: counts every action that passed the gates in either mode, so a report run shows what apply would do."""
    day = time.strftime("%Y-%m-%d", time.localtime(ctx.now))
    d = ctx.state.setdefault("day", {})
    if d.get("date") != day:
        d.clear()
        d.update(date=day, n={})
    limit = int(cm.sub("max_per_day", rung, MAX_PER_DAY.get(rung, 0)))
    if d["n"].get(rung, 0) >= limit:
        return False
    d["n"][rung] = d["n"].get(rung, 0) + 1
    return True


class _Run:
    """Per-run bookkeeping: rows for the log/Result and a few counters."""

    def __init__(self, ctx: Ctx, level: int):
        self.ctx, self.level, self.rows, self.fail, self.alert = ctx, level, [], 0, ""

    def add(self, rung: str, action: str, target: str, cls: str, outcome: str) -> dict:
        r = _row(self.ctx.now, self.level, rung, action, target, cls, outcome)
        self.rows.append(r)
        return r

    def n(self, rung: str, outcome: str) -> int:
        return sum(1 for r in self.rows if r["rung"] == rung and r["outcome"] == outcome)


def _do(run: _Run, rung: str, mode: str, what: str, target: str, cls: str, fn: Callable[[], Any], label: str,
        exempt: list[str] | None = None, protect: tuple[str, ...] = ()) -> bool:
    """One gated action -> one row. Outcomes: done | would | refused: protected.toml | refused: paused | failed: ..
    | refused: cap. Returns True if it ran."""
    ctx = run.ctx
    try:
        status = _act(ctx, mode == "apply", what, target, fn, exempt, protect)
    except CapExceeded as exc:
        run.add(rung, label, target, cls, f"refused: cap ({exc})")
        return False
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, getattr(core, "_Timeout", ())):
            raise
        run.fail += 1
        run.add(rung, label, target, cls, f"failed: {exc}")
        return False
    if status == "done":
        run.add(rung, label, target, cls, "done")
        return True
    run.add(rung, label, target, cls, {"protected": "refused: protected.toml", "dry-run": "would"}.get(
        status, "refused: paused" if core.paused(ctx.name) else "refused"))
    return False


# ---- L2 reclaim ---------------------------------------------------------------------------------------------------
def _rung_reclaim(run: _Run, cm: ClassMap, mode: str) -> None:
    ctx = run.ctx
    busy_cfg = ctx.protected.get("busy", {}) if isinstance(ctx.protected, dict) else {}
    ollama = re.sub(r"/api/ps/?$", "", str(busy_cfg.get("ollama_ps_url", "http://127.0.0.1:11434/api/ps")))
    comfy = re.sub(r"/queue/?$", "", str(busy_cfg.get("comfyui_queue_url", "http://127.0.0.1:8188/queue")))
    idle_s, gap = cm.n("idle_s", 300), cm.n("probe_gap_s", 2)
    exempt = ["^ollama:", "^comfyui:"]       # targets are "ollama:<model>" / "comfyui:free": an API call, not a kill
    seen = ctx.state.setdefault("ollama", {})
    models = _ollama_models(ollama)
    if models is not None:
        names = {m["name"] for m in models}
        for gone in [k for k in seen if k not in names]:
            del seen[gone]
        cands = []
        for m in models:
            exp = m.get("expires_at")
            ts = _parse_ts(exp)
            rec = seen.get(m["name"])
            if not rec or rec.get("exp") != exp:
                seen[m["name"]] = rec = {"exp": exp, "since": ctx.now}       # activity (or first sight): idle clock restarts
            # expires_at in the past on a loaded model means a request is in flight (or it is unloading): never touch
            if ts is not None and ts > ctx.now and ctx.now - rec["since"] >= idle_s:
                cands.append(m)
        if cands:
            _sleep(gap)                                                       # probe twice: expires_at must not have moved
            again = {m["name"]: m.get("expires_at") for m in (_ollama_models(ollama) or [])}
            cands = [m for m in cands if again.get(m["name"]) == m.get("expires_at")]
        if cands and _gate("ollama", ctx.cfg)[0]:                             # CPU says it is generating: veto
            cands = []
        for m in cands[:int(cm.n("max_models_per_run", 4))]:
            if not _budget(ctx, cm, "reclaim"):
                run.add("L2", "unload model", f"ollama:{m['name']}", "P1", "refused: max_per_day")
                break
            body = {"model": m["name"], "keep_alive": 0}
            if _do(run, "L2", mode, "ollama-unload", f"ollama:{m['name']}", "P1",
                   lambda b=body: _http_post(ollama + "/api/generate", b),
                   f"unload idle model ({human(int(_num(m.get('size_vram'), 0)))} VRAM)", exempt=exempt):
                seen.pop(m["name"], None)
    q = _comfy_queue(comfy)
    if q == 0 and ctx.now - float(ctx.state.get("comfy_free_t", 0)) >= cm.n("comfy_free_cooldown_min", 30) * 60:
        _sleep(gap)
        if _comfy_queue(comfy) == 0 and _budget(ctx, cm, "reclaim"):         # empty twice: only then /free
            if _do(run, "L2", mode, "comfyui-free", "comfyui:free", "P2",
                   lambda: _http_post(comfy + "/free", {"unload_models": True, "free_memory": True}),
                   "free idle ComfyUI models (queue empty)", exempt=exempt):
                ctx.state["comfy_free_t"] = ctx.now
    ctx.state["comfy_queue"] = q


# ---- L3 throttle --------------------------------------------------------------------------------------------------
def _restore_cpu(name: str, rec: dict) -> str:
    """Put a throttled container's CPU weight back. docker cannot "unset" cpu-shares (0 is ignored) and the shares ->
    weight map depends on the runc version, so for a container that had no explicit shares try candidate share values
    and keep the first whose cgroup weight reads the recorded one again. The throttle noted which map it saw
    (rec["map"]), so the right candidate goes first and the container never passes through a wrong intermediate weight.
    Returns a short outcome. Raises RuntimeError if docker refuses."""
    want = rec.get("weight")
    lin = _shares_for_weight_linear(int(want)) if isinstance(want, int) and want > 1 else None
    if rec.get("shares", 0) > 0:
        cands = [int(rec["shares"])]
    elif rec.get("map") == "linear" and lin:
        cands = [lin, 1024]
    else:                                              # quadratic (1024 -> default weight) or not known: 1024 first
        cands = [1024] + ([lin] if lin else [])
    last = None
    for s in cands:
        docker_update(name, ["--cpu-shares", str(s)])
        last = cpu_weight(rec.get("cid", ""))
        if want is None or last is None or last == want or rec.get("shares", 0) > 0:
            return f"restored cpu-shares {s} (weight {last})"
    return f"restored approx cpu-shares {cands[-1]} (weight {last}, wanted {want})"


def _container_state(name: str) -> tuple[str, str]:
    """('gone' | 'stopped' | 'running' | 'unknown', full id) from `docker inspect --type container` (read-only). `docker
    ps` lists only running containers, so this is the only way to tell a container that was removed from one that is
    merely stopped."""
    r = sh(["docker", "inspect", "--type", "container", "--format", STATE_FMT, name], timeout=30)
    if r.returncode == 0:
        cid, _, running = r.stdout.strip().partition("|")
        if re.fullmatch(r"[0-9a-f]{12,64}", cid) and running in ("true", "false"):
            return ("running" if running == "true" else "stopped"), cid
        return "unknown", ""
    return ("gone" if "no such" in f"{r.stderr} {r.stdout}".lower() else "unknown"), ""


def _release(ctx: Ctx, cm: ClassMap, run: "_Run", thr_why: str = "", stop_why: str = "",
             keep: Callable[[dict], bool] | None = None) -> int:
    """Undo what the ladder changed, by reason. `thr_why` / `stop_why` say why the throttles / the emergency stops are
    released now ("" leaves them); `keep(record)` keeps a throttle whose lever is still needed. A stop older than
    max_stop_min is started back whatever `stop_why` says: no emergency stop may outlive that. Every outcome is a row
    in `run`: a failed restore or start is `failed:` (so the run warns and alerts); a record whose container is gone,
    was recreated or was started by its owner is dropped. Direct sh + audit (ctx.act refuses while PAUSE exists).
    Does nothing in a dry run beyond saying what it would do. Returns the number of successful releases."""
    thr, stp = ctx.state.get("throttled") or {}, ctx.state.get("stopped") or {}
    max_stop = cm.n("max_stop_min", MAX_STOP_MIN) * 60
    todo_thr = [n for n in sorted(thr) if thr_why and not (keep and keep(thr[n]))]
    todo_stp = [n for n in sorted(stp) if stop_why or ctx.now - _num(stp[n].get("t"), ctx.now) >= max_stop]
    if not ctx.global_apply:
        if todo_thr:
            run.add("L3", "release", "-", "-", f"would release {len(todo_thr)} throttle(s): {thr_why}")
        if todo_stp:
            run.add("L5", "release", "-", "-", f"would start back {len(todo_stp)} container(s): {stop_why or 'max stop time'}")
        return 0
    n = 0
    running = gates.container_info() if todo_thr else None
    for name in todo_thr:
        rec = thr[name]
        cls, cur = rec.get("class", "P2"), (running or {}).get(name)
        if running is not None and (cur is None or (rec.get("cid") and cur.id != rec["cid"])):
            audit(ctx.name, "restore cpu-shares", name, 0, f"dropped: container gone or recreated ({thr_why})")
            run.add("L3", "restore cpu-shares", name, cls, "dropped: container gone or recreated")
            del thr[name]
            continue
        try:
            out = _restore_cpu(name, rec) if "--cpu-shares" in rec.get("flags", ["--cpu-shares"]) else "cpu untouched"
            if rec.get("blkio", 0) > 0 and "--blkio-weight" in rec.get("flags", []):
                docker_update(name, ["--blkio-weight", str(rec["blkio"])])
                out += f", blkio-weight {rec['blkio']}"
        except RuntimeError as exc:                     # stays recorded and is retried; the row makes it visible
            audit(ctx.name, "restore cpu-shares", name, 0, f"failed: {exc}")
            run.add("L3", "restore cpu-shares", name, cls, f"failed: {exc}")
            continue
        audit(ctx.name, "restore cpu-shares", name, 0, f"{out} ({thr_why})")
        run.add("L3", "restore cpu-shares", name, cls, f"done: {thr_why}")
        del thr[name]
        ctx.save_state()
        n += 1
    ctx.state["throttled"] = thr
    for name in todo_stp:
        rec, cls = stp[name], stp[name].get("class", "P3")
        forced = not stop_why
        why = stop_why or f"max stop time {int(max_stop // 60)} min reached"
        state, cid = _container_state(name)
        drop = ("container gone" if state == "gone" else "container recreated" if cid and rec.get("cid") and cid != rec["cid"]
                else "already running" if state == "running" else "")
        if drop:
            audit(ctx.name, "start stopped container", name, 0, f"dropped: {drop} ({why})")
            run.add("L5", "start container back", name, cls, f"dropped: {drop}")
            del stp[name]
            ctx.save_state()
            continue
        r = sh(["docker", "start", name], timeout=90)
        ok = r.returncode == 0
        audit(ctx.name, "start stopped container", name, 0, f"{'done' if ok else 'failed rc=' + str(r.returncode)} ({why})")
        if ok:
            run.add("L5", "start container back", name, cls, f"done: {why}")
            del stp[name]
            if forced:                                  # L5 must not stop it again at once if the stall goes on
                cool = ctx.state.setdefault("stop_cool", {})
                cool[name] = ctx.now
                for k in [k for k, t in cool.items() if ctx.now - _num(t, 0) >= 24 * 3600]:
                    del cool[k]
            n += 1
        else:
            rec["fails"] = int(rec.get("fails", 0)) + 1
            if rec["fails"] >= START_TRIES:             # one last alert, then stop retrying a start that cannot work
                audit(ctx.name, "start stopped container", name, 0, f"dropped: gave up after {rec['fails']} tries")
                run.add("L5", "start container back", name, cls,
                        f"failed: gave up after {rec['fails']} tries, left stopped")
                del stp[name]
            else:
                run.add("L5", "start container back", name, cls,
                        f"failed: start rc={r.returncode} (try {rec['fails']} of {START_TRIES})")
        ctx.save_state()
    return n


def _rung_throttle(run: _Run, cm: ClassMap, mode: str, dims: dict, info: dict, act: dict) -> None:
    """Slow batch containers: P3 first; P2 only when no active, unslowed P3 is left. Levers: cpu-shares (memory or cpu
    pressure) and blkio-weight (io pressure, only where the host honours io weights and only for containers that already
    have a weight, so it can be put back exactly). Nothing to pull => one explanatory row, no action."""
    ctx, st = run.ctx, run.ctx.state
    thr = st.setdefault("throttled", {})
    cpu_lever = max(dims.get("mem", 0), dims.get("cpu", 0)) >= 3
    blk_lever = dims.get("io", 0) >= 3 and io_weights_effective()
    if not (cpu_lever or blk_lever):
        run.add("L3", "slow batch", "-", "-", "no lever: io weights have no effect here (no bfq/iocost) and cpu-shares "
                "do not relieve an io stall")
        return
    fl = cm.section("floors")
    f_cpu, f_blk = int(_num(fl.get("cpu_shares_min"), 64)), int(_num(fl.get("blkio_weight_min"), 10))
    min_cpu = cm.n("throttle_min_cpu_pct", 10)

    def active(n: str) -> bool:
        a = act.get(n, {})
        return a.get("cpu_pct", 0) >= min_cpu or a.get("growth", 0) > 0.5 or (blk_lever and a.get("io_mib_s", 0) >= 5)

    # "unknown => P2" is a display default: only a container that classes.toml NAMES may be slowed
    unk = sorted(n for n, m in info.items() if not cm.of(n)[1] and n not in thr and active(n)
                 and not _never_throttle(n, m.image, m.service))
    if unk:
        run.add("L3", "slow batch", "-", "-", f"skipped: unclassified (not in classes.toml): {', '.join(unk[:3])}")
    todo: list[str] = []
    for cls in ("P3", "P2"):
        todo = sorted((n for n, m in info.items() if cm.of(n) == (cls, True) and n not in thr and active(n)
                       and not _never_throttle(n, m.image, m.service)),
                      key=lambda n: (-act.get(n, {}).get("cpu_pct", 0), n))[:int(cm.n("max_throttled_per_run", 8))]
        if todo:
            break
    if not todo:
        run.add("L3", "slow batch", "-", "-", "nothing eligible (active, unslowed P3/P2 batch container)")
        return
    hc = inspect_hc(todo) or {}
    exempt = [p for p in cm.lad.get("throttle_unprotect", []) if isinstance(p, str)]
    for n in todo:
        cls, h = cm.cls(n), hc.get(n)
        if h is None:
            run.add("L3", "slow", n, cls, "skipped: inspect failed")
            continue
        flags: list[str] = []
        tgt = int(cm.sub("throttle", cls, 128 if cls == "P3" else 256))
        if cpu_lever:
            if tgt < f_cpu:
                run.add("L3", "cpu-shares", n, cls, f"refused: {tgt} below floor {f_cpu}")
            elif not 0 < h["shares"] <= tgt:
                flags += ["--cpu-shares", str(tgt)]
        tb = max(f_blk, h["blkio"] // 4)
        if blk_lever and h["blkio"] > 0 and tb < h["blkio"]:
            flags += ["--blkio-weight", str(tb)]
        if not flags:
            run.add("L3", "slow", n, cls, "skipped: already at or below target")
            continue
        if not _budget(ctx, cm, "throttle"):
            run.add("L3", "slow", n, cls, "refused: max_per_day")
            break
        w0 = cpu_weight(h["id"])

        def fn(n=n, h=h, flags=flags, w0=w0, cls=cls) -> None:
            docker_update(n, flags)
            rec = {"shares": h["shares"], "blkio": h["blkio"], "weight": w0, "cid": h["id"], "class": cls,
                   "t": ctx.now, "flags": flags, "map": None}
            if "--cpu-shares" in flags:                     # which shares -> weight map does this runc use? (see restore)
                got, tgt_s = cpu_weight(h["id"]), int(flags[flags.index("--cpu-shares") + 1])
                rec["map"] = ("linear" if got == weight_linear(tgt_s) else "quadratic" if got == weight_quadratic(tgt_s)
                              else None)
            thr[n] = rec
            ctx.save_state()                                # recorded at once: a crash must not strand a throttle

        _do(run, "L3", mode, "docker-update-throttle", n, cls, fn,
            f"{' '.join(flags)} (was shares {h['shares'] or 'default'}, weight {w0})", exempt=exempt)
        if run.fail >= 2 or core.paused(ctx.name):
            break


# ---- L4 restart ---------------------------------------------------------------------------------------------------
def _restart_ok(st: dict, name: str, now: float, cap: int) -> tuple[bool, str]:
    """Retry budget: at most `cap` restarts per 6 h across ALL containers, and per container an exponential backoff
    of 30 min * 2^(n-1) (n = its restarts in 24 h) plus a deterministic 0-5 min jitter so recoveries never line up."""
    rs = st.get("restarts", {})
    recent = [t for ts in rs.values() for t in ts if now - t < 6 * 3600]
    if len(recent) >= cap:
        return False, f"budget: {cap} restarts in the last 6 h"
    own = [t for t in rs.get(name, []) if now - t < 86400]
    if own:
        wait = 1800 * 2 ** (len(own) - 1) + zlib.crc32(name.encode()) % 300
        if now - max(own) < wait:
            return False, f"backoff: wait {wait // 60} min after the last restart"
    return True, ""


def _note_restart(st: dict, name: str, now: float) -> None:
    """Record a restart; everything older than 24 h is dropped (it no longer counts for budget or backoff)."""
    rs = {k: [t for t in v if now - t < 86400] for k, v in (st.get("restarts") or {}).items()}
    rs.setdefault(name, []).append(now)
    st["restarts"] = {k: v for k, v in rs.items() if v}


def _stuck(ctx: Ctx) -> list[dict] | None:
    """stuck_detector's candidates, recomputed from the samples with ITS config (None = cannot judge yet). Same
    preconditions as the task: enough samples, a fresh newest one, a long enough window."""
    sd = Ctx(ctx.cfg, "stuck_detector", False, ctx.now)
    need, max_h = int(sd.opt("min_samples", 6)), float(sd.opt("max_window_hours", 6))
    recs = sorted((r for r in guard.read_samples(max_h * 3600, ctx.now)
                   if isinstance(r.get("t"), (int, float)) and isinstance(r.get("c"), dict) and r["c"]),
                  key=lambda r: r["t"])
    win = recs[-need:]
    if len(win) < need or (ctx.now - win[-1]["t"]) / 60 > float(sd.opt("max_sample_age_min", 45)) \
            or (win[-1]["t"] - win[0]["t"]) / 60 < float(sd.opt("min_window_min", 30)):
        return None
    return guard._stuck_candidates(sd, win)


def _rung_restart(run: _Run, cm: ClassMap, mode: str, dims: dict, act_level: bool) -> None:
    """Candidates are tracked every run at memory level >= 2 (the streak), restarted only at memory level >= 4."""
    ctx, st = run.ctx, run.ctx.state
    cands = _stuck(ctx)
    streak = st.get("streak", {})
    new = {}
    for c in cands or []:
        new[c["name"]] = streak.get(c["name"], 0) + 1
    st["streak"] = new
    if cands is None:
        return                                    # not enough fresh samples to judge: say nothing, never guess
    elig: list[dict] = []
    for c in cands[:6]:
        n = c["name"]
        cls, explicit = cm.of(n)
        ident = c.get("_ident", (n, "", ""))
        if c.get("protected") or cls in ("P0", "P1") or _never_throttle(*ident):
            run.add("L4", "alert only", n, cls, "protected/database: never restarted; memory ceiling is the backstop")
            run.alert = run.alert or n
        elif not explicit:                            # unknown is not P2 for a rung that kills: tell the owner instead
            run.add("L4", "alert only", n, "unclassified", "unclassified (not in classes.toml): never restarted; add it to a class")
            run.alert = run.alert or n
        elif c.get("busy"):
            run.add("L4", "restart", n, cls, f"skipped: busy ({c['busy'][:50]})")
        elif new[n] < cm.n("restart_min_streak", 2):
            run.add("L4", "restart", n, cls, f"watching: candidate {new[n]}/{int(cm.n('restart_min_streak', 2))} runs")
        else:
            elig.append(c)
    if not act_level or not elig:
        return
    if st.get("reclaim_attempts", 0) < 1:
        run.add("L4", "restart", elig[0]["name"], cm.cls(elig[0]["name"]), "waiting: reclaim (L2) has not been tried yet")
        return
    pick = None
    for c in elig:                                    # largest first; one in backoff must not shield the next one
        ok, why = _restart_ok(st, c["name"], ctx.now, int(cm.n("max_restarts_per_6h", 2)))
        if ok:
            pick = c
            break
        run.add("L4", "restart", c["name"], cm.cls(c["name"]), f"refused: {why}")
        if why.startswith("budget"):
            return                                    # the budget is global: no other candidate can pass either
    if pick is None:
        return
    n, cls = pick["name"], cm.cls(pick["name"])
    if not _budget(ctx, cm, "restart"):
        run.add("L4", "restart", n, cls, "refused: max_per_day")
        return

    def fn() -> None:
        guard._docker_restart(n)
        _note_restart(st, n, ctx.now)
        ctx.save_state()

    _do(run, "L4", mode, "docker restart", n, cls, fn, f"restart stuck container ({pick['reason'].split(':')[0]})",
        protect=pick.get("_ident", ()))


# ---- L5 emergency -------------------------------------------------------------------------------------------------
def _rung_emergency(run: _Run, cm: ClassMap, mode: str, info: dict, stall_min: float) -> None:
    ctx, st = run.ctx, run.ctx.state
    names = [x for x in cm.lad.get("emergency_stop", []) if isinstance(x, str)]
    if not names:
        run.add("L5", "emergency stop", "-", "-", "emergency_stop list is empty: nothing to do")
        return
    need = cm.n("emergency_after_min", 20)
    if stall_min < need:
        run.add("L5", "emergency stop", "-", "-", f"waiting: memory stall {stall_min:.0f} of {need:.0f} min")
        return
    cool_s = cm.n("stop_cooldown_min", STOP_COOLDOWN_MIN) * 60
    cool = {k: t for k, t in (st.get("stop_cool") or {}).items() if ctx.now - _num(t, 0) < cool_s}
    st["stop_cool"] = cool
    for n in sorted(k for k in names if k in cool and k in info):
        run.add("L5", "emergency stop", n, cm.cls(n), "skipped: cooldown after the max stop time")
    order = sorted((n for n in names if n in info and n not in (st.get("stopped") or {}) and n not in cool),
                   key=lambda n: (cm.cls(n) != "P3", n))
    for n in order:
        cls, m = cm.cls(n), info[n]
        if cls in ("P0", "P1") or _never_throttle(n, m.image, m.service):
            run.add("L5", "emergency stop", n, cls, f"refused: {cls}/database is never stopped")
            continue
        if not cm.of(n)[1]:                                   # unknown is not P2 for a rung that stops containers
            run.add("L5", "emergency stop", n, "unclassified", "refused: unclassified (not in classes.toml), add it to a class first")
            continue
        if ctx.is_protected(n, m.image, m.service):          # protected.toml names it: no budget is spent on a refusal
            audit(ctx.name, "docker stop", n, 0, "refused-protected")
            run.add("L5", "emergency stop", n, cls, "refused: protected.toml")
            continue
        if not _budget(ctx, cm, "emergency"):
            run.add("L5", "emergency stop", n, cls, "refused: max_per_day")
            return

        def fn(n=n, m=m, cls=cls) -> None:
            r = sh(["docker", "stop", "-t", "30", n], timeout=90)
            if r.returncode != 0:
                raise RuntimeError(f"docker stop {n} rc={r.returncode}")
            st.setdefault("stopped", {})[n] = {"t": ctx.now, "cid": m.id, "class": cls}
            ctx.save_state()

        done = _do(run, "L5", mode, "docker stop", n, cls, fn, "emergency stop best-effort container",
                   protect=(n, m.image, m.service))
        if done or mode != "apply" or run.fail or core.paused(ctx.name):
            return                                            # ONE per run (and in report mode only one "would")


# ---- pressure_response --------------------------------------------------------------------------------------------
def _release_step(ctx: Ctx, cm: ClassMap, run: _Run, modes: dict, dims: dict, level: int, stale: bool, paused: bool) -> None:
    """Decide what to undo this run. Throttles go when PAUSE, an unknown state, or the throttle rung leaving "apply"
    say so, or once the resource they answer is below L3 (memory and cpu for cpu-shares, io for blkio weights; the dims are
    already debounced by the hysteresis). Emergency stops go when PAUSE or "pressure over" holds AND memory is known to
    be below L4 (an unknown state, a stall that is still on, or a throttle-mode change never starts one); the time bound
    in `_release` still applies. A stop that is kept on purpose says so in a row."""
    mem, cpu, io = dims.get("mem", 0), dims.get("cpu", 0), dims.get("io", 0)
    mem_ok = not stale and mem < 4
    keep: Callable[[dict], bool] | None = None
    if paused or stale or modes["throttle"] != "apply":
        thr_why = "paused" if paused else "state stale" if stale else "throttle not in apply mode"
    else:
        thr_why = "pressure over" if level == 0 else "resource below L3"
        cpu_on, blk_on = max(mem, cpu) >= 3, io >= 3 and io_weights_effective()
        keep = lambda rec: bool((cpu_on and "--cpu-shares" in rec.get("flags", ["--cpu-shares"]))      # noqa: E731
                                or (blk_on and "--blkio-weight" in rec.get("flags", [])))
    stop_why = ("paused" if paused else "pressure over" if level == 0 else "memory below L4") if mem_ok else ""
    _release(ctx, cm, run, thr_why, stop_why, keep)
    if (ctx.state.get("stopped") or {}) and (paused or stale) and not stop_why and ctx.global_apply:
        what = "pressure state unknown" if stale else f"paused, memory still L{mem}"
        run.add("L5", "start container back", "-", "-",
                f"held: {what}; started back below L4 or after {int(cm.n('max_stop_min', MAX_STOP_MIN))} min")


def _held_note(ctx: Ctx, run: _Run, held: bool, mode: str, dims: dict) -> str:
    """The tail of the summary for a run that is not climbing the ladder (paused, stale, calm). "" for a clean calm run.
    A release that failed or was kept on purpose must show: 'L0 normal: nothing to do' would be a lie."""
    thr, stp = len(ctx.state.get("throttled") or {}), len(ctx.state.get("stopped") or {})
    bad = [r for r in run.rows if r["outcome"].startswith("failed")]
    nthr = sum(1 for r in bad if r["action"] == "restore cpu-shares")
    nstp = sum(1 for r in bad if r["action"] == "start container back")
    bits = ([f"{thr} throttle(s) not restored"] if nthr else []) + ([f"start-back failed for {nstp} container(s)"] if nstp else [])
    if mode == "calm":
        return ("L0 normal, but " + "; ".join(bits)) if bits else ""
    if bits:
        return ", " + "; ".join(bits)
    if not held or not ctx.global_apply:
        return ""
    if stp:
        why = "pressure state unknown" if mode == "stale" else f"memory still L{dims.get('mem', 0)}"
        return f", {stp} stopped container(s) kept down ({why})" + (f", {thr} throttle(s) kept" if thr else "")
    return ", changes released" if mode == "paused" else ""


@task("pressure_response", klass="C1", tier="check", title="Spike response ladder", timeout=240)
def pressure_response(ctx: Ctx) -> Result:
    cm = load_classes()
    modes = _modes(ctx)
    ps = _task_state("pressure_state")
    age_min = (ctx.now - ps["t"]) / 60 if isinstance(ps.get("t"), (int, float)) else None
    stale = age_min is None or age_min > cm.n("stale_state_min", 45)
    level = 0 if stale else int(ps.get("level", 0))
    dims = {} if stale else {d: int((ps.get("dims", {}).get(d, {}) or {}).get("level", 0)) for d in DIMS}
    run = _Run(ctx, level)
    paused = core.paused(ctx.name)
    held = bool(ctx.state.get("throttled") or ctx.state.get("stopped"))

    # Release first, by the resource each hold answers (see the module docstring), not by the host level.
    if held:
        _release_step(ctx, cm, run, modes, dims, level, stale, paused)
    base = {"level": level, "dims": dims, "stale": stale, "modes": modes,
            "spike_id": (ps.get("spike") or {}).get("id") if isinstance(ps.get("spike"), dict) else None}
    if paused:
        return _finish(ctx, run, cm, base, "paused: no action taken" + _held_note(ctx, run, held, "paused", dims))
    if stale or level == 0:
        ctx.state["reclaim_attempts"] = 0
        if stale:
            return _finish(ctx, run, cm, base, "state unknown (stale), no action" + _held_note(ctx, run, held, "stale", dims))
        return _finish(ctx, run, cm, base, _held_note(ctx, run, held, "calm", dims) or "L0 normal: nothing to do")

    spike = ps.get("spike") or {}
    if ctx.state.get("annotated") != spike.get("id"):
        run.add("L1", "annotate", "-", "-", f"noted: {ps.get('level_name') or LEVEL_NAMES[level]} (dims {dims})")
        ctx.state["annotated"] = spike.get("id")

    info = gates.container_info() or {}
    mem_gpu = max(dims.get("mem", 0), dims.get("gpu", 0))
    if mem_gpu >= 2:
        ctx.state["reclaim_attempts"] = ctx.state.get("reclaim_attempts", 0) + 1     # L4 waits until L2 had its turn
        if modes["reclaim"] != "off":                                                # (an owner who turned L2 off skipped it)
            _rung_reclaim(run, cm, modes["reclaim"])
    elif max(dims.values(), default=0) >= 2 and mem_gpu < 2:
        run.add("L2", "reclaim", "-", "-", "skipped: no memory or GPU pressure (io/cpu only), nothing to reclaim")
    if max(dims.get("mem", 0), dims.get("io", 0), dims.get("cpu", 0)) >= 3 and modes["throttle"] != "off":
        if not cm.ok:
            run.add("L3", "slow batch", "-", "-", "refused: classes.toml missing or broken (fail closed)")
        else:
            _rung_throttle(run, cm, modes["throttle"], dims, info, activity(ctx.now))
    if dims.get("mem", 0) < 2:
        ctx.state["streak"] = {}                       # "proven stuck" means consecutive runs under memory pressure
    if dims.get("mem", 0) >= 2 and modes["restart"] != "off":
        if not cm.ok:
            run.add("L4", "restart", "-", "-", "refused: classes.toml missing or broken (fail closed)")
        else:
            _rung_restart(run, cm, modes["restart"], dims, dims.get("mem", 0) >= 4)
    if dims.get("mem", 0) >= 5 and modes["emergency"] != "off":
        if not cm.ok:
            run.add("L5", "emergency stop", "-", "-", "refused: classes.toml missing or broken (fail closed)")
        else:
            _rung_emergency(run, cm, modes["emergency"], info, float(ps.get("stall_min") or 0))
    return _finish(ctx, run, cm, base, "")


def _loggable(ctx: Ctx, rows: list[dict], spike_id: Any) -> list[dict]:
    """Rows worth keeping in pressure-log.jsonl. Actions (done / would / failed) always; an explanatory row (no lever,
    refused: budget, alert only, ...) once per spike and distinct text, or the log and the website's action list would
    fill with the same sentence every 15 minutes while a spike lasts. "watching" rows (a restart candidate seen once
    so far) stay in the run's own Result and are never logged: they are not something the ladder did."""
    seen = ctx.state.setdefault("noted", {})
    if len(seen) > 200:
        seen.clear()
    out = []
    fl = ctx.state.setdefault("fail_logged", {})
    for k in [k for k, t in fl.items() if ctx.now - _num(t, 0) >= 6 * 3600]:
        del fl[k]
    for r in rows:
        o = r["outcome"]
        if o.startswith("failed") and r["action"] in ("restore cpu-shares", "start container back"):
            key = f"{r['action']}|{r['target']}|{o[:16]}"          # retried every run: log the failure once per 6 h
            if key not in fl:
                fl[key] = ctx.now
                out.append(r)
            continue
        if o == "would" or o.startswith(("done", "failed")):
            out.append(r)
            continue
        if o.startswith("watching"):
            continue
        key = f"{r['rung']}|{r['action']}|{r['target']}|{o[:48]}"
        if key not in seen or seen[key] != spike_id:
            seen[key] = spike_id
            out.append(r)
    return out


def _finish(ctx: Ctx, run: _Run, cm: ClassMap, base: dict, note: str) -> Result:
    if base["level"] == 0 or base["stale"]:
        ctx.state.pop("noted", None)                       # the next spike starts with a clean slate
    for r in _loggable(ctx, run.rows, base.get("spike_id")):
        _append_jsonl(log_path(), r, max_bytes=400_000, keep=1000)
    done = [r for r in run.rows if r["outcome"].startswith("done")]
    would = [r for r in run.rows if r["outcome"] == "would"]
    failed = [r for r in run.rows if r["outcome"].startswith("failed")]
    # refusals that mean "look at this" (a protected target, a used-up budget or cap, a floor, a broken class map), shown
    # as a warning without paging; backoff, PAUSE and "never stop a P0/P1/database" are the design working, not news
    odd = [r for r in run.rows if r["outcome"].startswith("refused") and "never stopped" not in r["outcome"]
           and not r["outcome"].startswith(("refused: backoff", "refused: paused"))]
    restore_failed = sum(1 for r in failed if r["action"] == "restore cpu-shares")
    parts = [f"{len(done)} done"] * bool(done) + [f"{len(would)} would"] * bool(would) + \
            [f"{len(failed)} failed"] * bool(failed) + \
            [f"{len(ctx.state.get('throttled') or {})} throttle(s) not restored"] * bool(restore_failed)
    lvl = base["level"]
    m = base["modes"]
    summary = note or f"L{lvl} {level_label(lvl, base['dims'] or {})}: " + (", ".join(parts) or "no action needed") \
        + (f"; ALERT ONLY {run.alert} (protected/unclassified, runaway)" if run.alert else "")
    status = "warn" if (failed or run.alert or odd) else "info" if (done or would) else "ok"
    metrics = {"level": lvl, "dims": base["dims"], "stale": base["stale"], "mode_reclaim": m["reclaim"],
               "mode_throttle": m["throttle"], "mode_restart": m["restart"], "mode_emergency": m["emergency"],
               "done": len(done), "would": len(would), "failed": len(failed),
               "throttled": len(ctx.state.get("throttled") or {}), "stopped": len(ctx.state.get("stopped") or {}),
               "restarts_6h": sum(1 for ts in (ctx.state.get("restarts") or {}).values() for t in ts if ctx.now - t < 6 * 3600),
               "io_weights_effective": io_weights_effective()}
    return Result(status, _a(summary), metrics, run.rows[-12:], alert=bool(run.alert or failed))


# =========================================================================== qos_classes
@task("qos_classes", klass="C1", tier="daily", title="Service class baselines", timeout=300)
def qos_classes(ctx: Ctx) -> Result:
    """Baseline cpu-shares (and, where they work, blkio-weight / memory-reservation) per class, via `docker update`.
    Idempotent: a container already at its target is skipped. Refuses anything below the floors, never lowers a
    database, never touches P0, never fights an active L3 throttle, never lowers a higher explicit P1 value."""
    cm = load_classes()
    if not cm.ok:
        return Result("skipped", "classes.toml missing or broken: nothing done", {"mode": "skipped"}, alert=False)
    info = gates.container_info()
    if info is None:
        return Result("skipped", "docker unavailable: nothing done", {"mode": "skipped"}, alert=False)
    hc = inspect_hc(sorted(info)) or {}
    floors = cm.section("floors")
    f_cpu, f_blk = int(_num(floors.get("cpu_shares_min"), 64)), int(_num(floors.get("blkio_weight_min"), 10))
    io_ok, mem_ok = io_weights_effective(), mem_reservation_effective()
    throttled = set((_task_state("pressure_response").get("throttled") or {}))
    exempt = [p for p in cm.lad.get("qos_unprotect", []) if isinstance(p, str)]
    rows: list[dict] = []
    n = {"done": 0, "would": 0, "ok": 0, "skip": 0, "fail": 0, "prot": 0, "unc": 0}
    for name in sorted(info):
        cls = cm.cls(name)
        h = hc.get(name)
        pol = cm.section("defaults").get(cls) or {}
        if not cm.of(name)[1]:                       # unknown is only a display default: no baseline for an unnamed container
            n["unc"] += 1
            continue
        if cls == "P0" or not pol or h is None:
            continue
        m = info[name]
        if name in throttled:
            n["skip"] += 1
            rows.append({"name": name, "class": cls, "state": "skipped: L3 throttle active"})
            continue
        flags: list[str] = []
        want_cpu = int(_num(pol.get("cpu_shares"), 0))
        if want_cpu:
            lowering = want_cpu < (h["shares"] or 1024)
            if want_cpu < f_cpu:
                rows.append({"name": name, "class": cls, "state": f"refused: cpu-shares {want_cpu} below floor {f_cpu}"})
                n["fail"] += 1
            elif lowering and _never_throttle(name, m.image, m.service):
                rows.append({"name": name, "class": cls, "state": "kept: database is never lowered"})
                n["skip"] += 1
            elif h["shares"] != want_cpu and not (cls == "P1" and h["shares"] > want_cpu) \
                    and not (cls == "P3" and 0 < h["shares"] < want_cpu):      # never fight a stricter setting
                flags += ["--cpu-shares", str(want_cpu)]
        want_blk = int(_num(pol.get("blkio_weight"), 0))
        if want_blk and io_ok and want_blk >= f_blk and h["blkio"] != want_blk \
                and not (want_blk < (h["blkio"] or 500) and _never_throttle(name, m.image, m.service)):
            flags += ["--blkio-weight", str(want_blk)]
        want_res = int(_num(pol.get("memory_reservation_mib"), 0)) * 1024 ** 2
        if want_res and mem_ok and h["resv"] != want_res and (h["mem"] == 0 or want_res <= h["mem"]):
            flags += ["--memory-reservation", str(want_res)]
        if not flags:
            n["ok"] += 1
            continue
        label = f"{' '.join(flags)}"

        def fn(name=name, flags=flags) -> None:
            docker_update(name, flags)

        try:
            status = _act(ctx, ctx.apply, "docker-update-qos", name, fn, exempt=exempt)
        except CapExceeded:
            rows.append({"name": name, "class": cls, "state": "deferred: per-run cap"})
            break
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, getattr(core, "_Timeout", ())):
                raise
            n["fail"] += 1
            rows.append({"name": name, "class": cls, "state": f"failed: {str(exc)[:60]}"})
            continue
        if status == "protected":                    # protected.toml names it and qos_unprotect does not: a config gap
            n["prot"] += 1
            rows.append({"name": name, "class": cls, "state": "refused: protected.toml (add to qos_unprotect)"})
            continue
        n["done" if status == "done" else "would" if status == "dry-run" else "fail"] += 1
        rows.append({"name": name, "class": cls, "state": {"done": "done: ", "dry-run": "would: "}.get(status, "refused: ") + label})
    tail = "" if io_ok else "; io weights skipped (no bfq/iocost on this host)"
    sel = n["done"] if ctx.apply else n["would"]
    summary = (f"set baseline on {sel} container(s)" if ctx.apply else f"report: would set baseline on {sel} container(s)") \
        + f", {n['ok']} already at policy" + (f", {n['skip']} kept" if n["skip"] else "") \
        + (f", {n['fail']} refused/failed" if n["fail"] else "") \
        + (f", {n['prot']} protected (not in qos_unprotect)" if n["prot"] else "") + tail
    metrics = {"mode": "apply" if ctx.apply else "report", "selected": sel, "already": n["ok"], "kept": n["skip"],
               "refused": n["fail"], "protected": n["prot"], "unclassified": n["unc"], "io_weights_effective": io_ok,
               "memory_reservation_effective": mem_ok}
    return Result("warn" if n["fail"] else "info" if sel or n["prot"] else "ok", _a(summary), metrics, rows[:12], alert=False)


# =========================================================================== bulkhead_check
def _ollama_env(unit: str) -> dict[str, str] | None:
    """OLLAMA_* variables of the unit (other variables are never read into the result: they may hold secrets)."""
    r = sh(["systemctl", "show", unit, "-p", "Environment", "--value"], timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    out = {}
    try:
        toks = shlex.split(r.stdout.strip())
    except ValueError:
        return None
    for t in toks:
        k, _, v = t.partition("=")
        if k.startswith("OLLAMA_"):
            out[k] = v
    return out


def _dur_s(v: str) -> float | None:
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([smh]?)", v.strip())
    if not m:
        return None
    x = float(m.group(1))
    return float("inf") if x < 0 else x * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def _plex_prefs() -> Path:
    return Path("/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Preferences.xml")


@task("bulkhead_check", klass="C0", tier="weekly", title="Concurrency limits (bulkheads)", timeout=120)
def bulkhead_check(ctx: Ctx) -> Result:
    """Compare agreed per-app concurrency limits (admission control) with what is configured. Read-only, info only."""
    cm = load_classes()
    bk = cm.section("bulkheads")
    items: list[dict] = []
    checked: list[str] = []
    P5 = "principle 5: admission control / bulkheads"

    def drift(item, setting, actual, rec, why):
        items.append({"item": item, "setting": setting, "actual": _a(actual, 40), "recommended": _a(rec, 40),
                      "why": _a(why, 150), "principle": P5})

    # -- Ollama (systemd unit environment)
    unit = str((ctx.protected.get("busy", {}) if isinstance(ctx.protected, dict) else {}).get("ollama_unit", "ollama.service"))
    env = _ollama_env(re.sub(r"\.service$", "", unit))
    if env is None:
        items.append({"item": "ollama", "setting": "-", "actual": "unreadable", "recommended": "-",
                      "why": "could not read the unit environment (systemctl show)", "principle": P5})
    else:
        checked.append("ollama")
        want_par, want_q = int(_num(bk.get("ollama_num_parallel"), 2)), int(_num(bk.get("ollama_max_queue"), 128))
        if "OLLAMA_NUM_PARALLEL" not in env:
            drift("ollama", "OLLAMA_NUM_PARALLEL", "unset (auto, up to 4)", str(want_par),
                  "each parallel slot multiplies the KV cache; at a 32k context that is a memory spike per request burst")
        elif env["OLLAMA_NUM_PARALLEL"].isdigit() and int(env["OLLAMA_NUM_PARALLEL"]) > want_par * 2:
            drift("ollama", "OLLAMA_NUM_PARALLEL", env["OLLAMA_NUM_PARALLEL"], str(want_par), "too many parallel slots")
        if "OLLAMA_MAX_QUEUE" not in env:
            drift("ollama", "OLLAMA_MAX_QUEUE", "unset (512)", str(want_q),
                  "a bounded queue answers 503 under overload instead of letting latency grow without limit")
        mx = int(_num(bk.get("ollama_max_loaded_models"), 3))
        if env.get("OLLAMA_MAX_LOADED_MODELS", "").isdigit() and int(env["OLLAMA_MAX_LOADED_MODELS"]) > mx:
            drift("ollama", "OLLAMA_MAX_LOADED_MODELS", env["OLLAMA_MAX_LOADED_MODELS"], str(mx), "more resident models than VRAM can hold")
        ka = _dur_s(env.get("OLLAMA_KEEP_ALIVE", "5m")) if "OLLAMA_KEEP_ALIVE" in env else None
        if ka is not None and ka > _num(bk.get("ollama_keep_alive_max_s"), 1800):
            drift("ollama", "OLLAMA_KEEP_ALIVE", env["OLLAMA_KEEP_ALIVE"], "<= 30m", "idle models would stay resident for hours")

    # -- ComfyUI (container flags + queue)
    cs = gates.container_state("comfyui")
    if cs == "running":
        checked.append("comfyui")
        r = sh(["docker", "inspect", "--format", "{{json .Config.Cmd}}", "comfyui"], timeout=20)
        cmd = r.stdout if r.returncode == 0 else ""
        for flag in bk.get("comfyui_required_flags", ["--disable-smart-memory"]):
            if isinstance(flag, str) and flag not in cmd:
                drift("comfyui", flag, "missing", "present", "ComfyUI keeps models in VRAM between jobs without it, squeezing Plex NVENC and Immich ML")
    # -- Open Notebook worker
    r = sh(["docker", "inspect", "--format", "{{range .Config.Env}}{{$kv := split . \"=\"}}{{if eq (index $kv 0) "
            "\"OPEN_NOTEBOOK_WORKER_MAX_TASKS\"}}{{println .}}{{end}}{{end}}", "open-notebook-open_notebook-1"], timeout=20)
    if r.returncode == 0:
        checked.append("open-notebook")
        m = re.search(r"^OPEN_NOTEBOOK_WORKER_MAX_TASKS=(\d+)\s*$", r.stdout, re.M)   # only this variable is read
        want = int(_num(bk.get("open_notebook_worker_max_tasks"), 1))
        if not m:
            drift("open-notebook", "OPEN_NOTEBOOK_WORKER_MAX_TASKS", "unset", str(want), "unbounded background tasks pile up behind one SurrealDB")
        elif int(m.group(1)) > want:
            drift("open-notebook", "OPEN_NOTEBOOK_WORKER_MAX_TASKS", m.group(1), str(want), "more concurrent notebook tasks than the store handles")
    # -- Plex transcode count (Preferences.xml is root-only)
    try:
        prefs = _plex_prefs().read_text(errors="replace")
        checked.append("plex")
        m = re.search(r'TranscodeCountLimit="(\d+)"', prefs)
        lim, want = (int(m.group(1)) if m else 0), int(_num(bk.get("plex_transcode_limit"), 4))
        if lim == 0 or lim > want * 2:
            drift("plex", "TranscodeCountLimit", "0 (unlimited)" if lim == 0 else str(lim), str(want),
                  "a burst of simultaneous transcodes is the biggest P1 CPU/GPU consumer and starves everything else")
    except OSError:
        items.append({"item": "plex", "setting": "TranscodeCountLimit", "actual": "unreadable", "recommended": "-",
                      "why": "Preferences.xml is root-only: run as root to check", "principle": P5})
    unread = [i for i in items if i["actual"] == "unreadable"]
    real = [i for i in items if i["actual"] != "unreadable"]
    # principle 2: the ladder only acts on containers classes.toml names; a new app nobody classified is the weekly note
    unc = unclassified(cm, gates.container_info())
    drift_n = len(real)
    metrics = {"checked": checked, "drift": drift_n, "unreadable": len(unread), "unclassified": len(unc),
               "immich_jobs": "not discoverable read-only (stored in the Immich database)"}
    if unc:
        real.append({"item": "classes", "setting": "classes.toml", "actual": _a(f"{len(unc)} unclassified: {', '.join(unc[:4])}", 40),
                     "recommended": "add to P1/P2/P3 in classes.toml",
                     "why": "an unclassified container is never slowed, restarted or stopped by the ladder, and shows as P2 only on the page",
                     "principle": "principle 2: classes of service"})
    summary = f"{drift_n} bulkhead drift(s) in {len(checked)} app(s)" + (f", {len(unread)} unreadable" if unread else "") \
        + (f", {len(unc)} unclassified container(s)" if unc else "") \
        if real or unread else f"bulkheads match the agreed limits ({', '.join(checked)})"
    return Result("info" if real else "ok", _a(summary), metrics, (real + unread)[:12], alert=False)


# =========================================================================== export (pressure.json)
def export(now: float | None = None) -> dict:
    """The public pressure.json: current level, 24 h level history, last 30 spikes, last 50 ladder actions and the
    class table. Pure reads of this module's own files; no secrets, only names, levels and short outcome words."""
    now = time.time() if now is None else now
    ps = _task_state("pressure_state")
    cm = load_classes()
    pol = {**POLICY_DEFAULT, **{k: v for k, v in cm.section("policy").items() if isinstance(v, str)}}
    mem = ps.get("members") if isinstance(ps.get("members"), dict) else {}
    rows = [r for r in _read_jsonl(spikes_path()) if r.get("kind") == "spike"]       # closed spikes, one line each
    if isinstance(ps.get("spike"), dict):                    # the spike in progress (not in the file until it ends)
        rows.append(_spike_record(ps["spike"], "open", None, now, ps.get("oom_prev")))
    spikes = sorted(rows, key=lambda r: r.get("t", 0))[-30:]
    spikes = [{k: (_a(v, 160) if isinstance(v, str) else v) for k, v in r.items() if k != "kind"} for r in spikes][::-1]
    actions = [{k: r.get(k) for k in ("ts", "level", "rung", "action", "target", "class", "outcome")}
               for r in _read_jsonl(log_path())[-50:]][::-1]
    modes: dict[str, str] = {}
    try:
        t = core.load_config().get("tasks", {}).get("pressure_response", {})
        modes = {r: (t.get(r) if t.get(r) in ("apply", "report", "off") else d) for r, d in RUNG_DEFAULTS.items()}
        if t.get("mode") == "report":
            modes = {r: "report" if m == "apply" else m for r, m in modes.items()}
    except Exception:  # noqa: BLE001
        modes = dict(RUNG_DEFAULTS)
    names = {2: "reclaim", 3: "throttle", 4: "restart", 5: "emergency"}
    since = ps.get("since")
    dims = {d: int((ps.get("dims", {}).get(d, {}) or {}).get("level", 0)) for d in DIMS} if ps else {}
    lvl = min(max(int(ps.get("level", 0)), 0), 5) if ps else 0
    # `level` is for the dashboard; `gate_level` (memory and cpu only) is what other consumers should gate on. A state file
    # written before gate_level existed is read through its dims.
    gate = int(ps["gate_level"]) if isinstance(ps.get("gate_level"), int) else gate_level(dims)
    label = ps["level_name"] if isinstance(ps.get("level_name"), str) and ps["level_name"] else \
        (level_label(lvl, dims) if dims else LEVEL_NAMES[lvl])
    return {"generated_at": now, "level": lvl, "gate_level": gate, "since": since,
            "level_name": _a(label, 40) if ps else "unknown",
            "unclassified": [_a(n, 40) for n in (ps.get("unclassified") or [])][:40] if isinstance(ps.get("unclassified"), list) else [],
            "dims": dims,
            "history": [{"t": x["t"], "level": x["level"]} for x in ps.get("history", [])
                        if isinstance(x, dict) and now - x.get("t", 0) < 86400][-96:],
            "spikes": spikes, "actions": actions,
            "classes": [{"class": c, "members": list(mem.get(c, []))[:80], "policy": pol[c]} for c in CLASSES],
            "ladder": [{"level": lv, "name": nm, "what": txt, "mode": modes.get(names.get(lv, ""), "always")}
                       for lv, nm, txt in LADDER_TEXT]}
