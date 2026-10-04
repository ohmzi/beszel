"""The runner side of the website's first-run login (SPEC6 s7): acks_auth, its registration in the production inbox path, the quarantine hygiene,
`homelab-maint web bootstrap`, ack/ modes, and an end-to-end run against the REAL web/app.py (setup through the website API -> runner tick -> login).

Everything runs in tmp dirs; nothing is sent and nothing outside the tmp tree is touched. The website is the real web/app.py on a loopback port.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402,F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint.core is imported)
import website_helper as wh  # noqa: E402

from homelab_maint import acks, acks_auth, cli, core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
NOW = 1_790_000_000.0
SECRET = "bootstrap-secret-for-tests"
PASS = "correct horse battery staple"
FAST = 1000                                           # PBKDF2 iterations for the tests that are not about the iteration count

needs_web = pytest.mark.skipif(not wh.have_web(), reason="web/ is not in this tree")


@pytest.fixture(scope="module")
def webapp():
    return wh.load_app()


@pytest.fixture(scope="module")
def reference():
    return wh.load("hm_web_ack_auth_reference", WEB / "tools" / "ack_auth_handlers.py")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A STATE tree as install.sh + `ack init` leave it, with the bootstrap secret, and a fresh process (no handler registered)."""
    for name, sub in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("CONF_DIR", "conf"), ("RUN_DIR", "run")):
        (tmp_path / sub).mkdir()
        monkeypatch.setattr(core, name, tmp_path / sub)
    monkeypatch.setattr(core, "audit", lambda *a: None)
    monkeypatch.setattr(acks, "_HANDLERS", {})
    monkeypatch.setattr(acks_auth, "MIN_ITER", FAST)
    acks.init_dirs()
    ack = tmp_path / "state" / "ack"
    (ack / "bootstrap.secret").write_text(SECRET + "\n")
    os.chmod(ack / "bootstrap.secret", 0o600)
    monkeypatch.delenv("HM_WEB_GID", raising=False)
    monkeypatch.setattr(acks, "_now", lambda now=None: NOW if now is None else now)
    return type("W", (), {"tmp": tmp_path, "state": tmp_path / "state", "ack": ack, "inbox": ack / "inbox", "key": (ack / "web.key").read_text().strip().encode()})


def pw_obj(iters: int = FAST, passphrase: str = PASS) -> dict:
    salt = os.urandom(16)
    return {"alg": "pbkdf2-sha256", "iter": iters, "salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iters).hex()}


def put(w, req: dict, signed: bool = True) -> Path:
    if signed:
        req["sig"] = acks.sign(req, w.key)
    p = w.inbox / f"{int(NOW * 1000)}-{os.urandom(4).hex()}.json"
    p.write_text(json.dumps(req))
    return p


def setup_req(secret: str = SECRET, *, totp_secret: str | None = None, recovery=None, **over) -> dict:
    rid = over.pop("rid", os.urandom(4).hex())
    req = {"v": 1, "kind": "auth_setup", "source": "web", "ts": int(NOW), "rid": rid, "pw": pw_obj()}
    if totp_secret:
        pad = hmac.new(acks_auth.bootstrap_key(secret), acks_auth.WRAP_LABEL + rid.encode(), hashlib.sha256).digest()
        req["totp"] = {"enc": bytes(a ^ b for a, b in zip(totp_secret.encode(), pad)).hex()}
    if recovery is not None:
        req["recovery"] = list(recovery)
    req.update(over)
    req["proof"] = hmac.new(acks_auth.bootstrap_key(secret), acks_auth.canonical(req), hashlib.sha256).hexdigest()
    return req


def result(w, rid: str) -> dict:
    return json.loads((w.ack / "auth_result.json").read_text())["results"][rid]


# =========================================================================== the copy equals the tested reference
@needs_web
def test_the_runner_copy_is_the_tested_web_reference(reference):
    """The reference is what web/tests exercise (and the dev sandbox runs); the runner's copy may differ only in its docstring and register()."""
    consts = ("WEB_GID_DEFAULT", "MAX_ITER", "BOOTSTRAP_KDF", "WRAP_LABEL", "KEEP_RESULTS", "SECRET_KEYS")             # (MIN_ITER is compared with the website in the next test)
    assert {c: getattr(acks_auth, c) for c in consts} == {c: getattr(reference, c) for c in consts}
    for c in ("HEX64", "HEX32", "RID", "_B32_RUN", "_HEX_RUN"):
        assert getattr(acks_auth, c).pattern == getattr(reference, c).pattern, c
    funcs = ("detect_web_gid", "web_gid", "readable_by", "write_atomic", "write_auth_file", "_locked", "canonical", "bootstrap_key", "totp_unwrap",
             "_b32", "validate_auth", "scrub", "make_auth_handlers")
    for f in funcs:
        a, b = inspect.getsource(getattr(acks_auth, f)), inspect.getsource(getattr(reference, f))
        assert a.strip() == b.strip(), f"{f} drifted from web/tools/ack_auth_handlers.py"
    assert inspect.getsource(acks_auth.Unreadable) == inspect.getsource(reference.Unreadable)


@needs_web
def test_the_formats_equal_the_websites(webapp):
    """The three things both sides must compute alike: the proof key, the authenticator wrap label, the canonical bytes, and what auth.json may hold."""
    assert acks_auth.BOOTSTRAP_KDF == webapp.BOOTSTRAP_KDF and acks_auth.WRAP_LABEL == webapp.TOTP_WRAP_LABEL
    shipped = int(re.search(r"^MIN_ITER = ([\d_]+)", Path(acks_auth.__file__).read_text(), re.M)[1].replace("_", ""))          # (the fixture lowers the live value)
    assert shipped == webapp.PBKDF2_ITER == 600_000 and acks_auth.MAX_ITER >= webapp.PBKDF2_ITER
    assert acks_auth.bootstrap_key(SECRET) == webapp.bootstrap_key(SECRET)
    req = {"v": 1, "kind": "auth_setup", "z": [1, {"b": 2, "a": "é"}], "sig": "x"}
    assert acks_auth.canonical({**req, "sig": "y"}) == webapp.canonical(req) == acks.canonical(req)


@needs_web
def test_the_validator_agrees_with_the_websites_parse_auth(webapp, monkeypatch):
    monkeypatch.setattr(acks_auth, "MIN_ITER", webapp.PBKDF2_ITER)                                    # the shipped floor, not the tests' fast one
    good = {"v": 1, "updated_at": 5, "pw": {"alg": "pbkdf2-sha256", "iter": 600_000, "salt": "ab" * 16, "hash": "cd" * 32}}
    totp = {"secret": base64.b32encode(os.urandom(20)).decode().rstrip("="), "digits": 6, "period": 30}
    rec = ["ef" * 16] * 3
    cases = {"plain": good, "totp": {**good, "totp": totp, "recovery": rec}, "weak iter": {**good, "pw": {**good["pw"], "iter": 599_999}},
             "bad alg": {**good, "pw": {**good["pw"], "alg": "md5"}}, "short salt": {**good, "pw": {**good["pw"], "salt": "ab" * 4}},
             "hash not hex": {**good, "pw": {**good["pw"], "hash": "zz" * 32}}, "33 recovery": {**good, "recovery": ["ef" * 16] * 33},
             "bad recovery": {**good, "recovery": ["nope"]}, "digits 8": {**good, "totp": {**totp, "digits": 8}},
             "period 60": {**good, "totp": {**totp, "period": 60}}, "totp not b32": {**good, "totp": {**totp, "secret": "!!!"}},
             "v 2": {**good, "v": 2}, "v true": {**good, "v": True}, "list": [good], "no pw": {"v": 1}}
    for name, doc in cases.items():
        assert acks_auth.validate_auth(doc) is bool(webapp.parse_auth(doc)), name


# =========================================================================== registered by the production inbox path
def test_process_inbox_registers_both_kinds_before_the_first_request_and_an_idle_minute_registers_nothing(world):
    assert acks.process_inbox(NOW).note == "" and acks._HANDLERS == {}                                # an empty inbox: one listing, nothing imported or registered
    put(world, setup_req())
    rep = acks.process_inbox(NOW)
    assert rep.handled == ["auth_setup"] and rep.rejected == [] and set(acks._HANDLERS) == {"auth_setup", "auth_change"}
    assert acks._HANDLERS["auth_setup"][1] >= {"rid", "pw", "totp", "recovery", "proof"} and acks._HANDLERS["auth_change"][1] >= {"rid", "op", "h"}
    assert acks._HANDLERS["auth_setup"][0]._acks_auth is True


def test_the_one_minute_tick_and_run_once_serve_the_setup_screen(world):
    put(world, setup_req())
    cli._ack_tick()                                                                                   # cmd_tick's call: acks.run_once()
    assert (world.ack / "auth.json").is_file()
    os.unlink(world.ack / "auth.json")
    put(world, setup_req())
    assert acks.run_once(NOW)["rejected"] == 0 and (world.ack / "auth.json").is_file()


def test_a_handler_someone_else_registered_is_left_alone(world):
    seen = []
    mine = lambda r, n: seen.append(r) or (True, "ok")                                                # noqa: E731
    acks.register_inbox_handler("auth_change", mine, allowed_keys=("rid", "op", "h"))
    put(world, setup_req())
    assert acks.process_inbox(NOW).handled == ["auth_setup"]                                          # the kind nobody else serves is still ours ...
    assert acks._HANDLERS["auth_change"][0] is mine and getattr(acks._HANDLERS["auth_setup"][0], "_acks_auth", False)      # ... the other stayed theirs
    put(world, {"v": 1, "kind": "auth_change", "source": "web", "ts": int(NOW), "rid": "66666666", "op": "burn", "h": "ab" * 16})
    assert acks.process_inbox(NOW).handled == ["auth_change"] and len(seen) == 1


def test_ours_follow_a_changed_state_dir(world, monkeypatch, tmp_path):
    put(world, setup_req())
    acks.process_inbox(NOW)
    other = tmp_path / "other"
    (other / "state").mkdir(parents=True)
    monkeypatch.setattr(core, "STATE_DIR", other / "state")
    acks.init_dirs()
    (other / "state" / "ack" / "bootstrap.secret").write_text(SECRET + "\n")
    key = (other / "state" / "ack" / "web.key").read_text().strip().encode()
    req = setup_req()
    req["sig"] = acks.sign(req, key)
    (other / "state" / "ack" / "inbox" / f"{int(NOW * 1000)}-{os.urandom(4).hex()}.json").write_text(json.dumps(req))
    assert acks.process_inbox(NOW).handled == ["auth_setup"] and (other / "state" / "ack" / "auth.json").is_file()


def test_module_run_as_main_registers_into_its_own_table(world, tmp_path):
    """`python3 -m homelab_maint.acks process` (the ack-process job) runs this module as __main__: registering into the package module's table
    would leave the kinds unanswered there. A real subprocess against the same tmp state, with the REAL iteration count."""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "HOMELAB_MAINT_STATE": str(world.state), "HOMELAB_MAINT_LOG": str(world.tmp / "log"),
           "HOMELAB_MAINT_CONF": str(world.tmp / "conf"), "HOMELAB_MAINT_RUN": str(world.tmp / "run"), "HOMELAB_MAINT_NO_SYSLOG": "1"}
    req = setup_req(pw=pw_obj(600_000), ts=int(time.time()))                                          # (the subprocess reads the real clock)
    put(world, req)
    r = subprocess.run([sys.executable, "-B", "-m", "homelab_maint.acks", "process"], capture_output=True, text=True, env=env, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    doc = json.loads((world.ack / "auth.json").read_text())
    assert doc["v"] == 1 and doc["pw"]["iter"] == 600_000 and (world.ack / "auth.json").stat().st_mode & 0o777 == 0o640
    assert result(world, req["rid"]) == {"ok": True, "reason": "ok", "kind": "auth_setup", "at": result(world, req["rid"])["at"]}


# =========================================================================== the handlers
def test_setup_writes_auth_json_and_the_result_and_keeps_the_secret_out_of_every_log(world):
    totp = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    req = setup_req(totp_secret=totp, recovery=["ab" * 16, "cd" * 16])
    put(world, req)
    rep = acks.process_inbox(NOW)
    assert rep.handled == ["auth_setup"] and rep.rejected == []
    p = world.ack / "auth.json"
    doc = json.loads(p.read_text())
    assert doc["totp"] == {"secret": totp, "digits": 6, "period": 30} and doc["recovery"] == ["ab" * 16, "cd" * 16] and doc["pw"] == req["pw"]
    assert p.stat().st_mode & 0o777 == 0o640 and not [q for q in world.ack.iterdir() if q.name.endswith(".tmp")]
    assert result(world, req["rid"])["ok"] is True and (world.ack / "auth_result.json").stat().st_mode & 0o777 == 0o644
    assert acks.auth_state(world.ack) == ("ok", "")
    blob = b"".join(q.read_bytes() for q in world.state.rglob("*") if q.is_file() and q.name not in ("auth.json", "bootstrap.secret"))
    for secret in (SECRET.encode(), PASS.encode(), totp.encode(), b"abababab"):
        assert secret not in blob, secret                                                             # acks.jsonl, the store, auth_result.json: nowhere


@pytest.mark.parametrize("how,reason", [("proof", "bad_proof"), ("nosecret", "no_bootstrap"), ("empty", "no_bootstrap"), ("exists", "exists"),
                                        ("pw", "invalid"), ("totp", "invalid"), ("recovery_alone", "invalid")])
def test_setup_refusals_write_nothing_and_say_why(world, how, reason):
    secret, over = SECRET, {}
    if how == "proof":
        secret = "not-the-secret-at-all"
    elif how == "nosecret":
        os.unlink(world.ack / "bootstrap.secret")
    elif how == "empty":
        (world.ack / "bootstrap.secret").write_text("\n")
    elif how == "exists":
        (world.ack / "auth.json").write_text('{"keep": "me"}')
    elif how == "pw":
        over = {"pw": pw_obj(iters=100)}                                                              # weaker than the floor the website enforces too
    elif how == "recovery_alone":
        over = {"recovery": ["ab" * 16]}                                                              # recovery codes without an authenticator
    req = setup_req(secret, **over)
    if how == "totp":
        req = setup_req(totp_secret="A" * 26 + "==", recovery=[])
        req["totp"] = {"enc": "00" * 20}                                                              # not what the website sends: unwrap gives garbage
        req["proof"] = hmac.new(acks_auth.bootstrap_key(SECRET), acks_auth.canonical({k: v for k, v in req.items() if k != "proof"}), hashlib.sha256).hexdigest()
    put(world, req)
    rep = acks.process_inbox(NOW)
    assert rep.handled == [] and rep.rejected == [reason]
    assert result(world, req["rid"]) == {"ok": False, "reason": reason, "kind": "auth_setup", "at": NOW}
    if how == "exists":
        assert (world.ack / "auth.json").read_text() == '{"keep": "me"}'                              # untouched
    else:
        assert not (world.ack / "auth.json").exists()


def test_a_second_setup_cannot_replace_the_first(world):
    put(world, setup_req())
    acks.process_inbox(NOW)
    before = (world.ack / "auth.json").read_bytes()
    put(world, setup_req())                                                                           # even with the right proof: the site is already set up
    assert acks.process_inbox(NOW + 1).rejected == ["exists"] and (world.ack / "auth.json").read_bytes() == before


def test_a_request_the_envelope_refuses_never_reaches_the_handler(world):
    stale = setup_req(ts=int(NOW) - 3600)
    put(world, stale)
    unsigned = setup_req()
    put(world, unsigned, signed=False)
    extra = setup_req(note="x")                                                                       # a key the kind did not declare
    put(world, extra)
    rep = acks.process_inbox(NOW)
    assert rep.handled == [] and sorted(rep.rejected) == ["bad_signature", "schema", "stale"] and not (world.ack / "auth.json").exists()


def test_burn_removes_exactly_one_recovery_hash_durably_and_refuses_the_rest(world):
    totp = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    hashes = [f"{i:02x}" * 16 for i in range(1, 5)]
    put(world, setup_req(totp_secret=totp, recovery=hashes))
    acks.process_inbox(NOW)
    base = {"v": 1, "kind": "auth_change", "source": "web", "ts": int(NOW)}
    r1 = {**base, "rid": "11111111", "op": "burn", "h": hashes[1]}
    put(world, r1)
    assert acks.process_inbox(NOW).handled == ["auth_change"]
    doc = json.loads((world.ack / "auth.json").read_text())
    assert doc["recovery"] == [hashes[0], hashes[2], hashes[3]] and doc["totp"]["secret"] == totp and (world.ack / "auth.json").stat().st_mode & 0o777 == 0o640
    put(world, {**r1, "rid": "22222222"})                                                             # the same code twice (a new request, not a replay)
    put(world, {**base, "rid": "33333333", "op": "passphrase", "h": hashes[0]})                       # no other operation exists
    put(world, {**base, "rid": "44444444", "op": "burn", "h": "zz"})
    rep = acks.process_inbox(NOW)
    assert sorted(rep.rejected) == ["invalid", "invalid", "unknown_hash"] and json.loads((world.ack / "auth.json").read_text())["recovery"] == doc["recovery"]


def test_a_handled_request_syncs_the_directory_after_the_rename(world, monkeypatch):
    """write_atomic fsyncs the FILE; the rename that publishes it survives a power cut only once ack/ itself is synced, so a burned recovery code
    cannot come back after a crash. Observed through os.fsync: at the moment the directory is synced, auth.json already holds the new list."""
    totp = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    hashes = ["ab" * 16, "cd" * 16]
    put(world, setup_req(totp_secret=totp, recovery=hashes))
    acks.process_inbox(NOW)
    synced, real = [], os.fsync

    def spy(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) and os.path.samestat(st, os.stat(world.ack)):
            synced.append(json.loads((world.ack / "auth.json").read_text())["recovery"])
        return real(fd)
    monkeypatch.setattr(acks_auth.os, "fsync", spy)
    put(world, {"v": 1, "kind": "auth_change", "source": "web", "ts": int(NOW), "rid": "77777777", "op": "burn", "h": hashes[0]})
    assert acks.process_inbox(NOW).handled == ["auth_change"]
    assert synced and synced[-1] == [hashes[1]]                                                       # the burned list was in place when the directory was flushed
    synced.clear()
    put(world, {"v": 1, "kind": "auth_change", "source": "web", "ts": int(NOW), "rid": "88888888", "op": "burn", "h": "ee" * 16})
    assert acks.process_inbox(NOW).rejected == ["unknown_hash"] and synced                            # a refusal writes auth_result.json: flushed as well
    acks_auth.fsync_dir(world.tmp / "no" / "such" / "dir")                                            # best effort: a missing directory is not an error


def test_burn_without_a_setup_says_no_auth(world):
    put(world, {"v": 1, "kind": "auth_change", "source": "web", "ts": int(NOW), "rid": "55555555", "op": "burn", "h": "ab" * 16})
    assert acks.process_inbox(NOW).rejected == ["no_auth"] and result(world, "55555555")["reason"] == "no_auth"


def test_auth_state_reads_the_file_the_way_the_site_would(world, tmp_path):
    assert acks.auth_state(world.ack) == ("absent", "")
    p = world.ack / "auth.json"
    p.write_text("not json")
    assert acks.auth_state(world.ack)[0] == "unusable"
    p.write_text(json.dumps({"v": 1, "pw": {"alg": "pbkdf2-sha256", "iter": 10, "salt": "ab" * 16, "hash": "cd" * 32}}))
    assert acks.auth_state(world.ack)[0] == "unusable" and "refuse" in acks.auth_state(world.ack)[1]
    good = {"v": 1, "pw": pw_obj()}
    p.write_text(json.dumps(good))
    os.chmod(p, 0o600)                                                                                # group has no read: the site (gid 10001) could not read it
    st, why = acks.auth_state(world.ack)
    assert st == "unreadable" and "0o" not in why and "600" in why
    os.chmod(p, 0o644)
    assert acks.auth_state(world.ack) == ("ok", "")
    p.unlink()
    p.symlink_to(tmp_path)                                                                            # never follows a symlink
    assert acks.auth_state(world.ack)[0] == "unusable"
    assert acks.auth_state(tmp_path / "no" / "such")[0] == "absent"


# =========================================================================== quarantine hygiene
def test_a_refused_auth_request_keeps_no_secret(world):
    totp = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    req = setup_req(totp_secret=totp, recovery=["ef" * 16], ts=int(NOW) - 3600)                       # stale: refused before authentication, but quarantined
    req["totp"]["secret"] = totp                                                                      # (an older client that sent the secret in clear)
    put(world, req)
    assert acks.process_inbox(NOW).rejected == ["stale"]
    (q,) = (world.inbox / "rejected").iterdir()
    text = q.read_bytes()
    for leak in (totp.encode(), b"efefef", req["proof"][:12].encode(), req["pw"]["hash"][:12].encode(), req["pw"]["salt"][:12].encode()):
        assert leak not in text, leak
    kept = json.loads(text)
    assert kept["kind"] == "auth_setup" and kept["rid"] == req["rid"] and kept["pw"] == "<redacted>" and q.stat().st_mode & 0o777 == 0o600


def test_a_base32_run_is_redacted_in_any_quarantined_file_and_hex_signatures_are_cut(world):
    secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    kept = acks._kept(json.dumps({"kind": "ack", "note": f"my authenticator is {secret} ok", "sig": "ab" * 32, "fp": "0" * 16}).encode())
    assert secret.encode() not in kept and b"<redacted>" in kept and b"abababab.." in kept and b"ab" * 32 not in kept
    assert acks._kept(b"x" * 25 + b"AAAA") == b"x" * 25 + b"AAAA"                                       # shorter than a secret (26+): not touched
    assert acks._kept(b"{broken " + secret.encode()) == b"{broken <redacted>"                         # not even JSON: the regex fallback
    assert acks._kept(b"\xff\xfe" + secret.encode()).count(secret.encode()) == 0                      # not even text


# =========================================================================== ack/ is group-only, like install.sh
def test_ack_init_makes_ack_0750_and_closes_an_old_0755_once_it_belongs_to_the_group(world, monkeypatch):
    os.chmod(world.ack, 0o755)
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((Path(p).name, u, g)))
    done = acks.init_dirs(10001)
    assert ("ack", 0, 10001) in calls and ("inbox", 0, 10001) in calls and ("web.key", 0, 10001) in calls
    assert world.ack.stat().st_mode & 0o777 == 0o750 and "ack/ mode 0755 -> 0750 (group only)" in done
    assert acks.init_dirs(10001) == [] or "ack/ mode" not in " ".join(acks.init_dirs(10001))          # a second run changes nothing
    fresh = world.tmp / "fresh"
    (fresh / "ack").parent.mkdir()
    monkeypatch.setattr(core, "STATE_DIR", fresh)
    acks.init_dirs(10001)
    assert (fresh / "ack").stat().st_mode & 0o777 == 0o750 and (fresh / "ack" / "inbox").stat().st_mode & 0o7777 == 0o1730


def test_ack_init_without_a_group_leaves_an_existing_mode_alone(world):
    os.chmod(world.ack, 0o755)
    acks.init_dirs()
    assert world.ack.stat().st_mode & 0o777 == 0o755                                                  # nobody to hand it to: not closed on its own


# =========================================================================== `homelab-maint web bootstrap`
@pytest.fixture
def root_tty(monkeypatch):
    monkeypatch.setattr(acks, "_is_root", lambda: True)
    monkeypatch.setattr(acks, "_stdout_is_tty", lambda: True)


def test_bootstrap_shows_the_secret_once_on_a_root_terminal_and_logs_that_not_what(world, root_tty, capsys):
    assert cli.main(["web", "bootstrap"]) == 0
    out = capsys.readouterr()
    assert out.out.count(SECRET) == 1 and SECRET not in out.err
    blob = b"".join(q.read_bytes() for q in list(world.state.rglob("*")) + list((world.tmp / "log").rglob("*")) if q.is_file() and q.name != "bootstrap.secret")
    assert SECRET.encode() not in blob and b"bootstrap-shown" in blob                                  # acks.jsonl says it was shown
    assert (world.ack / "bootstrap.secret").read_text().strip() == SECRET                              # the file stays: setup is closed by auth.json


def test_bootstrap_refuses_a_non_root_caller(world, monkeypatch, capsys):
    monkeypatch.setattr(acks, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(acks, "_is_root", lambda: False)
    assert acks.web_main(["bootstrap"]) == 1
    cap = capsys.readouterr()
    assert SECRET not in cap.out + cap.err and "run as root" in cap.err


def test_bootstrap_refuses_once_setup_is_complete_whatever_state_the_file_is_in(world, root_tty, capsys):
    (world.ack / "auth.json").write_text("garbage")                                                   # even an unusable file means setup is not first-run any more
    assert acks.web_main(["bootstrap"]) == 1
    cap = capsys.readouterr()
    assert SECRET not in cap.out + cap.err and "already complete" in cap.err and "unusable" in cap.err
    (world.ack / "auth.json").write_text(json.dumps({"v": 1, "pw": pw_obj()}))
    os.chmod(world.ack / "auth.json", 0o644)
    assert acks.web_main(["bootstrap"]) == 1 and SECRET not in capsys.readouterr().out


def test_bootstrap_never_prints_into_a_pipe_or_a_file(world, monkeypatch, capsys):
    monkeypatch.setattr(acks, "_is_root", lambda: True)
    monkeypatch.setattr(acks, "_stdout_is_tty", lambda: False)
    assert acks.web_main(["bootstrap"]) == 1
    cap = capsys.readouterr()
    assert SECRET not in cap.out + cap.err and "not a terminal" in cap.err


@pytest.mark.parametrize("how", ["missing", "empty", "symlink", "foreign_owner", "dir"])
def test_bootstrap_refuses_an_unusable_secret_file(world, root_tty, capsys, monkeypatch, how):
    p = world.ack / "bootstrap.secret"
    if how == "missing":
        p.unlink()
    elif how == "empty":
        p.write_text("  \n")
    elif how == "symlink":
        other = world.tmp / "elsewhere"
        other.write_text("a-secret-from-elsewhere\n")
        p.unlink()
        p.symlink_to(other)
    elif how == "dir":
        p.unlink()
        p.mkdir()
    else:
        monkeypatch.setattr(os, "getuid", lambda: 4242)                                                # the file is owned by neither root nor the caller
    assert acks.web_main(["bootstrap"]) == 1
    cap = capsys.readouterr()
    assert "elsewhere" not in cap.out + cap.err and SECRET not in cap.out and cap.err.startswith("error:")


def test_bootstrap_usage_and_dispatch(capsys):
    assert acks.web_main([]) == 2 and acks.web_main(["bootstrap", "--force"]) == 2 and acks.web_main(["login"]) == 2
    assert "web bootstrap" in capsys.readouterr().err
    assert cli.PASS["web"] == ("acks", "web_main", ())
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert re.search(r"^\s+web\s", capsys.readouterr().out, re.M)                                      # listed in --help


# =========================================================================== end to end against the REAL web/app.py
def totp_code(secret_b32: str, t: float) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(t // 30)), "sha1").digest()
    o = mac[19] & 15
    return str((int.from_bytes(mac[o:o + 4], "big") & 0x7FFFFFFF) % 10 ** 6).zfill(6)


@needs_web
@pytest.mark.parametrize("totp", [False, True])
def test_end_to_end_setup_through_the_site_the_production_tick_and_a_working_login(world, webapp, monkeypatch, totp):
    """The owner's first run: the site (real web/app.py, real PBKDF2 iterations) queues auth_setup; the production tick (cli._ack_tick, a fresh
    process: nothing registered by any helper) writes auth.json; the same site then accepts the passphrase (+ the authenticator) and a
    recovery code, which the tick burns durably."""
    monkeypatch.setattr(acks_auth, "MIN_ITER", webapp.PBKDF2_ITER)
    monkeypatch.setattr(acks, "_now", lambda now=None: time.time() if now is None else now)           # the site stamps requests with the real clock
    site = wh.Site(webapp, world.tmp, world.state, world.ack)
    try:
        st, state = site.req("GET", "/api/auth/state")
        assert st == 200 and state["setup_required"] is True
        st, r = site.req("POST", "/api/auth/setup", {"bootstrap": SECRET, "passphrase": PASS, "totp": totp})
        assert st == 202 and r["state"] == "received"
        assert len(list(world.inbox.glob("*.json"))) == 1 and not (world.ack / "auth.json").exists()
        cli._ack_tick()                                                                               # <- the production path, one minute later
        assert not list(world.inbox.glob("*.json")) and (world.ack / "auth.json").stat().st_mode & 0o777 == 0o640
        st, state = site.req("GET", f"/api/auth/state?rid={r['rid']}")
        assert state["setup_required"] is False and state["result"] == {"ok": True, "reason": "ok", "kind": "auth_setup"}
        assert acks.auth_state(world.ack) == ("ok", "")
        site.jar.clear()
        assert site.req("POST", "/api/auth/login", {"passphrase": "wrong passphrase here"})[0] == 401
        if not totp:
            st, body = site.req("POST", "/api/auth/login", {"passphrase": PASS})
            assert st == 200 and body["authenticated"] is True
            return
        secret = r["totp"]["secret"]                                                                  # the secret the owner saw: it reached auth.json through the wrap
        assert json.loads((world.ack / "auth.json").read_text())["totp"]["secret"] == secret
        assert site.req("POST", "/api/auth/login", {"passphrase": PASS})[0] == 401                    # the passphrase alone is not enough now
        st, body = site.req("POST", "/api/auth/login", {"passphrase": PASS, "totp": totp_code(secret, time.time())})
        assert st == 200 and body["authenticated"] is True
        site.jar.clear()
        code = r["recovery"][0]
        st, body = site.req("POST", "/api/auth/login", {"passphrase": PASS, "totp": code})
        assert st == 200 and body["recovery_used"] is True and body["recovery_left"] == 7
        h = webapp.recovery_hash(webapp.recovery_norm(code))
        assert h in json.loads((world.ack / "auth.json").read_text())["recovery"]                      # burned only in the site's memory so far
        cli._ack_tick()
        assert h not in json.loads((world.ack / "auth.json").read_text())["recovery"]                  # the runner made it durable
        assert len(json.loads((world.ack / "auth.json").read_text())["recovery"]) == 7
    finally:
        site.stop()
