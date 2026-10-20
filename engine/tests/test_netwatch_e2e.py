"""End to end: a signed net_block request, written into ack/inbox the way the web container writes it, goes through acks.process_inbox
to netwatch/block.py. The request is signed with the real web.key (acks' canonical bytes and HMAC-SHA256). The firewall is a fake shell
that records argv, Cloudflare is a fake client that must never be called here, and the topology is the stale one (no docker).
Documentation ranges only (203.0.113.0/24 for the internet peer, 192.168.40.0/24 for the LAN). Nothing touches nft, sudo or the network.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import hashlib
import hmac
import json
import os
import subprocess

import pytest

from homelab_maint import acks, core
from homelab_maint.netwatch import block, entities, store

NOW = 1_790_000_000.0
FINDING = "0123456789abcdef"


class FakeSh:
    """Every argv is recorded and succeeds. `nft list chain` shows nothing, so ensure_table adds its rules once."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=60, check=False, input_=None, env=None):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    def element_adds(self):
        return [c for c in self.calls if c[:3] == ["nft", "add", "element"]]


class NoCloudflare:
    def create_access_rule(self, *a):
        raise AssertionError("Cloudflare must not be called for a host block")

    def delete_access_rule(self, *a):
        raise AssertionError("Cloudflare must not be called for a host block")


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name, sub in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("CONF_DIR", "conf"), ("RUN_DIR", "run")):
        (tmp_path / sub).mkdir()
        monkeypatch.setattr(core, name, tmp_path / sub)
    monkeypatch.setattr(core, "audit", lambda *a, **k: None)
    monkeypatch.setattr(core, "load_config", lambda: {"netwatch": {}})
    monkeypatch.setattr(entities, "load_topology", lambda sh=None: entities.Topology.empty_stale())
    acks.init_dirs()
    key = (core.STATE_DIR / "ack" / "web.key").read_text().strip().encode()
    sh, cf = FakeSh(), NoCloudflare()
    real_apply = block.apply_block
    # acks registers the handler on every run; the handler here is the real one with the firewall and Cloudflare faked
    monkeypatch.setattr(block, "apply_block", lambda req, now, **kw: real_apply(req, now, cf=cf, sh=sh, db=None))
    return {"key": key, "sh": sh, "inbox": core.STATE_DIR / "ack" / "inbox", "state": core.STATE_DIR}


def _drop(world, req: dict) -> None:
    """Sign like the agent does: the canonical bytes of the request without sig, HMAC-SHA256 with web.key, written atomically."""
    signed = dict(req)
    signed["sig"] = hmac.new(world["key"], acks.canonical(req), hashlib.sha256).hexdigest()
    name = f"{int(NOW * 1000)}-{os.urandom(4).hex()}.json"
    tmp = world["inbox"] / f".w-{os.urandom(3).hex()}"
    tmp.write_bytes(json.dumps(signed).encode())
    os.replace(tmp, world["inbox"] / name)


def _net_block(ip: str, **over) -> dict:
    req = {"v": 1, "kind": "net_block", "source": "web", "ts": NOW, "finding": FINDING, "ip": ip, "target": "host", "zone": "",
           "days": 30, "permanent": False, "override_reason": ""}
    req.update(over)
    return req


def _requests(state):
    db = store.open_db(state / "netwatch.db")
    try:
        return db.execute("SELECT kind, ip, target, ok, reason FROM block_requests ORDER BY id").fetchall()
    finally:
        db.close()


def test_a_signed_block_is_applied_and_a_hard_range_request_is_refused(world):
    _drop(world, _net_block("203.0.113.9"))
    _drop(world, _net_block("192.168.40.7", days=2, override_reason="owner says so"))

    rep = acks.process_inbox(NOW)

    assert rep.handled == ["net_block"]
    assert rep.rejected == ["hard:192.168.40.0/24"]
    assert world["sh"].element_adds() == [["nft", "add", "element", "inet", "netwatch", "block4", "{ 203.0.113.9 timeout 30d }"]]
    assert list(world["inbox"].glob("*.json")) == []                    # both files are spent, the refused one is not kept as a request

    db = store.open_db(world["state"] / "netwatch.db")
    try:
        (row,) = db.execute("SELECT ip, target, removed FROM blocks").fetchall()
    finally:
        db.close()
    assert row == ("203.0.113.9", "host", None)
    assert sorted(_requests(world["state"])) == sorted([
        ("block", "203.0.113.9", "host", 1, "ok"),
        ("block", "192.168.40.7", "host", 0, "hard:192.168.40.0/24"),
    ])                                                                    # the two files share a timestamp, so their order is not fixed


def test_a_replayed_block_request_is_a_duplicate_and_adds_nothing(world):
    req = _net_block("203.0.113.9")
    _drop(world, req)
    acks.process_inbox(NOW)
    _drop(world, req)                                                     # the same bytes again: the replay guard recognises them
    rep = acks.process_inbox(NOW + 1)
    assert rep.duplicates == 1 and rep.handled == []
    assert len(world["sh"].element_adds()) == 1
