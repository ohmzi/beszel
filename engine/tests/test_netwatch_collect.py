"""Tests for homelab_maint/netwatch/collect.py: the always-on collector, with fake shell, Cloudflare and clock. No loop is started."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import os
import subprocess
import time

import pytest

from homelab_maint.netwatch import entities, protect, store
from homelab_maint.netwatch.cloudflare import CFUnavailable
from homelab_maint.netwatch.collect import Collector
from homelab_maint.netwatch.model import EdgeRow, NetEvent

NOW = 1798761600 + 1234  # 2027-01-01T00:20:34Z
BUCKET = 1798761600
CFG = {
    "tunnel_local_ports": [], "lan_cidrs": ["192.168.40.0/24"], "tailscale_cidrs": ["100.64.0.0/10"],
    "cf_account_id": "", "cf_zones": [], "sockets_every_s": 30, "journal_every_s": 60, "plex_every_s": 60,
    "cloudflare_every_s": 300, "cloudflare_window_s": 900, "keep_days": 30, "path_classes": {},
}

UFW = ("2027-01-01T00:19:00.000000+00:00 host kernel: [UFW BLOCK] IN=eth0 OUT= SRC=198.51.100.9 DST=203.0.113.10 "
       "LEN=40 PROTO=TCP DPT=23")
SSH_OK = "2027-01-01T00:19:45.000000+00:00 host sshd[100]: Accepted publickey for ohmz from 198.51.100.9 port 4001 ssh2"
SSH_FAIL = "2027-01-01T00:19:30.000000+00:00 host sshd[99]: Failed password for root from 198.51.100.9 port 4000 ssh2"
ESTAB = "ESTAB 0 0 203.0.113.10:22 203.0.113.7:50000\n\t cubic bytes_sent:100 bytes_acked:100 bytes_received:200\n"


def _cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


@pytest.fixture(autouse=True)
def dockerd_up(monkeypatch):
    monkeypatch.setattr(entities, "dockerd_running", lambda: True)


def fake_sh(calls=None, journal_k="", journal_ssh=""):
    """Host with no containers, one listening port and one established inbound socket; journalctl answers per unit."""
    def sh(cmd, timeout=60, check=False, input_=None, env=None):
        if calls is not None:
            calls.append(list(cmd))
        head = cmd[0]
        if head == "systemctl":
            return _cp("active\n")
        if head == "docker":
            return _cp("")
        if head == "hostname":
            return _cp("203.0.113.10 \n")
        if head == "ss" and "-tlnH" in cmd:
            return _cp("LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n")
        if head == "ss" and "-ulnH" in cmd:
            return _cp("")
        if head == "ss":
            return _cp(ESTAB)
        if head == "journalctl":
            body = journal_k if "-k" in cmd else journal_ssh
            return _cp(body + "-- cursor: s=cur;i=1\n")
        return _cp("", rc=1)
    return sh


class FakeCF:
    def __init__(self, fail=None):
        self.fail = fail
        self.edge_calls = []

    def _maybe_fail(self):
        if self.fail:
            raise CFUnavailable(self.fail)

    def zones(self):
        self._maybe_fail()
        return {"example.test": "zone1"}

    def tunnel_hostnames(self):
        self._maybe_fail()
        return {"app.example.test": "http://127.0.0.1:9000"}

    def edge_requests(self, zone_id, start, end, path_classes):
        self._maybe_fail()
        self.edge_calls.append((zone_id, start, end))
        return [EdgeRow(bucket=BUCKET, zone=zone_id, host="app.example.test", client_ip="198.51.100.20", country="US",
                        asn=64500, status=200, path_class="page", count=3, bytes=900)]

    def security_events(self, zone_id, start, end):
        self._maybe_fail()
        return [NetEvent(ts=NOW - 100, kind="cf_security", src="198.51.100.30", dst="app.example.test", dport=443,
                         detail="block/waf")]


def _collector(tmp_path, cf=None, sh=None, loadavg=lambda: (0.1, 0.0, 0.0), cpus=lambda: 4, clock=lambda: NOW):
    db = store.open_db(tmp_path / "n.db")
    cfg = {**CFG, "plex_dir": str(tmp_path / "plex")}
    col = Collector(db, cfg, cf, sh=sh or fake_sh(), clock=clock, loadavg=loadavg, cpus=cpus)
    return db, col


def _count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_one_tick_writes_flow_edge_and_event_rows(tmp_path):
    _plex_log(tmp_path)           # an empty Plex log: present, nothing in it
    sh = fake_sh(journal_k=UFW + "\n", journal_ssh=SSH_FAIL + "\n")
    db, col = _collector(tmp_path, cf=FakeCF(), sh=sh)
    status = col.tick(NOW)

    assert status == {
        "sockets": "ok", "kernel": "ok", "auth": "ok", "plex": "ok", "signins": "ok", "cloudflare": "ok", "tunnel": "ok",
    }
    assert _count(db, "flows") >= 1
    assert _count(db, "edge") == 1
    kinds = {row[0] for row in db.execute("SELECT kind FROM events")}
    assert {"fw_block", "auth_fail", "cf_security"} <= kinds
    assert store.meta_get(db, "source:sockets").startswith("ok|")
    assert store.meta_get(db, "source:cloudflare").startswith("ok|")


def test_cloudflare_unavailable_is_stale_and_other_sources_still_record(tmp_path):
    db, col = _collector(tmp_path, cf=FakeCF(fail="token"), sh=fake_sh(journal_k=UFW + "\n"))
    status = col.tick(NOW)

    assert status["cloudflare"] == "stale:token"
    assert status["sockets"] == "ok"
    assert store.meta_get(db, "source:cloudflare") == f"stale:token|{NOW}"
    assert _count(db, "flows") >= 1
    assert _count(db, "edge") == 0
    assert store.meta_get(db, "source:kernel") == f"ok|{NOW}"


def test_missing_cloudflare_is_skipped_not_ok(tmp_path):
    db, col = _collector(tmp_path, cf=None)
    status = col.tick(NOW)
    assert status["cloudflare"] == "skipped"
    assert status["tunnel"] == "skipped"
    assert status["sockets"] == "ok"


def test_journal_failure_is_stale_and_never_ok(tmp_path):
    def sh(cmd, timeout=60, check=False, input_=None, env=None):
        if cmd[0] == "journalctl":
            return _cp("", rc=1)
        return fake_sh()(cmd)
    db, col = _collector(tmp_path, cf=None, sh=sh)
    status = col.tick(NOW)
    assert status["kernel"] == "stale:journalctl"
    assert store.meta_get(db, "source:kernel") == f"stale:journalctl|{NOW}"


def test_a_load_at_twice_the_cpus_doubles_the_next_socket_interval(tmp_path):
    db, col = _collector(tmp_path, cf=None, loadavg=lambda: (8.0, 0.0, 0.0), cpus=lambda: 4)
    first = col.tick(NOW)
    assert "sockets" in first
    assert "sockets" not in col.tick(NOW + 30)     # a 30 s interval would have run here
    assert "sockets" in col.tick(NOW + 60)         # doubled: 60 s


def test_the_stretch_is_capped_at_four_times_and_resets_when_load_drops(tmp_path):
    load = [8.0]
    db, col = _collector(tmp_path, cf=None, loadavg=lambda: (load[0], 0.0, 0.0), cpus=lambda: 4)
    col.tick(NOW)
    for i in range(1, 6):
        col.tick(NOW + i)
    assert col._stretch == 4
    load[0] = 0.5
    col.tick(NOW + 6)
    assert col._stretch == 1


def test_first_run_is_set_once_and_never_moves_on_a_later_start(tmp_path):
    db = store.open_db(tmp_path / "n.db")
    Collector(db, CFG, None, sh=fake_sh(), clock=lambda: 1000.0)
    assert store.meta_get(db, "first_run") == "1000"
    Collector(db, CFG, None, sh=fake_sh(), clock=lambda: 5000.0)
    assert store.meta_get(db, "first_run") == "1000"


def test_cloudflare_security_events_are_not_duplicated_by_overlapping_windows(tmp_path):
    db, col = _collector(tmp_path, cf=FakeCF(), sh=fake_sh())
    col.tick(NOW)
    col.tick(NOW + 300)   # the 900 s window still covers the event; it must not be stored twice
    assert _count(db, "events WHERE kind = 'cf_security'") == 1


def test_edge_windows_cover_the_configured_span(tmp_path):
    cf = FakeCF()
    db, col = _collector(tmp_path, cf=cf, sh=fake_sh())
    col.tick(NOW)
    assert cf.edge_calls == [("zone1", NOW - 900, NOW)]



def _plex_stamp(epoch: float) -> str:
    return time.strftime("%b %d, %Y %H:%M:%S", time.localtime(epoch)) + ".000"


def _plex_log(tmp_path, *epochs: float) -> None:
    logs = tmp_path / "plex" / "Logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = [f"{_plex_stamp(t)} [1] WARN - [Req#176eb82/CompanionProxy/Subscribe/poc_23919] "
             "CompanionProxy/Subscribe unsupported protocol 'searchOne'" for t in epochs]
    (logs / "Plex Media Server.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _exploit_count(db) -> int:
    return _count(db, "events WHERE kind = 'plex_exploit'")


def test_overlapping_plex_reads_store_each_exploit_once(tmp_path):
    # A line stamped a few seconds after the first tick is read by the first tick and would be read again by a cursor set to `now`.
    _plex_log(tmp_path, NOW - 60, NOW + 5)
    db, col = _collector(tmp_path, cf=None, sh=fake_sh())
    col.tick(NOW)
    assert _exploit_count(db) == 2
    col._due["plex"] = 0.0        # force the Plex read again over the same log
    col.tick(NOW + 60)
    assert _exploit_count(db) == 2


def test_plex_cursor_advances_past_the_newest_consumed_line(tmp_path):
    _plex_log(tmp_path, NOW - 60, NOW + 5)
    db, col = _collector(tmp_path, cf=None, sh=fake_sh())
    col.tick(NOW)
    cursor = float(store.meta_get(db, "plex:since"))
    assert NOW + 5 < cursor <= NOW + 5 + 0.01


def test_plex_cursor_is_unchanged_when_no_line_parses(tmp_path):
    logs = tmp_path / "plex" / "Logs"
    logs.mkdir(parents=True)
    (logs / "Plex Media Server.log").write_text("no timestamp here\n", encoding="utf-8")
    db, col = _collector(tmp_path, cf=None, sh=fake_sh())
    status = col.tick(NOW)
    assert status["plex"] == "ok"
    assert store.meta_get(db, "plex:since") is None


def test_a_missing_plex_log_is_stale_and_leaves_the_cursor_alone(tmp_path):
    db, col = _collector(tmp_path, cf=None, sh=fake_sh())
    store.meta_set(db, "plex:since", "123.0")
    status = col.tick(NOW)
    assert status["plex"] == "stale:missing"
    assert store.meta_get(db, "source:plex") == f"stale:missing|{NOW}"
    assert store.meta_get(db, "plex:since") == "123.0"


def test_a_quiet_codec_file_is_reported_once_across_ticks(tmp_path):
    # The codec was written after the last log line and no request explains it: one event, however many ticks follow.
    _plex_log(tmp_path, NOW - 100)
    codecs = tmp_path / "plex" / "Codecs"
    codecs.mkdir(parents=True)
    codec = codecs / "libexample_decoder.so"
    codec.write_bytes(b"")
    os.utime(codec, (NOW - 30, NOW - 30))
    db, col = _collector(tmp_path, cf=None, sh=fake_sh())
    col.tick(NOW)
    col._due["plex"] = 0.0
    col.tick(NOW + 60)
    col._due["plex"] = 0.0
    col.tick(NOW + 120)
    assert _count(db, "events WHERE kind = 'codec_unexplained'") == 1


def test_signins_source_records_sshd_sign_ins_and_the_plex_source_is_called_with_the_config(tmp_path, monkeypatch):
    seen_cfg = []
    monkeypatch.setattr(protect, "plex_session_ips", lambda cfg, **kw: seen_cfg.append(cfg) or ["198.51.100.4"])
    monkeypatch.setattr(protect, "_internet", lambda addr: True)
    db, col = _collector(tmp_path, cf=None, sh=fake_sh(journal_ssh=SSH_OK + "\n"))
    status = col.tick(NOW)
    assert status["signins"] == "ok"
    assert store.meta_get(db, "source:signins") == f"ok|{NOW}"
    assert protect.protection("198.51.100.9", db, entities.Topology.empty_stale(), NOW)[0] == "signin"
    assert protect.protection("198.51.100.4", db, entities.Topology.empty_stale(), NOW) == ("signin", "plex 2027-01-01")
    assert seen_cfg and seen_cfg[0]["plex_dir"] == str(tmp_path / "plex")


def test_signins_failure_is_stale_and_the_other_sources_still_run(tmp_path, monkeypatch):
    def boom(db, now, cfg, plex_sessions=None):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(protect, "record_signins", boom)
    db, col = _collector(tmp_path, cf=None, sh=fake_sh(journal_k=UFW + "\n"))
    status = col.tick(NOW)
    assert status["signins"] == "stale:RuntimeError"
    assert store.meta_get(db, "source:signins") == f"stale:RuntimeError|{NOW}"
    assert status["sockets"] == "ok"
    assert store.meta_get(db, "source:kernel") == f"ok|{NOW}"
