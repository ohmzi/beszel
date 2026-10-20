"""Tests for homelab_maint/tasks/net_watch.py: the C0 verdict over the netwatch store.

Documentation ranges only (203.0.113.0/24 for internet peers, 172.18.0.0/16 and 192.168.40.0/24 for containers and LAN) and example.test
hosts. The threat index is a real ThreatIndex built from a list file under tmp_path, never mocked. The topology is a fake (no docker).
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import ipaddress
import json
import os
import stat

import pytest

from homelab_maint import core, cli
from homelab_maint.netwatch import entities, store
from homelab_maint.netwatch.model import EdgeRow, Flow, NetEvent
from homelab_maint.netwatch.threat import ThreatIndex

NOW = 1798761600 + 1234  # 2027-01-01T00:20:34Z
BUCKET = 1798761600      # the epoch hour that contains NOW
MIN = 60
CFG = {"signin": {"hosts": ["app.example.test"]}}
TOPO = entities.Topology(
    containers={},
    subnets=[ipaddress.ip_network("172.18.0.0/16")],
    compose={"web": "shop", "api": "shop", "blog": "other"},
    listening=set(),
    host_ips=frozenset({ipaddress.ip_address("192.168.40.2")}),
)
SOURCES = ("sockets", "kernel", "auth", "plex", "cloudflare", "tunnel")


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(core, "STATE_DIR", state)
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    monkeypatch.setattr(entities, "load_topology", lambda *a, **k: TOPO)
    threat_dir = state / "netwatch" / "threat"
    threat_dir.mkdir(parents=True)
    (threat_dir / "spamhaus_drop_v4.txt").write_text("203.0.113.0/24\n")
    for name in ("spamhaus_drop_v6", "firehol_level1", "feodo"):      # a list never downloaded is stale: keep every list present
        (threat_dir / f"{name}.txt").write_text("198.51.100.0/24\n")
    for path in threat_dir.glob("*.txt"):
        os.utime(path, (NOW, NOW))                                  # the clock in these tests is NOW, not the wall clock
    db = store.open_db()
    yield db
    db.close()


def _ctx(now=NOW):
    cfg = {"netwatch": CFG, "tasks": {}, "caps": {}, "protected": {"patterns": []}}
    return core.Ctx(cfg, "net_watch", apply=False, now=now)


def _run(now=NOW):
    return core.REGISTRY["net_watch"].run(_ctx(now))


def _sources(db, now, stale=()):
    """Every collector source fresh at `now`, except the names in `stale`, which are reported as stale."""
    for name in SOURCES:
        status = "stale:timeout" if name in stale else "ok"
        store.meta_set(db, f"source:{name}", f"{status}|{int(now - 10)}")
    store.meta_set(db, "first_run", str(NOW - 8 * 86400))


def _flow(db, direction, src, dst, dport, conns=1, bucket=BUCKET):
    store.upsert_flows(db, [Flow(bucket, direction, src, dst, dport, "tcp", conns, 0, 0)])


def _edge(db, client_ip="203.0.113.50", country="US", count=1, status=200, path_class="other", bucket=BUCKET):
    store.upsert_edge(db, [EdgeRow(bucket=bucket, zone="zone1", host="app.example.test", client_ip=client_ip, country=country,
                                   asn=64500, status=status, path_class=path_class, count=count, bytes=100)])


def _seeded_exploit(db):
    _flow(db, "in", "203.0.113.9", "host", 32400)
    store.add_events(db, [NetEvent(ts=NOW - 20, kind="plex_exploit", src="unknown", dst="plex", dport=32400, detail="Req#7")])


# ---- registration and the scaffold's contract -----------------------------------------------------------------------------

def test_registered_as_a_c0_check_that_needs_root_with_a_timeout():
    cli.load_tasks()
    t = core.REGISTRY["net_watch"]
    assert (t.klass, t.tier, t.needs_root) == ("C0", "check", True)
    assert callable(t.run) and 0 < t.timeout <= 60 and t.title == "Network watch"


def test_result_is_one_short_ascii_line_on_an_empty_store(env):
    _sources(env, NOW)
    res = _run()
    assert isinstance(res, core.Result) and res.status in ("ok", "info", "warn", "crit", "skipped", "error")
    assert res.summary.isascii() and "\n" not in res.summary and len(res.summary) <= 140


def test_summary_stays_one_ascii_line_with_hostile_text(env):
    _sources(env, NOW)
    store.add_events(env, [NetEvent(ts=NOW - 20, kind="plex_exploit", src="unknown", dst="plex", dport=32400, detail="Req#7")])
    _flow(env, "in", "203.0.113.9", "host", 32400)
    res = _run()
    assert res.summary.isascii() and len(res.summary) <= 140 and "\n" not in res.summary


def test_never_mutates_the_host_or_the_store_in_a_run(env, tmp_path):
    _sources(env, NOW)
    _seeded_exploit(env)
    victim = tmp_path / "keep.txt"
    victim.write_text("keep me")
    flows_before = env.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
    ctx = _ctx()
    ctx.apply = True                       # a C0 check is read-only whatever the flags say
    core.REGISTRY["net_watch"].run(ctx)
    assert victim.read_text() == "keep me" and ctx.freed == 0
    assert env.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == flows_before


# ---- the verdict ----------------------------------------------------------------------------------------------------------

def test_a_seeded_plex_exploit_is_crit_and_links_to_its_finding(env):
    _sources(env, NOW)
    _seeded_exploit(env)
    res = _run()
    assert res.status == "crit"
    assert res.summary == "Network: 1 crit (plex_exploit 203.0.113.9), 1 warn"
    top = res.items[0]
    assert top["rule"] == "plex_exploit" and top["severity"] == "crit" and top["src"] == "203.0.113.9"
    assert res.link == f"/network?finding={top['id']}"
    assert res.issue_key and "plex_exploit:203.0.113.9" in res.issue_key
    assert res.metrics["flags"] == 2                      # the exploit, and the address is also on a threat list
    assert [i["rule"] for i in res.items] == ["plex_exploit", "threat_active"]


def test_findings_json_is_written_for_the_publisher_with_mode_0600(env):
    _sources(env, NOW)
    _seeded_exploit(env)
    res = _run()
    path = core.STATE_DIR / "netwatch" / "findings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    doc = json.loads(path.read_text())
    assert doc["generated"] == NOW
    assert [f["id"] for f in doc["findings"]] == [i["id"] for i in res.items]
    assert doc["findings"][0]["rule"] == "plex_exploit"


def test_scanning_alone_from_a_listed_address_is_ok_and_silent(env):
    _sources(env, NOW)
    _edge(env, client_ip="203.0.113.50", count=5, status=404)
    res = _run()
    assert res.status == "ok" and res.summary == "Network: no findings"
    assert res.items == [] and res.link == "" and res.issue_key is None
    assert res.metrics["flags"] == 0


def test_a_cloudflare_source_stale_for_31_minutes_is_a_warning(env):
    _sources(env, NOW, stale=("cloudflare",))
    store.meta_set(env, "stale_since:cloudflare", repr(NOW - 31 * MIN))
    res = _run()
    assert res.status == "warn"
    assert res.summary == "Network: edge data stale"
    assert res.metrics["stale_sources"] == 1
    assert res.issue_key == "stale:cloudflare"
    assert res.link == ""


def test_a_stale_source_is_not_counted_before_thirty_minutes(env):
    _sources(env, NOW, stale=("cloudflare",))
    res = _run()
    assert res.status == "ok" and res.metrics["stale_sources"] == 0
    assert store.meta_get(env, "stale_since:cloudflare") == repr(NOW)


def test_staleness_builds_across_runs_and_the_marker_is_removed_once_ok(env):
    _sources(env, NOW, stale=("cloudflare",))
    assert _run(NOW).status == "ok"
    later = NOW + 31 * MIN
    _sources(env, later, stale=("cloudflare",))
    res = _run(later)
    assert res.status == "warn" and res.summary == "Network: edge data stale"
    _sources(env, later + 60)                                   # fresh again
    res = _run(later + 60)
    assert res.status == "ok"
    assert store.meta_get(env, "stale_since:cloudflare") is None


def test_a_skipped_source_is_never_stale(env):
    _sources(env, NOW)
    store.meta_set(env, "source:cloudflare", f"skipped|{NOW - 3 * 3600}")
    store.meta_set(env, "stale_since:cloudflare", repr(NOW - 3 * 3600))
    res = _run()
    assert res.status == "ok" and res.metrics["stale_sources"] == 0
    assert store.meta_get(env, "stale_since:cloudflare") is None


def test_other_stale_sources_are_named_in_the_summary(env):
    _sources(env, NOW, stale=("kernel", "plex"))
    for name in ("kernel", "plex"):
        store.meta_set(env, f"stale_since:{name}", repr(NOW - 40 * MIN))
    res = _run()
    assert res.status == "warn"
    assert res.summary == "Network: 2 sources stale: kernel, plex"


def test_metrics_sum_the_last_day_and_name_the_top_talkers(env):
    _sources(env, NOW)
    _flow(env, "in", "203.0.113.9", "host", 443, conns=7)
    _flow(env, "out", "host", "203.0.113.9", 443, conns=2)
    _flow(env, "svc", "container:web", "container:api", 5432, conns=4)
    _flow(env, "lan", "lan:192.168.40.5", "host", 22, conns=3)
    _flow(env, "in", "203.0.113.9", "host", 443, conns=100, bucket=BUCKET - 3 * 86400)   # outside the day: not counted
    _edge(env, client_ip="203.0.113.50", country="CA", count=12)
    _edge(env, client_ip="198.51.100.2", country="US", count=30)
    _edge(env, client_ip="198.51.100.2", country="US", count=30, status=401, path_class="auth_api", bucket=BUCKET - 1)
    _edge(env, client_ip="203.0.113.77", country="DE", count=2)
    _edge(env, client_ip="203.0.113.99", country="FR", count=1, bucket=BUCKET - 2 * 86400)     # outside the day
    res = _run()
    m = res.metrics
    assert (m["in24"], m["out24"], m["svc24"], m["lan24"]) == (7, 2, 4, 3)
    assert m["edge24"] == 12 + 30 + 30 + 2
    assert m["blocked"] == 0
    assert m["top_talkers"] == "198.51.100.2 US 60; 203.0.113.50 CA 12; 203.0.113.77 DE 2"
    assert len(m["top_talkers"]) <= 120


def test_items_are_at_most_twelve_and_crit_comes_first(env):
    _sources(env, NOW)
    _seeded_exploit(env)
    for n in range(1, 15):
        store.add_events(env, [NetEvent(ts=NOW - 5, kind="auth_fail", src=f"203.0.113.{n}", dst="host", dport=22, detail="x")] * 6)
    res = _run()
    assert len(res.items) <= 12
    assert res.items[0]["severity"] == "crit"
    assert all(len(i["why"]) <= 120 for i in res.items)


# ---- fail closed ----------------------------------------------------------------------------------------------------------

def test_an_unreadable_store_is_an_error_not_an_exception(env, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(store, "open_db", denied)
    res = core.REGISTRY["net_watch"].run(_ctx())
    assert res.status == "error" and res.alert is True
    assert res.summary == "Network: store unreadable (PermissionError)"
    assert res.summary.isascii() and len(res.summary) <= 140


def test_an_unreadable_topology_is_treated_as_stale_so_nothing_is_blocked(env, monkeypatch):
    _sources(env, NOW)
    _seeded_exploit(env)

    def broken(*a, **k):
        raise RuntimeError("docker is in a bad state")
    monkeypatch.setattr(entities, "load_topology", broken)
    res = _run()
    assert res.status == "crit"
    assert res.items[0]["target"] == "plex"
    doc = json.loads((core.STATE_DIR / "netwatch" / "findings.json").read_text())
    assert doc["findings"][0]["rule"] == "plex_exploit"
    assert [f["block"] for f in doc["findings"]] == [None] * len(doc["findings"])


def test_threat_lists_that_cannot_be_read_are_an_error(env, monkeypatch):
    def unreadable(*a, **k):
        raise ValueError("not a network")
    monkeypatch.setattr(ThreatIndex, "load", unreadable)
    res = _run()
    assert res.status == "error" and res.summary == "Network: threat lists unreadable (ValueError)"
