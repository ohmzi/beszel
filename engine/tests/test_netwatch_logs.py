"""Tests for homelab_maint/netwatch/logs.py: UFW and sshd lines turned into NetEvents, journalctl driven through a fake sh."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import subprocess
from datetime import datetime

from homelab_maint.netwatch.logs import parse_auth, parse_ufw, read_journal
from homelab_maint.netwatch.store import meta_get, meta_set, open_db

UFW_V4 = (
    "2026-10-17T23:42:55.795927-04:00 myhost kernel: [UFW BLOCK] IN=eno1 OUT= MAC=aa:bb:cc:dd:ee:ff "
    "SRC=203.0.113.98 DST=192.168.40.20 LEN=40 TOS=0x00 PREC=0x00 TTL=50 ID=0 DF PROTO=TCP SPT=443 "
    "DPT=51000 WINDOW=0 RES=0x00 RST URGP=0"
)
UFW_V6 = (
    "2026-10-17T23:43:01.000001-04:00 myhost kernel: [UFW BLOCK] IN=eno1 OUT= MAC=aa:bb:cc:dd:ee:ff "
    "SRC=2001:db8::98 DST=2001:db8:0:40::20 LEN=60 TC=0 HOPLIMIT=50 FLOWLBL=0 PROTO=UDP SPT=5353 DPT=53 LEN=40"
)
UFW_ICMP = (
    "2026-10-17T23:44:00.000000-04:00 myhost kernel: [UFW BLOCK] IN=eno1 OUT= MAC= SRC=203.0.113.99 "
    "DST=192.168.40.20 LEN=84 TOS=0x00 PREC=0x00 TTL=50 ID=0 PROTO=ICMP TYPE=8 CODE=0"
)
NOT_UFW = "2026-10-17T23:45:00.000000-04:00 myhost kernel: usb 1-1: new high-speed USB device"

AUTH = [
    "Oct 19 08:12:01 myhost sshd[1234]: Failed password for invalid user admin from 203.0.113.5 port 52314 ssh2",
    "Oct 19 08:12:03 myhost sshd[1235]: Invalid user test from 203.0.113.6 port 40000",
    "Oct 19 08:12:09 myhost sshd[1236]: Accepted publickey for ohmz from 192.168.40.11 port 50000 ssh2",
    "Oct 19 08:12:10 myhost sshd[1237]: Failed password for ohmz from 203.0.113.7 port 52000 ssh2",
    "Oct 19 08:12:11 myhost sshd[1238]: pam_unix(sshd:session): session opened for user ohmz",
]


def test_ufw_v4_line_gives_block_event():
    (ev,) = parse_ufw([UFW_V4])
    assert ev.kind == "fw_block"
    assert ev.src == "203.0.113.98"
    assert ev.dst == "192.168.40.20"
    assert ev.dport == 51000
    assert ev.detail == "PROTO=TCP IN=eno1"
    assert ev.ts == datetime.fromisoformat("2026-10-17T23:42:55.795927-04:00").timestamp()


def test_ufw_v6_line_gives_block_event():
    (ev,) = parse_ufw([UFW_V6])
    assert (ev.src, ev.dst, ev.dport) == ("2001:db8::98", "2001:db8:0:40::20", 53)
    assert ev.detail == "PROTO=UDP IN=eno1"


def test_ufw_icmp_without_port_has_dport_zero_and_skips_other_kernel_lines():
    events = parse_ufw([UFW_ICMP, NOT_UFW, ""])
    assert len(events) == 1
    assert events[0].dport == 0
    assert events[0].detail == "PROTO=ICMP IN=eno1"


def test_auth_fixture_gives_three_failures_and_one_success():
    events = parse_auth(AUTH)
    fails = [e for e in events if e.kind == "auth_fail"]
    oks = [e for e in events if e.kind == "auth_ok"]
    assert len(fails) == 3
    assert len(oks) == 1
    assert {e.src for e in fails} == {"203.0.113.5", "203.0.113.6", "203.0.113.7"}
    assert oks[0].src == "192.168.40.11"
    for e in events:
        assert (e.dst, e.dport, e.detail) == ("host", 22, "sshd")


def test_auth_accepts_journal_short_iso_lines_too():
    line = "2026-10-19T08:12:01-04:00 myhost sshd[1234]: Invalid user test from 203.0.113.6 port 40000"
    (ev,) = parse_auth([line])
    assert ev.kind == "auth_fail"
    assert ev.src == "203.0.113.6"


class _FakeSh:
    def __init__(self, rc=0, stdout=""):
        self.rc, self.stdout, self.calls = rc, stdout, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def _db(tmp_path):
    return open_db(tmp_path / "netwatch.db")


def test_read_journal_first_run_uses_since_and_stores_cursor(tmp_path):
    db = _db(tmp_path)
    sh = _FakeSh(stdout=f"{UFW_V4}\n-- cursor: s=abc;i=1\n")
    lines, status = read_journal("-k", "cursor_kernel", db, sh=sh)
    assert lines == [UFW_V4]
    assert status == "ok"
    assert sh.calls == [["journalctl", "-o", "short-iso", "--no-pager", "--show-cursor", "-k", "--since", "-10min"]]
    assert meta_get(db, "cursor_kernel") == "s=abc;i=1"


def test_read_journal_with_unit_and_saved_cursor_uses_after_cursor(tmp_path):
    db = _db(tmp_path)
    meta_set(db, "cursor_ssh", "s=old;i=9")
    sh = _FakeSh(stdout="-- cursor: s=new;i=10\n")
    assert read_journal("ssh.service", "cursor_ssh", db, sh=sh) == ([], "ok")
    assert sh.calls == [[
        "journalctl", "-o", "short-iso", "--no-pager", "--show-cursor",
        "-u", "ssh.service", "--after-cursor", "s=old;i=9",
    ]]
    assert meta_get(db, "cursor_ssh") == "s=new;i=10"


def test_read_journal_failure_returns_nothing_and_keeps_cursor(tmp_path):
    db = _db(tmp_path)
    meta_set(db, "cursor_kernel", "s=keep;i=5")
    assert read_journal("-k", "cursor_kernel", db, sh=_FakeSh(rc=1, stdout="")) == ([], "stale:journalctl")
    assert meta_get(db, "cursor_kernel") == "s=keep;i=5"


def test_read_journal_without_cursor_line_keeps_cursor(tmp_path):
    db = _db(tmp_path)
    meta_set(db, "cursor_kernel", "s=keep;i=5")
    assert read_journal("-k", "cursor_kernel", db, sh=_FakeSh(stdout="-- No entries --\n")) == ([], "ok")
    assert meta_get(db, "cursor_kernel") == "s=keep;i=5"

