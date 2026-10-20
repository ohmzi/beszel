"""Tests for homelab_maint/netwatch/protect.py: hard ranges, sign-ins from edge, sshd and Plex, and the Plex session reader.

Documentation and private ranges only. The Plex reader is given a fake http and a Preferences.xml under tmp_path, never the real one.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import io
import ipaddress
import urllib.error

import pytest

from homelab_maint.netwatch import entities, protect, store
from homelab_maint.netwatch.model import EdgeRow, NetEvent

NOW = 1798761600 + 1234  # 2027-01-01T00:20:34Z
BUCKET = 1798761600      # the epoch hour that contains NOW
DAY = 86400
CFG = {"signin": {"hosts": ["app.example.test"]}}
TOPO = entities.Topology(containers={}, subnets=[ipaddress.ip_network("172.18.0.0/16")], compose={}, listening=set())


@pytest.fixture
def db(tmp_path):
    return store.open_db(tmp_path / "n.db")


def _edge(db, *rows):
    store.upsert_edge(db, list(rows))


def _edge_row(status=200, path_class="auth_api", host="app.example.test", client_ip="198.51.100.20", bucket=BUCKET):
    return EdgeRow(bucket=bucket, zone="zone1", host=host, client_ip=client_ip, country="US", asn=64500,
                   status=status, path_class=path_class, count=1, bytes=100)


def _auth_ok(db, ts, src="198.51.100.9"):
    store.add_events(db, [NetEvent(ts=ts, kind="auth_ok", src=src, dst="host", dport=22, detail="ssh")])


# ---- hard ranges ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ip, cidr", [
    ("192.168.40.7", "192.168.40.0/24"),
    ("100.100.1.1", "100.64.0.0/10"),
    ("172.18.0.5", "172.18.0.0/16"),
    ("127.0.0.1", "127.0.0.0/8"),
    ("::1", "::1/128"),
    ("::ffff:192.168.40.7", "192.168.40.0/24"),
])
def test_lan_tailscale_docker_and_loopback_are_hard(db, ip, cidr):
    assert protect.protection(ip, db, TOPO, NOW, CFG) == ("hard", cidr)


def test_a_stale_topology_treats_private_ranges_as_hard(db):
    assert protect.protection("10.1.2.3", db, entities.Topology.empty_stale(), NOW, CFG) == ("hard", "10.0.0.0/8")


def test_a_hard_range_wins_over_a_sign_in(db):
    _auth_ok(db, NOW - 100, src="192.168.40.7")
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("192.168.40.7", db, TOPO, NOW, CFG) == ("hard", "192.168.40.0/24")


def test_an_unknown_public_address_is_not_protected(db):
    assert protect.protection("198.51.100.77", db, TOPO, NOW, CFG) is None


# ---- edge sign-ins -------------------------------------------------------------------------------------------------------

def test_an_edge_200_on_an_auth_path_signs_in_for_30_days(db):
    _edge(db, _edge_row(status=200))
    assert protect.record_signins(db, NOW, CFG) == 1
    assert protect.protection("198.51.100.20", db, TOPO, NOW, CFG) == ("signin", "edge:app.example.test 2027-01-01")
    assert protect.protection("198.51.100.20", db, TOPO, NOW + 29 * DAY, CFG) is not None
    assert protect.protection("198.51.100.20", db, TOPO, NOW + 31 * DAY, CFG) is None


def test_an_edge_401_on_the_same_path_is_no_sign_in(db):
    _edge(db, _edge_row(status=401))
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("198.51.100.20", db, TOPO, NOW, CFG) is None


def test_an_edge_200_on_another_host_or_path_class_is_no_sign_in(db):
    _edge(db, _edge_row(host="other.example.test"), _edge_row(path_class="page", client_ip="198.51.100.21"))
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("198.51.100.20", db, TOPO, NOW, CFG) is None
    assert protect.protection("198.51.100.21", db, TOPO, NOW, CFG) is None


def test_no_listed_hosts_means_no_edge_sign_ins(db):
    _edge(db, _edge_row())
    assert protect.record_signins(db, NOW, {"signin": {"hosts": []}}) == 0


# ---- sshd sign-ins -------------------------------------------------------------------------------------------------------

def test_an_sshd_auth_ok_is_a_sign_in_dated_by_its_line(db):
    _auth_ok(db, NOW - 100)
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("198.51.100.9", db, TOPO, NOW, CFG) == ("signin", "sshd 2027-01-01")


def test_an_ipv4_mapped_address_matches_its_plain_form(db):
    _auth_ok(db, NOW - 100, src="::ffff:198.51.100.9")
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("::ffff:198.51.100.9", db, TOPO, NOW, CFG) == ("signin", "sshd 2027-01-01")
    assert protect.protection("198.51.100.9", db, TOPO, NOW, CFG) == ("signin", "sshd 2027-01-01")


def test_the_most_recent_sign_in_is_the_one_reported(db):
    # The edge row is an hour that ends after NOW, so its last_seen is capped at NOW: later than the sshd line at NOW - 100.
    _edge(db, _edge_row(client_ip="198.51.100.9", bucket=BUCKET))
    _auth_ok(db, NOW - 100)
    protect.record_signins(db, NOW, CFG)
    assert protect.protection("198.51.100.9", db, TOPO, NOW, CFG) == ("signin", "edge:app.example.test 2027-01-01")


def test_a_second_read_only_takes_events_after_the_mark(db):
    _auth_ok(db, NOW - 100)
    assert protect.record_signins(db, NOW, CFG) == 1
    assert protect.record_signins(db, NOW + 60, CFG) == 0
    assert store.meta_get(db, "signins:since") == repr(NOW + 60)


def test_the_upsert_keeps_the_latest_last_seen_per_address_and_source(db):
    _auth_ok(db, NOW - 200)
    protect.record_signins(db, NOW, CFG)
    _auth_ok(db, NOW + 10)
    protect.record_signins(db, NOW + 100, CFG)
    rows = db.execute("SELECT last_seen FROM signins WHERE ip = '198.51.100.9' AND source = 'sshd'").fetchall()
    assert rows == [(NOW + 10,)]


# ---- Plex sign-ins -------------------------------------------------------------------------------------------------------

def test_a_plex_internet_player_is_a_sign_in(db, monkeypatch):
    # 203.0.113.4 is a documentation address, which is not "global"; the seam stands in for an internet peer.
    monkeypatch.setattr(protect, "_internet", lambda addr: not addr.is_loopback)
    sessions = lambda: ["203.0.113.4", "127.0.0.1"]
    assert protect.record_signins(db, NOW, CFG, plex_sessions=sessions) == 1
    assert protect.protection("203.0.113.4", db, TOPO, NOW, CFG) == ("signin", "plex 2027-01-01")
    assert protect.protection("127.0.0.1", db, TOPO, NOW, CFG) == ("hard", "127.0.0.0/8")


def test_plex_players_that_are_not_internet_peers_are_not_sign_ins(db):
    sessions = lambda: ["203.0.113.4", "192.168.40.9", "not-an-address"]
    assert protect.record_signins(db, NOW, CFG, plex_sessions=sessions) == 0
    assert protect.protection("203.0.113.4", db, TOPO, NOW, CFG) is None


# ---- Plex session reader -------------------------------------------------------------------------------------------------

TOKEN = "tok-test-value"
SESSIONS_XML = b'<MediaContainer size="2"><Video><Player address="203.0.113.4"/></Video><Player address="198.51.100.2"/></MediaContainer>'


def _prefs(tmp_path, token=TOKEN):
    path = tmp_path / "Preferences.xml"
    path.write_text(f'<?xml version="1.0"?>\n<Preferences PlexOnlineToken="{token}" />\n', encoding="utf-8")
    return path


class FakeHttp:
    def __init__(self, body=SESSIONS_XML, raise_=None):
        self.body = body
        self.raise_ = raise_
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.raise_:
            raise self.raise_
        return io.BytesIO(self.body)


def test_plex_session_ips_reads_the_player_addresses_with_the_token_in_a_header(tmp_path):
    http = FakeHttp()
    ips = protect.plex_session_ips({}, http=http, prefs_path=_prefs(tmp_path))
    assert ips == ["203.0.113.4", "198.51.100.2"]
    req, _timeout = http.requests[0]
    assert req.full_url == "http://127.0.0.1:32400/status/sessions"
    assert req.get_header("X-plex-token") == TOKEN
    assert TOKEN not in req.full_url


def test_plex_session_ips_is_empty_when_the_token_is_absent(tmp_path):
    http = FakeHttp()
    assert protect.plex_session_ips({}, http=http, prefs_path=_prefs(tmp_path, token="")) == []
    assert http.requests == []


@pytest.mark.parametrize("make", [
    lambda tmp_path: tmp_path / "missing" / "Preferences.xml",
    lambda tmp_path: _bad_xml(tmp_path),
])
def test_plex_session_ips_is_empty_when_preferences_cannot_be_read(tmp_path, make):
    http = FakeHttp()
    assert protect.plex_session_ips({}, http=http, prefs_path=make(tmp_path)) == []
    assert http.requests == []


def _bad_xml(tmp_path):
    path = tmp_path / "Preferences.xml"
    path.write_text("<Preferences PlexOnlineToken=", encoding="utf-8")
    return path


def test_plex_session_ips_is_empty_on_a_network_failure_and_never_echoes_the_token(tmp_path):
    http = FakeHttp(raise_=urllib.error.URLError(f"refused {TOKEN}"))
    assert protect.plex_session_ips({}, http=http, prefs_path=_prefs(tmp_path)) == []


def test_plex_session_ips_is_empty_on_an_unparseable_reply(tmp_path):
    http = FakeHttp(body=b"<MediaContainer")
    assert protect.plex_session_ips({}, http=http, prefs_path=_prefs(tmp_path)) == []


def test_plex_session_ips_reads_the_prefs_under_plex_dir_from_config(tmp_path):
    (tmp_path / "plexdir").mkdir()
    _prefs(tmp_path / "plexdir")
    http = FakeHttp()
    assert protect.plex_session_ips({"plex_dir": str(tmp_path / "plexdir")}, http=http) == ["203.0.113.4", "198.51.100.2"]
