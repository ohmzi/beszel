"""Tests for homelab_maint/netwatch/block.py: validation, protection, the nft argv, Cloudflare rules, restore, sweep and registration.
Fake shell (records argv, keeps a tiny nft model), fake Cloudflare client, fake audit. Nothing touches nft, sudo or the network."""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)

import subprocess
from datetime import datetime, timezone

import pytest

from homelab_maint import acks, core
from homelab_maint.netwatch import block, entities, store
from homelab_maint.netwatch.cloudflare import CFUnavailable

NOW = 1_800_000_000.0
FINDING = "0123456789abcdef"
ZONE = "example.test"


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class FakeSh:
    """Records every argv. Keeps the rules that `nft add rule` adds, so `nft list chain` shows them as the real command would."""

    def __init__(self, delete_rc=0, delete_out=""):
        self.calls: list[list[str]] = []
        self.chains: dict[str, str] = {}
        self.delete_rc = delete_rc
        self.delete_out = delete_out

    def __call__(self, cmd, timeout=60, check=False, input_=None, env=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["nft", "list", "chain"]:
            return _cp(self.chains.get(cmd[5], ""))
        if cmd[:3] == ["nft", "add", "rule"]:
            self.chains[cmd[5]] = self.chains.get(cmd[5], "") + " ".join(cmd[6:]) + "\n"
            return _cp()
        if cmd[:3] == ["nft", "delete", "element"]:
            return _cp("", self.delete_rc, self.delete_out)
        return _cp()

    def element_adds(self):
        return [c for c in self.calls if c[:3] == ["nft", "add", "element"]]

    def rule_adds(self):
        return [c for c in self.calls if c[:3] == ["nft", "add", "rule"]]


class FakeCF:
    def __init__(self, fail=None, rules=None, delete_fail=None):
        self.fail = fail
        self.delete_fail = delete_fail
        self.created: list[tuple] = []
        self.deleted: list[tuple] = []
        self.rules = rules or []

    def create_access_rule(self, zone, ip, note):
        if self.fail:
            raise CFUnavailable(self.fail)
        self.created.append((zone, ip, note))
        return f"rule-{len(self.created)}"

    def delete_access_rule(self, zone, rule_id):
        if self.delete_fail:
            raise CFUnavailable(self.delete_fail)
        self.deleted.append((zone, rule_id))

    def list_access_rules(self, zone):
        return list(self.rules)


@pytest.fixture
def audit(monkeypatch):
    rows = []
    monkeypatch.setattr(core, "audit", lambda task, what, target, size, outcome, n=1: rows.append((task, what, target, outcome)))
    return rows


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(core, "load_config", lambda: {"netwatch": {}})
    monkeypatch.setattr(entities, "load_topology", lambda sh=None: entities.Topology.empty_stale())


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "n.db")
    yield conn
    conn.close()


def _req(**over):
    req = {"finding": FINDING, "ip": "203.0.113.9", "target": "host", "zone": "", "days": 30, "permanent": False,
           "override_reason": ""}
    req.update(over)
    return req


def _row(db, bid):
    return db.execute("SELECT ip, target, zone, until, permanent, reason, cf_rule_id, removed FROM blocks WHERE id = ?", (bid,)).fetchone()


def _add_signin(db, ip, now=NOW - 100):
    db.execute("INSERT INTO signins (ip, source, last_seen) VALUES (?, ?, ?)", (ip, "auth_api", now))
    db.commit()


# ---- refusals ----------------------------------------------------------------------------------------------------------

def test_lan_address_is_refused_even_with_an_override_reason(db, audit):
    sh = FakeSh()
    ok, reason = block.apply_block(_req(ip="192.168.40.7", override_reason="I know what I am doing"), NOW, sh=sh, db=db)

    assert ok is False and reason.startswith("hard:")
    assert sh.calls == [] and db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 0 and audit == []


@pytest.mark.parametrize("override", ["", "   \t"])
def test_signin_address_without_a_reason_is_refused(db, audit, override):
    _add_signin(db, "203.0.113.9")
    sh = FakeSh()
    ok, reason = block.apply_block(_req(override_reason=override), NOW, sh=sh, db=db)

    assert (ok, reason) == (False, "signin-protected")
    assert sh.calls == [] and db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 0 and audit == []


def test_signin_address_with_a_reason_is_applied_and_the_reason_is_audited(db, audit):
    _add_signin(db, "203.0.113.9")
    sh = FakeSh()
    ok, reason = block.apply_block(_req(override_reason="scanner, not me"), NOW, sh=sh, db=db)

    assert (ok, reason) == (True, "ok")
    assert ["nft", "add", "element", "inet", "netwatch", "block4", "{ 203.0.113.9 timeout 30d }"] in sh.calls
    assert audit == [("netwatch", "block", "203.0.113.9", "ok:host override:scanner, not me")]
    assert _row(db, block.block_id("203.0.113.9", "host", ""))[5] == "scanner, not me"


@pytest.mark.parametrize("bad", [
    {"ip": "not-an-ip"}, {"ip": None}, {"ip": 5},
    {"target": "ufw"}, {"target": None},
    {"target": "cloudflare", "zone": ""}, {"target": "cloudflare", "zone": None},
    {"days": 0}, {"days": 366}, {"days": True}, {"days": "30"}, {"days": 1.5},
    {"permanent": "yes"}, {"permanent": 1},
    {"finding": "xyz"}, {"finding": "0123456789ABCDEF0"}, {"finding": 123},
    {"override_reason": "x" * 201}, {"override_reason": None},
])
def test_invalid_requests_are_refused_and_nothing_is_applied(db, audit, bad):
    sh, cf = FakeSh(), FakeCF()
    ok, _reason = block.apply_block(_req(**bad), NOW, cf=cf, sh=sh, db=db)

    assert ok is False
    assert sh.calls == [] and cf.created == [] and db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 0 and audit == []


# ---- host target --------------------------------------------------------------------------------------------------------

def test_host_block_runs_the_exact_nft_argv_and_writes_a_row(db, audit):
    sh = FakeSh()
    ok, reason = block.apply_block(_req(), NOW, sh=sh, db=db)

    assert (ok, reason) == (True, "ok")
    assert sh.element_adds() == [["nft", "add", "element", "inet", "netwatch", "block4", "{ 203.0.113.9 timeout 30d }"]]
    bid = block.block_id("203.0.113.9", "host", "")
    assert _row(db, bid) == ("203.0.113.9", "host", "", NOW + 30 * 86400, 0, "", None, None)
    assert audit == [("netwatch", "block", "203.0.113.9", "ok:host")]


def test_permanent_host_block_has_no_timeout(db):
    sh = FakeSh()
    assert block.apply_block(_req(permanent=True, days=1), NOW, sh=sh, db=db) == (True, "ok")
    assert sh.element_adds()[0][-1] == "{ 203.0.113.9 }"
    assert _row(db, block.block_id("203.0.113.9", "host", ""))[3] is None


def test_v6_address_goes_to_block6(db):
    sh = FakeSh()
    assert block.apply_block(_req(ip="2001:db8::9", days=7), NOW, sh=sh, db=db) == (True, "ok")
    assert sh.element_adds() == [["nft", "add", "element", "inet", "netwatch", "block6", "{ 2001:db8::9 timeout 7d }"]]


def test_v4_mapped_address_is_normalised_and_goes_to_block4(db):
    sh = FakeSh()
    assert block.apply_block(_req(ip="::ffff:203.0.113.9"), NOW, sh=sh, db=db) == (True, "ok")
    assert sh.element_adds()[0][5] == "block4" and sh.element_adds()[0][-1] == "{ 203.0.113.9 timeout 30d }"
    assert _row(db, block.block_id("203.0.113.9", "host", "")) is not None


def test_repeat_block_is_idempotent(db, audit):
    sh, cf = FakeSh(), FakeCF()
    assert block.apply_block(_req(), NOW, cf=cf, sh=sh, db=db) == (True, "ok")
    before = len(sh.calls)
    assert block.apply_block(_req(), NOW + 60, cf=cf, sh=sh, db=db) == (True, "already-blocked")

    assert len(sh.calls) == before and len(sh.element_adds()) == 1
    assert db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 1 and len(audit) == 1


def test_host_block_with_nft_failure_writes_no_row(db, audit):
    class Broken(FakeSh):
        def __call__(self, cmd, **kw):
            if cmd[:3] == ["nft", "add", "element"]:
                return _cp("", 1, "Error: boom")
            return super().__call__(cmd, **kw)

    ok, reason = block.apply_block(_req(), NOW, sh=Broken(), db=db)

    assert (ok, reason) == (False, "nft:add")
    assert db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 0 and audit == []


# ---- cloudflare target ---------------------------------------------------------------------------------------------------

def test_cloudflare_block_uses_the_zone_and_the_note(db, audit):
    cf = FakeCF()
    req = _req(target="cloudflare", zone=ZONE, days=30)
    assert block.apply_block(req, NOW, cf=cf, sh=FakeSh(), db=db) == (True, "ok")

    bid = block.block_id("203.0.113.9", "cloudflare", ZONE)
    until_day = datetime.fromtimestamp(NOW + 30 * 86400, timezone.utc).strftime("%Y-%m-%d")
    assert cf.created == [(ZONE, "203.0.113.9", f"netwatch:{bid} until {until_day}")]
    assert _row(db, bid)[6] == "rule-1"
    assert audit == [("netwatch", "block", "203.0.113.9", "ok:cloudflare")]


def test_cloudflare_permanent_note_says_permanent(db):
    cf = FakeCF()
    block.apply_block(_req(target="cloudflare", zone=ZONE, permanent=True), NOW, cf=cf, sh=FakeSh(), db=db)
    assert cf.created[0][2] == f"netwatch:{block.block_id('203.0.113.9', 'cloudflare', ZONE)} permanent"


def test_repeat_cloudflare_block_creates_no_second_rule(db):
    cf = FakeCF()
    req = _req(target="cloudflare", zone=ZONE)
    block.apply_block(req, NOW, cf=cf, sh=FakeSh(), db=db)
    assert block.apply_block(req, NOW, cf=cf, sh=FakeSh(), db=db) == (True, "already-blocked")
    assert len(cf.created) == 1


def test_cloudflare_unavailable_on_create_writes_no_row(db, audit):
    cf = FakeCF(fail="token")
    ok, reason = block.apply_block(_req(target="cloudflare", zone=ZONE), NOW, cf=cf, sh=FakeSh(), db=db)

    assert (ok, reason) == (False, "cloudflare:token")
    assert db.execute("SELECT count(*) FROM blocks").fetchone()[0] == 0 and audit == []


# ---- ensure_table ----------------------------------------------------------------------------------------------------

def test_ensure_table_creates_the_table_sets_and_chains_with_exact_argv():
    sh = FakeSh()
    block.ensure_table(sh)

    assert sh.calls[:4] == [
        ["nft", "add", "table", "inet", "netwatch"],
        ["nft", "add", "set", "inet", "netwatch", "block4", "{ type ipv4_addr; flags timeout; }"],
        ["nft", "add", "set", "inet", "netwatch", "block6", "{ type ipv6_addr; flags timeout; }"],
        ["nft", "add", "chain", "inet", "netwatch", "input", "{ type filter hook input priority -10; policy accept; }"],
    ]
    assert ["nft", "add", "chain", "inet", "netwatch", "forward", "{ type filter hook forward priority -10; policy accept; }"] in sh.calls


def test_ensure_table_twice_adds_each_drop_rule_once():
    sh = FakeSh()
    block.ensure_table(sh)
    assert len(sh.rule_adds()) == 4        # block4 and block6 in input and in forward
    block.ensure_table(sh)
    assert len(sh.rule_adds()) == 4        # the second call finds them in the chain listing and adds nothing


# ---- unblock -------------------------------------------------------------------------------------------------------------

def test_unblock_removes_the_element_and_closes_the_row(db, audit):
    sh = FakeSh()
    block.apply_block(_req(), NOW, sh=sh, db=db)
    bid = block.block_id("203.0.113.9", "host", "")

    assert block.apply_unblock({"block_id": bid}, NOW + 5, sh=sh, db=db) == (True, "ok")
    assert ["nft", "delete", "element", "inet", "netwatch", "block4", "{ 203.0.113.9 }"] in sh.calls
    assert _row(db, bid)[7] == NOW + 5
    assert audit[-1] == ("netwatch", "unblock", "203.0.113.9", "ok:host")


def test_unblock_of_an_expired_block_is_ok(db):
    sh = FakeSh(delete_rc=1, delete_out="Error: Could not process rule: No such file or directory")
    block.apply_block(_req(days=1), NOW, sh=sh, db=db)
    bid = block.block_id("203.0.113.9", "host", "")

    assert block.apply_unblock({"block_id": bid}, NOW + 2 * 86400, sh=sh, db=db) == (True, "ok")
    assert _row(db, bid)[7] == NOW + 2 * 86400


def test_unblock_of_a_missing_or_removed_block_is_not_blocked(db):
    sh = FakeSh()
    assert block.apply_unblock({"block_id": "0123456789abcdef"}, NOW, sh=sh, db=db) == (True, "not-blocked")
    block.apply_block(_req(), NOW, sh=sh, db=db)
    bid = block.block_id("203.0.113.9", "host", "")
    block.apply_unblock({"block_id": bid}, NOW, sh=sh, db=db)
    assert block.apply_unblock({"block_id": bid}, NOW, sh=sh, db=db) == (True, "not-blocked")


def test_unblock_with_a_real_nft_failure_keeps_the_row_open(db):
    sh = FakeSh()
    block.apply_block(_req(), NOW, sh=sh, db=db)
    bid = block.block_id("203.0.113.9", "host", "")
    sh.delete_rc, sh.delete_out = 1, "Error: permission denied"

    assert block.apply_unblock({"block_id": bid}, NOW, sh=sh, db=db) == (False, "nft:delete")
    assert _row(db, bid)[7] is None


def test_cloudflare_unblock_deletes_the_rule_and_404_is_success(db):
    cf = FakeCF()
    block.apply_block(_req(target="cloudflare", zone=ZONE), NOW, cf=cf, sh=FakeSh(), db=db)
    bid = block.block_id("203.0.113.9", "cloudflare", ZONE)

    assert block.apply_unblock({"block_id": bid}, NOW, cf=cf, sh=FakeSh(), db=db) == (True, "ok")
    assert cf.deleted == [(ZONE, "rule-1")]

    block.apply_block(_req(target="cloudflare", zone=ZONE, ip="203.0.113.10"), NOW, cf=cf, sh=FakeSh(), db=db)
    gone = FakeCF(delete_fail="http-404")
    bid2 = block.block_id("203.0.113.10", "cloudflare", ZONE)
    assert block.apply_unblock({"block_id": bid2}, NOW, cf=gone, sh=FakeSh(), db=db) == (True, "ok")


def test_unblock_rejects_a_malformed_id(db):
    assert block.apply_unblock({"block_id": "../../etc"}, NOW, sh=FakeSh(), db=db) == (False, "bad-block-id")


# ---- restore ---------------------------------------------------------------------------------------------------------------

def test_restore_readds_open_host_blocks_with_the_remaining_seconds(db):
    block.apply_block(_req(ip="203.0.113.9", days=1), NOW, sh=FakeSh(), db=db)               # 1 day at NOW
    block.apply_block(_req(ip="203.0.113.10", permanent=True), NOW, sh=FakeSh(), db=db)
    db.execute("UPDATE blocks SET until = ? WHERE ip = ?", (NOW + 1000, "203.0.113.9"))
    db.commit()
    sh = FakeSh()

    assert block.restore(db, NOW, sh=sh) == "ok"
    assert sorted(c[-1] for c in sh.element_adds()) == ["{ 203.0.113.10 }", "{ 203.0.113.9 timeout 1000s }"]


def test_restore_closes_expired_host_blocks_and_never_readds_them(db):
    sh = FakeSh()
    block.apply_block(_req(days=1), NOW, sh=sh, db=db)
    sh2 = FakeSh()
    assert block.restore(db, NOW + 2 * 86400, sh=sh2) == "ok"
    assert sh2.element_adds() == []
    assert _row(db, block.block_id("203.0.113.9", "host", ""))[7] == NOW + 2 * 86400


def test_restore_reports_stale_when_nft_is_unavailable(db):
    def dead(cmd, **kw):
        return _cp("", 1, "nft: not found")
    assert block.restore(db, NOW, sh=dead) == "stale:nft"


# ---- sweep -----------------------------------------------------------------------------------------------------------------

def test_sweep_deletes_only_expired_cloudflare_rules_and_closes_host_rows(db):
    cf = FakeCF()
    expired_day = "2020-01-01"
    live = _req(target="cloudflare", zone=ZONE, ip="203.0.113.20", days=30)
    block.apply_block(live, NOW, cf=cf, sh=FakeSh(), db=db)                     # future
    block.apply_block(_req(target="cloudflare", zone=ZONE, ip="203.0.113.21", days=1), NOW, cf=cf, sh=FakeSh(), db=db)
    block.apply_block(_req(ip="203.0.113.22", days=1), NOW, sh=FakeSh(), db=db)
    cf.rules = [
        {"id": "orphan", "notes": f"netwatch:deadbeefdeadbeef until {expired_day}"},
        {"id": "forever", "notes": "netwatch:deadbeefdeadbeef permanent"},
        {"id": "future", "notes": f"netwatch:deadbeefdeadbeef until {datetime.fromtimestamp(NOW + 9 * 86400, timezone.utc):%Y-%m-%d}"},
        {"id": "mine", "notes": "handwritten, not ours"},
    ]
    later = NOW + 2 * 86400

    assert block.sweep(db, later, cf) == "ok"
    deleted = {rid for _zone, rid in cf.deleted}
    assert "orphan" in deleted and "forever" not in deleted and "future" not in deleted and "mine" not in deleted
    assert "rule-2" in deleted and "rule-1" not in deleted        # the 1-day block expired; the 30-day one is live
    assert _row(db, block.block_id("203.0.113.22", "host", ""))[7] == later
    assert _row(db, block.block_id("203.0.113.21", "cloudflare", ZONE))[7] == later
    assert _row(db, block.block_id("203.0.113.20", "cloudflare", ZONE))[7] is None


def test_sweep_without_a_cloudflare_client_leaves_rules_alone_and_says_so(db):
    block.apply_block(_req(target="cloudflare", zone=ZONE, days=1), NOW, cf=FakeCF(), sh=FakeSh(), db=db)
    assert block.sweep(db, NOW + 2 * 86400, None) == "skipped"
    assert db.execute("SELECT removed FROM blocks").fetchone()[0] is None


# ---- registration -----------------------------------------------------------------------------------------------------------

def test_netwatch_handlers_are_registered_with_the_inbox(monkeypatch):
    monkeypatch.setattr(acks, "_HANDLERS", {})
    acks._netwatch_handlers()

    assert set(acks._HANDLERS) == {"net_block", "net_unblock"}
    assert acks._HANDLERS["net_block"][1] >= set(block.NET_BLOCK_KEYS)
    assert acks._HANDLERS["net_unblock"][1] >= set(block.NET_UNBLOCK_KEYS)
