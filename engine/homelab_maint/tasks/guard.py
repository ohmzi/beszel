"""guard: memory-spike sampling, stuck-container detection, orphan-process report, image ledger.

All four are C0 (they never touch the workload). spike_sampler and image_ledger write the tool's OWN state
(samples.jsonl / ledger/images.json), which is not a mutation of the host.

Samples live in STATE_DIR/samples.jsonl, NOT in core's history.jsonl: a sample is ~9 KB (72 containers), i.e. over half
of everything the check tier writes, and core.append_history rewrites its whole file once it passes 20 MB. Read them with
read_samples(since_s) (same record format core.read_history(since_s, "sample") used to return).

Image ledger file (STATE_DIR/ledger/images.json), read it with load_ledger() / ledger_last_seen():
    {"version": 1, "created": <epoch>, "updated": <epoch>,
     "images": {"sha256:<64 hex>": {"first_seen": <epoch>, "last_seen": <epoch>, "names": ["container", ...]}}}
`created` is when the ledger started: an image that is absent from it is only "unused for N days" if the ledger
itself is older than N days (see ledger_age_days()).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .. import swapwatch
from ..core import GIB, STATE_DIR, Ctx, Result, human, read_json, sh, task, write_json_atomic
from . import gates

MIB = 1024 ** 2
SAMPLE_TOP_PROCS = 8


def _ascii(s: Any, n: int = 140) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


# =========================================================================== sample store
_T_PREFIX = re.compile(rb'\{"t":\s*([0-9.]+)')


def _samples_path() -> Path:
    return STATE_DIR / "samples.jsonl"


def _line_t(ln: bytes) -> float:
    """Timestamp of a samples line. Lines start with {"t":...}, so the common case never runs json.loads."""
    m = _T_PREFIX.match(ln)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    try:
        return float(json.loads(ln).get("t", 0))
    except (ValueError, AttributeError, TypeError):
        return 0.0                                      # corrupt/partial line: oldest possible, so a trim drops it


def _trim_samples(p: Path, cutoff: float) -> None:
    """Drop lines older than `cutoff`, but only when the FIRST line is more than a day past it: the file is rewritten
    about once a day instead of on every append. Temp file + fsync + os.replace, so a SIGALRM or an OOM kill at any
    point leaves either the old or the new file, never a truncated one. Caller holds the lock."""
    with open(p, "rb") as f:
        first = f.readline()
    if not first or _line_t(first) >= cutoff - 86400:
        return
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(p, "rb") as src, open(tmp, "wb") as dst:
            for ln in src:
                if _line_t(ln) >= cutoff:
                    dst.write(ln)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, os.stat(p).st_mode & 0o777)
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()                                # only still there when something above failed
        except OSError:
            pass


def append_sample(rec: dict, keep_days: float, now: float) -> None:
    """Append one compact record to samples.jsonl and trim it (see _trim_samples). Serialised with flock."""
    p = _samples_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
    with open(p.with_name("samples.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(p, "a") as f:
            f.write(line)
        try:
            _trim_samples(p, now - keep_days * 86400)
        except OSError:
            pass                                        # trimming is housekeeping: the sample itself is safely written


def read_samples(since_s: float, now: float | None = None) -> list[dict]:
    """Sample records newer than now - since_s, oldest first (as written). Corrupt lines are skipped."""
    cutoff = (time.time() if now is None else now) - since_s
    out: list[dict] = []
    try:
        with open(_samples_path(), "rb") as f:
            for ln in f:
                if _line_t(ln) < cutoff:
                    continue                            # cheap prefix check: no JSON parsing for old lines
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


# =========================================================================== spike_sampler
def parse_psi(text: str | None) -> dict[str, dict[str, float]]:
    """'some avg10=0.00 avg60=0.00 avg300=0.00 total=1' lines -> {"some": {"avg10":..}, "full": {...}}."""
    out: dict[str, dict[str, float]] = {}
    for ln in (text or "").splitlines():
        parts = ln.split()
        if parts and parts[0] in ("some", "full"):
            d = {}
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                try:
                    d[k] = float(v)
                except ValueError:
                    pass
            out[parts[0]] = d
    return out


def _psi60(path: Path | str, kind: str) -> float:
    return parse_psi(gates.read_text(path)).get(kind, {}).get("avg60", 0.0)


def _io_bytes(d: Path) -> int | None:
    """rbytes+wbytes summed over devices; None when io.stat is absent (io controller off) or has no such counters."""
    vals = re.findall(r"\b(?:rbytes|wbytes)=(\d+)", gates.read_text(d / "io.stat") or "")
    return sum(int(v) for v in vals) if vals else None


def _cg_sample(d: Path) -> tuple[dict, list[int]] | None:
    """One container's counters from its cgroup v2 dir, plus its pids. None if the cgroup vanished."""
    cur = gates.read_int(d / "memory.current")
    stat = gates.read_text(d / "memory.stat")
    if cur is None or not stat:
        return None
    anon = file_ = 0
    for ln in stat.splitlines():
        if ln.startswith("anon "):
            anon = int(ln.split()[1])
        elif ln.startswith("file "):
            file_ = int(ln.split()[1])
    oom = 0
    for ln in (gates.read_text(d / "memory.events") or "").splitlines():
        if ln.startswith("oom_kill "):
            oom = int(ln.split()[1])
    pids = [int(x) for x in (gates.read_text(d / "cgroup.procs") or "").split() if x.isdigit()]
    rec = {"anon": anon, "file": file_, "swap": gates.read_int(d / "memory.swap.current") or 0, "cur": cur,
           "peak": gates.read_int(d / "memory.peak") or 0, "cpu_us": gates.cpu_usec(d), "io_b": _io_bytes(d),
           "oom": oom, "pressure_full60": round(_psi60(d / "memory.pressure", "full"), 1)}
    # ~1300 samples x 72 containers for 14 days: leave out the fields that are almost always zero (readers use
    # .get(key, 0)). cpu_us / io_b are different: None means "this cgroup cannot tell" (controller off, file gone),
    # which is NOT zero progress, so the key is omitted and stuck_detector then refuses to judge the container.
    for k in ("swap", "oom", "pressure_full60"):
        if not rec[k]:
            del rec[k]
    for k in ("cpu_us", "io_b"):
        if rec[k] is None:
            del rec[k]
    return rec, pids


def _meminfo() -> dict[str, int]:
    out = {}
    for ln in (gates.read_text(gates.PROC / "meminfo") or "").splitlines():
        k, _, v = ln.partition(":")
        if k in ("MemAvailable", "SwapTotal", "SwapFree", "MemTotal"):
            try:
                out[k] = int(v.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    return out


def _vmstat() -> dict[str, int]:
    out = {}
    for ln in (gates.read_text(gates.PROC / "vmstat") or "").splitlines():
        k, _, v = ln.partition(" ")
        if k in ("pswpin", "pswpout", "oom_kill"):
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def _gpu() -> list[int] | None:
    """[util %, vram MiB] summed over GPUs, or None when nvidia-smi is missing/failing."""
    r = sh(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"], timeout=8)
    if r.returncode != 0:
        return None
    util = mem = n = 0
    for ln in r.stdout.splitlines():
        try:
            u, m = (int(x.strip()) for x in ln.split(","))
        except ValueError:
            continue
        util, mem, n = max(util, u), mem + m, n + 1
    return [util, mem] if n else None


def _host_sample() -> dict:
    mi, vm = _meminfo(), _vmstat()
    p = gates.PROC / "pressure"
    return {"mem_avail": mi.get("MemAvailable", 0),
            "swap_used": max(mi.get("SwapTotal", 0) - mi.get("SwapFree", 0), 0),
            "psi_mem_some60": _psi60(p / "memory", "some"), "psi_mem_full60": _psi60(p / "memory", "full"),
            "psi_io_some60": _psi60(p / "io", "some"), "psi_io_full60": _psi60(p / "io", "full"),
            "psi_cpu_some60": _psi60(p / "cpu", "some"),
            "pswpin": vm.get("pswpin", 0), "pswpout": vm.get("pswpout", 0), "oom_kill": vm.get("oom_kill", 0),
            "gpu": _gpu()}


def _top_host_procs(skip: set[int], n: int = SAMPLE_TOP_PROCS) -> list[list]:
    """Top-n processes by anonymous memory that are NOT inside a container: [name, pid, anon_kb]."""
    rows = []
    for ent in os.listdir(gates.PROC):
        if not ent.isdigit() or int(ent) in skip:
            continue
        t = gates.read_text(f"{gates.PROC}/{ent}/status")
        if not t:
            continue
        m = re.search(r"^RssAnon:\s+(\d+)\s*kB", t, re.M)
        nm = re.search(r"^Name:\s+(.*)$", t, re.M)
        if m and nm:                                    # kernel threads have no RssAnon line
            rows.append([nm.group(1).strip()[:32], int(ent), int(m.group(1))])
    rows.sort(key=lambda r: -r[2])
    return rows[:n]


@task("spike_sampler", klass="C0", tier="check", title="Memory spike sampler", timeout=60)
def spike_sampler(ctx: Ctx) -> Result:
    names = gates.containers()
    c: dict[str, dict] = {}
    skipped = 0
    cpids: set[int] = set()
    for name, cid in (names or {}).items():
        d = gates.cg_dir(cid)
        s = _cg_sample(d) if d else None
        if s is None:
            skipped += 1                              # exited between `docker ps` and the read
            continue
        c[name] = s[0]
        cpids.update(s[1])
    rec: dict[str, Any] = {"t": ctx.now, "kind": "sample", "host": _host_sample(), "c": c,
                           "p": _top_host_procs(cpids)}
    if names is None:
        rec["err"] = "docker ps failed"               # keep host data, stuck_detector ignores empty `c`
    append_sample(rec, float(ctx.opt("keep_days", 14)), ctx.now)

    anon_total = sum(v["anon"] for v in c.values())
    top = sorted(c.items(), key=lambda kv: -kv[1]["anon"])
    h = rec["host"]
    metrics = {"containers": len(c), "skipped": skipped, "anon_total_gib": round(anon_total / GIB, 1),
               "anon_total_h": human(anon_total),
               "largest": [{"name": n, "anon_h": human(v["anon"])} for n, v in top[:3]],
               "mem_avail_h": human(h["mem_avail"]), "psi_mem_full60": h["psi_mem_full60"],
               "gpu_util": h["gpu"][0] if h["gpu"] else None,
               "gpu_vram_h": human(h["gpu"][1] * MIB) if h["gpu"] else None}
    items = [{"name": n, "anon_h": human(v["anon"]), "swap_h": human(v.get("swap", 0)), "cur_h": human(v["cur"]),
              "peak_h": human(v["peak"])} for n, v in top[:12]]
    if names is None:
        return Result("warn", "docker ps failed: container sample skipped (host sample recorded)", metrics, items)
    if skipped and (not c or skipped > 0.2 * len(names)):
        # docker lists the containers but their cgroups cannot be read (cgroup driver/layout change, custom
        # --cgroup-parent): the whole guard subsystem would be blind while looking healthy. A few vanishing
        # between `docker ps` and the read is normal and stays ok.
        return Result("warn", _ascii(f"{skipped} of {len(names)} cgroups unreadable (layout changed?); "
                                     f"sampled {len(c)}, {human(anon_total)} anon"), metrics, items)
    big = ", ".join(f"{n} {v['anon'] / GIB:.1f}G" for n, v in top[:3])
    return Result("ok", _ascii(f"{len(c)} containers, {human(anon_total)} anon; top: {big}"), metrics, items)


# =========================================================================== stuck_detector
# container name -> gate that knows whether its application is working (queue/jobs/streams)
_APP_GATES = [(r"comfyui", "comfyui"), (r"^ollama", "ollama"), (r"immich", "immich"), (r"plex", "plex"),
              (r"buildkit", "docker_build")]
# protected.toml matches NAMES only. Databases and build daemons must be recognised by what they RUN as well: an
# `afsaane-prod-db` is postgres:17-alpine and a quiet buildx builder looks idle between builds. Matched against
# "<name> <image> <compose service>"; a false positive only means "reported, never restarted".
_NEVER_RESTART = re.compile(
    r"postgres|postgis|timescale|mysql|mariadb|percona|redis|valkey|keydb|mongo|etcd|cassandra|couchdb|influxdb|"
    r"clickhouse|elasticsearch|opensearch|rabbitmq|memcached|minio|qdrant|surreal|meilisearch|kafka|zookeeper|"
    r"buildkit|buildx_|[-_.]db\d*(?:[-_.]|$)|database", re.I)


def _is_protected(ctx: Ctx, name: str, image: str = "", service: str = "") -> bool:
    return ctx.is_protected(name, image, service) or bool(_NEVER_RESTART.search(f"{name} {image} {service}"))


def _slope(ts: list[float], ys: list[float]) -> float:
    """Least-squares slope (units per second)."""
    n = len(ts)
    mt, my = sum(ts) / n, sum(ys) / n
    den = sum((t - mt) ** 2 for t in ts)
    return sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / den if den else 0.0


def _pressure(ctx: Ctx, win: list[dict]) -> tuple[str, dict]:
    """'none' | 'warn' | 'crit' from the same thresholds memory_health uses, plus swap-in over the window."""
    mh = ctx.cfg.get("tasks", {}).get("memory_health", {})
    last, first = win[-1].get("host", {}), win[0].get("host", {})
    dt = max(win[-1]["t"] - win[0]["t"], 1)
    swap_in = 0.0
    if "pswpin" in last and "pswpin" in first:          # counter is in pages; absent in old/partial samples
        swap_in = swapwatch.discount(max(last["pswpin"] - first["pswpin"], 0), win[0]["t"], win[-1]["t"]) / dt     # a deliberate relief is not pressure
    full = last.get("psi_mem_full60", 0.0)
    avail = last.get("mem_avail")                      # missing host data is "unknown", never "pressure"
    low = lambda gib: avail is not None and avail < float(mh.get(gib[0], gib[1])) * GIB   # noqa: E731
    lvl = "none"
    if full >= float(mh.get("psi_mem_full_warn", 5.0)) or low(("mem_available_warn_gib", 10)) \
            or swap_in >= float(mh.get("swap_in_pages_per_s_warn", 2000)):
        lvl = "warn"
    if full >= float(mh.get("psi_mem_full_crit", 15.0)) or low(("mem_available_crit_gib", 4)):
        lvl = "crit"
    return lvl, {"psi_mem_full60": full, "mem_avail_h": human(avail) if avail is not None else "?",
                 "swap_in_pps": round(swap_in)}


def _restart_allowed(state: dict, name: str, now: float) -> tuple[bool, str]:
    """At most 2 restarts per 6 h per container, with exponential backoff 30 min * 2^(n-1) after n restarts/24 h."""
    hist = [t for t in state.get("restarts", {}).get(name, []) if now - t < 86400]
    if len([t for t in hist if now - t < 6 * 3600]) >= 2:
        return False, "2 restarts in the last 6 h"
    if hist:
        wait = 1800 * 2 ** (len(hist) - 1)
        if now - max(hist) < wait:
            return False, f"backoff: wait {wait // 60} min between restarts"
    return True, ""


def _docker_restart(name: str) -> None:
    r = sh(["docker", "restart", "-t", "60", name], timeout=150)
    if r.returncode != 0:
        raise RuntimeError(f"docker restart {name} rc={r.returncode}")


def _known(x: dict) -> bool:
    """Both progress counters were actually read (a missing key means the cgroup could not tell)."""
    return all(isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool) for k in ("cpu_us", "io_b"))


def _evaluate(win, name, lc, dt, min_mem, grow_lim, idle_cpu, idle_io) -> dict | None:
    """The stuck-candidate rule for one container over the window; None when it is fine OR cannot be judged."""
    series = [s["c"].get(name) for s in win]
    if any(x is None for x in series):
        return None                                    # started mid-window: no honest rate
    if not all(_known(x) for x in series):
        return None                                    # a counter was unreadable: unknown is not "idle", say nothing
    if any(b["cpu_us"] < a["cpu_us"] or b["io_b"] < a["io_b"] for a, b in zip(series, series[1:])):
        return None                                    # counters went backwards => container restarted
    mem = lc["anon"] + lc.get("swap", 0)
    if mem < min_mem:
        return None
    cpu_pct = (lc["cpu_us"] - series[0]["cpu_us"]) / (dt * 1e6) * 100
    io_mib_min = (lc["io_b"] - series[0]["io_b"]) / MIB / (dt / 60)
    if cpu_pct >= idle_cpu or io_mib_min >= idle_io:
        return None                                    # making progress
    growth = _slope([s["t"] for s in win], [x["anon"] + x.get("swap", 0) for x in series]) * 3600 / GIB
    if growth > grow_lim:
        reason = "leak/runaway: memory growing with no CPU or IO progress"
    elif growth >= -grow_lim:
        reason = "idle but holding: large and flat, no CPU or IO progress"
    else:
        return None                                    # shrinking: it is releasing memory by itself
    return {"name": name, "mem": mem, "anon_h": human(lc["anon"]), "swap_h": human(lc.get("swap", 0)),
            "growth_gib_h": round(growth, 2), "cpu_pct": round(cpu_pct, 2), "io_kib_min": round(io_mib_min * 1024),
            "reason": reason}


def _annotate(ctx: Ctx, cands: list[dict]) -> None:
    """Add protected / busy / image to each candidate, in place.

    Protection needs the container's CURRENT image and compose service, which the samples do not carry, so docker is
    asked once (only when there is a candidate). If it cannot answer, or the container is gone, nothing is verified
    and the candidate counts as protected: unknown means do nothing."""
    info = gates.container_info() if cands else {}
    gate_cache: dict[str, tuple[bool, str]] = {}
    for c in cands:
        meta = info.get(c["name"]) if info is not None else None
        image, service = (meta.image, meta.service) if meta else ("", "")
        c["image"] = image[:40]
        c["_ident"] = (c["name"], image, service)
        c["protected"] = meta is None or _is_protected(ctx, c["name"], image, service)
        if meta is None:
            c["note"] = "docker ps failed: protection unverified" if info is None else "no longer running"
        c["busy"] = ""
        for rx, gate in _APP_GATES:                    # ask the app's own gate whether it is working
            if re.search(rx, " ".join(c["_ident"]), re.I):
                if gate not in gate_cache:
                    gate_cache[gate] = gates.busy(gate, ctx.cfg)
                if gate_cache[gate][0]:
                    c["busy"] = gate_cache[gate][1]
                break


def _stuck_candidates(ctx: Ctx, win: list[dict]) -> list[dict]:
    first, last = win[0], win[-1]
    dt = last["t"] - first["t"]
    min_mem = float(ctx.opt("min_anon_gib", 4)) * GIB
    grow_lim = float(ctx.opt("growth_gib_per_hour", 0.5))
    idle_cpu = float(ctx.opt("idle_cpu_pct", 1.0))
    idle_io = float(ctx.opt("idle_io_mib_per_min", 1.0))
    out = []
    for name, lc in last["c"].items():
        try:
            cand = _evaluate(win, name, lc, dt, min_mem, grow_lim, idle_cpu, idle_io)
        except (KeyError, TypeError, ValueError):
            continue                                   # malformed sample entry: say nothing rather than guess
        if cand:
            out.append(cand)
    _annotate(ctx, out)
    out.sort(key=lambda c: (c["protected"] or bool(c["busy"]), -c["mem"]))
    return out


def _row(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "mem" and not k.startswith("_")}


@task("stuck_detector", klass="C0", tier="check", title="Stuck containers", timeout=60)
def stuck_detector(ctx: Ctx) -> Result:
    need = int(ctx.opt("min_samples", 6))
    max_h = float(ctx.opt("max_window_hours", 6))
    dead_h = float(ctx.opt("no_data_hours", 2))
    every = [r for r in read_samples(max(max_h, 2 * dead_h) * 3600, ctx.now) if isinstance(r.get("t"), (int, float))]
    every.sort(key=lambda r: r["t"])
    recs = [r for r in every if isinstance(r.get("c"), dict) and r["c"] and r["t"] >= ctx.now - max_h * 3600]
    win = recs[-need:]
    enforce = ctx.opt("enforce") is True
    # Records keep arriving but none carries container data (cgroup layout change, docker down): the sampler's own
    # warn can be missed, and "collecting samples" would then be shown forever. Say it plainly after `no_data_hours`.
    if every and ctx.now - every[-1]["t"] <= float(ctx.opt("max_sample_age_min", 45)) * 60:
        last_ok = max((r["t"] for r in every if isinstance(r.get("c"), dict) and r["c"]), default=every[0]["t"])
        if every[-1]["t"] - last_ok >= dead_h * 3600:
            usable = sum(1 for r in every if isinstance(r.get("c"), dict) and r["c"])
            gap_h = (every[-1]["t"] - last_ok) / 3600
            return Result("warn", _ascii(f"no usable samples for {gap_h:.1f} h: {usable} of {len(every)} records have "
                                         f"container data (cgroups unreadable?)"),
                          {"samples": len(win), "records": len(every), "usable": usable, "enforce": enforce})
    if len(win) < need:
        return Result("info", f"collecting samples: {len(win)}/{need}", {"samples": len(win), "enforce": enforce},
                      alert=False)
    age_min = (ctx.now - win[-1]["t"]) / 60
    if age_min > float(ctx.opt("max_sample_age_min", 45)):          # a dead sampler must not look like "stuck"
        return Result("info", f"newest sample is {age_min:.0f} min old; sampler not running?",
                      {"samples": len(win), "enforce": enforce}, alert=False)
    span_min = (win[-1]["t"] - win[0]["t"]) / 60
    if span_min < float(ctx.opt("min_window_min", 30)):
        return Result("info", f"samples span only {span_min:.0f} min, need more time", {"samples": len(win)},
                      alert=False)

    cands = _stuck_candidates(ctx, win)
    pressure, pm = _pressure(ctx, win)
    actionable = [c for c in cands if not c["protected"] and not c["busy"]]
    metrics = {"samples": len(win), "window_min": round(span_min), "candidates": len(cands),
               "actionable": len(actionable), "pressure": pressure, "enforce": enforce, **pm}

    # --- restart path: dormant (enforce = false, and a C0 task never has ctx.apply). One restart per run, only
    # the largest actionable candidate, only under CRITICAL memory pressure, rate-limited with backoff.
    restarted = ""
    if enforce and ctx.apply and pressure == "crit":
        for c in actionable:
            ok, why = _restart_allowed(ctx.state, c["name"], ctx.now)
            if not ok:
                c["note"] = why
                continue
            try:
                done = ctx.act("docker restart", c["name"], 0, lambda n=c["name"]: _docker_restart(n),
                               protect_names=c["_ident"])
            except RuntimeError as exc:
                c["note"] = _ascii(exc, 80)
                break
            if done:
                st = ctx.state.setdefault("restarts", {})
                st[c["name"]] = [t for t in st.get(c["name"], []) if ctx.now - t < 86400] + [ctx.now]
                restarted = c["name"]
            break
        metrics["restarted"] = restarted
    items = [_row(c) for c in cands[:12]]

    ptxt = f"; mem pressure {pressure}" if pressure != "none" else ""
    if not cands:
        return Result("info" if pressure != "none" else "ok",
                      _ascii(f"no stuck containers ({len(win)} samples, {span_min:.0f} min){ptxt}"), metrics, items,
                      alert=False)
    top = cands[0]
    head = f"{len(cands)} candidate(s), {len(actionable)} actionable"
    if restarted:
        head += f", restarted {restarted}"
    summary = _ascii(f"{head}: {top['name']} {human(top['mem'])} {top['reason'].split(':')[0]}{ptxt}")
    # warn only for real, unprotected, non-busy candidates; protected or legitimately busy ones are information
    return Result("warn" if actionable else "info", summary, metrics, items, alert=bool(actionable))


# =========================================================================== orphan_report
_KIND_ORDER = {"emulator-orphan": 0, "gradle-idle": 1, "stray-server": 2, "crashpad-orphan": 3,
               "emulator-long": 4, "zombie": 5}
_DEFAULT_STRAYS = [r"^python3?(?:\.\d+)? (?:\S*/)?slow\.py\b"]


def _reparented(p: gates.Proc, by_pid: dict[int, gates.Proc]) -> bool:
    """True when the parent chain is gone: re-parented to init or to a systemd (user) subreaper."""
    par = by_pid.get(p.ppid)
    return p.ppid <= 1 or par is None or par.comm.startswith("systemd")


def _uptime_s() -> float:
    try:
        return float((gates.read_text(gates.PROC / "uptime") or "").split()[0])
    except (IndexError, ValueError):
        return 0.0


@task("orphan_report", klass="C0", tier="check", title="Orphaned processes", timeout=60)
def orphan_report(ctx: Ctx) -> Result:
    procs = gates.scan_procs()                         # OSError => runner records an error, nothing is guessed
    by_pid = {p.pid: p for p in procs}
    up = _uptime_s()
    hz = gates.CLK_TCK

    def age_h(p):
        return max(up - p.start / hz, 0) / 3600

    items: list[dict] = []
    anon_kb = 0

    def add(kind, p, **extra):
        nonlocal anon_kb
        kb = gates.rss_anon_kb(p.pid) or 0
        anon_kb += kb
        items.append({"kind": kind, "pid": p.pid, "name": p.exe[:32], "age_h": round(age_h(p), 1),
                      "anon_h": human(kb * 1024), **extra})

    # -- idle Gradle / Kotlin daemons, judged from CPU ticks remembered between runs (ctx.state)
    live_build = any(p.state != "Z" and gates.GRADLE_WORKERS.search(p.cmd) for p in procs)
    idle_s = float(ctx.opt("idle_hours", 3)) * 3600
    idle_cpu = float(ctx.opt("idle_cpu_pct", 0.5))
    seen, nxt = ctx.state.get("gradle", {}), {}
    n_gradle = 0
    for p in procs:
        if p.state == "Z" or not gates.GRADLE_DAEMONS.search(p.cmd):
            continue
        key = f"{p.pid}:{p.start}"                     # start time guards against pid reuse
        prev = seen.get(key)
        if prev is None:
            rec = {"ticks": p.ticks, "t": ctx.now, "since": ctx.now}
        elif ctx.now - prev["t"] < 60:                 # two runs seconds apart tell nothing; keep the old baseline
            rec = prev
        else:
            pct = max(p.ticks - prev["ticks"], 0) / hz / (ctx.now - prev["t"]) * 100
            rec = {"ticks": p.ticks, "t": ctx.now, "since": ctx.now if pct > idle_cpu else prev["since"]}
        nxt[key] = rec
        if not live_build and ctx.now - rec["since"] >= idle_s:
            n_gradle += 1
            add("gradle-idle", p, idle_h=round((ctx.now - rec["since"]) / 3600, 1))
    ctx.state["gradle"] = nxt

    # -- headless emulators: orphaned (parent chain gone) vs merely long-running
    emu_alive = False
    n_emu = n_emu_long = 0
    long_h = float(ctx.opt("emulator_warn_hours", 24))
    for p in procs:
        if p.state == "Z":
            continue
        if p.exe.startswith("qemu-system") or p.exe == "emulator":
            emu_alive = True
        if p.exe.startswith("qemu-system") and "-avd" in p.argv:
            avd = p.argv[p.argv.index("-avd") + 1] if p.argv.index("-avd") + 1 < len(p.argv) else "?"
            if _reparented(p, by_pid):
                n_emu += 1
                add("emulator-orphan", p, avd=_ascii(avd, 40))
            elif age_h(p) >= long_h:
                n_emu_long += 1
                add("emulator-long", p, avd=_ascii(avd, 40))       # parent alive: informational only

    # -- stray test servers, orphan crashpad handlers (exact name: Chrome's chrome_crashpad_handler is not one)
    strays = [re.compile(x) for x in ctx.opt("stray_patterns", _DEFAULT_STRAYS)]
    n_stray = n_crash = n_zombie = 0
    for p in procs:
        if p.state == "Z":
            n_zombie += 1
            if n_zombie <= 6:
                items.append({"kind": "zombie", "pid": p.pid, "name": p.comm[:32], "ppid": p.ppid})
            continue
        if any(r.search(" ".join([p.exe] + p.argv[1:])) for r in strays):
            n_stray += 1
            add("stray-server", p)
        elif p.exe == "crashpad_handler" and _reparented(p, by_pid) and not emu_alive:
            n_crash += 1
            add("crashpad-orphan", p)

    total = n_gradle + n_emu + n_stray + n_zombie + n_crash
    metrics = {"gradle_idle": n_gradle, "emulator_orphans": n_emu, "emulators_long": n_emu_long,
               "strays": n_stray, "zombies": n_zombie, "crashpad": n_crash, "findings": total,
               "reported_anon_h": human(anon_kb * 1024)}
    items.sort(key=lambda i: _KIND_ORDER.get(i["kind"], 9))     # real offenders first, zombies (no memory) last
    parts = [f"{n} {w}{'s' if n != 1 else ''}" for n, w in (
        (n_gradle, "idle gradle daemon"), (n_emu, "orphan emulator"), (n_stray, "stray server"),
        (n_crash, "orphan crashpad handler"), (n_zombie, "zombie"), (n_emu_long, "long-running emulator")) if n]
    if not parts:
        return Result("ok", "no orphaned or idle build processes", metrics, items[:12], alert=False)
    summary = _ascii(f"{', '.join(parts)} ({human(anon_kb * 1024)} anon)")
    huge = anon_kb * 1024 >= float(ctx.opt("alert_gib", 8)) * GIB or n_zombie >= int(ctx.opt("alert_zombies", 100))
    return Result("warn" if huge else "info", summary, metrics, items[:12], alert=huge)


# =========================================================================== image_ledger
_IMG = re.compile(r"sha256:[0-9a-f]{64}")


def _ledger_path() -> Path:
    return STATE_DIR / "ledger" / "images.json"


def load_ledger() -> dict:
    """The ledger dict (empty skeleton if absent or unreadable; callers must then treat everything as unknown)."""
    d = read_json(_ledger_path(), None)
    if not isinstance(d, dict) or not isinstance(d.get("images"), dict):
        return {"version": 1, "created": None, "updated": None, "images": {}}
    return d


def ledger_last_seen(image_id: str) -> float | None:
    """Epoch a container last referenced this image ID, or None if never seen."""
    e = load_ledger()["images"].get(image_id)
    return float(e["last_seen"]) if isinstance(e, dict) and "last_seen" in e else None


def ledger_age_days(now: float | None = None) -> float | None:
    """How long the ledger has existed; None if it does not exist yet."""
    c = load_ledger().get("created")
    return None if c is None else ((now if now is not None else time.time()) - float(c)) / 86400


@task("image_ledger", klass="C0", tier="check", title="Image usage ledger", timeout=90)
def image_ledger(ctx: Ctx) -> Result:
    ps = sh(["docker", "ps", "-a", "-q", "--no-trunc"], timeout=30)
    if ps.returncode != 0:
        return Result("warn", "docker ps failed: ledger not updated", {"updated": False})
    ids = [x for x in ps.stdout.split() if re.fullmatch(r"[0-9a-f]{12,64}", x)]
    refs: dict[str, set[str]] = {}
    partial = False
    for i in range(0, len(ids), 100):                  # keep argv small
        r = sh(["docker", "inspect", "--format", "{{.Image}} {{.Name}}", *ids[i:i + 100]], timeout=60)
        partial = partial or r.returncode != 0         # a container may vanish mid-run: use what we got
        for ln in r.stdout.splitlines():
            img, _, nm = ln.strip().partition(" ")
            if _IMG.fullmatch(img):
                refs.setdefault(img, set()).add(nm.lstrip("/")[:40])
    if ids and not refs:
        return Result("warn", "docker inspect returned no image ids: ledger not updated", {"updated": False})

    led = load_ledger()
    now = ctx.now
    led["created"] = led.get("created") or now
    imgs = led["images"]
    added = 0
    for img, names in refs.items():
        e = imgs.get(img)
        if not isinstance(e, dict):
            e = {"first_seen": now}
            added += 1
        e["last_seen"] = now
        e["names"] = sorted(names)[:3]
        imgs[img] = e
    keep = float(ctx.opt("keep_days", 30)) * 86400
    stale = [k for k, e in imgs.items() if k not in refs and now - float(e.get("last_seen", 0)) > keep]
    for k in stale:
        del imgs[k]
    led.update(version=1, updated=now)
    write_json_atomic(_ledger_path(), led, 0o644)

    on_host: set[str] = set()
    r = sh(["docker", "images", "--no-trunc", "-q"], timeout=30)
    if r.returncode == 0:
        on_host = {x for x in r.stdout.split() if _IMG.fullmatch(x)}
    unref = len(on_host - set(refs))
    metrics = {"tracked": len(imgs), "referenced": len(refs), "added": added, "expired": len(stale),
               "images_on_host": len(on_host), "unreferenced": unref, "partial": partial,
               "ledger_age_days": round(ledger_age_days(now) or 0, 1)}
    return Result("ok", _ascii(f"{len(refs)} images in use, {unref} unreferenced on host, ledger {len(imgs)} entries"),
                  metrics)
