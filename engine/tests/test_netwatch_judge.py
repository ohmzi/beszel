"""Tests for homelab_maint/netwatch/judge.py: one per rule, the window, the baseline, protection and staleness.

Documentation ranges only (203.0.113.0/24 and 2001:db8::/32 for internet peers, 172.18.0.0/16 and 192.168.40.0/24 for containers and
LAN), and example.test hosts. The threat index is a real ThreatIndex built from a list file under tmp_path, never mocked.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import hashlib
import ipaddress

import pytest

from homelab_maint.netwatch import entities, judge, store
from homelab_maint.netwatch.model import EdgeRow, Flow, NetEvent
from homelab_maint.netwatch.threat import ThreatIndex

NOW = 1798761600 + 1234  # 2027-01-01T00:20:34Z
BUCKET = 1798761600      # the epoch hour that contains NOW
DAY = 86400
CFG = {"signin": {"hosts": ["app.example.test"]}}
TOPO = entities.Topology(
    containers={},
    subnets=[ipaddress.ip_network("172.18.0.0/16")],
    compose={"web": "shop", "api": "shop", "blog": "other"},
    listening=set(),
    host_ips=frozenset({ipaddress.ip_address("192.168.40.2")}),
)


@pytest.fixture
def db(tmp_path):
    return store.open_db(tmp_path / "n.db")


@pytest.fixture
def threat(tmp_path):
    directory = tmp_path / "threat"
    directory.mkdir()
    (directory / "spamhaus_drop_v4.txt").write_text("203.0.113.0/24\n2001:db8::/32\n")
    return ThreatIndex.load(directory, tmp_path / "netwatch-ioc.toml")


def _flow(db, direction, src, dst, dport, bucket=BUCKET, conns=1):
    store.upsert_flows(db, [Flow(bucket, direction, src, dst, dport, "tcp", conns, 0, 0)])


def _edge(db, status=200, path_class="other", count=1, client_ip="203.0.113.50", host="app.example.test", zone="zone1",
          bucket=BUCKET):
    store.upsert_edge(db, [EdgeRow(bucket=bucket, zone=zone, host=host, client_ip=client_ip, country="US", asn=64500,
                                   status=status, path_class=path_class, count=count, bytes=100)])


def _event(db, ts, kind, src, dst, dport, detail="x"):
    store.add_events(db, [NetEvent(ts=ts, kind=kind, src=src, dst=dst, dport=dport, detail=detail)])


def _learned(db, age=8 * DAY):
    store.meta_set(db, "first_run", str(NOW - age))


def _judge(db, threat, topo=TOPO, cfg=CFG, now=NOW):
    return judge.judge(db, topo, threat, now, cfg)


def _rule(findings, rule):
    return [f for f in findings if f.rule == rule]


# ---- plex_exploit and codec_unexplained -----------------------------------------------------------------------------------

def test_plex_exploit_infers_the_only_internet_peer(db, threat):
    _flow(db, "in", "203.0.113.9", "host", 32400)
    _event(db, NOW - 20, "plex_exploit", "unknown", "plex", 32400, "Req#7")
    [f] = _rule(_judge(db, threat), "plex_exploit")
    assert f.severity == "crit"
    assert f.src == "203.0.113.9"
    assert f.target == "plex"
    assert f.block == "host"
    assert "inferred: the only internet peer on 32400 that hour" in f.why


def test_plex_exploit_with_several_peers_is_unknown_and_not_blockable(db, threat):
    _flow(db, "in", "203.0.113.9", "host", 32400)
    _flow(db, "in", "203.0.113.10", "host", 32400)
    _flow(db, "in", "192.168.40.7", "host", 32400)   # LAN: not a candidate
    _event(db, NOW - 20, "plex_exploit", "unknown", "plex", 32400, "Req#7")
    [f] = _rule(_judge(db, threat), "plex_exploit")
    assert f.src == "unknown"
    assert f.block is None
    assert "203.0.113.9" in f.why and "203.0.113.10" in f.why
    assert "192.168.40.7" not in f.why


def test_codec_unexplained_is_crit_unknown_and_not_blockable(db, threat):
    _event(db, NOW - 60, "codec_unexplained", "unknown", "plex", 32400, "Codecs/libx.so")
    [f] = _rule(_judge(db, threat), "codec_unexplained")
    assert f.severity == "crit"
    assert f.src == "unknown"
    assert f.target == "Codecs/libx.so"
    assert f.block is None


# ---- threat_active --------------------------------------------------------------------------------------------------------

def test_threat_active_from_edge_blocks_at_cloudflare_with_the_zone(db, threat):
    _edge(db, status=404, path_class="other", count=25, zone="zone1")
    [f] = _rule(_judge(db, threat), "threat_active")
    assert f.severity == "warn"
    assert f.src == "203.0.113.50"
    assert f.target == "app.example.test"
    assert f.block == "cloudflare"
    assert f.zone == "zone1"
    assert "spamhaus_drop_v4" in f.why


def test_threat_active_from_inbound_flow_blocks_on_the_host(db, threat):
    _flow(db, "in", "203.0.113.50", "host", 22, conns=3)
    [f] = _rule(_judge(db, threat), "threat_active")
    assert f.src == "203.0.113.50"
    assert f.target == "host"
    assert f.block == "host"
    assert f.zone is None
    assert "spamhaus_drop_v4" in f.why


def test_threat_active_from_a_2xx_on_a_non_other_path_is_a_finding(db, threat):
    _edge(db, status=200, path_class="auth_api", count=1)
    [f] = _rule(_judge(db, threat), "threat_active")
    assert f.block == "cloudflare"


def test_scanning_below_the_bar_is_counted_not_flagged(db, threat):
    _edge(db, status=404, path_class="other", count=3)
    _edge(db, status=200, path_class="other", count=1, client_ip="203.0.113.51")
    assert _judge(db, threat) == []


def test_scan_only_no_findings(db, threat):
    rows = [
        EdgeRow(bucket=BUCKET, zone="zone1", host="app.example.test", client_ip=f"2001:db8::{i:x}", country="US", asn=64500,
                status=404, path_class="other", count=1, bytes=100)
        for i in range(1, 5001)
    ]
    store.upsert_edge(db, rows)
    assert threat.lookup("2001:db8::1") == "spamhaus_drop_v4"
    assert _judge(db, threat) == []


def test_events_outside_the_window_do_not_count(db, threat):
    _event(db, NOW - 3000, "plex_exploit", "unknown", "plex", 32400, "Req#1")
    _event(db, NOW - 3000, "codec_unexplained", "unknown", "plex", 32400, "Codecs/old.so")
    assert _judge(db, threat) == []


# ---- auth_burst -----------------------------------------------------------------------------------------------------------

def test_auth_burst_sshd_five_failures_in_ten_minutes(db, threat):
    for i in range(5):
        _event(db, NOW - 100 - i, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    [f] = _rule(_judge(db, threat), "auth_burst")
    assert f.severity == "warn"
    assert f.src == "203.0.113.9"
    assert f.target == "sshd"
    assert f.block == "host"


def test_auth_burst_sshd_four_failures_is_not_a_burst(db, threat):
    for i in range(4):
        _event(db, NOW - 100 - i, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    assert _rule(_judge(db, threat), "auth_burst") == []


def test_auth_burst_sshd_only_counts_the_last_ten_minutes(db, threat):
    for i in range(4):
        _event(db, NOW - 100 - i, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    _event(db, NOW - 700, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    assert _rule(_judge(db, threat), "auth_burst") == []


def test_auth_burst_edge_ten_refusals_block_at_cloudflare(db, threat):
    _edge(db, status=401, path_class="auth_login", count=10)
    [f] = _rule(_judge(db, threat), "auth_burst")
    assert f.src == "203.0.113.50"
    assert f.target == "app.example.test"
    assert f.block == "cloudflare"
    assert f.zone == "zone1"


def test_auth_burst_edge_nine_refusals_is_not_a_burst(db, threat):
    _edge(db, status=403, path_class="auth_login", count=9)
    assert _rule(_judge(db, threat), "auth_burst") == []


# ---- line_not_meant -------------------------------------------------------------------------------------------------------

def test_line_not_meant_is_a_container_hitting_a_host_port(db, threat):
    _event(db, NOW - 60, "fw_block", "172.18.0.5", "192.168.40.2", 8080)
    _event(db, NOW - 60, "fw_block", "203.0.113.9", "192.168.40.2", 8080)   # internet: not a pinhole violation
    [f] = _rule(_judge(db, threat), "line_not_meant")
    assert f.severity == "warn"
    assert f.src == "172.18.0.5"
    assert f.target == "host:8080"
    assert f.block is None
    assert "(never blocked: 172.18.0.0/16)" in f.why


# ---- new_peer -------------------------------------------------------------------------------------------------------------

def test_new_peer_silent_during_learning(db, threat):
    _learned(db, age=DAY)
    _flow(db, "out", "container:web", "203.0.113.9", 443)
    _flow(db, "lan", "lan:192.168.40.7", "host", 2222)
    assert _rule(_judge(db, threat), "new_peer") == []
    key = db.execute("SELECT entity, peer_key, first_seen FROM baseline WHERE entity = 'container:web'").fetchone()
    assert key == ("container:web", "out:203.0.113.0/24:443", NOW)


def test_new_out_destination_after_learning_is_a_finding(db, threat):
    _learned(db)
    _flow(db, "out", "container:web", "203.0.113.9", 443)
    [f] = _rule(_judge(db, threat), "new_peer")
    assert f.severity == "warn"
    assert f.src == "container:web"
    assert f.target == "out:203.0.113.0/24:443"
    assert f.block is None


def test_known_peer_is_not_new(db, threat):
    _learned(db)
    db.execute("INSERT INTO baseline VALUES ('container:web', 'out:203.0.113.0/24:443', ?, ?)", (NOW - 3 * DAY, NOW - DAY))
    _flow(db, "out", "container:web", "203.0.113.9", 443)
    assert _rule(_judge(db, threat), "new_peer") == []


def test_same_project_svc_never_flags(db, threat):
    _learned(db)
    _flow(db, "svc", "container:web", "container:api", 5432)
    assert _rule(_judge(db, threat), "new_peer") == []
    assert db.execute("SELECT COUNT(*) FROM baseline").fetchone()[0] == 0


def test_cross_project_svc_flags(db, threat):
    _learned(db)
    _flow(db, "svc", "container:web", "container:blog", 6379)
    [f] = _rule(_judge(db, threat), "new_peer")
    assert f.src == "container:web"
    assert f.target == "svc:container:blog:6379"


def test_lan_inbound_new_port_flags_but_outbound_does_not(db, threat):
    _learned(db)
    _flow(db, "lan", "lan:192.168.40.7", "host", 2222)
    _flow(db, "lan", "host", "lan:192.168.40.7", 9100)
    [f] = _rule(_judge(db, threat), "new_peer")
    assert f.src == "lan:192.168.40.7"
    assert f.target == "lan:host:2222"


def test_missing_first_run_is_learning_and_is_not_set(db, threat):
    _flow(db, "out", "container:web", "203.0.113.9", 443)
    assert _rule(_judge(db, threat), "new_peer") == []
    assert store.meta_get(db, "first_run") is None
    assert db.execute("SELECT COUNT(*) FROM baseline").fetchone()[0] == 1


# ---- protection -----------------------------------------------------------------------------------------------------------

def test_signin_source_marks_stolen_account_and_keeps_the_block(db, threat):
    db.execute("INSERT INTO signins VALUES ('203.0.113.9', 'sshd', ?)", (NOW - 100,))
    _flow(db, "in", "203.0.113.9", "host", 32400)
    _event(db, NOW - 20, "plex_exploit", "unknown", "plex", 32400, "Req#7")
    [f] = _rule(_judge(db, threat), "plex_exploit")
    assert f.stolen_account == "sshd 2027-01-01"
    assert f.block == "host"


def test_hard_range_source_is_never_blocked(db, threat):
    for i in range(5):
        _event(db, NOW - 100 - i, "auth_fail", "192.168.40.7", "host", 22, "sshd")
    [f] = _rule(_judge(db, threat), "auth_burst")
    assert f.block is None
    assert "(never blocked: 192.168.40.0/24)" in f.why


# ---- ids and staleness ----------------------------------------------------------------------------------------------------

def test_ids_stable(db, threat):
    for i in range(5):
        _event(db, NOW - 100 - i, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    first = _judge(db, threat)
    second = _judge(db, threat)
    [f] = _rule(first, "auth_burst")
    assert f.id == hashlib.sha1("auth_burst|203.0.113.9|sshd".encode()).hexdigest()[:16]
    assert len(f.id) == 16
    assert [x.id for x in first] == [x.id for x in second]


def test_stale_topology_has_no_blocks(db, threat):
    _learned(db)
    _flow(db, "in", "203.0.113.9", "host", 32400)
    _event(db, NOW - 20, "plex_exploit", "unknown", "plex", 32400, "Req#7")
    for i in range(5):
        _event(db, NOW - 100 - i, "auth_fail", "203.0.113.9", "host", 22, "sshd")
    _edge(db, status=404, path_class="other", count=25, client_ip="203.0.113.50")
    _event(db, NOW - 60, "fw_block", "172.18.0.5", "192.168.40.2", 8080)
    _flow(db, "out", "container:web", "198.51.100.9", 443)

    findings = _judge(db, threat, topo=entities.Topology.empty_stale())
    assert findings
    assert all(f.block is None for f in findings)
    assert _rule(findings, "line_not_meant") == []
    assert _rule(findings, "new_peer") == []
    assert db.execute("SELECT COUNT(*) FROM baseline").fetchone()[0] == 0
