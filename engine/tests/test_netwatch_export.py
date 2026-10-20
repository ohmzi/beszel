"""Tests for homelab_maint/netwatch/export.py: network.json, the Network Watch data for the dashboard.

Documentation ranges only (2001:db8::/32 for talkers, 203.0.113.0/24 and 198.51.100.0/24 for internet peers, 192.168.40.0/24 for the
LAN) and example.test hosts. The store is a real SQLite file under tmp_path; the topology is a fake (no docker).
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import ipaddress
import json

import pytest

from homelab_maint import core, publish
from homelab_maint.netwatch import entities, export, store
from homelab_maint.netwatch.model import EdgeRow, Flow

NOW = 1798761600 + 1234          # 2027-01-01T00:20:34Z
HOUR = 1798761600                # the epoch hour that contains NOW
TOKEN = "FIXTURE-CF-TOKEN-7f3a9c2e51b84d60a9e1"
TOPO = entities.Topology(
    containers={},
    subnets=[ipaddress.ip_network("172.18.0.0/16")],
    compose={},
    listening=set(),
    host_ips=frozenset({ipaddress.ip_address("192.168.40.2")}),
)
V6_BASE = int(ipaddress.IPv6Address("2001:db8::"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "cloudflare.token").write_text(TOKEN + "\n")          # never read by the export: must never appear in its output
    monkeypatch.setattr(core, "STATE_DIR", state)
    monkeypatch.setattr(core, "CONF_DIR", conf)
    monkeypatch.setattr(entities, "load_topology", lambda *a, **k: TOPO)
    return state


@pytest.fixture
def db(env):
    conn = store.open_db()
    yield conn
    conn.close()


def _talker(i: int) -> str:
    return str(ipaddress.IPv6Address(V6_BASE + i + 1))


def _seed_edges(db, n: int) -> None:
    rows = [EdgeRow(HOUR, "example.test", f"h{i % 3}.example.test", _talker(i), "DE", 64500 + i, 200, "web", 1 + i % 997, 10)
            for i in range(n)]
    store.upsert_edge(db, rows)


def _build(db, now=NOW, **kw):
    return export.build_network({}, now, db=db, **kw)


def test_ten_thousand_talkers_stay_under_the_cap_with_fifty_listed(db):
    _seed_edges(db, 10_000)
    doc = _build(db)
    assert len(doc["talkers"]) == export.TALKERS_MAX == 50
    size = len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    assert size <= 512 * 1024
    counts = [t["requests"] for t in doc["talkers"]]
    assert counts == sorted(counts, reverse=True)                  # the top talkers, busiest first
    assert counts[0] == max(1 + i % 997 for i in range(10_000))


def test_talker_fields_hosts_threat_and_protection(db, env):
    store.upsert_edge(db, [EdgeRow(HOUR, "example.test", "a.example.test", "203.0.113.9", "NL", 64501, 200, "web", 7, 1),
                           EdgeRow(HOUR, "example.test", "b.example.test", "203.0.113.9", "NL", 64501, 404, "web", 3, 1)])
    store.meta_set(db, "first_run", str(HOUR))
    db.execute("INSERT INTO signins (ip, source, last_seen) VALUES (?, ?, ?)", ("198.51.100.4", "plex", NOW - 60))
    db.commit()
    threat_dir = env / "netwatch" / "threat"
    threat_dir.mkdir(parents=True)
    (threat_dir / "spamhaus_drop_v4.txt").write_text("203.0.113.0/24\n")
    doc = _build(db)
    (talker,) = doc["talkers"]
    assert talker == {"ip": "203.0.113.9", "country": "NL", "asn": 64501, "hosts": ["a.example.test", "b.example.test"],
                      "requests": 10, "threat": ["spamhaus_drop_v4"], "protection": None}
    assert doc["learning_until"] == HOUR + 7 * 86400


def test_protection_kind_is_hard_for_the_lan_and_signin_for_a_signed_in_address(db):
    store.upsert_edge(db, [EdgeRow(HOUR, "example.test", "a.example.test", "192.168.40.7", "CA", 1, 200, "web", 2, 1),
                           EdgeRow(HOUR, "example.test", "a.example.test", "198.51.100.4", "CA", 2, 200, "web", 1, 1)])
    db.execute("INSERT INTO signins (ip, source, last_seen) VALUES (?, ?, ?)", ("198.51.100.4", "plex", NOW - 60))
    db.commit()
    by_ip = {t["ip"]: t["protection"] for t in _build(db)["talkers"]}
    assert by_ip["192.168.40.7"] == "hard"
    assert by_ip["198.51.100.4"] == "signin"


def test_store_strings_go_through_the_publisher_redaction(db):
    store.upsert_edge(db, [EdgeRow(HOUR, "example.test", "https://u:hunter2@example.test/x", "203.0.113.5", "US", 1, 200, "web", 1, 1)])
    db.execute("INSERT INTO blocks (id, ip, target, zone, created, until, permanent, reason, cf_rule_id, removed) "
               "VALUES ('b1', '203.0.113.5', 'host', 'example.test', ?, ?, 0, 'password=hunter2 seen', NULL, NULL)", (NOW, NOW + 3600))
    db.commit()
    doc = _build(db)
    dumped = json.dumps(doc)
    assert "hunter2" not in dumped
    assert doc["blocks"][0]["reason"].startswith("password=") and "[redacted]" in doc["blocks"][0]["reason"]


def test_token_file_contents_never_appear(db, env):
    _seed_edges(db, 20)
    store.upsert_flows(db, [Flow(HOUR, "in", "203.0.113.9", "container:shop", 443, "tcp", 4, 1, 1)])
    doc = _build(db)
    assert TOKEN not in json.dumps(doc)
    assert TOKEN not in json.dumps(export.build_network({}, NOW, db=db, findings_path=env / "absent.json"))


def test_unavailable_without_a_database_and_the_file_is_not_created(env):
    doc = export.build_network({}, NOW)
    assert doc == {"available": False, "generated": NOW}
    assert not (env / "netwatch.db").exists()


def test_findings_are_read_from_the_file_and_missing_is_empty(db, env):
    assert _build(db)["findings"] == []
    path = env / "netwatch" / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated": NOW, "findings": [
        {"id": "0123456789abcdef", "rule": "plex_exploit", "severity": "crit", "src": "203.0.113.7", "target": "plex",
         "why": "probe", "first": NOW - 60, "last": NOW, "count": 2, "block": True, "zone": "", "stolen_account": False},
        {"id": "fedcba9876543210", "rule": "ssh_burst", "severity": "warn", "src": "198.51.100.23", "target": "host",
         "why": "5 failed", "first": NOW - 60, "last": NOW, "count": 5, "block": False, "zone": "", "stolen_account": False}]}))
    found = _build(db)["findings"]
    assert [f["id"] for f in found] == ["0123456789abcdef", "fedcba9876543210"]
    assert found[0]["severity"] == "crit" and found[0]["block"] is True and found[1]["count"] == 5
    assert _build(db, findings_path=path)["findings"] == found


def test_size_cap_drops_lesser_findings_and_keeps_the_crit_one(db, monkeypatch):
    findings = [{"id": f"{i:016x}", "rule": "ssh_burst", "severity": "warn", "src": "198.51.100.23", "target": "host", "why": "w" * 150,
                 "first": NOW, "last": NOW, "count": 5, "block": False, "zone": "", "stolen_account": False} for i in range(40)]
    findings.insert(25, dict(findings[0], id="cafe" * 4, severity="crit", rule="plex_exploit"))
    monkeypatch.setattr(export, "SIZE_CAP", 4000)
    doc = _build(db, findings_path=_write(db, findings))
    assert len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= 4000
    assert [f["id"] for f in doc["findings"] if f["severity"] == "crit"] == ["cafe" * 4]


def _write(db, findings):
    path = core.STATE_DIR / "netwatch" / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated": NOW, "findings": findings}))
    return path


def test_directions_count_24h_and_30d_and_services_count_peers(db):
    store.upsert_flows(db, [
        Flow(HOUR, "in", "198.51.100.23", "container:shop", 443, "tcp", 5, 1, 1),
        Flow(HOUR, "in", "198.51.100.24", "container:shop", 443, "tcp", 2, 1, 1),
        Flow(HOUR, "out", "container:shop", "203.0.113.40", 80, "tcp", 9, 1, 1),
        Flow(HOUR - 40 * 86400, "lan", "lan:192.168.40.9", "host", 22, "tcp", 100, 1, 1),
    ])
    doc = _build(db)
    assert doc["directions"]["24h"] == {"in": 7, "out": 9, "svc": 0, "lan": 0}
    assert doc["directions"]["30d"]["lan"] == 0                    # 40 days old: outside the 30-day window
    shop = next(s for s in doc["services"] if s["name"] == "container:shop")
    assert shop == {"name": "container:shop", "in": 2, "out": 1}


def test_publish_adapter_writes_network_json_with_its_own_cap(env, db):
    _seed_edges(db, 200)
    db.close()
    run = publish._Run({}, NOW, env)
    doc, trim = publish.build_network_json(run)
    assert doc is publish.WROTE and trim is None
    written = json.loads((env / "network.json").read_text())
    assert written["generated"] == NOW and len(written["talkers"]) == 50
