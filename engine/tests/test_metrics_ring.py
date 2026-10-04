"""Tests for homelab_maint.metrics_ring (7-day hourly ring of temperatures, fans and load) and its systemd units.

Every test runs against a tmp state dir, a fake /sys/class/hwmon, a fake /proc, a fake nvidia-smi script and (where
needed) a fake sensor-exporter on an ephemeral loopback port, so nothing here reads this host's real sensors.
"""
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab_maint import metrics_ring as mr

ROOT = Path(__file__).resolve().parent.parent
H0 = 168 * 3000          # an hour number divisible by 168, so slot index == hours since H0 (easy to reason about)
T0 = H0 * 3600           # its start, epoch seconds
METRICS = mr.METRICS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Isolated state dir + empty fake hwmon/proc, no nvidia-smi, no exporter. Tests fill in what they need."""
    state, hw, proc = tmp_path / "state", tmp_path / "hwmon", tmp_path / "proc"
    for d in (state, hw, proc):
        d.mkdir()
    monkeypatch.setattr(mr, "STATE_DIR", state)
    monkeypatch.setattr(mr, "HWMON", hw)
    monkeypatch.setattr(mr, "PROC", proc)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(tmp_path / "no-such-nvidia-smi"))
    monkeypatch.setattr(mr, "EXPORTER", ("127.0.0.1", _free_port()))     # nothing listens there
    monkeypatch.setattr(mr, "QUICK_CPU_S", 0)
    return SimpleNamespace(state=state, hw=hw, proc=proc, tmp=tmp_path, ring=state / "metrics-ring.json")


def rec(t, **m):
    return mr.record(t, m)


def read_ring(env):
    return json.loads(env.ring.read_text())


# --------------------------------------------------------------------------- fake host
def make_chip(env, idx, name, files):
    d = env.hw / f"hwmon{idx}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for k, v in files.items():
        (d / k).write_text(f"{v}\n")


def fake_hwmon(env, fans=True):
    make_chip(env, 0, "acpitz", {"temp1_input": 27800})
    make_chip(env, 1, "nvme", {"temp1_label": "Composite", "temp1_input": 54850, "temp2_label": "Sensor 1",
                               "temp2_input": 54850, "temp3_label": "Sensor 2", "temp3_input": 59850})
    make_chip(env, 2, "coretemp", {"temp1_label": "Package id 0", "temp1_input": 47000, "temp2_label": "Core 0",
                                   "temp2_input": 35000})
    f = {f"fan{i}_input": r for i, r in zip(range(1, 8), (976, 642, 656, 1062, 1005, 0, 0))} if fans else {}
    make_chip(env, 3, "nct6798", {"temp1_label": "SYSTIN", "temp1_input": 37000, **f})
    make_chip(env, 4, "drivetemp", {"temp1_input": 41000})
    make_chip(env, 13, "spd5118", {"temp1_input": 38000})
    make_chip(env, 16, "spd5118", {"temp1_input": 36500})


def fake_proc(env, stat="cpu  300 0 100 1100 100 0 0 0 0 0\ncpu0 1 1 1 1 1 1 1 1 1 1\n"):
    (env.proc / "stat").write_text(stat)
    (env.proc / "meminfo").write_text("MemTotal:       98608356 kB\nMemFree: 1 kB\nMemAvailable:   70927756 kB\n")
    (env.proc / "loadavg").write_text("29.87 25.42 22.74 4/7617 2558406\n")


def fake_smi(env, monkeypatch, line="61, 7, 38, 127.08, 1366, 24576", rc=0):
    p = env.tmp / "nvidia-smi"
    p.write_text(f"#!/bin/sh\necho '{line}'\nexit {rc}\n")
    p.chmod(0o755)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(p))


class FakeExporter:
    """Stand-in for sensor-exporter on 127.0.0.1:<ephemeral>. `hang` accepts the request and never answers."""

    def __init__(self, monkeypatch, payload=None, body=None, status=200, hang=False):
        raw = body if body is not None else json.dumps(payload).encode()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if hang:
                    time.sleep(6)
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        monkeypatch.setattr(mr, "EXPORTER", ("127.0.0.1", self.srv.server_address[1]))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def exporter(monkeypatch):
    made = []

    def make(**kw):
        made.append(FakeExporter(monkeypatch, **kw))
        return made[-1]
    yield make
    for e in made:
        e.close()


EXPORTER_JSON = {"cpu_temp": 38, "gpu_temp": 61, "gpu_fan": 50, "systin": 37, "nvme_temp": 54.9, "cpu_fan": 1019,
                 "case_fan": 768, "fans": {"fan1": 976}, "gpu_fan_rpm": 450, "fan_duty_cpu": 30}
EXPORTER_BODY = json.dumps(EXPORTER_JSON).encode()
EXPORTER_HEAD = b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(EXPORTER_BODY)


class Drip:
    """Raw-socket stand-in for a misbehaving sensor-exporter on an ephemeral loopback port.

    `script` is a list of (bytes, seconds_to_sleep_afterwards); after it ran the connection is held open for `hold`
    seconds (a peer that keeps the socket open after answering) and then closed.
    """

    def __init__(self, monkeypatch, script, hold=0.0):
        self.script, self.hold, self.stop = script, hold, threading.Event()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.srv.settimeout(0.05)
        threading.Thread(target=self._accept, daemon=True).start()
        monkeypatch.setattr(mr, "EXPORTER", ("127.0.0.1", self.srv.getsockname()[1]))

    def _accept(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        with c:
            try:
                c.recv(4096)                                    # the request
                for data, pause in self.script:
                    c.sendall(data)
                    if self.stop.wait(pause):
                        return
                self.stop.wait(self.hold)
            except OSError:
                pass                                            # the client gave up: exactly what we are testing for

    def close(self):
        self.stop.set()
        self.srv.close()


def per_byte(data: bytes, gap: float):
    return [(data[i:i + 1], gap) for i in range(len(data))]


@pytest.fixture
def drip(monkeypatch):
    made = []

    def make(script, hold=0.0):
        made.append(Drip(monkeypatch, script, hold))
        return made[-1]
    yield make
    for d in made:
        d.close()


# --------------------------------------------------------------------------- ring: rollover, wrap, partial
def test_hour_rollover_starts_a_fresh_slot(env):
    assert rec(T0 + 3599, load1=10.0)
    assert rec(T0 + 3600, load1=20.0)
    assert rec(T0 + 3660, load1=40.0)
    e = mr.export(now=T0 + 3700)
    assert e["series"]["t"][-2:] == [T0, T0 + 3600]
    assert e["series"]["load1"][-2:] == [10.0, 30.0]          # the new hour does not inherit the old one's sample
    assert (e["hour_avg"]["load1"], e["prev_hour_avg"]["load1"]) == (30.0, 10.0)
    hours = read_ring(env)["hours"]
    assert (hours[0]["h"], hours[0]["n"]) == (H0, 1) and (hours[1]["h"], hours[1]["n"]) == (H0 + 1, 2)
    # next hour, no sample yet: in-progress average is empty, previous hour keeps its mean, nothing is invented
    e = mr.export(now=T0 + 7300)
    assert e["hour_avg"]["load1"] is None and e["prev_hour_avg"]["load1"] == 30.0 and e["series"]["load1"][-1] is None


def test_ring_wrap_overwrites_the_oldest_hour(env):
    for i in range(168):
        rec(T0 + i * 3600 + 5, load1=float(i))                 # hours H0 .. H0+167, value == offset
    e = mr.export(now=T0 + 167 * 3600 + 10)
    assert e["loop"]["complete"] is True and e["loop"]["oldest_hour"] == T0
    assert e["series"]["load1"][0] == 0.0 and e["series"]["load1"][-1] == 167.0
    # an hour exactly 168 slots after the first lands in the same slot and replaces it
    assert rec(T0 + 168 * 3600 + 5, load1=999.0)
    slot = read_ring(env)["hours"][0]
    assert (slot["h"], slot["n"], slot["sum"]["load1"]) == (H0 + 168, 1, 999.0)   # nothing of the old hour is left
    e = mr.export(now=T0 + 168 * 3600 + 10)
    s = e["series"]["load1"]
    assert len(s) == 168 and s[-1] == 999.0 and s[0] == 1.0 and 0.0 not in s    # hour H0 is gone, H0+1 is the oldest
    assert e["series"]["t"][0] == T0 + 3600 and e["loop"]["oldest_hour"] == T0 + 3600
    assert e["loop"]["complete"] is True and len(read_ring(env)["hours"]) == 168


def test_complete_does_not_flap_at_the_top_of_the_hour(env):
    for i in range(168):
        rec(T0 + i * 3600 + 5, load1=float(i))
    # :00 has passed but the sampler has not ticked yet: the old hour is still in the file, the ring is still full
    e = mr.export(now=T0 + 168 * 3600 + 1)
    assert e["loop"]["complete"] is True and e["series"]["load1"][-1] is None and e["series"]["load1"][0] == 1.0


def test_a_missing_hour_leaves_a_gap_not_stale_data(env):
    rec(T0 + 5, load1=1.0)
    rec(T0 + 3 * 3600 + 5, load1=3.0)                           # hours 1 and 2 never sampled
    s = mr.export(now=T0 + 3 * 3600 + 10)["series"]["load1"]
    assert s[-4:] == [1.0, None, None, 3.0]
    # a slot still holding last week's hour must not be shown as this week's
    rec(T0 + 168 * 3600 + 5, load1=7.0)                         # reuses slot 0
    rec(T0 + 171 * 3600 + 5, load1=8.0)
    s = mr.export(now=T0 + 171 * 3600 + 10)["series"]["load1"]
    assert s[-4:] == [7.0, None, None, 8.0] and 1.0 not in s and 3.0 not in s


def test_partial_ring_first_day(env):
    for i in range(3):
        rec(T0 + i * 3600 + 5, load1=10.0 * (i + 1), cpu_temp=40.0 + i)
    e = mr.export(now=T0 + 2 * 3600 + 100)
    assert len(e["series"]["t"]) == 168 and len(e["series"]["load1"]) == 168
    assert e["series"]["load1"][:165] == [None] * 165 and e["series"]["load1"][-3:] == [10.0, 20.0, 30.0]
    assert e["loop"]["complete"] is False and e["loop"]["filled"] == 3 and e["loop"]["oldest_hour"] == T0
    assert e["avg_7d"]["load1"] == 20.0 and e["avg_24h"]["load1"] == 20.0 and e["max_7d"]["load1"] == 30.0
    assert e["avg_7d"]["gpu_temp"] is None and e["max_7d"]["gpu_temp"] is None


def test_averages_are_sample_weighted_and_windowed(env):
    for k in range(60):
        rec(T0 + k * 60, load1=10.0)                           # an hour of 60 samples at 10 ...
    rec(T0 + 3600 + 5, load1=100.0)                            # ... and one sample at 100 in the next hour
    e = mr.export(now=T0 + 3600 + 30)
    assert e["avg_24h"]["load1"] == round((600 + 100) / 61, 1)  # not the mean of means (55.0)
    # data 25 hours old is outside the 24 h window but inside the 7 d one
    rec(T0 + 30 * 3600, load1=50.0)
    e = mr.export(now=T0 + 30 * 3600 + 10)
    assert e["avg_24h"]["load1"] == 50.0 and e["avg_7d"]["load1"] == round((600 + 100 + 50) / 62, 1)
    assert e["max_7d"]["load1"] == 100.0


# --------------------------------------------------------------------------- ring: None / junk values
def test_none_values_do_not_skew_the_mean(env):
    rec(T0 + 1, cpu_temp=40.0, gpu_temp=None)
    rec(T0 + 2, cpu_temp=None, gpu_temp=None)
    rec(T0 + 3, cpu_temp=60.0)
    rec(T0 + 4)                                                 # a sample with nothing at all still counts as a sample
    slot = read_ring(env)["hours"][0]
    assert slot["n"] == 4 and slot["cnt"] == {"cpu_temp": 2} and slot["sum"] == {"cpu_temp": 100.0}
    assert (slot["max"]["cpu_temp"], slot["min"]["cpu_temp"]) == (60.0, 40.0)
    e = mr.export(now=T0 + 10)
    assert e["series"]["cpu_temp"][-1] == 50.0 and e["hour_avg"]["cpu_temp"] == 50.0   # 100/2, not 100/4
    assert e["series"]["gpu_temp"][-1] is None and e["avg_7d"]["gpu_temp"] is None and e["max_7d"]["gpu_temp"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "hot", True, [1], {"a": 1}, None, 1e12, -273.0 * 1000])
def test_junk_readings_become_none(env, bad):
    rec(T0 + 1, cpu_temp=bad, cpu_fan_rpm=bad, load1=5.0)
    slot = read_ring(env)["hours"][0]
    assert slot["cnt"] == {"load1": 1}                          # nothing else was folded in


def test_out_of_range_readings_are_dropped_per_metric(env):
    rec(T0 + 1, cpu_temp=151.0, nvme_temp=-41.0, cpu_pct=100.5, gpu_pct=-0.1, cpu_fan_rpm=65535.0, gpu_power_w=5000)
    assert read_ring(env)["hours"][0]["cnt"] == {}
    rec(T0 + 2, cpu_temp=150.0, nvme_temp=-40.0, cpu_pct=100.0, gpu_pct=0.0, cpu_fan_rpm=0.0)
    assert read_ring(env)["hours"][0]["cnt"] == {"cpu_temp": 1, "nvme_temp": 1, "cpu_pct": 1, "gpu_pct": 1, "cpu_fan_rpm": 1}


def test_unknown_metric_names_never_enter_the_ring(env):
    rec(T0 + 1, load1=1.0, secret_token="abc", cpu_temp=40.0)
    assert set(read_ring(env)["hours"][0]["sum"]) == {"load1", "cpu_temp"}


# --------------------------------------------------------------------------- ring: clock going backwards
def test_clock_going_backwards_is_ignored(env):
    assert rec(T0 + 10 * 3600 + 5, load1=5.0)                   # newest hour: H0+10
    assert rec(T0 + 5 * 3600, load1=99.0) is False              # 5 h behind: ignored, not folded into the past
    ring = read_ring(env)
    assert ring["hours"][5]["n"] == 0 and ring["last"]["rej"] == 1 and ring["last"]["cur"]["load1"] == 5.0
    e = mr.export(now=T0 + 10 * 3600 + 10)
    assert 99.0 not in e["series"]["load1"] and e["current"]["load1"] == 5.0 and e["series"]["load1"][-1] == 5.0
    # SPEC2: only samples older than the newest hour minus 1 are ignored
    assert rec(T0 + 9 * 3600 + 5, load1=7.0) is True
    assert rec(T0 + 10 * 3600 + 1, load1=9.0) is True           # a small step back inside the same hour is fine
    ring = read_ring(env)
    assert ring["last"]["rej"] == 0 and ring["hours"][9]["n"] == 1 and ring["hours"][10]["n"] == 2
    assert len(ring["hours"]) == 168


def test_first_sample_is_accepted_whatever_the_clock_says(env):
    assert rec(5, load1=1.0) is True                            # empty ring: nothing to be "behind"


def test_clock_stuck_behind_drops_the_bogus_future_hours(env):
    # RTC garbage at boot wrote two hours far in the future; NTP then corrected the clock.
    rec(T0 + 500 * 3600, load1=1.0)
    rec(T0 + 501 * 3600, load1=1.0)
    n = mr.REWIND_AFTER
    res = [rec(T0 + 100 * 3600 + i * 60, load1=2.0) for i in range(n + 2)]
    assert res[:n - 1] == [False] * (n - 1) and all(res[n - 1:])    # patient, then recovers by itself
    ring = read_ring(env)
    assert not any(s["h"] > H0 + 100 for s in ring["hours"]) and ring["last"]["rej"] == 0
    e = mr.export(now=T0 + 100 * 3600 + 1000)
    assert e["series"]["load1"][-1] == 2.0 and e["series"]["load1"].count(None) == 167
    assert ring["hours"][100]["n"] == 3                         # the three samples taken after the rewind


def test_rewind_keeps_valid_older_hours(env):
    rec(T0 + 90 * 3600 + 5, load1=1.0)                          # legitimate history
    rec(T0 + 99 * 3600 + 5, load1=1.0)
    rec(T0 + 300 * 3600, load1=1.0)                             # bogus future
    for i in range(mr.REWIND_AFTER):
        rec(T0 + 100 * 3600 + i * 60, load1=2.0)
    s = mr.export(now=T0 + 100 * 3600 + 700)["series"]["load1"]
    assert s[-1] == 2.0 and s[-2] == 1.0 and s[-11] == 1.0 and s[-3:-1].count(None) == 1


# --------------------------------------------------------------------------- ring: corrupt and damaged files
CORRUPT = {
    "empty": b"",
    "not json": b"not json {{",
    "binary": bytes(range(256)) * 8,
    "wrong type": b"[]",
    "no hours": b'{"v":1,"slots":168}',
    "short hours": json.dumps({"v": 1, "slots": 168, "hours": [{}] * 10}).encode(),
    "wrong version": json.dumps({"v": 2, "slots": 168, "hours": []}).encode(),
    "wrong slot count": json.dumps({"v": 1, "slots": 24, "hours": [{}] * 24}).encode(),
    "deeply nested": b"[" * 200_000,
    "truncated": b'{"v":1,"slots":168,"hours":[{"h":1,"n":1,"su',
}


@pytest.mark.parametrize("name", sorted(CORRUPT))
def test_corrupt_file_is_replaced_and_kept_as_bad(env, name):
    env.ring.write_bytes(CORRUPT[name])
    assert rec(T0 + 5, load1=3.0) is True
    bad = env.state / "metrics-ring.json.bad"
    assert bad.read_bytes() == CORRUPT[name]                    # kept for inspection
    ring = read_ring(env)
    assert len(ring["hours"]) == 168 and ring["hours"][0]["n"] == 1 and ring["hours"][0]["sum"] == {"load1": 3.0}


@pytest.mark.parametrize("name", sorted(CORRUPT))
def test_export_of_a_corrupt_file_is_empty_and_changes_nothing(env, name):
    env.ring.write_bytes(CORRUPT[name])
    e = mr.export(now=T0)
    assert e["stale"] is True and e["loop"]["filled"] == 0 and len(e["series"]["t"]) == 168
    assert all(v is None for v in e["series"]["load1"]) and e["current"]["sampled_at"] is None
    assert env.ring.read_bytes() == CORRUPT[name] and not (env.state / "metrics-ring.json.bad").exists()


def test_a_damaged_slot_is_reset_but_the_rest_survives(env):
    for i in range(4):
        rec(T0 + i * 3600 + 5, load1=float(i + 1))
    ring = read_ring(env)
    ring["hours"][1] = "junk"                                   # not a dict
    ring["hours"][2]["n"] = "x"                                 # wrong type
    ring["hours"][3]["sum"]["evil"] = 1.0                       # unknown metric
    ring["hours"][4] = dict(ring["hours"][0])                   # hour H0 stored under index 4: wrong index
    env.ring.write_text(json.dumps(ring))
    loaded = mr._load(env.ring, repair=True)
    assert [s["n"] for s in loaded["hours"][:5]] == [1, 0, 0, 1, 0]
    assert "evil" not in loaded["hours"][3]["sum"] and not (env.state / "metrics-ring.json.bad").exists()
    assert rec(T0 + 4 * 3600 + 5, load1=9.0)                    # and the ring keeps working


def test_non_finite_numbers_in_the_file_cannot_reach_the_export(env):
    rec(T0 + 5, load1=4.0, cpu_temp=40.0)
    raw = env.ring.read_text().replace('"load1":4.0', '"load1":NaN').replace('"cpu_temp":40.0', '"cpu_temp":Infinity')
    env.ring.write_text(raw)                                    # Python's json accepts both literals
    e = mr.export(now=T0 + 10)
    assert e["series"]["load1"][-1] is None
    json.dumps(e, allow_nan=False)                              # the export is always strict JSON


# --------------------------------------------------------------------------- ring: unreadable (not corrupt) file
def week_of_data(env, hours=100):
    for i in range(hours):
        rec(T0 + i * 3600 + 5, load1=float(i), cpu_temp=40.0 + i % 7)
    return env.ring.read_bytes()


def fail_ring_reads(monkeypatch, err, skip=0, times=1):
    """The first `times` reads of the ring file after `skip` good ones raise OSError(err): a transient I/O hiccup."""
    real, n = Path.read_bytes, {"i": 0}

    def read_bytes(self):
        if self.name == mr.RING_FILE:
            n["i"] += 1
            if skip < n["i"] <= skip + times:
                raise OSError(err, os.strerror(err))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return n


def hours_with_data(env):
    return sum(1 for s in read_ring(env)["hours"] if s["n"])


@pytest.mark.parametrize("err", [errno.EIO, errno.EMFILE, errno.EACCES, errno.ENOMEM])
def test_a_transient_read_error_never_resets_the_ring(env, monkeypatch, err):
    before = week_of_data(env)
    assert hours_with_data(env) == 100
    fail_ring_reads(monkeypatch, err)                           # one-shot, like the real thing
    with pytest.raises(OSError) as exc:
        mr.record(T0 + 100 * 3600 + 5, {"load1": 1.0})          # the sample fails ...
    assert exc.value.errno == err
    assert env.ring.read_bytes() == before                      # ... and 7 days of history are exactly as they were
    assert not (env.state / "metrics-ring.json.bad").exists() and not list(env.state.glob("*.tmp"))
    assert mr.record(T0 + 100 * 3600 + 5, {"load1": 1.0}) is True    # the next minute carries on from the old data
    assert hours_with_data(env) == 101 and read_ring(env)["hours"][0]["n"] == 1


@pytest.mark.parametrize("skip,times", [(0, 2), (1, 1)], ids=["pre-read-and-write-read", "write-read-only"])
def test_sampler_reports_an_unreadable_ring_and_leaves_it_alone(env, monkeypatch, skip, times):
    fake_hwmon(env)
    before = week_of_data(env)
    fail_ring_reads(monkeypatch, errno.EIO, skip=skip, times=times)
    out, problem = mr._sample(now=T0 + 100 * 3600 + 5)
    assert problem and "OSError" in problem and out["cpu_temp"] == 47.0     # reported, sensors still read, never raised
    assert env.ring.read_bytes() == before and not (env.state / "metrics-ring.json.bad").exists()


def test_cli_sample_fails_loudly_on_an_unreadable_ring(env, monkeypatch, capsys):
    before = week_of_data(env)
    fail_ring_reads(monkeypatch, errno.EIO, times=10**6)        # persistently unreadable
    assert mr.main(["sample"]) == 1                             # the unit shows as failed instead of destroying the file
    assert "metrics-ring:" in capsys.readouterr().err
    with open(env.ring, "rb") as f:                             # (Path.read_bytes is the patched one)
        assert f.read() == before


def test_export_of_an_unreadable_ring_is_empty_and_changes_nothing(env, monkeypatch):
    before = week_of_data(env)
    n = fail_ring_reads(monkeypatch, errno.EIO)
    e = mr.export(now=T0 + 99 * 3600 + 100)                     # readers keep the old fall-back-to-empty behaviour
    assert e["stale"] is True and e["loop"]["filled"] == 0 and n["i"] == 1
    assert env.ring.read_bytes() == before and not (env.state / "metrics-ring.json.bad").exists()
    assert mr.export(now=T0 + 99 * 3600 + 100)["loop"]["filled"] == 99 + 1   # the next read sees everything again


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_really_unreadable_file_is_left_alone(env):
    before = week_of_data(env)
    env.ring.chmod(0)
    try:
        with pytest.raises(PermissionError):
            mr.record(T0 + 100 * 3600 + 5, {"load1": 1.0})
    finally:
        env.ring.chmod(0o644)
    assert env.ring.read_bytes() == before and not (env.state / "metrics-ring.json.bad").exists()


def test_a_missing_ring_still_means_start_a_new_one(env):
    week_of_data(env, hours=3)
    env.ring.unlink()                                           # FileNotFoundError is the one "start over" case
    assert rec(T0 + 5 * 3600, load1=1.0) is True and hours_with_data(env) == 1
    shutil.rmtree(env.state)                                    # even the state dir itself
    assert mr.sample_once(now=T0)["sampled_at"] == T0 and env.ring.exists()
    assert not (env.state / "metrics-ring.json.bad").exists()


# --------------------------------------------------------------------------- ring: concurrency (flock)
def test_concurrent_threads_lose_no_samples(env):
    def worker(w):
        for k in range(25):
            assert mr.record(T0 + 100 + w * 30 + k, {"load1": 1.0, "cpu_temp": 50.0})
    ts = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    slot = read_ring(env)["hours"][0]
    assert slot["n"] == 200 and slot["cnt"] == {"load1": 200, "cpu_temp": 200} and slot["sum"]["load1"] == 200.0
    assert not (env.state / "metrics-ring.json.bad").exists()   # no reader ever saw a torn file


def test_concurrent_processes_lose_no_samples(env):
    code = ("import sys\nfrom homelab_maint import metrics_ring as mr\n"
            "w = int(sys.argv[1])\n"
            "for k in range(15):\n"
            "    assert mr.record(%d + 100 + w * 30 + k, {'load1': 2.0})\n" % T0)
    penv = {**os.environ, "HOMELAB_MAINT_STATE": str(env.state), "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(w)], env=penv, cwd=ROOT) for w in range(4)]
    assert [p.wait(timeout=60) for p in procs] == [0] * 4
    slot = read_ring(env)["hours"][0]
    assert slot["n"] == 60 and slot["sum"]["load1"] == 120.0 and not (env.state / "metrics-ring.json.bad").exists()


def test_lock_held_elsewhere_times_out_cleanly(env, monkeypatch):
    monkeypatch.setattr(mr, "LOCK_WAIT_S", 0.1)
    with open(env.state / "metrics.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            mr.record(T0, {"load1": 1.0}, timeout=0.1)
        out, problem = mr._sample(now=T0)                       # the sampler reports it, never raises
        assert "busy" in problem and set(METRICS) <= set(out)
    assert mr.record(T0, {"load1": 1.0})                        # released: works again


def test_file_mode_atomic_and_lock_file(env):
    old = os.umask(0o077)
    try:
        rec(T0 + 1, load1=1.0)
    finally:
        os.umask(old)
    assert env.ring.stat().st_mode & 0o777 == 0o644             # readable by the www/publish side whatever the umask
    assert (env.state / "metrics.lock").exists() and not list(env.state.glob("*.tmp"))


def test_ring_size_is_bounded_over_two_weeks(env):
    for i in range(14 * 24):
        mr.record(T0 + i * 3600 + 7, {m: 20.0 + i % 50 for m in METRICS})
    ring = read_ring(env)
    assert len(ring["hours"]) == 168 and ring["slots"] == 168
    assert env.ring.stat().st_size < 250_000                    # ~185 KB with all 15 metrics in every slot
    e = mr.export(now=T0 + 14 * 24 * 3600 - 3000)
    assert e["loop"]["complete"] is True and all(v is not None for v in e["series"]["cpu_temp"])


# --------------------------------------------------------------------------- export shape
def check_shape(e, now):
    assert set(e) == {"generated_at", "interval_s", "slots", "stale", "current", "hour_avg", "prev_hour_avg", "avg_24h",
                      "avg_7d", "max_7d", "loop", "series"}
    assert (e["generated_at"], e["interval_s"], e["slots"]) == (now, 3600, 168)
    assert set(e["current"]) == set(METRICS) | {"sampled_at"}
    for k in ("hour_avg", "prev_hour_avg", "avg_24h", "avg_7d", "max_7d"):
        assert set(e[k]) == set(METRICS), k
    assert set(e["series"]) == set(METRICS) | {"t"}
    t = e["series"]["t"]
    cur = int(now // 3600) * 3600
    assert len(t) == 168 and t[-1] == cur and t[0] == cur - 167 * 3600
    assert all(b - a == 3600 for a, b in zip(t, t[1:]))
    for m in METRICS:
        assert len(e["series"][m]) == 168, m
    assert set(e["loop"]) == {"pos", "slot", "overwrites_next_at", "oldest_hour", "complete", "filled"}
    assert e["loop"]["pos"] == 167 and e["loop"]["overwrites_next_at"] == cur + 3600
    assert e["loop"]["slot"] == (cur // 3600) % 168
    json.dumps(e, allow_nan=False)


def test_export_shape_empty_partial_and_full(env):
    e = mr.export(now=T0 + 1234.5)
    check_shape(e, T0 + 1234.5)
    assert e["stale"] is True and e["loop"]["complete"] is False and e["loop"]["oldest_hour"] is None
    assert all(v is None for m in METRICS for v in e["series"][m]) and e["current"]["sampled_at"] is None
    rec(T0 + 10, **{m: 20.0 for m in METRICS})
    check_shape(mr.export(now=T0 + 20), T0 + 20)
    for i in range(1, 200):
        mr.record(T0 + i * 3600 + 5, {m: 20.0 for m in METRICS})
    e = mr.export(now=T0 + 199 * 3600 + 20)
    check_shape(e, T0 + 199 * 3600 + 20)
    assert e["loop"]["complete"] is True and e["loop"]["oldest_hour"] == e["series"]["t"][0]


def test_export_is_rounded_and_reports_current_values(env):
    for k, v in enumerate((47.04, 47.06, 47.11)):
        rec(T0 + 100 + k, cpu_temp=v, cpu_fan_rpm=1019.0)
    e = mr.export(now=T0 + 110)
    assert e["current"]["cpu_temp"] == 47.1 and e["current"]["sampled_at"] == T0 + 102   # last reading, 1 decimal
    assert e["series"]["cpu_temp"][-1] == 47.1 and e["avg_7d"]["cpu_fan_rpm"] == 1019.0
    assert e["current"]["gpu_temp"] is None


def test_export_never_touches_the_state_dir(env):
    rec(T0 + 1, load1=1.0)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in env.state.iterdir())
    mr.export(now=T0 + 5)
    mr.export(now=T0 + 10 * 3600)
    assert before == sorted((p.name, p.stat().st_mtime_ns) for p in env.state.iterdir())
    shutil.rmtree(env.state)                                    # no state dir at all is fine too
    assert mr.export(now=T0)["stale"] is True and not env.state.exists()


# --------------------------------------------------------------------------- stale flag
def test_stale_flag(env):
    rec(T0 + 100, load1=1.0)
    assert mr.export(now=T0 + 100 + 299)["stale"] is False
    assert mr.export(now=T0 + 100 + 301)["stale"] is True       # sampler stopped for more than 5 minutes
    assert mr.export(now=T0 + 100 - 200)["stale"] is False      # small clock skew is not "stopped"
    assert mr.export(now=T0 + 100 - 400)["stale"] is True       # a sample from the future is not live data either
    assert mr.export(now=T0 + 100 + 301)["current"]["load1"] == 1.0   # last known values stay available


# --------------------------------------------------------------------------- sensors
def test_sample_reads_sysfs_proc_smi_and_exporter(env, monkeypatch, exporter):
    fake_hwmon(env)
    fake_proc(env)
    fake_smi(env, monkeypatch)
    exporter(payload=EXPORTER_JSON)
    mr.record(T0 - 55, {}, cpu=[1000, 800])                     # previous /proc/stat reading, 60 s ago
    got = mr.sample_once(now=T0 + 5)
    assert set(METRICS) | {"sampled_at"} == set(got) and got["sampled_at"] == T0 + 5
    assert got["cpu_temp"] == 47.0                              # sysfs package temp, not the exporter's rounded 38
    assert got["ram_temp"] == 37.25 and got["ram_temp_max"] == 38.0
    assert got["nvme_temp"] == 54.85                            # Composite, not the hotter "Sensor 2"
    assert got["cpu_fan_rpm"] == 1019.0 and got["case_fan_rpm"] == 768.0 and got["gpu_fan_rpm"] == 450.0
    assert (got["gpu_temp"], got["gpu_pct"], got["gpu_fan_pct"], got["gpu_power_w"]) == (61.0, 7.0, 38.0, 127.08)
    assert got["gpu_mem_pct"] == pytest.approx(100 * 1366 / 24576, abs=1e-3)
    assert got["ram_pct"] == pytest.approx(100 * (1 - 70927756 / 98608356), abs=1e-3)
    assert got["load1"] == 29.87
    assert got["cpu_pct"] == pytest.approx(100 * 200 / 600, abs=1e-3)   # (1600-1000) jiffies, 400 of them idle
    # ... and it was recorded
    e = mr.export(now=T0 + 10)
    assert e["current"]["cpu_temp"] == 47.0 and e["hour_avg"]["gpu_temp"] == 61.0 and e["stale"] is False
    assert read_ring(env)["last"]["cpu"] == [1600, 1200]


def test_sample_falls_back_to_hwmon_fans_when_the_exporter_is_down(env):
    fake_hwmon(env)
    fake_proc(env)
    got = mr.sample_once(now=T0)                                # EXPORTER points at a closed port, no nvidia-smi
    assert got["cpu_fan_rpm"] == 1019.0                         # mean of fan1 + fan4, the exporter's CPU group
    assert got["case_fan_rpm"] == pytest.approx((642 + 656 + 1005) / 3, abs=1e-3)   # fans > 0 only (fan6/7 read 0)
    assert got["gpu_temp"] is None and got["gpu_pct"] is None and got["gpu_fan_rpm"] is None
    assert got["cpu_temp"] == 47.0


def test_sample_falls_back_to_the_exporter_when_sysfs_and_smi_are_missing(env, exporter):
    exporter(payload=EXPORTER_JSON)
    got = mr.sample_once(now=T0)
    assert (got["cpu_temp"], got["nvme_temp"], got["gpu_temp"], got["gpu_fan_pct"]) == (38.0, 54.9, 61.0, 50.0)
    assert (got["cpu_fan_rpm"], got["case_fan_rpm"], got["gpu_fan_rpm"]) == (1019.0, 768.0, 450.0)
    assert got["ram_temp"] is None and got["gpu_pct"] is None and got["cpu_pct"] is None and got["ram_pct"] is None


@pytest.mark.parametrize("kw", [
    {"body": b"<html>nope</html>"}, {"body": b"[1,2,3]"}, {"body": b"null"}, {"payload": EXPORTER_JSON, "status": 500},
    {"payload": {"cpu_temp": "hot", "cpu_fan": True, "case_fan": 1e9, "gpu_fan_rpm": None, "nvme_temp": [1]}},
], ids=["html", "list", "null", "http-500", "wrong-types"])
def test_garbage_from_the_exporter_is_ignored(env, exporter, kw):
    exporter(**kw)
    got = mr.sample_once(now=T0)
    assert all(got[m] is None for m in METRICS)


def test_absurdly_large_numbers_do_not_blank_the_whole_sample(env, exporter):
    fake_hwmon(env)
    exporter(body=b'{"cpu_fan": 1' + b"0" * 400 + b', "gpu_fan": 1e999, "case_fan": 768, "gpu_fan_rpm": -1e999}')
    got = mr.sample_once(now=T0)
    assert got["cpu_temp"] == 47.0 and got["case_fan_rpm"] == 768.0       # the rest of the sample is intact
    assert got["cpu_fan_rpm"] == 1019.0 and got["gpu_fan_pct"] is None and got["gpu_fan_rpm"] is None   # hwmon fallback
    rec(T0 + 4000, load1=4.0)
    env.ring.write_text(env.ring.read_text().replace('"load1":4.0', '"load1":' + "9" * 400))
    assert mr.export(now=T0 + 4010)["series"]["load1"][-1] is None       # a 400-digit int in the file is dropped too


def test_slow_exporter_does_not_blow_the_time_budget(env, monkeypatch, exporter):
    fake_hwmon(env)
    fake_proc(env)
    fake_smi(env, monkeypatch)
    exporter(hang=True)
    t = time.monotonic()
    got = mr.sample_once(now=T0)
    assert time.monotonic() - t < 3.0                           # SPEC2: a sample finishes in < 3 s
    assert got["cpu_temp"] == 47.0 and got["gpu_temp"] == 61.0 and got["cpu_fan_rpm"] == 1019.0   # fallbacks filled in


def test_nvidia_smi_na_fields_and_failures(env, monkeypatch):
    fake_smi(env, monkeypatch, line="[N/A], 5, [N/A], [N/A], [Not Supported], 24576")
    got = mr.sample_once(now=T0)
    assert got["gpu_pct"] == 5.0 and got["gpu_temp"] is None and got["gpu_fan_pct"] is None
    assert got["gpu_power_w"] is None and got["gpu_mem_pct"] is None
    for k, (line, rc) in enumerate((("", 0), ("garbage", 0), ("61, 7, 38, 127, 1366, 24576", 9)), start=1):
        fake_smi(env, monkeypatch, line=line, rc=rc)
        got = mr.sample_once(now=T0 + 3600 * k)
        assert got["gpu_temp"] is None and got["gpu_pct"] is None, (line, rc)
    fake_smi(env, monkeypatch, line="61, 7")                    # too few columns: what is there is used, the rest is None
    got = mr.sample_once(now=T0 + 3600 * 5)
    assert (got["gpu_temp"], got["gpu_pct"], got["gpu_fan_pct"], got["gpu_mem_pct"]) == (61.0, 7.0, None, None)
    fake_smi(env, monkeypatch, line="61, 7, 38, 127, 0, 0")     # total memory 0: no division by zero
    assert mr.sample_once(now=T0 + 7200)["gpu_mem_pct"] is None


# --------------------------------------------------------------------------- hard deadlines: dripping exporter
def timed(fn, *a):
    t = time.monotonic()
    out = fn(*a)
    return out, time.monotonic() - t


def test_exporter_that_drips_its_body_is_cut_off_at_the_deadline(drip):
    # headers at once, then the 170-odd body bytes at one per 0.3 s: ~50 s of dripping, each recv() well inside 0.8 s
    drip([(EXPORTER_HEAD, 0)] + per_byte(EXPORTER_BODY, 0.3))
    out, took = timed(mr._exporter, 0.5)
    assert out == {} and took < 1.0, took                       # one overall deadline, not one timeout per recv()


def test_exporter_that_drips_its_headers_is_cut_off_at_the_deadline(drip):
    drip(per_byte(EXPORTER_HEAD + EXPORTER_BODY, 0.2))          # the status line alone takes 3 s
    out, took = timed(mr._exporter, 0.5)
    assert out == {} and took < 1.0, took


def test_exporter_that_answers_slowly_but_in_time_is_used(drip):
    mid = len(EXPORTER_BODY) // 2
    drip([(EXPORTER_HEAD[:20], 0.1), (EXPORTER_HEAD[20:] + EXPORTER_BODY[:mid], 0.1), (EXPORTER_BODY[mid:], 0)])
    out, took = timed(mr._exporter, 1.5)
    assert out == EXPORTER_JSON and took < 1.0                  # the deadline cuts off drips, not slow honest answers


@pytest.mark.parametrize("script,hold,want", [
    ([(b"HTTP/1.0 200 OK\r\n\r\n" + EXPORTER_BODY, 0)], 0, EXPORTER_JSON),          # no Content-Length: the body ends at EOF
    ([(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(EXPORTER_BODY) + EXPORTER_BODY, 0)], 5, EXPORTER_JSON),
    ([(EXPORTER_HEAD + EXPORTER_BODY + b"trailing junk", 0)], 0, EXPORTER_JSON),    # bytes past Content-Length are ignored
    ([(b"HTTP/1.0 200 OK\r\nContent-Length: 99999999\r\n\r\n{}", 0)], 0, {}),       # oversized
    ([(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n", 0)], 0, {}),
    ([(b"HTTP/1.0 200 OK\r\nContent-Length: 500\r\n\r\n" + EXPORTER_BODY, 0)], 0, {}),   # truncated: closed early
    ([(b"HTTP/1.0 200 OK\r\nContent-Length: abc\r\n\r\n{}", 0)], 0, {}),
    ([(b"HTTP/1.0 200 OK\r\nContent-Length: \xc2\xb2\r\n\r\n{}", 0)], 0, {}),       # a non-ASCII "digit"
    ([(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 2\r\n\r\n{}", 0)], 0, {}),
    ([(b"SSH-2.0-OpenSSH_9.6\r\n\r\n{}", 0)], 0, {}),                               # something else is on that port
    ([(b"HTTP/1.0 200 OK\r\nX-Pad: " + b"a" * 20000, 0)], 0, {}),                  # headers that never end
    ([(b"HTTP/1.0 200 OK\r\n\r\n" + b" " * 70000 + b"{}", 0)], 0, {}),             # endless body without a length
    ([(b"HTTP/1.0 200 OK\r\n", 0)], 0, {}),                                         # closed before the end of the headers
    ([], 0, {}),                                                                    # accepted, then closed with no answer
], ids=["eof-body", "keep-alive-with-length", "trailing-junk", "oversized", "chunked", "truncated", "bad-length",
        "unicode-length", "http-503", "not-http", "endless-headers", "endless-body", "no-header-end", "silent"])
def test_exporter_response_variants(drip, script, hold, want):
    drip(script, hold=hold)
    out, took = timed(mr._exporter, 1.0)
    assert out == want and took < 0.9, (out, took)


def test_exporter_hanging_or_refused_is_still_bounded(exporter, monkeypatch):
    exporter(hang=True)
    assert timed(mr._exporter, 0.4)[1] < 0.9                    # accepted, never answered
    monkeypatch.setattr(mr, "EXPORTER", ("127.0.0.1", _free_port()))     # nothing listens
    out, took = timed(mr._exporter, 0.4)
    assert out == {} and took < 0.3


# --------------------------------------------------------------------------- hard deadlines: wedged nvidia-smi
def fake_hanging_smi(env, monkeypatch, secs="7.77"):
    """nvidia-smi that prints nothing and sleeps (its own pid and its child's go to smi.pids)."""
    p = env.tmp / "nvidia-smi"
    p.write_text(f"#!/bin/sh\necho $$ > '{env.tmp}/smi.pids'\nsleep {secs} &\necho $! >> '{env.tmp}/smi.pids'\nwait\n")
    p.chmod(0o755)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(p))
    return env.tmp / "smi.pids"


def pids_of(pidfile):
    for _ in range(100):                                        # the script needs a moment to start and write them
        if pidfile.exists() and len(pidfile.read_text().split()) == 2:
            return [int(x) for x in pidfile.read_text().split()]
        time.sleep(0.02)
    raise AssertionError("fake nvidia-smi never started")


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")                              # a zombie is dead, it just has not been reaped yet


def wait_dead(pids, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end and any(alive(p) for p in pids):
        time.sleep(0.02)
    return not any(alive(p) for p in pids)


@pytest.fixture
def unkillable(monkeypatch, env):
    """Simulate a child that SIGKILL cannot remove (D state): kill()/killpg() do nothing. Cleans the stragglers up."""
    real_killpg, pidfile = os.killpg, env.tmp / "smi.pids"
    monkeypatch.setattr(subprocess.Popen, "kill", lambda self: None)
    monkeypatch.setattr(os, "killpg", lambda *a: None)
    yield pidfile
    if pidfile.exists():
        pids = [int(x) for x in pidfile.read_text().split()]
        for p in pids[:1]:                                      # the script leads its own session/group (pid == pgid)
            try:
                ours = str(env.tmp) in Path(f"/proc/{p}/cmdline").read_text()    # only our own fake, never a reused pid
            except OSError:
                continue
            if ours:
                real_killpg(p, signal.SIGKILL)


def test_nvidia_smi_that_cannot_be_killed_does_not_hold_the_sampler(env, monkeypatch, unkillable):
    fake_hanging_smi(env, monkeypatch)
    out, took = timed(mr._nvidia, 0.5)                          # subprocess.run() would wait ~8 s here, then forever
    assert out == {} and took < 1.5, took
    assert all(alive(p) for p in pids_of(unkillable))           # (the simulation worked: nothing could kill it)


def test_nvidia_smi_timeout_kills_the_whole_process_group(env, monkeypatch):
    pidfile = fake_hanging_smi(env, monkeypatch)
    out, took = timed(mr._nvidia, 0.5)
    pids = pids_of(pidfile)
    assert out == {} and took < 1.5, took
    assert wait_dead(pids), "nvidia-smi or its child survived the timeout"


def test_nvidia_smi_gets_devnull_as_stdin(env, monkeypatch):
    p = env.tmp / "nvidia-smi"                                  # prints a temperature of 1 only if fd 0 is /dev/null
    p.write_text("#!/bin/sh\n[ \"$(readlink /proc/$$/fd/0)\" = /dev/null ] && echo '1, 1, 1, 1, 1, 1' || echo '9, 9, 9, 9, 9, 9'\n")
    p.chmod(0o755)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(p))
    assert mr._nvidia(2.0)["gpu_temp"] == 1.0


def test_nvidia_smi_missing_or_not_executable(env, monkeypatch):
    assert mr._nvidia(1.0) == {}                                # not installed
    p = env.tmp / "nvidia-smi"
    p.write_text("#!/bin/sh\necho 1, 1, 1, 1, 1, 1\n")
    p.chmod(0o644)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(p))
    assert mr._nvidia(1.0) == {}                                # PermissionError is just "no GPU reading"


def test_nvidia_smi_garbage_bytes_and_stderr_noise(env, monkeypatch):
    p = env.tmp / "nvidia-smi"
    p.write_bytes(b"#!/bin/sh\necho oops >&2\nprintf '61, \\377\\376, 38, 127, 100, 200\\n'\n")
    p.chmod(0o755)
    monkeypatch.setattr(mr, "NVIDIA_SMI", str(p))
    got = mr._nvidia(2.0)
    assert got["gpu_temp"] == 61.0 and got["gpu_pct"] is None and got["gpu_fan_pct"] == 38.0   # bad bytes: that field only


def test_a_sample_with_a_dripping_exporter_and_an_unkillable_nvidia_smi_stays_under_3_s(env, monkeypatch, drip,
                                                                                         unkillable):
    fake_hwmon(env)
    fake_proc(env)
    fake_hanging_smi(env, monkeypatch)
    drip([(EXPORTER_HEAD, 0)] + per_byte(EXPORTER_BODY, 0.3))   # the reviewer's 103 s case, plus the 12 s one, together
    out, took = timed(mr._sample, T0)
    metrics, problem = out
    assert took < 3.0, took                                     # SPEC2: a sample finishes in < 3 s whatever the peers do
    assert problem is None and metrics["cpu_temp"] == 47.0 and metrics["cpu_fan_rpm"] == 1019.0   # hwmon fallbacks
    assert metrics["gpu_temp"] is None and metrics["load1"] == 29.87
    assert mr.export(now=T0 + 10)["current"]["cpu_temp"] == 47.0       # and it reached the ring


def test_zero_fan_rpm_is_kept_when_the_chip_says_nothing_turns(env):
    make_chip(env, 3, "nct6798", {f"fan{i}_input": 0 for i in range(1, 8)})
    assert mr.sample_once(now=T0)["cpu_fan_rpm"] == 0.0         # a stopped CPU fan is exactly what the chart is for
    assert mr.sample_once(now=T0 + 60)["case_fan_rpm"] == 0.0


def test_missing_dimm_sensors_leave_ram_temp_empty(env):
    make_chip(env, 2, "coretemp", {"temp1_label": "Package id 0", "temp1_input": 47000})
    got = mr.sample_once(now=T0)
    assert got["ram_temp"] is None and got["ram_temp_max"] is None and got["cpu_temp"] == 47.0


# --------------------------------------------------------------------------- never raises
def boom(*a, **k):
    raise RuntimeError("sensor exploded")


def test_sample_once_with_no_sensors_at_all(env):
    got = mr.sample_once(now=T0)                                # empty hwmon + proc, no smi, no exporter
    assert {m: got[m] for m in METRICS} == {m: None for m in METRICS} and got["sampled_at"] == T0
    assert mr.export(now=T0 + 1)["current"]["sampled_at"] == T0     # even an all-None sample is recorded (liveness)
    assert read_ring(env)["hours"][0]["n"] == 1


def test_sample_once_with_missing_sysfs_and_proc_directories(env, monkeypatch):
    monkeypatch.setattr(mr, "HWMON", env.tmp / "nope" / "hwmon")
    monkeypatch.setattr(mr, "PROC", env.tmp / "nope" / "proc")
    assert mr.sample_once(now=T0)["cpu_temp"] is None


@pytest.mark.parametrize("name", ["_chips", "_hw_temps", "_hw_fans", "_exporter", "_nvidia", "_cpu", "_jiffies", "_ram_pct",
                                  "_load1", "_collect", "_clean", "record", "_load"])
def test_sample_once_survives_any_reader_raising(env, monkeypatch, name):
    fake_hwmon(env)
    fake_proc(env)
    monkeypatch.setattr(mr, name, boom)
    got = mr.sample_once(now=T0)
    assert set(METRICS) | {"sampled_at"} <= set(got)


def test_sample_once_with_bad_input_and_unwritable_state(env, monkeypatch):
    fake_hwmon(env)
    assert mr.sample_once(now="not a time")["cpu_temp"] == 47.0     # falls back to the wall clock
    blocker = env.tmp / "file"
    blocker.write_text("x")
    monkeypatch.setattr(mr, "STATE_DIR", blocker / "state")         # mkdir under a regular file fails
    out, problem = mr._sample(now=T0)
    assert out["cpu_temp"] == 47.0 and problem and "Error" in problem
    monkeypatch.setattr(mr, "STATE_DIR", env.state)
    env.state.chmod(0o500)                                          # read-only dir (a no-op for root, which is fine)
    try:
        assert mr.sample_once(now=T0 + 3600)["cpu_temp"] == 47.0
    finally:
        env.state.chmod(0o755)


def test_sample_once_returns_quickly_with_everything_present(env, monkeypatch, exporter):
    fake_hwmon(env)
    fake_proc(env)
    fake_smi(env, monkeypatch)
    exporter(payload=EXPORTER_JSON)
    t = time.monotonic()
    mr.sample_once(now=T0)
    assert time.monotonic() - t < 1.0


# --------------------------------------------------------------------------- cpu counters
def test_jiffies_parse_counts_iowait_as_idle(env):
    (env.proc / "stat").write_text("cpu  117091941 2882008 30243449 1293808693 44660378 0 1370633 0 446572 0\ncpu0 1 2 3 4\n")
    total = 117091941 + 2882008 + 30243449 + 1293808693 + 44660378 + 1370633   # guest columns are inside user/nice
    assert mr._jiffies() == [total, 1293808693 + 44660378]
    for bad in ("", "garbage", "cpu a b c", "intr 1 2 3\n"):
        (env.proc / "stat").write_text(bad)
        assert mr._jiffies() is None
    (env.proc / "stat").unlink()
    assert mr._jiffies() is None


def seq(monkeypatch, *vals):
    it = iter(vals)
    monkeypatch.setattr(mr, "_jiffies", lambda: next(it))


def test_cpu_pct_is_the_average_since_the_previous_sample(env, monkeypatch):
    seq(monkeypatch, [2000, 1300])
    assert mr._cpu([1000, 800], 100.0, 160.0) == (50.0, [2000, 1300])   # one read: the previous one is diffed


def test_cpu_pct_without_a_usable_previous_reading_uses_a_short_delta(env, monkeypatch):
    seq(monkeypatch, [100, 80], [200, 100])                     # the second read is the in-run 0.2 s later one
    assert mr._cpu(None, None, 160.0) == (80.0, [200, 100])
    seq(monkeypatch, [100, 80], [200, 100])                     # counters went backwards: the host rebooted
    assert mr._cpu([9000, 8000], 100.0, 160.0) == (80.0, [200, 100])
    seq(monkeypatch, [100, 80], [200, 100])                     # previous reading is 20 min old: not "the last minute"
    assert mr._cpu([10, 5], 100.0, 1300.0) == (80.0, [200, 100])
    seq(monkeypatch, [100, 80], [200, 100])                     # previous reading from the future (clock stepped back)
    assert mr._cpu([10, 5], 500.0, 160.0) == (80.0, [200, 100])


def test_busy_pct_edge_cases():
    assert mr._busy_pct([0, 0], [100, 100]) == 0.0
    assert mr._busy_pct([0, 0], [100, 0]) == 100.0
    assert mr._busy_pct([5, 5], [5, 5]) is None                 # no time passed
    assert mr._busy_pct([5, 5], [10, 3]) is None                # idle went backwards
    assert mr._busy_pct([5, 0], [10, 9]) is None                # more idle than total


# --------------------------------------------------------------------------- CLI
def test_cli_export_and_sample(env, monkeypatch, capsys):
    assert mr.main(["export"]) == 0
    e = json.loads(capsys.readouterr().out)
    check_shape(e, e["generated_at"])
    assert mr.main(["export", "--pretty"]) == 0 and "\n " in capsys.readouterr().out
    fake_hwmon(env)
    assert mr.main(["sample"]) == 0
    capsys.readouterr()
    assert mr.main(["sample", "-v"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cpu_temp"] == 47.0 and "sampled_at" in out
    blocker = env.tmp / "file"
    blocker.write_text("x")
    monkeypatch.setattr(mr, "STATE_DIR", blocker / "state")
    assert mr.main(["sample"]) == 1                             # not recorded: the unit shows as failed
    assert "metrics-ring:" in capsys.readouterr().err


def test_cli_sample_is_silent_on_success(env, capsys):
    assert mr.main(["sample"]) == 0
    cap = capsys.readouterr()
    assert (cap.out, cap.err) == ("", "")                       # one line per minute would flood the journal


def test_module_entry_point_prints_export_json(tmp_path):
    penv = {**os.environ, "HOMELAB_MAINT_STATE": str(tmp_path), "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "homelab_maint.metrics_ring", "export"], env=penv, cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    e = json.loads(r.stdout)
    assert len(e["series"]["t"]) == 168 and e["stale"] is True


# --------------------------------------------------------------------------- systemd units
def unit(path):
    out: dict = {}
    sec = None
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", ln)
        if m:
            sec = m.group(1)
        else:
            k, _, v = ln.partition("=")
            out.setdefault(sec, {}).setdefault(k, []).append(v)
    return out


def test_service_unit_matches_spec2():
    s = unit(ROOT / "systemd" / "homelab-maint-metrics.service")["Service"]
    one = {k: v[0] for k, v in s.items()}
    assert one["Type"] == "oneshot" and one["User"] == "root" and one["Nice"] == "15"
    assert one["IOSchedulingClass"] == "idle" and one["TimeoutStartSec"] == "20"
    assert one["TimeoutStopSec"] == "5"                         # a child stuck in D state must not hold the unit for 90 s
    assert one["ExecStart"] == "/usr/bin/python3 -B -m homelab_maint.metrics_ring sample"
    assert "PYTHONPATH=/usr/local/lib/homelab-maint" in s["Environment"]
    assert one["ProtectSystem"] == "strict" and one["ReadWritePaths"] == "/var/lib/homelab-maint"
    assert one["ProtectHome"] == "yes" and one["PrivateTmp"] == "yes" and one["NoNewPrivileges"] == "yes"
    assert "PrivateDevices" not in one                          # nvidia-smi needs /dev/nvidia*
    assert one["UMask"] == "0022"


def test_timer_unit_matches_spec2():
    t = unit(ROOT / "systemd" / "homelab-maint-metrics.timer")
    one = {k: v[0] for k, v in t["Timer"].items()}
    assert one["OnBootSec"] == "1min" and one["OnUnitActiveSec"] == "1min" and one["AccuracySec"] == "5s"
    assert t["Install"]["WantedBy"] == ["timers.target"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze missing")
def test_systemd_analyze_verify_accepts_the_units(tmp_path):
    for n in ("homelab-maint-metrics.service", "homelab-maint-metrics.timer"):
        shutil.copy(ROOT / "systemd" / n, tmp_path / n)
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / "homelab-maint-metrics.service"),
                        str(tmp_path / "homelab-maint-metrics.timer")], capture_output=True, text=True, cwd=tmp_path)
    ours = [ln for ln in (r.stdout + r.stderr).splitlines() if str(tmp_path) in ln or "homelab-maint-metrics" in ln]
    assert not ours, "\n".join(ours)
