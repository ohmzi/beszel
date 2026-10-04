"""7-day hourly ring buffer of temperatures, fan speeds and load (SPEC2 section 3).

  python3 -m homelab_maint.metrics_ring sample [-v]    take one sample and fold it into the ring (timer: every 60 s)
  python3 -m homelab_maint.metrics_ring export [--pretty]   print the JSON the widgets and the web UI show (no sensors)

Ring file STATE_DIR/metrics-ring.json (0644), 168 slots, one per hour:
  {"v":1,"slots":168,"hours":[{"h":H,"n":samples,"sum":{m:x},"cnt":{m:k},"max":{m:x},"min":{m:x}} x168],
   "last":{"t":sampled_at,"cur":{m:x},"cpu":[jiffies_total,jiffies_idle],"rej":n}}
`H` is the hour number (epoch // 3600), the slot index is H % 168. A sample for hour H into a slot holding another hour
RESETS the slot first: that is the overwrite ("the hour 7 days ago disappears"). Means are sum/cnt per metric, so a sensor
that was missing for part of an hour never drags the mean towards zero. `last` is the only addition to the SPEC2 layout:
export() runs without sensors, so the latest reading (for "current" and the stale flag) and the previous /proc/stat
counters (so cpu_pct is the true average since the last sample, not a 0.2 s snapshot) must live in the file.
A full ring is ~185 KB (SPEC2 guessed < 60 KB; the per-metric keys of sum/cnt/max/min for 15 metrics x 168 hours are
what takes the space), rewritten once a minute: about 3 KB/s of writes, nothing to worry about.

Writers hold an flock on STATE_DIR/metrics.lock around read-modify-write and replace the file atomically; readers
(export) take no lock because the file is only ever replaced whole. A corrupt file is kept as metrics-ring.json.bad and
replaced by a fresh ring; a file that merely cannot be read right now (EIO, EACCES, ...) is NOT treated as corrupt:
that sample fails (exit 1) and the ring is left alone. The sampler never raises and never blocks past its time budget:
a sensor that is missing, slow or insane becomes None for that metric only, and the two things that can wedge (the
exporter socket, nvidia-smi) run under hard deadlines that do not depend on the peer or the child behaving.

Sources, in order (all read-only):
  temps       sysfs hwmon: coretemp "Package id 0", spd5118 DIMMs (mean + max), nvme "Composite" (hottest)
              -> sensor-exporter http://127.0.0.1:9110/ as the fallback for cpu_temp / nvme_temp
  fans        sensor-exporter (it owns the CPU/case header grouping and the GPU fan RPM, which needs nvidia-settings)
              -> hwmon nct6798 tachs with the same grouping as the fallback
  GPU         one `nvidia-smi --query-gpu` call (temp, util, fan %, power, memory) -> exporter gpu_temp / gpu_fan
  load        /proc/stat deltas (cpu_pct), /proc/meminfo (ram_pct), /proc/loadavg (load1)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import core

STATE_DIR = core.STATE_DIR          # read at call time, so tests can monkeypatch it
HWMON = Path("/sys/class/hwmon")
PROC = Path("/proc")
EXPORTER = ("127.0.0.1", 9110)
EXPORTER_MAX = 65536                # the exporter answers ~600 bytes; a body or Content-Length above this is not it
EXPORTER_HEAD_MAX = 16384           # response headers: same idea
NVIDIA_SMI = "nvidia-smi"

SLOTS = 168                         # 7 days of hours
HOUR = 3600
VERSION = 1
RING_FILE, LOCK_FILE = "metrics-ring.json", "metrics.lock"
STALE_S = 300                       # newest sample older than this => "sampler stopped"
BUDGET_S = 2.0                      # sensor reads; with the lock wait below a sample stays inside 3 s even when stuck
QUICK_CPU_S = 0.2                   # in-run cpu delta, only when there is no previous reading to diff against
LOCK_WAIT_S = 0.9                   # how long the sampler waits for metrics.lock before giving the sample up
REWIND_AFTER = 10                   # consecutive "clock is behind" samples before the future hours are dropped
MAX_FILE = 4_000_000                # a full ring is ~185 KB; anything this large is garbage, not data

METRICS = ("cpu_temp", "gpu_temp", "ram_temp", "ram_temp_max", "nvme_temp", "cpu_fan_rpm", "case_fan_rpm",
           "gpu_fan_pct", "gpu_fan_rpm", "cpu_pct", "gpu_pct", "ram_pct", "gpu_mem_pct", "gpu_power_w", "load1")

# A reading outside these bounds is a sensor glitch (0xFFFF tach, -128 C, 65535 W), not data: it becomes None.
_RANGE: dict[str, tuple[float, float]] = {m: (-40.0, 150.0) for m in
                                          ("cpu_temp", "gpu_temp", "ram_temp", "ram_temp_max", "nvme_temp")}
_RANGE.update({m: (0.0, 30000.0) for m in ("cpu_fan_rpm", "case_fan_rpm", "gpu_fan_rpm")})
_RANGE.update({m: (0.0, 100.0) for m in ("gpu_fan_pct", "cpu_pct", "gpu_pct", "ram_pct", "gpu_mem_pct")})
_RANGE.update(gpu_power_w=(0.0, 2000.0), load1=(0.0, 100000.0))

# nct6798 header groups, copied from ~/sensor-exporter/sensor_exporter.py (CPU_PWM; CASE_PWM + the fan3 follower).
# Only used when the exporter is down.
CPU_FANS, CASE_FANS = (1, 4), (2, 3, 5)


# --------------------------------------------------------------------------- values
def _num(v) -> float | None:
    """Finite float or None (bool and strings are not numbers here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:                       # an absurdly large int from a damaged file or a confused exporter
        return None
    return f if math.isfinite(f) else None


def _clean(m: dict) -> dict:
    """Every metric present, rounded to 3 decimals, None when missing, non-numeric or out of its sane range."""
    out: dict = {}
    for k in METRICS:
        v = _num(m.get(k))
        lo, hi = _RANGE[k]
        out[k] = round(v, 3) if v is not None and lo <= v <= hi else None
    return out


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def _guard(fn, default, *args):
    """Run one sensor read; any failure is that sensor's problem only."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - the sampler must never raise
        return default


# --------------------------------------------------------------------------- ring storage
def _empty_slot(h: int = 0) -> dict:
    return {"h": h, "n": 0, "sum": {}, "cnt": {}, "max": {}, "min": {}}


def _fresh() -> dict:
    return {"v": VERSION, "slots": SLOTS, "hours": [_empty_slot() for _ in range(SLOTS)], "last": {}}


def _int(v) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def _valid_slot(s) -> dict | None:
    """A well-formed slot, or None if the slot itself is junk. A metric survives only if all four aggregates for it
    are present and finite with cnt >= 1, so a half-damaged entry can never turn into a bogus mean."""
    if not isinstance(s, dict):
        return None
    h, n = _int(s.get("h")), _int(s.get("n"))
    if h is None or n is None or n < 0:
        return None
    agg: dict = {}
    for key in ("sum", "cnt", "max", "min"):
        d = s.get(key)
        if not isinstance(d, dict):
            return None
        agg[key] = {m: v for m, v in d.items() if m in _RANGE and _num(v) is not None and (key != "cnt" or v >= 1)}
    keep = set(agg["sum"]) & set(agg["cnt"]) & set(agg["max"]) & set(agg["min"])
    return {"h": h, "n": n, **{key: {m: (int(v) if key == "cnt" else float(v)) for m, v in d.items() if m in keep}
                              for key, d in agg.items()}}


def _valid_last(last) -> dict:
    if not isinstance(last, dict):
        return {}
    out: dict = {}
    t = _num(last.get("t"))
    if t is not None:
        out["t"] = t
    if isinstance(last.get("cur"), dict):
        out["cur"] = _clean(last["cur"])
    cpu = last.get("cpu")
    if isinstance(cpu, list) and len(cpu) == 2 and all(_int(x) is not None and x >= 0 for x in cpu):
        out["cpu"] = [int(cpu[0]), int(cpu[1])]
    out["rej"] = max(_int(last.get("rej")) or 0, 0)
    return out


def _from_raw(obj) -> dict | None:
    """Validated ring, or None when the file as a whole is not a ring (wrong shape/version/slot count)."""
    if not isinstance(obj, dict) or obj.get("v") != VERSION or obj.get("slots") != SLOTS:
        return None
    hours = obj.get("hours")
    if not isinstance(hours, list) or len(hours) != SLOTS:
        return None
    slots = [_valid_slot(s) or _empty_slot() for s in hours]
    for i, s in enumerate(slots):
        if s["n"] and s["h"] % SLOTS != i:       # an hour stored under the wrong index would shadow a real one
            slots[i] = _empty_slot()
    return {"v": VERSION, "slots": SLOTS, "hours": slots, "last": _valid_last(obj.get("last"))}


def _load(path: Path, repair: bool) -> dict:
    """Read the ring. Missing => fresh ring. Corrupt => fresh ring, and with repair=True the file is kept as `.bad`.

    A file that cannot be READ is neither missing nor corrupt: an OSError other than FileNotFoundError (EIO, EMFILE,
    EACCES during a hiccup) says nothing about the content, and answering "start over" would let record() replace
    7 days of history with one hour. So the writer (repair=True) lets it propagate: the sample fails (exit 1), the
    file is untouched and the next minute tries again. Readers (export, the sampler's pre-read of `last`) fall back to
    an empty ring, which costs them one empty answer and nothing else.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _fresh()
    except OSError:
        if repair:
            raise
        return _fresh()
    ring = None
    if len(raw) <= MAX_FILE:
        try:
            ring = _from_raw(json.loads(raw))
        except (ValueError, RecursionError):
            ring = None
    if ring is None:
        if repair:
            try:
                os.replace(path, path.with_name(path.name + ".bad"))
            except OSError:
                pass
        return _fresh()
    return ring


def _save(path: Path, ring: dict) -> None:
    """Atomic replace (tmp + os.replace), 0644 whatever the umask, compact JSON."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(ring, separators=(",", ":"), allow_nan=False)
    try:
        with open(tmp, "w") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _locked(lock: Path, timeout: float):
    """flock(LOCK_EX) with a deadline: a stuck writer must not hang the sampler (systemd would kill it anyway)."""
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    f = open(lock, "a")
    try:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError(f"{lock.name} busy") from None
                time.sleep(0.01)
        yield
    finally:
        f.close()                                  # closing the descriptor releases the lock


def _fold(slot: dict, m: dict) -> None:
    slot["n"] += 1
    for k, v in m.items():
        if v is None:
            continue                                # a missing reading adds nothing, not a zero
        # Rounded on store to keep the file small (the per-metric keys already make a full ring ~185 KB); the error
        # this adds to an hourly mean is at most 0.005 (60 samples), and max/min are only ever shown with one decimal.
        slot["sum"][k] = round(slot["sum"].get(k, 0.0) + v, 2)
        slot["cnt"][k] = slot["cnt"].get(k, 0) + 1
        r = round(v, 1)
        slot["max"][k] = max(slot["max"].get(k, r), r)
        slot["min"][k] = min(slot["min"].get(k, r), r)


def record(t: float, metrics: dict, cpu: list[int] | None = None, timeout: float = 5.0) -> bool:
    """Fold one sample taken at epoch `t` into the ring. False if it was ignored because the clock is behind.

    Raises OSError/TimeoutError when the ring cannot be written (sample_once catches those).
    """
    m = _clean(metrics)
    h = int(t // HOUR)
    path = STATE_DIR / RING_FILE
    with _locked(STATE_DIR / LOCK_FILE, timeout):
        ring = _load(path, repair=True)
        slots, last = ring["hours"], ring["last"]
        newest = max((s["h"] for s in slots if s["n"]), default=None)
        if newest is not None and h < newest - 1:
            # The clock went backwards (NTP step, bad RTC at boot). Folding this in would write into the past and
            # a later correction could not be told from garbage, so ignore it. If it stays behind for REWIND_AFTER
            # samples the old "future" hours were the bogus ones: drop those and carry on from the real time.
            last["rej"] = last.get("rej", 0) + 1
            if last["rej"] < REWIND_AFTER:
                _save(path, ring)
                return False
            for i, s in enumerate(slots):
                if s["h"] > h:
                    slots[i] = _empty_slot()
        slot = slots[h % SLOTS]
        if slot["h"] != h or not slot["n"]:
            slot = slots[h % SLOTS] = _empty_slot(h)    # the overwrite: the hour 168 h ago is gone
        _fold(slot, m)
        ring["last"] = {"t": t, "cur": m, "rej": 0, **({"cpu": cpu} if cpu else {})}
        _save(path, ring)
        return True


# --------------------------------------------------------------------------- sensors
def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except (OSError, ValueError):
        return None


def _milli(p: Path) -> float | None:
    s = _read(p)
    try:
        return int(s) / 1000 if s is not None else None
    except ValueError:
        return None


def _chips() -> list[tuple[str, Path]]:
    out = []
    for d in sorted(HWMON.glob("hwmon*")):
        name = _read(d / "name")
        if name:
            out.append((name, d))
    return out


def _labelled(d: Path, label: str) -> float | None:
    for lp in sorted(d.glob("temp*_label")):
        if _read(lp) == label:
            return _milli(lp.with_name(lp.name.replace("_label", "_input")))
    return None


def _hw_temps(chips: list[tuple[str, Path]]) -> dict:
    cpu, dimm, nvme = None, [], []
    for name, d in chips:
        if name == "coretemp" and cpu is None:
            cpu = _labelled(d, "Package id 0")
        elif name == "spd5118":
            v = _milli(d / "temp1_input")
            if v is not None:
                dimm.append(v)
        elif name.startswith("nvme"):
            v = _first(_labelled(d, "Composite"), _milli(d / "temp1_input"))
            if v is not None:
                nvme.append(v)
    return {"cpu_temp": cpu, "ram_temp": sum(dimm) / len(dimm) if dimm else None,
            "ram_temp_max": max(dimm) if dimm else None, "nvme_temp": max(nvme) if nvme else None}


def _hw_fans(chips: list[tuple[str, Path]]) -> tuple[float | None, float | None]:
    """(cpu_rpm, case_rpm): mean of the fans that turn; 0 when the chip is there but nothing turns."""
    for name, d in chips:
        if not name.startswith("nct67"):
            continue
        rpm = {}
        for i in range(1, 8):
            s = _read(d / f"fan{i}_input")
            if s is not None and s.isdigit():
                rpm[i] = int(s)
        if not rpm:
            continue

        def mean(idx):
            v = [rpm[i] for i in idx if rpm.get(i)]
            return sum(v) / len(v) if v else 0.0
        return mean(CPU_FANS), mean(CASE_FANS)
    return None, None


def _http_head(head: bytes) -> tuple[int | None, int | None]:
    """(status, content_length) from a raw HTTP/1.x response head; (None, None) for anything we will not trust."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not (parts[1].isascii() and parts[1].isdigit()):
        return None, None
    clen = None
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "transfer-encoding":                 # we ask for HTTP/1.0, so a chunked answer is not our exporter
            return None, None
        if k == "content-length":
            if not (v.isascii() and v.isdigit()) or int(v) > EXPORTER_MAX:
                return None, None
            clen = int(v)
    return int(parts[1]), clen


def _exporter(timeout: float) -> dict:
    """GET http://127.0.0.1:9110/ (sensor-exporter) inside ONE overall deadline of `timeout` seconds.

    A raw HTTP/1.0 exchange rather than http.client, on purpose: a socket timeout only bounds each recv(), so a peer
    that trickles a byte every few hundred ms (in the headers or the body) never trips it and the sample would hang
    until systemd killed the unit. Here every recv() is capped at what is left of the deadline. No proxies, no
    redirects, bounded size; down, slow or garbage all become {} and the sysfs fallbacks cover it.
    """
    end = time.monotonic() + timeout
    try:
        with socket.create_connection(EXPORTER, timeout=timeout) as s:
            s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nAccept: application/json\r\nConnection: close\r\n\r\n")
            buf, head, clen = b"", None, None
            while True:
                if head is None and (i := buf.find(b"\r\n\r\n")) >= 0:
                    head, buf = buf[:i], buf[i + 4:]
                    status, clen = _http_head(head)
                    if status != 200:
                        return {}
                if head is not None and clen is not None and len(buf) >= clen:
                    break                                          # complete, even if the peer keeps the socket open
                if len(buf) > (EXPORTER_MAX if head is not None else EXPORTER_HEAD_MAX):
                    return {}
                left = end - time.monotonic()
                if left <= 0:
                    return {}                                      # the whole exchange is out of time
                s.settimeout(left)
                chunk = s.recv(4096)
                if not chunk:
                    break                                          # EOF ends a body that has no Content-Length
                buf += chunk
        if head is None or (clen is not None and len(buf) < clen):
            return {}                                              # closed before the headers ended / body cut short
        d = json.loads(buf if clen is None else buf[:clen])
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 - down, slow or garbage: the sysfs fallbacks cover it
        return {}


def _abandon(p: subprocess.Popen) -> None:
    """Kill the child's whole process group and walk away. Deliberately no wait(): subprocess.run() would block on
    one after the kill, and a child stuck in uninterruptible sleep (a GPU that fell off the bus) never dies."""
    try:
        os.killpg(p.pid, signal.SIGKILL)         # start_new_session=True made the child its own group leader
    except OSError:
        pass
    try:
        p.stdout.close()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        p.poll()                                 # reaps it if it is already dead; never blocks
    except OSError:
        pass


def _nvidia(timeout: float) -> dict:
    """One nvidia-smi query that cannot hold the sampler past `timeout` even if the child is unkillable.

    When the GPU misbehaves (Xid, off the bus) is exactly when its temperature matters, and nvidia-smi can then sit in
    D state. subprocess.run() would wait on it forever after kill() (and Popen.__exit__ waits again), losing the whole
    sample: CPU, fans and RAM included. So: own session + communicate(timeout) + killpg + no wait.
    """
    cmd = [NVIDIA_SMI, "--query-gpu=temperature.gpu,utilization.gpu,fan.speed,power.draw,memory.used,memory.total",
           "--format=csv,noheader,nounits"]
    env = {**os.environ, "LC_ALL": "C", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, errors="replace", env=env, start_new_session=True)
    except OSError:                                # not installed / not executable: no GPU readings
        return {}
    try:
        out, _ = p.communicate(timeout=timeout)
    except Exception:  # noqa: BLE001 - TimeoutExpired or anything else: give up on this reading
        _abandon(p)
        return {}
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if p.returncode != 0 or not lines:
        return {}
    f = []
    for p in (lines[0].split(",") + [""] * 6)[:6]:     # first GPU; "[N/A]" and "" become None
        try:
            f.append(_num(float(p.strip())))
        except ValueError:
            f.append(None)
    temp, util, fan, power, used, total = f
    return {"gpu_temp": temp, "gpu_pct": util, "gpu_fan_pct": fan, "gpu_power_w": power,
            "gpu_mem_pct": 100.0 * used / total if used is not None and total else None}


def _jiffies() -> list[int] | None:
    """[total, idle] aggregate CPU jiffies. idle counts iowait (a CPU waiting for disk is not busy), like top."""
    try:
        f = (_read(PROC / "stat") or "").splitlines()[0].split()
        v = [int(x) for x in f[1:]]
        return [sum(v[:8]), v[3] + v[4]] if f[0] == "cpu" else None   # guest columns are already inside user/nice
    except (IndexError, ValueError):
        return None


def _busy_pct(a: list[int], b: list[int]) -> float | None:
    dt, di = b[0] - a[0], b[1] - a[1]
    if dt <= 0 or di < 0 or di > dt:                           # counters reset (reboot) or no time passed
        return None
    return 100.0 * (dt - di) / dt


def _cpu(prev: list[int] | None, prev_t: float | None, t: float) -> tuple[float | None, list[int] | None]:
    cur = _jiffies()
    if cur is None:
        return None, None
    if prev and prev_t is not None and 0 < t - prev_t <= 600:
        pct = _busy_pct(prev, cur)
        if pct is not None:
            return pct, cur
    time.sleep(QUICK_CPU_S)               # first run, reboot or long gap: a short in-run delta beats a hole
    nxt = _jiffies() or cur
    return _busy_pct(cur, nxt), nxt


def _ram_pct() -> float | None:
    kv = {}
    for ln in (_read(PROC / "meminfo") or "").splitlines():
        k, _, rest = ln.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            kv[k] = int(rest.split()[0])
    return 100.0 * (1 - kv["MemAvailable"] / kv["MemTotal"]) if kv.get("MemTotal") and "MemAvailable" in kv else None


def _load1() -> float | None:
    return float((_read(PROC / "loadavg") or "").split()[0])


def _collect(prev: list[int] | None, prev_t: float | None, t: float) -> tuple[dict, list[int] | None]:
    deadline = time.monotonic() + BUDGET_S

    def left(cap: float) -> float:
        return max(0.1, min(cap, deadline - time.monotonic()))

    chips = _guard(_chips, [])
    hw = _guard(_hw_temps, {}, chips)
    ex = _guard(_exporter, {}, left(0.8))
    gpu = _guard(_nvidia, {}, left(1.0))
    hw_cpu_fan, hw_case_fan = _guard(_hw_fans, (None, None), chips)
    pct, cpu_now = _guard(_cpu, (None, None), prev, prev_t, t)
    m = {
        "cpu_temp": _first(hw.get("cpu_temp"), _num(ex.get("cpu_temp"))),
        "ram_temp": hw.get("ram_temp"), "ram_temp_max": hw.get("ram_temp_max"),
        "nvme_temp": _first(hw.get("nvme_temp"), _num(ex.get("nvme_temp"))),
        "gpu_temp": _first(gpu.get("gpu_temp"), _num(ex.get("gpu_temp"))),
        "gpu_fan_pct": _first(gpu.get("gpu_fan_pct"), _num(ex.get("gpu_fan"))),
        "gpu_pct": gpu.get("gpu_pct"), "gpu_power_w": gpu.get("gpu_power_w"), "gpu_mem_pct": gpu.get("gpu_mem_pct"),
        "gpu_fan_rpm": _num(ex.get("gpu_fan_rpm")),
        "cpu_fan_rpm": _first(_num(ex.get("cpu_fan")), hw_cpu_fan),
        "case_fan_rpm": _first(_num(ex.get("case_fan")), hw_case_fan),
        "cpu_pct": pct, "ram_pct": _guard(_ram_pct, None), "load1": _guard(_load1, None),
    }
    return m, cpu_now


# --------------------------------------------------------------------------- sampler
def _sample(now: float | None = None) -> tuple[dict, str | None]:
    """(metrics, problem). `problem` is None when the sample reached the ring, else a short reason. Never raises."""
    try:
        t = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        t = time.time()
    metrics: dict = {k: None for k in METRICS}
    problem = None
    try:
        last = _guard(lambda: _load(STATE_DIR / RING_FILE, repair=False)["last"], {})
        got, cpu_now = _collect(last.get("cpu"), last.get("t"), t)
        metrics = _clean(got)
        if not record(t, metrics, cpu=cpu_now, timeout=LOCK_WAIT_S):
            problem = "clock is behind the ring; sample ignored"
    except Exception as exc:  # noqa: BLE001 - OSError, lock timeout, anything: report it, never raise
        problem = f"{type(exc).__name__}: {exc}"[:120]
    metrics["sampled_at"] = t
    return metrics, problem


def sample_once(now: float | None = None) -> dict:
    """Read every sensor once, fold the reading into the ring, return it (+ `sampled_at`). Never raises."""
    return _sample(now)[0]


# --------------------------------------------------------------------------- export
def _mean(slots: list, m: str) -> float | None:
    """Sample-weighted mean of metric `m` over the non-empty slots (None when no sample had it)."""
    s = k = 0.0
    for sl in slots:
        if sl and sl["cnt"].get(m):
            s += sl["sum"].get(m, 0.0)
            k += sl["cnt"][m]
    return round(s / k, 1) if k else None


def _max(slots: list, m: str) -> float | None:
    v = [sl["max"][m] for sl in slots if sl and sl["cnt"].get(m) and m in sl["max"]]
    return round(max(v), 1) if v else None


def export(now: float | None = None) -> dict:
    """Everything the widgets and the web UI show, from the ring file only (no sensors, no lock, no writes)."""
    t = time.time() if now is None else float(now)
    ring = _load(STATE_DIR / RING_FILE, repair=False)
    cur_h = int(t // HOUR)
    hours = [cur_h - (SLOTS - 1) + i for i in range(SLOTS)]          # oldest first, ending at the hour in progress
    win = []
    for h in hours:
        s = ring["hours"][h % SLOTS]
        win.append(s if s["n"] and s["h"] == h else None)

    last = ring["last"]
    sampled_at = last.get("t")
    age = None if sampled_at is None else t - sampled_at
    cur = last.get("cur") or {}
    # Newest sample older than 5 min, or "in the future" by more than that (clock stepped back): not live data.
    stale = age is None or not (-STALE_S <= age <= STALE_S)

    # The hour whose slot the next sample will overwrite is still in the file at the moment of a rollover, so
    # "complete" counts the ring's slots rather than the 168-hour window (it must not flap at every :00).
    filled = sum(1 for s in ring["hours"] if s["n"] and cur_h - SLOTS <= s["h"] <= cur_h)
    first = next((h for h, s in zip(hours, win) if s), None)

    def per(slots):
        return {m: _mean(slots, m) for m in METRICS}

    series: dict = {"t": [h * HOUR for h in hours]}
    for m in METRICS:
        series[m] = [_mean([s], m) for s in win]
    return {
        "generated_at": t, "interval_s": HOUR, "slots": SLOTS, "stale": stale,
        "current": {**{m: (round(cur[m], 1) if cur.get(m) is not None else None) for m in METRICS},
                    "sampled_at": sampled_at},
        "hour_avg": per(win[-1:]), "prev_hour_avg": per(win[-2:-1]), "avg_24h": per(win[-24:]), "avg_7d": per(win),
        "max_7d": {m: _max(win, m) for m in METRICS},
        # pos is where the hour in progress sits in `series` (always the right edge); `slot` is its physical ring
        # index. The next sample after :00 overwrites series[0] (the oldest hour) at `overwrites_next_at`.
        "loop": {"pos": SLOTS - 1, "slot": cur_h % SLOTS, "overwrites_next_at": (cur_h + 1) * HOUR,
                 "oldest_hour": first * HOUR if first is not None else None,
                 "complete": filled == SLOTS, "filled": filled},
        "series": series,
    }


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m homelab_maint.metrics_ring", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="take one sample (exit 1 if it could not be recorded)")
    s.add_argument("-v", "--verbose", action="store_true", help="print the sample")
    e = sub.add_parser("export", help="print the export JSON")
    e.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        metrics, problem = _sample()
        if a.verbose:
            print(json.dumps(metrics, sort_keys=True))
        if problem:
            print(f"metrics-ring: {problem}", file=sys.stderr)
            return 1                    # the unit shows as failed, which failed_units reports after 30 min
        return 0
    print(json.dumps(export(), indent=1 if a.pretty else None, separators=None if a.pretty else (",", ":"),
                     allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
