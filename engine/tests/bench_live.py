#!/usr/bin/env python3
"""Overhead benchmark for the live monitor: run the real daemon read-only against THIS host and report what it costs.

    cd /home/ohmz/homelab-maint && python3 tests/bench_live.py [--seconds 60] [--warmup 6]

What it does: starts `python3 -m homelab_maint.live` as a child with a throw-away STATE dir (the only place the daemon
writes), the repo's etc/ as config and the host's real /proc, cgroups, docker socket, nvidia-smi and service ports (all of
which it only reads). The state dir is pre-seeded with a FULL 720-point history (as after an hour of uptime) so the numbers
are steady-state: the biggest live.json and the biggest per-tick encode, not the cheap first minute (`--no-seed` to skip).
After a warm-up it samples /proc/<pid>/stat for utime+stime+cutime+cstime (the daemon, its threads AND the children it
forked, e.g. nvidia-smi) over the measured window and reads VmRSS/VmHWM, then stops it with SIGTERM (which also checks the
graceful exit). The systemd unit caps the whole cgroup (MemoryMax=64M), and nvidia-smi is a child in that cgroup, so one
nvidia-smi's peak RSS is measured too and added to the daemon's peak. Output is one report plus PASS/FAIL against the
SPEC3 S4 budget (CPU < 1.5 % of one core, daemon RSS < 40 MiB) and the unit's 64 MiB cap.

`--breakdown` additionally runs the daemon once per probe with ONLY that probe enabled (plus once with none, which is the
bare 5-second tick) and prints each one's CPU share, so you can see what a knob like `sensors_every_s` is worth.

Not a pytest module (the name does not start with test_). Exit code 0 = within budget, 1 = over budget, 2 = harness error.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CPU_BUDGET_PCT = 1.5
RSS_BUDGET_MB = 40.0
UNIT_CAP_MB = 64.0                  # MemoryMax in systemd/homelab-maint-live.service
POINTS, STEP = 720, 5.0
CLK = os.sysconf("SC_CLK_TCK")


def cpu_ticks(pid: int) -> tuple[int, int]:
    """(own ticks, children ticks) from /proc/<pid>/stat: fields utime stime | cutime cstime (after the ')' of comm)."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    f = raw[raw.rfind(")") + 2:].split()
    return int(f[11]) + int(f[12]), int(f[13]) + int(f[14])


def rss_mb(pid: int) -> tuple[float, float]:
    """(VmRSS, VmHWM) in MiB."""
    vals = {}
    for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
        k, _, v = ln.partition(":")
        if k in ("VmRSS", "VmHWM"):
            vals[k] = int(v.split()[0]) / 1024
    return vals.get("VmRSS", 0.0), vals.get("VmHWM", 0.0)


def seed_history(state: Path) -> None:
    """A realistic full history ending 10 s ago: busy-looking values so the file is as big as it ever gets."""
    rnd = random.Random(7)
    now = time.time()
    gen = {"cpu": lambda: round(rnd.uniform(3, 95), 1), "mem_pct": lambda: rnd.randint(40, 90), "psi_mem": lambda: round(rnd.uniform(0, 12), 1),
           "psi_io": lambda: round(rnd.uniform(0, 60), 1), "gpu": lambda: rnd.randint(0, 100), "net_rx": lambda: round(rnd.uniform(0, 120), 1),
           "net_tx": lambda: round(rnd.uniform(0, 40), 1), "disk_r": lambda: round(rnd.uniform(0, 900), 1),
           "disk_w": lambda: round(rnd.uniform(0, 300), 1)}
    series = {k: [g() for _ in range(POINTS)] for k, g in gen.items()}
    (state / "live-history.json").write_text(json.dumps({"v": 1, "step_s": STEP, "t_last": now - 10, "series": series}))


def child_peak_mb(argv: list[str]) -> float | None:
    """Peak RSS (MiB) of one run of `argv`, measured by a throw-away helper so RUSAGE_CHILDREN sees only that command."""
    if shutil.which(argv[0]) is None:
        return None
    code = "import resource,subprocess,sys; subprocess.run(sys.argv[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15); " \
           "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)"
    try:
        r = subprocess.run([sys.executable, "-c", code, *argv], capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip()) / 1024
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def breakdown(seconds: float, warmup: float) -> None:
    """CPU share of the bare tick and of each probe alone (same seeded state, own throw-away config dir per run)."""
    off = {"gpu": "false", "sensors": "false", "docker": "false", "disk": "false"}
    runs = [("tick only (all probes off)", {}), ("+ sensors (hwmon)", {"sensors": "true"}), ("+ docker (socket + cgroups)", {"docker": "true"}),
            ("+ gpu (nvidia-smi)", {"gpu": "true"}), ("+ disk (statvfs)", {"disk": "true"})]
    print(f"per-probe cost, {seconds:.0f} s each (the 'docker' line includes the per-container cgroup pass in the tick)")
    for label, on in runs:
        with tempfile.TemporaryDirectory(prefix="hm-live-bench-") as tmp:
            state, conf = Path(tmp) / "state", Path(tmp) / "conf"
            state.mkdir()
            conf.mkdir()
            seed_history(state)
            (conf / "maint.toml").write_text("[live]\nservices = []\n" + "".join(f"{k} = {on.get(k, v)}\n" for k, v in off.items()))
            env = dict(os.environ, HOMELAB_MAINT_STATE=str(state), HOMELAB_MAINT_CONF=str(conf), PYTHONPATH=str(ROOT),
                       PYTHONDONTWRITEBYTECODE="1")
            p = subprocess.Popen([sys.executable, "-B", "-m", "homelab_maint.live"], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(warmup)
                t0, c0 = time.monotonic(), cpu_ticks(p.pid)
                time.sleep(seconds)
                wall, c1 = time.monotonic() - t0, cpu_ticks(p.pid)
            finally:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        print(f"  {label:32s} daemon {(c1[0] - c0[0]) / CLK / wall * 100:5.2f} %   children {(c1[1] - c0[1]) / CLK / wall * 100:5.2f} %")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--breakdown", action="store_true", help="also measure each probe alone (adds about 5 x 36 s)")
    ap.add_argument("--breakdown-seconds", type=float, default=30.0)
    ap.add_argument("--no-seed", action="store_true", help="start from an empty history instead of a full 720-point one")
    ap.add_argument("--seconds", type=float, default=60.0, help="measured window (default 60)")
    ap.add_argument("--warmup", type=float, default=6.0, help="seconds to let imports and the first probe burst settle")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="hm-live-bench-") as tmp:
        state = Path(tmp) / "state"
        state.mkdir()
        if not a.no_seed:
            seed_history(state)
        env = dict(os.environ, HOMELAB_MAINT_STATE=str(state), HOMELAB_MAINT_CONF=str(ROOT / "etc"),
                   PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, "-B", "-m", "homelab_maint.live"], cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(a.warmup)
            if p.poll() is not None:
                print(f"daemon exited early rc={p.returncode}: {p.stderr.read()[-300:]}", file=sys.stderr)
                return 2
            w0, c0 = time.monotonic(), cpu_ticks(p.pid)
            rss_samples, tick_ms = [], []
            live = state / "public" / "live.json"
            while time.monotonic() - w0 < a.seconds:
                time.sleep(min(5.0, max(0.1, a.seconds - (time.monotonic() - w0))))
                rss_samples.append(rss_mb(p.pid)[0])
                try:
                    d = json.loads(live.read_text())
                    tick_ms.append(d["self"]["tick_ms"])
                except (OSError, ValueError, KeyError):
                    pass
            wall, c1 = time.monotonic() - w0, cpu_ticks(p.pid)
            rss, hwm = rss_mb(p.pid)
            size = live.stat().st_size if live.exists() else 0
            threads = len(list(Path(f"/proc/{p.pid}/task").iterdir()))
            t_stop = time.monotonic()
            p.send_signal(signal.SIGTERM)
            try:
                rc = p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = -9
            stop_s = time.monotonic() - t_stop
            hist = (state / "live-history.json").exists()
        finally:
            if p.poll() is None:
                p.kill()

    smi = child_peak_mb(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"])
    own = (c1[0] - c0[0]) / CLK / wall * 100
    kids = (c1[1] - c0[1]) / CLK / wall * 100
    cpu = own + kids
    peak = max(rss_samples + [rss, hwm]) if rss_samples else max(rss, hwm)
    print(f"live monitor, {wall:.0f} s window on this host (read-only, temp STATE dir)")
    print(f"  CPU   {cpu:.2f} % of one core  (daemon {own:.2f} % + forked children {kids:.2f} %)   budget < {CPU_BUDGET_PCT} %")
    print(f"  RSS   {rss:.1f} MiB now, {peak:.1f} MiB peak (VmHWM)                                budget < {RSS_BUDGET_MB:g} MiB")
    if tick_ms:
        print(f"  tick  {sum(tick_ms) / len(tick_ms):.1f} ms average, {max(tick_ms):.1f} ms max (the loop sleeps the other 5000)")
    print(f"  file  live.json {size / 1000:.1f} KB (cap 52 KB), {threads} threads, history {'full 720 points' if not a.no_seed else 'empty at start'}")
    unit_peak = peak + (smi or 0.0)
    smi_txt = f"+ nvidia-smi {smi:.1f}" if smi is not None else "(no nvidia-smi on this host)"
    print(f"  unit  {unit_peak:.1f} MiB worst case = daemon peak {peak:.1f} {smi_txt}                budget < {UNIT_CAP_MB:g} MiB (MemoryMax)")
    print(f"  stop  SIGTERM -> exit {rc} in {stop_s:.2f} s, history persisted: {hist}")
    ok = cpu < CPU_BUDGET_PCT and peak < RSS_BUDGET_MB and unit_peak < UNIT_CAP_MB and rc == 0 and hist and 0 < size < 52_000
    print("PASS" if ok else "FAIL")
    if a.breakdown:
        print()
        breakdown(a.breakdown_seconds, a.warmup)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
