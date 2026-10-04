#!/usr/bin/env python3
"""Generate the metrics-ring EXPORT fixtures (SPEC2 section 3) that the thermal/load payloads and widgets are tested with.

  python3 widgets/fixtures-metrics/make_fixtures.py        rewrites widgets/fixtures-metrics/*.json

Deterministic (seeded RNG, fixed NOW) so the committed files only change when this script does. The shapes are exactly what
`homelab_maint.metrics_ring.export()` returns; epochs are absolute, so the x labels the payloads compute depend on the TZ the
tests set (America/Toronto), not on the machine that generated the files.

  full_ring     168 hourly points, every metric, sampler fresh (normal week)
  partial_ring  sampler started 20 h ago: 20 hourly points, 148 empty slots, ring not complete
  stale         full ring but the newest sample is 47 min old and export() says stale
  all_none      sampler alive but every sensor read None (no data at all)
  no_gpu        full ring where every GPU metric is None (box without the card / driver down)
  hot           full ring whose last hours are hot: CPU 94 C, GPU 80 C, RAM 58 C and a stopped CPU fan (status colours + badges)
"""
from __future__ import annotations

import calendar
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLOTS, HOUR = 168, 3600
# Thu 2026-10-01 21:55:00 EDT (= 01:55 UTC Fri). Chosen to sit inside an hour so the in-progress hour is partial.
NOW = float(calendar.timegm((2026, 10, 2, 1, 55, 0)))

METRICS = ["cpu_temp", "gpu_temp", "ram_temp", "ram_temp_max", "nvme_temp", "cpu_fan_rpm", "case_fan_rpm", "gpu_fan_pct",
           "gpu_fan_rpm", "cpu_pct", "gpu_pct", "ram_pct", "gpu_mem_pct", "gpu_power_w", "load1"]
GPU = ["gpu_temp", "gpu_fan_pct", "gpu_fan_rpm", "gpu_pct", "gpu_mem_pct", "gpu_power_w"]


def _hour_values(h: int, rng: random.Random, busy: float) -> dict[str, float]:
    """One plausible hour for this host. `busy` 0..1 is a shared latent load so temps, fans and utilisation correlate."""
    tod = (h // HOUR) % 24
    day = 0.5 + 0.5 * math.sin((tod - 9) / 24 * 2 * math.pi)            # quieter at night
    cpu = min(97.0, 4 + 55 * busy * (0.5 + day) + rng.uniform(0, 6))
    gpu = min(99.0, 82 * busy * rng.uniform(0.5, 1.2)) if busy > 0.35 else rng.uniform(0, 4)
    cpu_t = 34 + 0.38 * cpu + rng.uniform(-1.5, 1.5)
    gpu_t = 31 + 0.55 * gpu + rng.uniform(-1, 1)
    gpu_fan = 0.0 if gpu_t < 50 else min(100.0, (gpu_t - 45) * 3.2 + rng.uniform(-2, 2))
    return {
        "cpu_temp": cpu_t, "gpu_temp": gpu_t, "ram_temp": 33 + 0.12 * cpu + 0.05 * gpu + rng.uniform(-0.6, 0.6),
        "ram_temp_max": 35 + 0.14 * cpu + 0.06 * gpu + rng.uniform(-0.6, 0.6), "nvme_temp": 45 + 0.07 * cpu + rng.uniform(-1, 1),
        "cpu_fan_rpm": 780 + 14 * (cpu_t - 34) + rng.uniform(-25, 25), "case_fan_rpm": 540 + 9 * (cpu_t - 34) + rng.uniform(-20, 20),
        "gpu_fan_pct": gpu_fan, "gpu_fan_rpm": 0.0 if gpu_fan == 0 else 620 + gpu_fan * 17 + rng.uniform(-20, 20),
        "cpu_pct": cpu, "gpu_pct": gpu, "ram_pct": 52 + 22 * day + rng.uniform(-3, 3) + 8 * busy,
        "gpu_mem_pct": 6 + 70 * (gpu / 100) + rng.uniform(0, 3), "gpu_power_w": 22 + 3.2 * gpu + rng.uniform(-2, 2),
        "load1": 1.5 + 30 * busy + rng.uniform(0, 3),
    }


def _busy_curve(n: int, rng: random.Random) -> list[float]:
    """Mostly idle with multi-hour bursts (ComfyUI / Immich / transcodes)."""
    out, left, level = [], 0, 0.15
    for _ in range(n):
        if left <= 0:
            left = rng.randint(2, 9)
            level = rng.choice([0.1, 0.12, 0.2, 0.3, 0.55, 0.75, 0.9])
        left -= 1
        out.append(max(0.02, min(1.0, level + rng.uniform(-0.05, 0.05))))
    return out


def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def build(kind: str) -> dict:
    rng = random.Random({"full_ring": 1, "partial_ring": 2, "stale": 3, "all_none": 4, "no_gpu": 5, "hot": 1}[kind])
    h_now = int(NOW // HOUR) * HOUR
    t = [h_now - (SLOTS - 1 - i) * HOUR for i in range(SLOTS)]
    busy = _busy_curve(SLOTS, rng)
    hours = [_hour_values(t[i], rng, busy[i]) for i in range(SLOTS)]
    cur_src = _hour_values(h_now, rng, busy[-1])

    first = 0
    sampled_at = NOW - 20
    stale = False
    if kind == "partial_ring":
        first = SLOTS - 20                      # sampler started 20 h ago
    if kind == "stale":
        sampled_at, stale = NOW - 47 * 60, True
    series: dict[str, list] = {"t": t}
    for m in METRICS:
        col: list[float | None] = []
        for i in range(SLOTS):
            v: float | None = hours[i][m] if i >= first else None
            if kind == "all_none" or (kind == "no_gpu" and m in GPU):
                v = None
            col.append(None if v is None else round(v, 1))
        series[m] = col

    hot = {"cpu_temp": 94.0, "gpu_temp": 80.0, "ram_temp": 58.3, "cpu_fan_rpm": 0.0}
    if kind == "hot":
        for m, v in hot.items():
            series[m][-1] = v

    def avg(last_n: int) -> dict:
        return {m: (None if _mean(series[m][-last_n:]) is None else round(_mean(series[m][-last_n:]), 1)) for m in METRICS}

    cur: dict = {m: (None if kind == "all_none" or (kind == "no_gpu" and m in GPU) else round(cur_src[m], 1)) for m in METRICS}
    if kind == "hot":
        cur.update(hot)
    cur["sampled_at"] = sampled_at
    mx = {m: (None if not [x for x in series[m] if x is not None] else max(x for x in series[m] if x is not None)) for m in METRICS}
    n_data = sum(1 for i in range(SLOTS) if any(series[m][i] is not None for m in METRICS))
    oldest = t[first] if kind != "all_none" else None
    return {
        "generated_at": NOW, "interval_s": HOUR, "slots": SLOTS, "current": cur,
        "hour_avg": {m: cur[m] for m in METRICS}, "prev_hour_avg": {m: series[m][-2] for m in METRICS},
        "avg_24h": avg(24), "avg_7d": avg(SLOTS), "max_7d": mx,
        "loop": {"pos": h_now // HOUR % SLOTS, "overwrites_next_at": float(h_now + HOUR), "oldest_hour": oldest,
                 "complete": n_data >= SLOTS},
        "series": series, "stale": stale,
    }


def main() -> None:
    for kind in ("full_ring", "partial_ring", "stale", "all_none", "no_gpu", "hot"):
        p = HERE / f"{kind}.json"
        p.write_text(json.dumps(build(kind), separators=(",", ":")) + "\n")
        print(f"wrote {p.name}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
