"""Tests for homelab_maint/netwatch/threat.py: list parsing, the atomic refresh with injected fetch, the lookup order and staleness.

Nothing downloads: every refresh gets a fake fetch. Documentation ranges only (203.0.113.0/24, 2001:db8::/32, example.test).
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import json
import os
import urllib.error

import pytest

from homelab_maint.netwatch import threat
from homelab_maint.netwatch.threat import ThreatIndex

NOW = 1798761600.0  # 2027-01-01T00:00:00Z
DAY = 86400


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeFetch:
    """Serves a body per URL; a value that is an exception instance is raised instead. Records every request it saw."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, request.get_header("User-agent"), timeout))
        value = self.responses[request.full_url]
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


def _all_good() -> dict[str, bytes]:
    drop_v4 = (json.dumps({"cidr": "203.0.113.0/24", "sbl": "SBL1"}) + "\n"
               + json.dumps({"cidr": "198.51.100.0/24", "sbl": "SBL2"}) + "\n"
               + json.dumps({"type": "metadata", "timestamp": 1}) + "\n")
    return {
        threat.LISTS["spamhaus_drop_v4"][0]: drop_v4.encode(),
        threat.LISTS["spamhaus_drop_v6"][0]: (json.dumps({"cidr": "2001:db8::/32"}) + "\n").encode(),
        threat.LISTS["firehol_level1"][0]: b"# FireHOL level 1\n192.0.2.1\n198.18.0.0/15 # bench\n\n",
        threat.LISTS["feodo"][0]: b"# Feodo\n203.0.113.77\n",
    }


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "threat"
    monkeypatch.setattr(threat, "threat_dir", lambda: d)
    return d


def _write(d, name, text):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.txt").write_text(text)
    return d / f"{name}.txt"


def test_v4_and_v6_cidr_membership(tmp_path, tdir):
    _write(tdir, "spam", "203.0.113.0/24\n2001:db8::/32\n")
    idx = ThreatIndex.load(tdir, tmp_path / "missing.toml")
    assert idx.lookup("203.0.113.9") == "spam"
    assert idx.lookup("203.0.114.1") is None
    assert idx.lookup("2001:db8::1") == "spam"
    assert idx.lookup("2001:db9::1") is None
    assert idx.lookup("::ffff:198.51.100.1") is None


def test_a_bare_address_in_a_list_matches_only_itself(tmp_path, tdir):
    _write(tdir, "feodo", "203.0.113.77\n")
    idx = ThreatIndex.load(tdir, tmp_path / "missing.toml")
    assert idx.lookup("203.0.113.77") == "feodo"
    assert idx.lookup("203.0.113.78") is None


def test_an_ioc_entry_wins_with_ioc(tmp_path, tdir):
    _write(tdir, "firehol_level1", "203.0.113.0/24\n")
    ioc = tmp_path / "netwatch-ioc.toml"
    ioc.write_text('ips = ["203.0.113.5"]\ndomains = ["Bad.Example.TEST."]\n')
    idx = ThreatIndex.load(tdir, ioc)
    assert idx.lookup("203.0.113.5") == "ioc"
    assert idx.lookup("203.0.113.6") == "firehol_level1"
    assert idx.domains == {"bad.example.test"}


def test_lists_are_checked_in_name_order_after_the_ioc(tmp_path, tdir):
    _write(tdir, "zeta", "203.0.113.0/24\n")
    _write(tdir, "alpha", "203.0.113.0/25\n")
    idx = ThreatIndex.load(tdir, tmp_path / "missing.toml")
    assert idx.lookup("203.0.113.9") == "alpha"


def test_an_empty_or_missing_directory_matches_nothing(tmp_path):
    idx = ThreatIndex.load(tmp_path / "nowhere", tmp_path / "missing.toml")
    assert idx.lookup("203.0.113.9") is None
    assert idx.domains == set()
    assert idx.stale_lists(NOW) == []


def test_refresh_writes_each_list_as_plain_cidrs(tdir):
    fetch = FakeFetch(_all_good())
    status = threat.refresh(fetch=fetch, now=NOW)
    assert status == {name: "ok" for name in threat.LISTS}
    assert (tdir / "spamhaus_drop_v4.txt").read_text() == "203.0.113.0/24\n198.51.100.0/24\n"
    assert (tdir / "spamhaus_drop_v6.txt").read_text() == "2001:db8::/32\n"
    assert (tdir / "firehol_level1.txt").read_text() == "192.0.2.1/32\n198.18.0.0/15\n"
    assert (tdir / "feodo.txt").read_text() == "203.0.113.77/32\n"
    assert not list(tdir.glob(".*.tmp")), "the temp file is replaced, never left behind"


def test_refresh_sends_the_user_agent_and_a_30_second_timeout(tdir):
    fetch = FakeFetch(_all_good())
    threat.refresh(fetch=fetch, now=NOW)
    assert {agent for _, agent, _ in fetch.calls} == {"homelab-maint-netwatch/1"}
    assert {timeout for _, _, timeout in fetch.calls} == {30}


def test_a_failed_fetch_keeps_the_previous_file(tdir):
    _write(tdir, "feodo", "198.51.100.1/32\n")
    good = _all_good()
    responses = dict(good)
    responses[threat.LISTS["feodo"][0]] = urllib.error.URLError("no route")
    status = threat.refresh(fetch=FakeFetch(responses), now=NOW)
    assert status["feodo"] == "kept:fetch:URLError"
    assert (tdir / "feodo.txt").read_text() == "198.51.100.1/32\n"
    assert status["spamhaus_drop_v4"] == "ok"


def test_an_unparseable_or_empty_body_keeps_the_previous_file(tdir):
    _write(tdir, "firehol_level1", "192.0.2.0/24\n")
    responses = _all_good()
    responses[threat.LISTS["firehol_level1"][0]] = b"<html>error page</html>"
    responses[threat.LISTS["feodo"][0]] = b"# only a comment\n\n"
    status = threat.refresh(fetch=FakeFetch(responses), now=NOW)
    assert status["firehol_level1"] == "kept:parse:ValueError"
    assert status["feodo"] == "kept:empty"
    assert (tdir / "firehol_level1.txt").read_text() == "192.0.2.0/24\n"
    assert not (tdir / "feodo.txt").exists()


def test_a_list_with_no_entries_is_kept_even_when_it_parses(tdir):
    responses = _all_good()
    responses[threat.LISTS["spamhaus_drop_v4"][0]] = json.dumps({"type": "metadata"}).encode()
    status = threat.refresh(fetch=FakeFetch(responses), now=NOW)
    assert status["spamhaus_drop_v4"] == "kept:empty"
    assert not (tdir / "spamhaus_drop_v4.txt").exists()


def test_a_file_older_than_three_days_is_stale(tmp_path, tdir):
    fresh = _write(tdir, "feodo", "203.0.113.77/32\n")
    old = _write(tdir, "firehol_level1", "192.0.2.0/24\n")
    os.utime(old, (NOW - 4 * DAY, NOW - 4 * DAY))
    os.utime(fresh, (NOW - 2 * DAY, NOW - 2 * DAY))
    idx = ThreatIndex.load(tdir, tmp_path / "missing.toml")
    assert idx.stale_lists(NOW) == ["firehol_level1"]


def test_a_refresh_stamps_its_files_with_the_clock_it_was_given(tdir):
    threat.refresh(fetch=FakeFetch(_all_good()), now=NOW - 4 * DAY)
    idx_stale = ThreatIndex.load(tdir, tdir / "none.toml").stale_lists(NOW)
    assert sorted(idx_stale) == sorted(threat.LISTS)


def test_main_refresh_prints_one_line_per_list_and_exits_zero(tdir, monkeypatch, capsys):
    monkeypatch.setattr(threat, "refresh", lambda: {"feodo": "kept:fetch:URLError", "spamhaus_drop_v4": "ok"})
    assert threat.main(["refresh"]) == 0
    assert capsys.readouterr().out == "feodo: kept:fetch:URLError\nspamhaus_drop_v4: ok\n"


def test_main_lookup_names_the_list_or_none(tmp_path, tdir, monkeypatch, capsys):
    _write(tdir, "feodo", "203.0.113.77/32\n")
    monkeypatch.setattr(threat, "ioc_path", lambda: tmp_path / "missing.toml")
    assert threat.main(["lookup", "203.0.113.77"]) == 0
    assert capsys.readouterr().out == "feodo\n"
    assert threat.main(["lookup", "198.51.100.1"]) == 0
    assert capsys.readouterr().out == "none\n"
    assert threat.main(["lookup", "not-an-ip"]) == 2
