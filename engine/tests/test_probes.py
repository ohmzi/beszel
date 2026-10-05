"""Tests for probes.py (the monitoring engine) and the shipped etc/probes.toml.

Everything runs against tmp dirs, a fake HTTP server on 127.0.0.1 and a scripted `sh` for docker/systemctl/commands. Nothing
here touches the network beyond loopback, the real Docker, systemd, Kuma or any state directory.
"""
import hashlib
import json
import os
import random
import re
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core, probes
from homelab_maint.probes import FAIL, OK, SKIP, WARN

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_790_900_000.0


# --------------------------------------------------------------------------- helpers
class FakeSh:
    """Replacement for `sh`: first matching (prefix, stdout[, rc]) wins; unknown commands => rc 127."""

    def __init__(self, *table):
        self.table = list(table)
        self.calls: list = []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(cmd)
        for row in self.table:
            if key.startswith(row[0]):
                out = row[1](key) if callable(row[1]) else row[1]
                return subprocess.CompletedProcess(cmd, row[2] if len(row) > 2 else 0, out, "")
        return subprocess.CompletedProcess(cmd, 127, "", "not found")


@pytest.fixture(autouse=True)
def _no_real_commands(monkeypatch):
    """Safety net for the whole module: no test may run a real docker/systemctl/curl, whatever it forgets to mock."""
    monkeypatch.setattr(probes, "sh", FakeSh())
    monkeypatch.setattr(core, "sh", FakeSh())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """STATE/LOG/RUN/CONF in tmp; `sh` knows no command (rc 127) until a test installs a FakeSh."""
    for attr, name in (("STATE_DIR", "state"), ("LOG_DIR", "log"), ("RUN_DIR", "run"), ("CONF_DIR", "conf")):
        d = tmp_path / name
        d.mkdir()
        os.chmod(d, 0o755)                                              # the loader distrusts group/world-writable directories
        monkeypatch.setattr(core, attr, d)
    monkeypatch.setattr(probes, "sh", FakeSh())
    return tmp_path


def mk(**kw) -> probes.Probe:
    raw = {"name": "t", "title": "T", "type": "http", **kw}
    p, errs = probes.build(raw, probes.DEFAULTS)
    assert p is not None, errs
    return p


def look(p: probes.Probe, now: float = NOW):
    """One observation of one probe, the way the engine takes it."""
    return probes.attempt(p, probes.Snapshot([p.target] if p.type == "systemd" else []), now)


def conf_of(*raws: dict, **defaults):
    d, ps, errs = probes.parse({"defaults": defaults, "probe": list(raws)})
    assert not errs, errs
    return d, ps, errs


def raw(name="x", **kw) -> dict:
    return {"name": name, "type": "http", "target": "http://127.0.0.1:1/", **kw}


def go(conf, t, **kw):
    """One forced run with no sleeping and a recording pusher."""
    pushes = kw.setdefault("pusher", None) or []
    rec = (lambda cfg, key, status, msg: pushes.append((key, status, msg)))
    kw["pusher"] = rec
    rep = probes.run_due(t, conf=conf, sleep=lambda s: None, force=kw.pop("force", True), **kw)
    rep.pushes = pushes
    return rep


def script(monkeypatch, typ="http"):
    """Make runners of `typ` return whatever `box["r"]` says: (level, detail) or a callable(p)."""
    box = {"r": (OK, "fine")}

    def fake(p, snap, now):
        r = box["r"]
        return r(p) if callable(r) else r

    monkeypatch.setitem(probes.RUNNERS, typ, fake)
    return box


# --------------------------------------------------------------------------- fake HTTP server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, body=b"", ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def _route(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        path = self.path.split("?")[0]
        if path == "/ok":
            self._reply(200, b"OK all good")
        elif path == "/json":
            self._reply(200, json.dumps({"status": "ok", "ready": True, "n": 5, "nested": {"a": [{"v": 1}, {"v": 2}]},
                                         "name": "svc-1"}).encode(), "application/json")
        elif path == "/notjson":
            self._reply(200, b"hello, not json")
        elif path == "/slow":
            time.sleep(1.5)
            self._reply(200, b"late")
        elif path == "/redir":
            self._reply(302, b"", headers={"Location": "/ok"})
        elif path == "/big":
            self._reply(200, b"x" * 300_000)
        elif path.startswith("/code/"):
            self._reply(int(path.rsplit("/", 1)[1]), b"code")
        else:
            self._reply(404, b"nope")

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _route


class Srv:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.seen = []
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def seen(self):
        return self.httpd.seen

    def url(self, path="/ok"):
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture(scope="module")
def _srv():
    s = Srv()
    yield s
    s.httpd.shutdown()


@pytest.fixture()
def srv(_srv):
    _srv.seen.clear()
    return _srv


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =========================================================================== config: build / parse / load
def test_build_defaults_and_clamps():
    p = mk(target="http://127.0.0.1:80/", interval_s=1, timeout_s=999, confirm=99, **{"class": "P0"})
    assert (p.interval_s, p.timeout_s, p.confirm) == (10, 30, 10)         # clamped, never trusted blindly
    assert p.severity == "crit" and p.method == "GET" and p.source == "native"
    assert mk(target="http://x/", **{"class": "P3"}).severity == "info"
    assert mk(target="http://x/", **{"class": "P2"}, severity="crit").severity == "crit"


@pytest.mark.parametrize("bad", [
    {"name": "Bad Name"}, {"name": ""}, {"name": "x" * 60}, {"type": "ftp"}, {"class": "P9"}, {"severity": "fatal"},
    {"method": "POST"}, {"method": "DELETE"}, {"source": "magic"}, {"kuma_push_key": "UPPER KEY"},
    {"target": "http://user:pw@127.0.0.1/"}, {"target": "ftp://127.0.0.1/"}, {"target": "127.0.0.1:80"},
    {"expect": {"bogus": 1}}, {"expect": {"status": 200}}, {"expect": {"status": ["abc"]}}, {"expect": {"max_ms": "fast"}},
    {"expect": {"regex": "("}}, {"expect": {"json": [{"path": "a"}]}}, {"expect": {"json": [{"path": "a", "frobnicate": 1}]}},
    {"method": "HEAD", "expect": {"regex": "x"}},
])
def test_build_rejects(bad):
    r = {"name": "t", "type": "http", "target": "http://127.0.0.1/", **bad}
    p, errs = probes.build(r, probes.DEFAULTS)
    assert p is None and errs, bad


@pytest.mark.parametrize("typ,target,ok", [
    ("tcp", "127.0.0.1:80", True), ("tcp", "[::1]:80", True), ("tcp", "nohost", False), ("tcp", "x:y", False),
    ("docker", "@daemon", True), ("docker", "*", True), ("docker", "immich_server", True), ("docker", "bad name;rm", False),
    ("systemd", "docker.service", True), ("systemd", "foo.timer", True), ("systemd", "docker", False), ("systemd", "a b.service", False),
    ("command", ["true"], True), ("command", "true", False), ("command", [], False), ("command", [""], False),
    ("file_age", "/var/x", True), ("file_age", "{STATE}/x", True), ("file_age", "relative/x", False),
])
def test_build_target_shapes(typ, target, ok):
    r = {"name": "t", "type": typ, "target": target, "expect": {"max_age_s": 10} if typ == "file_age" else {}}
    p, errs = probes.build(r, probes.DEFAULTS)
    assert (p is not None) == ok, errs


def test_build_requires_what_each_type_needs():
    assert probes.build({"name": "t", "type": "file_age", "target": "/x"}, probes.DEFAULTS)[0] is None      # no max_age_s
    assert probes.build({"name": "t", "type": "json", "target": "/x"}, probes.DEFAULTS)[0] is None          # no rules, no max_age
    assert probes.build({"name": "t", "type": "json", "target": "/x", "expect": {"max_age_s": 5}}, probes.DEFAULTS)[0]


def test_parse_groups_member_wins_and_expect_merges():
    doc = {"group": [{"group": "G", "type": "http", "class": "P1", "interval_s": 60, "expect": {"status": [200]},
                      "probes": [{"name": "a", "target": "http://127.0.0.1/a"},
                                 {"name": "b", "target": "http://127.0.0.1/b", "class": "P2", "expect": {"regex": "ok"}}]}]}
    d, ps, errs = probes.parse(doc)
    a, b = ps
    assert not errs and (a.group, a.cls, a.interval_s, a.expect) == ("G", "P1", 60, {"status": [200]})
    assert b.cls == "P2" and b.expect == {"status": [200], "regex": "ok"}


def test_parse_duplicates_and_bad_members_reported_not_fatal():
    d, ps, errs = probes.parse({"probe": [raw("a"), raw("a"), {"name": "bad", "type": "nope"}, "junk", raw("ok")]})
    assert [p.name for p in ps] == ["a", "ok"]
    assert any("duplicate" in e for e in errs) and any("bad" in e for e in errs)


def test_parse_defaults_are_overridable_but_unknown_keys_ignored():
    d, ps, errs = probes.parse({"defaults": {"workers": 3, "confirm": 4, "evil": 1}, "probe": [raw("a")]})
    assert d["workers"] == 3 and "evil" not in d and ps[0].confirm == 4


def _write_conf(env, name, text, mode=0o644):
    f = core.CONF_DIR / name
    f.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(f.parent, 0o755)
    f.write_text(text)
    os.chmod(f, mode)
    return f


def test_load_probes_missing_is_no_probes(env):
    d, ps, errs = probes.load_probes()
    assert ps == [] and errs == []


def test_load_probes_merges_probes_d_and_keeps_defaults_from_main(env):
    _write_conf(env, "probes.toml", '[defaults]\nconfirm = 3\n[kuma]\nignore = ["x"]\n[[probe]]\nname="a"\ntype="tcp"\ntarget="127.0.0.1:1"\n')
    _write_conf(env, "probes.d/extra.toml", '[defaults]\nconfirm = 9\n[[probe]]\nname="b"\ntype="tcp"\ntarget="127.0.0.1:2"\n')
    d, ps, errs = probes.load_probes()
    assert [p.name for p in ps] == ["a", "b"] and d["confirm"] == 3 and not errs      # extras cannot change the defaults


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o646])
def test_load_probes_ignores_writable_files(env, mode):
    _write_conf(env, "probes.toml", '[[probe]]\nname="a"\ntype="command"\ntarget=["true"]\n', mode)
    d, ps, errs = probes.load_probes()
    assert ps == [] and any("ignored" in e for e in errs)


def test_load_probes_ignores_files_in_writable_directories(env):
    _write_conf(env, "probes.toml", '[[probe]]\nname="a"\ntype="tcp"\ntarget="127.0.0.1:1"\n')
    os.chmod(core.CONF_DIR, 0o775)
    d, ps, errs = probes.load_probes()
    assert ps == [] and any("directory" in e for e in errs)
    os.chmod(core.CONF_DIR, 0o755)
    assert len(probes.load_probes()[1]) == 1


def test_load_probes_ignores_files_of_other_owners(env, monkeypatch):
    _write_conf(env, "probes.toml", '[[probe]]\nname="a"\ntype="tcp"\ntarget="127.0.0.1:1"\n')
    monkeypatch.setattr(os, "geteuid", lambda: 12345)               # file is ours, we pretend to be someone else
    assert probes.load_probes()[1] == []


def test_load_probes_survives_syntax_errors(env):
    _write_conf(env, "probes.toml", "this is = = not toml\n")
    d, ps, errs = probes.load_probes()
    assert ps == [] and errs and "unreadable" in errs[0]


# =========================================================================== scrubbing
def test_scrub_removes_credentials_tokens_and_control_chars():
    s = probes.scrub("GET http://user:hunter2@host/x?token=abc123&key=zzz&ok=1 failed\n\x00\x1b[31m")
    assert "hunter2" not in s and "abc123" not in s and "zzz" not in s and "\n" not in s and s.isascii()
    assert "ok=1" in s
    blob = "A" * 12 + "b" * 30
    assert blob not in probes.scrub(f"saw {blob} in output") and "[redacted]" in probes.scrub(f"saw {blob} in output")
    words = "word " * 100
    assert len(probes.scrub(words)) == 119 and len(probes.scrub(words, 30)) == 29     # truncated, and stripped of trailing space
    assert probes.scrub("x" * 500) == "[redacted]"                                    # one long opaque blob is a token until proven otherwise
    assert probes.scrub("héllo") == "h?llo"


# =========================================================================== json rules
DOC = {"status": "ok", "n": 5, "flag": True, "name": "svc-1", "ts": "2026-10-02T00:00:00+00:00", "epoch": NOW - 100,
       "nested": {"a": [{"v": 1}, {"v": 2}]}, "checks": {"x": {"status": "ok"}, "y": {"status": "degraded"}, "z": {"status": "down"}}}


def test_jpath_wildcards_indexes_and_missing():
    assert probes.jpath(DOC, "status") == [("status", "ok")]
    assert [v for _k, v in probes.jpath(DOC, "nested.a.*.v")] == [1, 2]
    assert probes.jpath(DOC, "nested.a.1.v") == [("nested.a.1.v", 2)]
    assert [k for k, _v in probes.jpath(DOC, "checks.*.status")] == ["checks.x.status", "checks.y.status", "checks.z.status"]
    assert probes.jpath(DOC, "nested.a.9.v") == [] and probes.jpath(DOC, "nope.deeper") == []


@pytest.mark.parametrize("rule,want", [
    ({"path": "status", "equals": "ok"}, OK), ({"path": "status", "equals": "bad"}, FAIL),
    ({"path": "flag", "equals": True}, OK), ({"path": "n", "equals": True}, FAIL), ({"path": "n", "equals": 5}, OK),
    ({"path": "status", "in": ["ok", "fine"]}, OK), ({"path": "status", "in": ["x"]}, FAIL),
    ({"path": "status", "in": ["x"], "warn_in": ["ok"]}, WARN), ({"path": "status", "not_in": ["ok"]}, FAIL),
    ({"path": "name", "regex": r"^svc-\d$"}, OK), ({"path": "name", "regex": r"^nope"}, FAIL),
    ({"path": "n", "min": 5}, OK), ({"path": "n", "min": 6}, FAIL), ({"path": "n", "max": 5}, OK), ({"path": "n", "max": 4}, FAIL),
    ({"path": "status", "min": 1}, FAIL),                                   # a non-number can never satisfy min/max
    ({"path": "status", "exists": True}, OK), ({"path": "nope", "exists": True}, FAIL), ({"path": "nope", "exists": False}, OK),
    ({"path": "status", "exists": False}, FAIL),
    ({"path": "epoch", "age_max_s": 200}, OK), ({"path": "epoch", "age_max_s": 50}, FAIL),
    ({"path": "epoch", "age_max_s": 500, "age_warn_s": 50}, WARN), ({"path": "ts", "age_max_s": 86400 * 30}, OK),
    ({"path": "status", "age_max_s": 5}, FAIL),                             # not a timestamp
    ({"path": "nested.a.*.v", "max": 1}, FAIL),                             # every item must satisfy the rule
])
def test_eval_rules_ops(rule, want):
    lvl, why = probes.eval_rules(DOC, [rule], NOW)
    assert lvl == want, (rule, why)
    assert (why != "") == (want != OK)


def test_eval_rules_worst_wins_and_names_the_path():
    lvl, why = probes.eval_rules(DOC, [{"path": "checks.*.status", "in": ["ok"], "warn_in": ["degraded"]}], NOW)
    assert lvl == FAIL and why.startswith("checks.z.status")                # down beats degraded; the offender is named
    lvl, why = probes.eval_rules(DOC, [{"path": "checks.x.status", "in": ["ok"]}, {"path": "checks.y.status", "in": ["ok"], "warn_in": ["degraded"]}], NOW)
    assert lvl == WARN and "checks.y.status" in why


# =========================================================================== http probes against the fake server
def test_http_ok_and_get_only(srv):
    p = mk(target=srv.url("/ok"))
    lvl, detail, ms = look(p)
    assert lvl == OK and detail.startswith("HTTP 200") and ms >= 0
    assert [m for m, _p, _h in srv.seen] == ["GET"]


def test_http_sends_no_credentials_cookies_or_body(srv, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")                 # a proxy in the environment must be ignored
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert look(mk(target=srv.url("/ok")))[0] == OK
    hdrs = {k.lower() for k in srv.seen[0][2]}
    assert hdrs <= {"host", "user-agent", "accept-encoding", "connection"}, hdrs
    assert srv.seen[0][2]["User-Agent"].startswith("homelab-maint-probe")


def test_http_head_method(srv):
    assert look(mk(target=srv.url("/ok"), method="HEAD"))[0] == OK
    assert [m for m, _p, _h in srv.seen] == ["HEAD"]


@pytest.mark.parametrize("path,expect,want", [
    ("/code/200", {}, OK), ("/code/204", {}, OK), ("/code/404", {}, FAIL), ("/code/503", {}, FAIL),
    ("/code/401", {"status": ["200-299", 401]}, OK), ("/code/301", {"status": ["300-399"]}, OK), ("/code/200", {"status": [201]}, FAIL),
    ("/ok", {"regex": "all good"}, OK), ("/ok", {"regex": "^nope"}, FAIL), ("/ok", {"not_regex": "good"}, FAIL),
    ("/ok", {"not_regex": "zebra"}, OK),
    ("/json", {"json": [{"path": "status", "equals": "ok"}]}, OK), ("/json", {"json": [{"path": "status", "equals": "bad"}]}, FAIL),
    ("/json", {"json": [{"path": "ready", "equals": True}, {"path": "nested.a.*.v", "min": 1}]}, OK),
    ("/notjson", {"json": [{"path": "status", "equals": "ok"}]}, FAIL),
])
def test_http_expectations(srv, path, expect, want):
    assert look(mk(target=srv.url(path), expect=expect))[0] == want


def test_http_redirect_is_an_answer_never_followed(srv):
    lvl, detail, _ms = look(mk(target=srv.url("/redir")))
    assert lvl == FAIL and "302" in detail
    assert [p for _m, p, _h in srv.seen] == ["/redir"]                      # /ok was never requested (SSRF)
    assert look(mk(target=srv.url("/redir"), expect={"status": ["300-399"]}))[0] == OK


def test_http_timeout_and_refused_fail_closed(srv):
    lvl, detail, ms = look(mk(target=srv.url("/slow"), timeout_s=0.5))
    assert lvl == FAIL and "timed out" in detail and ms < 1400
    lvl, detail, _ = look(mk(target=f"http://127.0.0.1:{closed_port()}/"))
    assert lvl == FAIL and detail


def test_http_slow_answer_is_a_warning_not_a_failure(srv):
    lvl, detail, _ = look(mk(target=srv.url("/slow"), timeout_s=5, expect={"max_ms": 200}))
    assert lvl == WARN and "slow" in detail


def test_http_big_body_is_capped(srv):
    t0 = time.monotonic()
    assert look(mk(target=srv.url("/big")))[0] == OK and time.monotonic() - t0 < 2


def test_http_public_addresses_refused_unless_external():
    p = mk(target="http://8.8.8.8/")
    lvl, detail, _ = look(p)
    assert lvl == FAIL and "public address refused" in detail               # refused BEFORE any packet is sent
    probes._host_ok("8.8.8.8", True)
    with pytest.raises(OSError):
        probes._host_ok("8.8.8.8", False)
    for private in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.9", "100.101.5.51", "::1"):
        probes._host_ok(private, False)


def test_http_unresolvable_host_fails_closed():
    lvl, detail, _ = look(mk(target="http://no-such-host.invalid/"))
    assert lvl == FAIL and "resolve" in detail


def test_http_details_never_leak_query_or_userinfo(srv):
    lvl, detail, _ = look(mk(target=srv.url("/code/500") + "?token=SECRETVALUE&x=1"))
    assert lvl == FAIL and "SECRETVALUE" not in detail


def test_tcp(srv):
    assert look(mk(type="tcp", target=f"127.0.0.1:{srv.port}"))[0] == OK
    assert look(mk(type="tcp", target=f"127.0.0.1:{closed_port()}"))[0] == FAIL
    assert look(mk(type="tcp", target="8.8.8.8:53"))[0] == FAIL            # public: refused without connecting


def test_json_probe_over_http(srv):
    p = mk(type="json", target=srv.url("/json"), expect={"json": [{"path": "n", "min": 1}]})
    assert look(p)[0] == OK
    assert look(mk(type="json", target=srv.url("/json"), expect={"json": [{"path": "n", "min": 9}]}))[0] == FAIL


# =========================================================================== docker / systemd / command (scripted sh)
PS = (
    "immich_server|running|Up 2 hours (healthy)\n"
    "kavita|running|Up 7 hours (healthy)\n"
    "wedged|running|Up 4 hours (unhealthy)\n"
    "booting|running|Up 3 seconds (health: starting)\n"
    "Radarr|running|Up 7 days\n"
    "comfyui|exited|Exited (137) 9 hours ago\n"
    "maintenance-web-test-81|running|Up 4 hours (unhealthy)\n"
    "weird, name|running|Up 1 hour\n"
)


def docker_sh(out=PS, rc=0):
    return FakeSh(("docker ps -a", out, rc))


@pytest.mark.parametrize("target,want,word", [
    ("immich_server", OK, "healthy"), ("Radarr", OK, "running"), ("wedged", FAIL, "unhealthy"), ("booting", WARN, "starting"),
    ("comfyui", FAIL, "exited"), ("ghost", FAIL, "no such container"), ("@daemon", OK, "answers"),
])
def test_docker_probe(env, monkeypatch, target, want, word):
    monkeypatch.setattr(probes, "sh", docker_sh())
    lvl, detail, _ = look(mk(type="docker", target=target))
    assert lvl == want and word in detail


def test_docker_health_ignore_and_state_expectation(env, monkeypatch):
    monkeypatch.setattr(probes, "sh", docker_sh())
    assert look(mk(type="docker", target="wedged", expect={"health": "ignore"}))[0] == OK
    assert look(mk(type="docker", target="comfyui", expect={"state": "exited"}))[0] == OK


def test_docker_fleet_names_offenders_and_honours_except(env, monkeypatch):
    monkeypatch.setattr(probes, "sh", docker_sh())
    lvl, detail, _ = look(mk(type="docker", target="*"))
    assert lvl == FAIL and "exited: comfyui" in detail and "unhealthy" in detail and "wedged" in detail
    ex = {"except": ["^comfyui$", r"^maintenance-web-test-\d+$", "^wedged$", "^booting$"]}
    lvl, detail, _ = look(mk(type="docker", target="*", expect=ex))
    assert lvl == OK and "containers fine" in detail


def test_docker_unavailable_fails_closed(env, monkeypatch):
    monkeypatch.setattr(probes, "sh", docker_sh("", 127))
    for target in ("@daemon", "x", "*"):
        lvl, detail, _ = look(mk(type="docker", target=target))
        assert lvl == FAIL and "docker" in detail


def test_docker_one_ps_per_pass_for_many_probes(env, monkeypatch):
    clean = "immich_server|running|Up 2 hours (healthy)\nkavita|running|Up 7 hours (healthy)\nRadarr|running|Up 7 days\n"
    fake = docker_sh(clean)
    monkeypatch.setattr(probes, "sh", fake)
    names = ["immich_server", "kavita", "Radarr", "*", "@daemon"]
    go(conf_of(*[{"name": f"c{i}", "type": "docker", "target": t} for i, t in enumerate(names)]), NOW)
    assert sum(1 for c in fake.calls if c[:2] == ["docker", "ps"]) == 1          # nothing to re-check: a single read serves all five


def test_docker_retry_pass_rereads_the_host_instead_of_the_stale_answer(env, monkeypatch):
    seen = iter([PS, PS.replace("wedged|running|Up 4 hours (unhealthy)", "wedged|running|Up 4 hours (healthy)")])
    fake = FakeSh(("docker ps -a", lambda key: next(seen)))
    monkeypatch.setattr(probes, "sh", fake)
    conf = conf_of({"name": "w", "type": "docker", "target": "wedged"}, {"name": "k", "type": "docker", "target": "kavita"}, confirm=1)
    go(conf, NOW)
    assert sum(1 for c in fake.calls if c[:2] == ["docker", "ps"]) == 2          # pass 1 saw it unhealthy, the re-look read docker again
    assert {r["name"]: r["state"] for r in probes.snapshot(NOW, conf)} == {"w": "up", "k": "up"}      # so the blip never counted


def test_docker_ps_is_read_only_argv(env, monkeypatch):
    fake = docker_sh()
    monkeypatch.setattr(probes, "sh", fake)
    look(mk(type="docker", target="kavita"))
    assert fake.calls == [["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"]]


def test_when_running_skips_while_the_container_is_stopped_by_design(env, monkeypatch):
    monkeypatch.setattr(probes, "sh", docker_sh())
    box = script(monkeypatch)
    lvl, detail, _ = look(mk(target="http://127.0.0.1:1/", when_running="comfyui"))
    assert lvl == SKIP and "not running" in detail
    lvl, _d, _ = look(mk(target="http://127.0.0.1:1/", when_running="kavita"))
    assert lvl == OK                                                       # running: the real probe runs


SHOW = (
    "Id=docker.service\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n\n"
    "Id=broken.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n\n"
    "Id=gone.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n\n"
    "Id=check.timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\n\n"
    "Id=oneshot.service\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n"
)


@pytest.mark.parametrize("unit,want,word", [
    ("docker.service", OK, "active"), ("broken.service", FAIL, "failed"), ("gone.service", FAIL, "not found"),
    ("check.timer", OK, "waiting"), ("smartd.service", FAIL, "not found"),
])
def test_systemd_probe(env, monkeypatch, unit, want, word):
    monkeypatch.setattr(probes, "sh", FakeSh(("systemctl show", SHOW)))
    lvl, detail, _ = look(mk(type="systemd", target=unit))
    assert lvl == want and word in detail


def test_systemd_active_states_and_failure_modes(env, monkeypatch):
    monkeypatch.setattr(probes, "sh", FakeSh(("systemctl show", SHOW)))
    assert look(mk(type="systemd", target="oneshot.service", expect={"active": ["active", "inactive"]}))[0] == OK
    assert look(mk(type="systemd", target="oneshot.service"))[0] == FAIL
    monkeypatch.setattr(probes, "sh", FakeSh(("systemctl show", "", 1)))
    lvl, detail, _ = look(mk(type="systemd", target="docker.service"))
    assert lvl == FAIL and "systemctl" in detail


def test_systemd_one_call_per_pass_for_many_units_and_only_show(env, monkeypatch):
    fake = FakeSh(("systemctl show", SHOW))
    monkeypatch.setattr(probes, "sh", fake)
    names = ["docker.service", "check.timer"]
    rep = go(conf_of(*[{"name": f"u{i}", "type": "systemd", "target": t} for i, t in enumerate(names)]), NOW)
    assert len(fake.calls) == 1 and fake.calls[0][:2] == ["systemctl", "show"] and len(rep.ran) == 2
    assert fake.calls[0].count("docker.service") == 1
    fake.calls.clear()                                                           # one broken unit: a second pass, one more read
    rep = go(conf_of(*[{"name": f"u{i}", "type": "systemd", "target": t} for i, t in enumerate([*names, "broken.service"])]), NOW + 60)
    assert len(fake.calls) == 2 and all(c[:2] == ["systemctl", "show"] for c in fake.calls) and len(rep.ran) == 3


def test_command_probe(env, monkeypatch):
    fake = FakeSh(("echo", "Server replied: pong\n"), ("failer", "boom\n", 3), ("slow", "", 124), ("secretive", "token=abc123 and more\n"))
    monkeypatch.setattr(probes, "sh", fake)
    assert look(mk(type="command", target=["echo", "x"], expect={"regex": "pong"}))[0] == OK
    assert look(mk(type="command", target=["echo", "x"], expect={"regex": "nope"}))[0] == FAIL
    assert look(mk(type="command", target=["echo", "x"], expect={"not_regex": "pong"}))[0] == FAIL
    lvl, detail, _ = look(mk(type="command", target=["failer"]))
    assert lvl == FAIL and "exit 3" in detail and "boom" not in detail       # output is only shown when asked
    assert look(mk(type="command", target=["failer"], expect={"ok_codes": [3]}))[0] == OK
    assert "timed out" in look(mk(type="command", target=["slow"]))[2 - 1]
    assert "not found" in look(mk(type="command", target=["missing"]))[1]
    lvl, detail, _ = look(mk(type="command", target=["secretive"], expect={"show_output": True}))
    assert "abc123" not in detail
    assert all(isinstance(c, list) for c in fake.calls)                     # argv, never a shell string


def test_command_needs_root_is_skipped_for_other_users(env, monkeypatch):
    fake = FakeSh(("echo", "pong"))
    monkeypatch.setattr(probes, "sh", fake)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert look(mk(type="command", target=["echo"], needs_root=True))[0] == SKIP and fake.calls == []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert look(mk(type="command", target=["echo"], needs_root=True))[0] == OK


# =========================================================================== file probes
def touch(path: Path, age_s: float, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


def test_file_age(env):
    f = touch(env / "a.txt", 100)
    ex = {"max_age_s": 600, "warn_age_s": 300}
    assert look(mk(type="file_age", target=str(f), expect=ex))[0] == OK
    touch(f, 400)
    assert look(mk(type="file_age", target=str(f), expect=ex))[0] == WARN
    touch(f, 900)
    lvl, detail, _ = look(mk(type="file_age", target=str(f), expect=ex))
    assert lvl == FAIL and "900" in detail
    assert look(mk(type="file_age", target=str(env / "missing"), expect=ex))[0] == FAIL
    assert look(mk(type="file_age", target=str(touch(env / "e", 1, "")), expect={"max_age_s": 9, "min_size": 1}))[0] == FAIL


def test_file_age_glob_picks_newest_and_expands_dirs(env):
    touch(env / "state" / "logs" / "a.log", 5000)
    touch(env / "state" / "logs" / "b.log", 50)
    assert look(mk(type="file_age", target="{STATE}/logs/*.log", expect={"max_age_s": 100}))[0] == OK
    assert look(mk(type="file_age", target="{STATE}/nothing/*.log", expect={"max_age_s": 100}))[0] == FAIL


def test_file_age_epoch_content(env):
    f = touch(env / "beat", 0, text=f"{NOW - 30}\n")                         # mtime says fresh; the content is what counts
    assert look(mk(type="file_age", target=str(f), expect={"max_age_s": 60, "epoch_content": True}))[0] == OK
    f.write_text(f"{NOW - 500}")
    assert look(mk(type="file_age", target=str(f), expect={"max_age_s": 60, "epoch_content": True}))[0] == FAIL
    f.write_text("not a number")
    assert look(mk(type="file_age", target=str(f), expect={"max_age_s": 60, "epoch_content": True}))[0] == FAIL


WATCHDOG = {"version": 2, "source": "watchdog",
            "run": {"last_run_at": NOW - 120, "killed_streak": 0, "inflight_at": None},
            "checks": {"gateway": {"status": "ok", "last_detail": "service active"},
                       "backlog": {"status": "degraded", "last_detail": "2 LOG results undelivered, oldest 20 min"},
                       "ticker": {"status": "down", "last_detail": "default: last beat 9 min ago"}}}


def test_json_file_probe_on_the_watchdog_state_shape(env):
    f = touch(env / "wd.json", 10, json.dumps(WATCHDOG))
    one = lambda key: mk(type="json", target=str(f), expect={"max_age_s": 1200, "detail_path": f"checks.{key}.last_detail",
                                                              "json": [{"path": f"checks.{key}.status", "in": ["ok"], "warn_in": ["degraded"]}]})
    assert look(one("gateway"))[0] == OK
    lvl, detail, _ = look(one("backlog"))
    assert lvl == WARN and "undelivered" in detail                          # the check's own words ride along
    lvl, detail, _ = look(one("ticker"))
    assert lvl == FAIL and "last beat" in detail
    assert look(one("nope"))[0] == FAIL                                      # a missing check is a failure, not silence
    alive = mk(type="json", target=str(f), expect={"max_age_s": 1200, "json": [{"path": "run.last_run_at", "age_max_s": 1200, "age_warn_s": 60},
                                                                                {"path": "run.killed_streak", "max": 1}]})
    assert look(alive)[0] == WARN                                            # last run 120 s ago > warn 60


def test_json_file_probe_failure_modes(env):
    rules = {"json": [{"path": "a", "exists": True}]}
    assert look(mk(type="json", target=str(env / "missing.json"), expect=rules))[0] == FAIL
    assert look(mk(type="json", target=str(touch(env / "bad.json", 1, "{nope")), expect=rules))[0] == FAIL
    assert look(mk(type="json", target=str(touch(env / "old.json", 5000, '{"a": 1}')), expect={**rules, "max_age_s": 100}))[0] == FAIL
    big = env / "big.json"
    big.write_text('{"a": "' + "x" * (1 << 21) + '"}')
    assert "too large" in look(mk(type="json", target=str(big), expect=rules))[1]


# ---- review fix: ONE bounded reader for every file a probe names (the engine may be root, the files are user-writable)
def _bounded(fn, *a, timeout=5.0):
    """Run fn on a daemon thread: a regression that blocks (FIFO) fails the test instead of freezing the suite."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(r=fn(*a)), daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the probe blocked"
    return box["r"]


JSON_RULES = {"json": [{"path": "a", "exists": True}]}


def test_read_small_returns_regular_files_and_bounds_the_read_even_when_st_size_lies(env, monkeypatch):
    f = touch(env / "r.txt", 0, "x" * 100)
    data, st = probes.read_small(str(f), limit=100)
    assert data == b"x" * 100 and st.st_size == 100
    with pytest.raises(probes.Unsafe, match="too large"):
        probes.read_small(str(f), limit=99)
    assert probes.read_small(str(f), limit=1, content=False)[0] == b""                  # vet-only mode never reads
    real = os.fstat

    def lying(fd):                                                                       # like /proc: a regular file that claims size 0
        s = real(fd)
        return os.stat_result((s.st_mode, s.st_ino, s.st_dev, s.st_nlink, s.st_uid, s.st_gid, 0, int(s.st_atime), int(s.st_mtime), int(s.st_ctime)))

    monkeypatch.setattr(os, "fstat", lying)
    with pytest.raises(probes.Unsafe, match="too large"):
        probes.read_small(str(f), limit=50)                                              # the read itself stops at limit + 1


def test_file_probes_refuse_symlinks_fifos_directories_and_say_why(env):
    real = touch(env / "real.json", 1, '{"a": 1}')
    link = env / "link.json"
    link.symlink_to(real)
    assert look(mk(type="json", target=str(real), expect=JSON_RULES))[0] == OK
    assert look(mk(type="json", target=str(link), expect=JSON_RULES))[:2] == (FAIL, "unsafe file: symlink refused")
    assert look(mk(type="file_age", target=str(link), expect={"max_age_s": 600}))[:2] == (FAIL, "unsafe file: symlink refused")   # mtime mode too
    fifo = env / "fifo.json"
    os.mkfifo(fifo)
    for typ, ex in (("json", JSON_RULES), ("file_age", {"max_age_s": 600, "epoch_content": True}), ("file_age", {"max_age_s": 600})):
        assert _bounded(look, mk(type=typ, target=str(fifo), expect=ex))[:2] == (FAIL, "unsafe file: not a regular file")
    assert look(mk(type="json", target=str(env), expect=JSON_RULES))[:2] == (FAIL, "unsafe file: not a regular file")
    assert look(mk(type="json", target=str(env / "absent.json"), expect=JSON_RULES))[1].startswith("unreadable")


def test_dev_zero_symlink_is_refused_without_reading_it_under_a_memory_cap(env):
    """Run in a child with a 700 MiB address-space cap: a regression (reading through the symlink) dies here, not on the test host."""
    link = env / "z.json"
    link.symlink_to("/dev/zero")
    code = textwrap.dedent(f"""
        import resource, sys
        resource.setrlimit(resource.RLIMIT_AS, (700 << 20, 700 << 20))
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from homelab_maint import probes
        for typ, ex in (("json", {JSON_RULES!r}), ("file_age", {{"max_age_s": 60, "epoch_content": True}})):
            p, errs = probes.build({{"name": "t", "type": typ, "target": {str(link)!r}, "expect": ex}}, probes.DEFAULTS)
            print(probes.attempt(p, probes.Snapshot([]), 1e9)[:2])
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=os.environ)
    assert r.stdout.splitlines() == ["(2, 'unsafe file: symlink refused')"] * 2, (r.stdout, r.stderr[-300:])


def test_epoch_content_and_json_errors_never_echo_file_content(env):
    ex = {"max_age_s": 60, "epoch_content": True}
    f = touch(env / "beat", 0, "root:x:0:0:SECRETPAYLOAD:/root:/bin/bash")
    assert look(mk(type="file_age", target=str(f), expect=ex))[:2] == (FAIL, "content is not a number")
    for junk in ("nan", "inf", "-inf", "1e999", "NaN"):                                  # these PARSE as floats and would read as fresh
        f.write_text(junk)
        assert look(mk(type="file_age", target=str(f), expect=ex))[:2] == (FAIL, "content is not a number"), junk
    f.write_bytes(b"\xff\xfe12")
    assert look(mk(type="file_age", target=str(f), expect=ex))[:2] == (FAIL, "content is not a number")
    f.write_text(f"{NOW - 5}")
    assert look(mk(type="file_age", target=str(f), expect=ex))[0] == OK                  # a real heartbeat still passes
    j = touch(env / "j.json", 0, '{"a": SECRETPAYLOAD}')
    assert look(mk(type="json", target=str(j), expect=JSON_RULES))[:2] == (FAIL, "not valid JSON")
    deep = touch(env / "deep.json", 0, "[" * 400_000)                                    # < 1 MiB, RecursionError inside json
    assert look(mk(type="json", target=str(deep), expect=JSON_RULES))[:2] == (FAIL, "not valid JSON")
    f.write_text("SECRETPAYLOAD")                                                         # and nothing reaches the state file
    go(conf_of(raw("e", type="file_age", target=str(f), expect=ex)), NOW)
    assert "SECRETPAYLOAD" not in (core.STATE_DIR / "probes.json").read_text()


def test_file_age_glob_never_matches_a_symlink(env):
    logs = env / "state" / "logs"
    touch(logs / "a.log", 5000)
    fresh = touch(env / "elsewhere" / "fresh.txt", 10)
    (logs / "newest.log").symlink_to(fresh)                                              # a planted link to a fresh file must not steer the probe
    assert probes._newest(str(logs / "*.log")) == str(logs / "a.log")
    assert look(mk(type="file_age", target="{STATE}/logs/*.log", expect={"max_age_s": 100}))[0] == FAIL      # the real newest file is old
    assert probes._newest(str(logs / "*.nothing")) is None


# =========================================================================== state machine
def stepper(**kw):
    p = mk(target="http://x/", **kw)
    return p, {}


def feed(p, s, seq, t0=NOW):
    out = []
    for i, lvl in enumerate(seq):
        out.append(probes.step(s, lvl, t0 + 60 * i, p))
    return out


def test_step_first_sight_ok_is_believed_at_once():
    p, s = stepper()
    assert feed(p, s, [OK]) == [False] and s["lvl"] == OK
    assert probes.state_of(p, s) == "up"


def test_step_first_sight_problem_is_unknown_until_confirmed():
    p, s = stepper(confirm=2)
    assert feed(p, s, [FAIL]) == [False]
    assert s.get("lvl") is None and probes.state_of(p, s) == "unknown"      # never shown as up
    assert feed(p, s, [FAIL]) == [True] and probes.state_of(p, s) == "down"
    assert "tr" not in s                                                    # the initial confirmation is not a flap
    p2, s2 = stepper(confirm=2)
    feed(p2, s2, [FAIL, OK])
    assert s2["lvl"] == OK                                                  # a clean first look settles it at once


def test_step_confirm_and_recover_counts():
    p, s = stepper(confirm=2, recover=3)
    seq = [OK, FAIL, OK, FAIL, FAIL, FAIL, OK, OK, FAIL, OK, OK, OK]
    ch = feed(p, s, seq)
    assert ch == [False, False, False, False, True, False, False, False, False, False, False, True]
    assert s["lvl"] == OK


def test_step_levels_warn_then_fail_and_back():
    p, s = stepper(confirm=2, recover=2)
    feed(p, s, [OK, WARN, WARN])
    assert s["lvl"] == WARN
    feed(p, s, [FAIL])
    assert s["lvl"] == WARN                                                 # one fail is not enough
    feed(p, s, [FAIL])
    assert s["lvl"] == FAIL
    feed(p, s, [WARN, WARN])
    assert s["lvl"] == WARN                                                 # improving needs `recover` runs
    feed(p, s, [OK, OK])
    assert s["lvl"] == OK and len(s["tr"]) == 4


def test_step_alternating_non_ok_levels_confirm_the_mildest_never_green():
    """Review fix: WARN/FAIL alternation (a crash-looping container flips restarting <-> health: starting) used to reset the streak
    on every run, so a probe that was bad on EVERY run stayed 'up' for ever."""
    p, s = stepper(confirm=2)
    ch = feed(p, s, [OK, WARN, FAIL, WARN, FAIL, WARN, FAIL])
    assert s["lvl"] == WARN and ch == [False, False, True, False, False, False, False]      # confirmed at the 2nd bad run, at the mildest level
    assert probes.state_of(p, s) == "warn"
    p2, s2 = stepper(confirm=2)
    feed(p2, s2, [OK] + [WARN, FAIL] * 20)
    assert s2["lvl"] == WARN                                                                # still not green after 40 bad runs
    p3, s3 = stepper(confirm=3)
    feed(p3, s3, [OK, FAIL, WARN, FAIL])
    assert s3["lvl"] == WARN                                                                # `confirm` counts every run above the confirmed level
    feed(p3, s3, [FAIL, FAIL, FAIL])
    assert s3["lvl"] == FAIL                                                                # but 3 solid FAILs still escalate


def test_step_recovery_streak_counts_any_better_run_and_confirms_the_highest():
    p, s = stepper(confirm=1, recover=2)
    feed(p, s, [OK, FAIL])
    assert s["lvl"] == FAIL
    feed(p, s, [OK, WARN])
    assert s["lvl"] == WARN                                                                 # OK then WARN: better twice, the HIGHEST of them
    feed(p, s, [OK, OK])
    assert s["lvl"] == OK
    p2, s2 = stepper(confirm=2, recover=2)
    feed(p2, s2, [OK, WARN, WARN])
    assert s2["lvl"] == WARN
    feed(p2, s2, [OK, WARN, OK, WARN, OK, WARN])
    assert s2["lvl"] == WARN                                                                # good/bad alternation never confirms a recovery
    feed(p2, s2, [FAIL, OK, FAIL, OK, FAIL])
    assert s2["lvl"] == WARN                                                                # ... nor a downgrade: the opposite side restarts the count


def test_step_transition_log_is_bounded():
    p, s = stepper(confirm=1, recover=1)
    feed(p, s, [OK] + [FAIL, OK] * 20)
    assert len(s["tr"]) == 12


def test_eff_severity():
    assert probes.eff_severity(mk(target="http://x/", **{"class": "P0"}), FAIL) == "crit"
    assert probes.eff_severity(mk(target="http://x/", **{"class": "P0"}), WARN) == "warn"      # degraded never pages as crit
    assert probes.eff_severity(mk(target="http://x/", **{"class": "P1"}), FAIL) == "warn"
    assert probes.eff_severity(mk(target="http://x/", severity="info"), FAIL) == "info"


# ---- run verdict: two passes (see the `two passes` section further down)


def test_attempt_never_raises_and_scrubs(monkeypatch):
    box = script(monkeypatch)
    box["r"] = lambda p: 1 / 0
    lvl, detail, _ = probes.attempt(mk(target="http://x/"), probes.Snapshot([]), NOW)
    assert lvl == FAIL and detail == "probe error: ZeroDivisionError"
    box["r"] = (FAIL, "GET http://u:p@h/x?token=SECRET failed")
    assert "SECRET" not in probes.attempt(mk(target="http://x/"), probes.Snapshot([]), NOW)[1]


def test_optional_probe_is_skipped_until_it_has_been_seen_up(monkeypatch):
    box = script(monkeypatch)
    box["r"] = (FAIL, "refused")
    p = mk(target="http://x/", optional=True)
    lvl, detail, _ = probes.attempt(p, probes.Snapshot([]), NOW, never_up=True)
    assert lvl == SKIP and "not deployed yet" in detail
    assert probes.attempt(p, probes.Snapshot([]), NOW, never_up=False)[0] == FAIL      # once seen up, a failure is real
    assert probes.attempt(mk(target="http://x/"), probes.Snapshot([]), NOW, never_up=True)[0] == FAIL


# =========================================================================== the engine: run_due
def test_run_due_cadence(env, monkeypatch):
    calls = []
    box = script(monkeypatch)
    box["r"] = lambda p: (calls.append(p.name), (OK, "fine"))[1]
    conf = conf_of(raw("fast", interval_s=60), raw("slow", interval_s=600))
    go(conf, NOW, force=False)
    assert sorted(calls) == ["fast", "slow"]                                # never run: everything is due
    calls.clear()
    go(conf, NOW + 30, force=False)
    assert calls == []                                                      # nothing due yet
    go(conf, NOW + 60, force=False)
    assert calls == ["fast"]                                                # a 60 s probe is due on the next 1-min tick
    calls.clear()
    go(conf, NOW + 120, force=False)
    assert calls == ["fast"]
    calls.clear()
    go(conf, NOW + 700, force=False)
    assert sorted(calls) == ["fast", "slow"]
    calls.clear()
    rep = go(conf, NOW + 701, force=True)
    assert sorted(calls) == ["fast", "slow"] and sorted(rep.ran) == ["fast", "slow"]
    calls.clear()
    go(conf, NOW + 702, force=False, only={"slow"})
    assert calls == ["slow"]


# ---- review fix: a clock that steps backwards must not park the plane
def test_is_due_treats_a_last_run_in_the_future_as_due():
    p = mk(target="http://x/", interval_s=60)
    assert probes.is_due(p, {"last_run": NOW + 3 * 86400}, NOW) is True                 # the clock stepped back 3 days
    assert probes.is_due(p, {"last_run": NOW + probes.SKEW_S}, NOW) is False             # a few seconds of skew is noise, not a step
    assert probes.is_due(p, {"last_run": NOW + probes.SKEW_S + 1}, NOW) is True
    assert probes.is_due(p, {"last_run": NOW - 30}, NOW) is False and probes.is_due(p, {"last_run": NOW - 54}, NOW) is True
    assert probes.is_due(p, None, NOW) is True and probes.is_due(p, {"last_run": "junk"}, NOW) is True
    assert probes.is_due(mk(target="http://x/", paused=True), {"last_run": NOW + 9e9}, NOW) is False


def test_a_clock_step_back_does_not_stop_probing_nor_invent_an_outage(env, monkeypatch):
    calls = []
    box = script(monkeypatch)
    box["r"] = lambda p: (calls.append(p.name), (OK, "x"))[1]
    conf = conf_of(raw("z", interval_s=60))
    go(conf, NOW + 3 * 86400, force=False)                                               # RTC / VM resume put the clock 3 days ahead
    calls.clear()
    rep = go(conf, NOW, force=False)                                                     # ... and it is corrected
    assert rep.ran == ["z"] and calls == ["z"]                                           # it used to wait 72 h
    r = probes.snapshot(NOW, conf)[0]
    assert r["avail_30d"] == 100.0 and r["obs_30d"] == 100.0                              # a negative gap books nothing
    assert go(conf, NOW + 30, force=False).ran == [] and go(conf, NOW + 60, force=False).ran == ["z"]     # and normal cadence resumes
    assert probes.load_state()["run"]["period"] <= 60                                    # a step back is never learned as a cadence


def test_recovery_down_time_is_never_negative_after_a_clock_step(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a", confirm=1, recover=1))
    go(conf, NOW + 3 * 86400)
    box["r"] = (FAIL, "x")
    assert go(conf, NOW + 3 * 86400 + 60).events[0]["to"] == "down"
    box["r"] = (OK, "x")
    rec = [e for e in go(conf, NOW).events if e["probe"] == "a"][0]                      # the clock was corrected meanwhile
    assert rec["to"] == "up" and rec["down_s"] == 0


def test_claims_and_backoffs_stamped_by_a_clock_that_stepped_back_do_not_wedge_delivery(env):
    st = probes.load_state()
    st["inflight"] = [{"id": "e1", "probe": "a", "severity": "warn", "t": NOW, "ct": NOW + 86400}]        # claimed "tomorrow"
    st["events"] = [{"id": "e2", "probe": "b", "severity": "warn", "t": NOW, "nb": NOW + 86400},         # backing off until "tomorrow"
                    {"id": "e3", "probe": "c", "severity": "warn", "t": NOW, "nb": NOW + 300}]           # a real, short back-off
    probes.save_state(st)
    assert {e["id"] for e in probes.claim_events(NOW)} == {"e1", "e2"}
    assert [e["id"] for e in probes.load_state()["events"]] == ["e3"]
    probes.release_events(["e1"], failed=True, now=NOW)
    assert probes.load_state()["events"][0]["nb"] == NOW + 60                             # normal back-off is unchanged


def test_run_due_paused_probe_never_runs(env, monkeypatch):
    calls = []
    box = script(monkeypatch)
    box["r"] = lambda p: (calls.append(p.name), (OK, "x"))[1]
    conf = conf_of(raw("a"), raw("b", paused=True))
    go(conf, NOW)
    go(conf, NOW + 9999, only={"b"})
    assert calls == ["a"]
    assert {r["name"]: r["state"] for r in probes.snapshot(NOW, conf)} == {"a": "up", "b": "paused"}


def test_run_due_with_nothing_configured_is_a_quiet_noop(env):
    rep = probes.run_due(NOW, conf=({**probes.DEFAULTS}, [], []))
    assert rep.ran == [] and rep.errors == [] and not (core.STATE_DIR / "probes.json").exists()      # fresh install: no file, no noise


def test_run_due_pool_is_bounded_and_parallel(env, monkeypatch):
    lock, cur, peak = threading.Lock(), [0], [0]

    def fake(p, snap, now):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        time.sleep(0.05)
        with lock:
            cur[0] -= 1
        return OK, "x"

    monkeypatch.setitem(probes.RUNNERS, "http", fake)
    rep = go(conf_of(*[raw(f"p{i}") for i in range(12)], workers=3), NOW)
    assert len(rep.ran) == 12 and 2 <= peak[0] <= 3


def test_run_due_budget_fails_only_the_probe_that_started_and_never_finished(env, monkeypatch):
    gate = threading.Event()

    def stuck(p, snap, now):
        gate.wait(10)
        return OK, "late"

    monkeypatch.setitem(probes.RUNNERS, "http", stuck)
    conf = conf_of(raw("a"), raw("b"), budget_s=0.3, workers=1, confirm=1, attempts=1)
    t0 = time.monotonic()
    rep = go(conf, NOW)
    assert time.monotonic() - t0 < 3 and rep.ran == ["a"]                  # one stuck worker: the run still returns, b was never reached
    rows = {r["name"]: r for r in probes.snapshot(NOW, conf)}
    assert rows["a"]["state"] == "down" and "did not finish" in rows["a"]["detail"]
    assert rows["b"]["state"] == "unknown" and rows["b"]["detail"] == "" and rows["b"]["avail_30d"] is None      # never looked at: no verdict
    gate.set()
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("probe-"))


def test_run_due_lock_contention_is_reported_not_raced(env, monkeypatch):
    calls = []
    box = script(monkeypatch)
    box["r"] = lambda p: (calls.append(1), (OK, "x"))[1]
    with probes._locked() as got:
        assert got
        rep = go(conf_of(raw("a")), NOW)
    assert rep.locked and calls == [] and rep.ran == []
    assert not go(conf_of(raw("a")), NOW).locked


def test_run_due_persists_state_and_survives_corruption(env, monkeypatch):
    script(monkeypatch)
    go(conf_of(raw("a"), raw("b")), NOW)
    st = json.loads((core.STATE_DIR / "probes.json").read_text())
    assert set(st["probes"]) == {"a", "b"} and st["run"]["ran"] == 2 and oct(os.stat(core.STATE_DIR / "probes.json").st_mode & 0o777) == "0o644"
    (core.STATE_DIR / "probes.json").write_text("{corrupt")
    assert probes.load_state()["probes"] == {}
    go(conf_of(raw("a")), NOW + 120)
    assert probes.snapshot(NOW + 120, conf_of(raw("a")))[0]["state"] == "up"


# ---- review fix: state outlives config trouble (it used to be pruned to the names of the currently VALID config)
def test_a_probe_missing_from_the_config_keeps_its_state_for_the_grace_period_then_loses_it(env, monkeypatch):
    script(monkeypatch)
    go(conf_of(raw("a"), raw("b")), NOW)
    go(conf_of(raw("a")), NOW + 60)
    st = probes.load_state()
    assert set(st["probes"]) == {"a", "b"} and set(st["days"]) == {"a", "b"} and st["gone"] == {"b": NOW + 60}
    go(conf_of(raw("a")), NOW + 60 + probes.GONE_KEEP_S - 60)
    assert "b" in probes.load_state()["probes"]                                           # still inside the grace period
    go(conf_of(raw("a")), NOW + 60 + probes.GONE_KEEP_S + 1)
    st = probes.load_state()
    assert set(st["probes"]) == {"a"} and set(st["days"]) == {"a"} and st["gone"] == {}   # gone for good, nothing left behind
    go(conf_of(raw("a"), raw("b")), NOW + 2 * probes.GONE_KEEP_S)
    assert probes.load_state()["gone"] == {}


def test_a_config_typo_does_not_wipe_state_history_or_announcements(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a", confirm=1), raw("b", confirm=1, optional=True))
    for i in range(5):
        go(conf, NOW + 60 * i)
    box["r"] = lambda p: (FAIL, "refused") if p.name == "b" else (OK, "fine")
    assert [e["probe"] for e in go(conf, NOW + 300).events] == ["b"]                      # b went down and was announced once
    before = probes.load_state()
    typo = probes.parse({"probe": [raw("a", confirm=1), raw("b", type="tcpx", confirm=1, optional=True)]})
    assert [p.name for p in typo[1]] == ["a"] and typo[2]                                 # b is invalid: reported, not loaded
    go(typo, NOW + 360)
    rep = go(typo, NOW + 420)
    assert any("tcpx" in e or "type must be" in e for e in rep.errors)
    st = probes.load_state()
    assert st["days"]["b"] == before["days"]["b"]                                         # the 30-day history survived the typo
    b = st["probes"]["b"]
    assert b["lvl"] == FAIL and b["ev"] == FAIL and "last_ok" in b and st["gone"].keys() == {"b"}
    ev = go(conf, NOW + 480).events                                                       # the typo is fixed
    assert [e for e in ev if e["probe"] != "*config"] == []                               # b is still down, but was already announced: no repeat
    r = {x["name"]: x for x in probes.snapshot(NOW + 480, conf)}
    assert r["b"]["state"] == "down" and r["b"]["since"] == before["probes"]["b"]["since"]     # a REAL outage, not "not deployed yet"
    assert probes.load_state()["gone"] == {} and r["b"]["avail_30d"] == round(100 * 5 / 7, 2)  # 5 ok + 2 fail samples, nothing lost, none invented


def test_an_untrusted_probes_d_file_does_not_cost_its_probes_their_state(env, monkeypatch):
    script(monkeypatch)
    (core.CONF_DIR / "probes.toml").write_text('[[probe]]\nname = "a"\ntype = "http"\ntarget = "http://127.0.0.1:1/"\n')
    (core.CONF_DIR / "probes.d").mkdir()
    extra = core.CONF_DIR / "probes.d" / "extra.toml"
    extra.write_text('[[probe]]\nname = "b"\ntype = "http"\ntarget = "http://127.0.0.1:1/"\n')
    for f in (core.CONF_DIR / "probes.toml", extra):
        os.chmod(f, 0o644)
    os.chmod(core.CONF_DIR / "probes.d", 0o755)
    run = lambda t: probes.run_due(t, force=True, sleep=lambda s: None, pusher=lambda *a: None)
    assert sorted(run(NOW).ran) == ["a", "b"]
    os.chmod(extra, 0o664)                                                                # group-writable: ignored, and reported
    rep = run(NOW + 60)
    assert rep.ran == ["a"] and any("ignored" in e for e in rep.errors)
    st = probes.load_state()
    assert set(st["probes"]) == {"a", "b"} and st["days"]["b"] == [[probes._day(NOW), 1, 1]]
    os.chmod(extra, 0o644)
    assert sorted(run(NOW + 120).ran) == ["a", "b"]
    assert sum(r[2] for r in probes.load_state()["days"]["b"]) == 2 and all(r[1] == r[2] for r in probes.load_state()["days"]["b"])   # continued


def test_run_due_confirmation_across_runs_and_availability(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("x", confirm=2, recover=2))
    state = lambda t: probes.snapshot(t, conf)[0]
    go(conf, NOW)
    assert state(NOW)["state"] == "up"
    box["r"] = (FAIL, "refused")
    go(conf, NOW + 60)
    r = state(NOW + 60)
    assert r["state"] == "up" and r["pending"] == 1 and r["detail"] == "refused"      # a first bad run does not flip it
    go(conf, NOW + 120)
    assert state(NOW + 120)["state"] == "down"
    box["r"] = (OK, "fine")
    go(conf, NOW + 180)
    assert state(NOW + 180)["state"] == "down"
    go(conf, NOW + 240)
    assert state(NOW + 240)["state"] == "up"
    # 5 bad/good runs counted per run verdict: ok, fail, fail, ok, ok
    assert state(NOW + 240)["avail_30d"] == 60.0


def test_availability_windows_and_trimming():
    day = lambda t: int(time.strftime("%Y%m%d", time.localtime(t)))
    rows = [[day(NOW - 40 * 86400), 0, 10], [day(NOW - 3 * 86400), 9, 10], [day(NOW), 10, 10]]
    assert probes.availability(rows, NOW, 1) == 100.0
    assert probes.availability(rows, NOW, 7) == 95.0
    assert probes.availability(rows, NOW, 30) == 95.0                        # the 40-day-old row is outside the window
    assert probes.availability([], NOW, 30) is None
    st = {"days": {"a": rows}}
    probes._bump_day(st, "a", NOW, False, 30)
    assert len(st["days"]["a"]) == 2 and st["days"]["a"][-1][1:] == [10, 11]


# ---- review fix: availability is time-honest (it used to count only the samples that were taken)
def test_availability_reads_old_three_column_rows_ignores_junk_and_reports_observed():
    day = lambda t: int(time.strftime("%Y%m%d", time.localtime(t)))
    rows = [[day(NOW), 9, 10], "junk", [day(NOW), "x", 1], [day(NOW)], [day(NOW), 1, 2, 1], None]
    assert probes.availability(rows, NOW, 1) == round(100 * 10 / 12, 2)
    assert probes.observed(rows, NOW, 1) == round(100 * 11 / 12, 2)                      # 1 of the 12 expected samples was never taken
    assert probes.observed([[day(NOW), 9, 10]], NOW, 1) == 100.0 and probes.observed([], NOW, 1) is None


def test_an_outage_of_the_engine_burns_availability_and_shows_as_unobserved(env, monkeypatch):
    """10 runs, 6 h of nothing (power cut), 10 runs: it used to read 100 % up for 7 d and 30 d."""
    script(monkeypatch)
    conf = conf_of(raw("a", interval_s=60))
    for i in range(10):
        go(conf, NOW + 60 * i, force=False)
    t1 = NOW + 6 * 3600
    for i in range(10):
        go(conf, t1 + 60 * i, force=False)
    r = probes.snapshot(t1 + 600, conf)[0]
    gap = t1 - (NOW + 540)
    n = int(gap // 60) - 1                                                                # 350 samples nobody took
    assert r["state"] == "up" and n == 350                                               # it is up NOW; the history says what happened
    assert r["avail_30d"] == r["avail_7d"] == round(100 * 20 / (20 + n), 2) and r["avail_30d"] < 10
    assert r["obs_30d"] == r["obs_7d"] == round(100 * 20 / (20 + n), 2)
    rows = probes.load_state()["days"]["a"]
    assert sum(x[2] for x in rows) == 20 + n and sum(x[3] for x in rows if len(x) > 3) == n


def test_gap_tolerance_formula_and_a_clock_that_went_backwards():
    st = {"days": {}}
    assert probes._book_gap(st, "a", NOW, NOW + 150, 60.0, 30) == 0                      # up to 2.5 x the spacing is a late tick, not an outage
    assert st["days"] == {}
    assert probes._book_gap(st, "a", NOW, NOW + 151, 60.0, 30) == 1                      # one sample missing
    assert probes._book_gap(st, "a", NOW + 5, NOW - 100, 60.0, 30) == 0                  # negative gap: the clock stepped back
    assert probes._book_gap(st, "a", NOW, NOW + 10_000, 0.0, 30) == 0                    # no expected spacing: nothing to compare with
    assert sum(r[2] for r in st["days"]["a"]) == 1


def test_a_long_gap_is_spread_over_the_days_it_covered(env):
    st = {"days": {}}
    n = probes._book_gap(st, "a", NOW, NOW + 3 * 86400, 3600.0, 30)
    rows = st["days"]["a"]
    assert n == 71 and sum(r[2] for r in rows) == 71 and sum(r[3] for r in rows) == 71 and sum(r[1] for r in rows) == 0
    assert len(rows) in (3, 4) and [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert all(23 <= r[2] <= 25 for r in rows[1:-1])                                     # a full day carries ~24 hourly samples, not today's lot
    n = probes._book_gap(st, "b", NOW, NOW + 400 * 86400, 3600.0, 30)
    assert n == 35 * 24 - 1 and len(st["days"]["b"]) <= 36                                # clamped to the retained history, loop bounded


def test_an_engine_that_runs_less_often_than_the_probe_interval_is_not_read_as_an_outage(env, monkeypatch):
    """Check tier only (every 15 min) with a 60 s probe: the engine's own cadence is learned, the 15 min spacing is normal."""
    script(monkeypatch)
    conf = conf_of(raw("a", interval_s=60))
    for i in range(10):
        go(conf, NOW + 900 * i, force=False)
    r = probes.snapshot(NOW + 8100, conf)[0]
    assert r["avail_30d"] == 100.0 and r["obs_30d"] == 100.0 and probes.load_state()["run"]["period"] == 900.0
    go(conf, NOW + 8100 + 6 * 3600, force=False)                                         # but a real outage is still seen
    r = probes.snapshot(NOW + 8100 + 6 * 3600, conf)[0]
    assert r["obs_30d"] == round(100 * 11 / (11 + 23), 2) and r["avail_30d"] == r["obs_30d"]
    assert probes.load_state()["run"]["period"] == 900.0                                 # an outage is never learned as the cadence


def test_time_paused_or_removed_from_the_config_is_not_booked_as_downtime(env, monkeypatch):
    script(monkeypatch)
    live, paused = conf_of(raw("a", interval_s=60), raw("b", interval_s=60)), conf_of(raw("a", interval_s=60, paused=True), raw("b", interval_s=60))
    for i in range(5):
        go(live, NOW + 60 * i, force=False)
    t = NOW + 3 * 86400
    go(paused, t, force=False)                                                            # a paused for 3 days
    go(conf_of(raw("b", interval_s=60)), t + 60, force=False)                              # and removed for a while
    go(live, t + 2 * 86400, force=False)
    go(live, t + 2 * 86400 + 60, force=False)
    rows = {r["name"]: r for r in probes.snapshot(t + 2 * 86400 + 60, live)}
    assert rows["a"]["avail_30d"] == 100.0 and rows["a"]["obs_30d"] == 100.0              # 7 samples, none invented for the paused/absent days
    assert sum(x[2] for x in probes.load_state()["days"]["a"]) == 7
    assert rows["b"]["obs_30d"] < 50                                                      # b WAS configured and nobody looked: that is booked


def test_force_and_only_runs_do_not_create_unobserved_time(env, monkeypatch):
    script(monkeypatch)
    conf = conf_of(raw("a", interval_s=60))
    go(conf, NOW)
    for i in range(1, 6):
        go(conf, NOW + 7 * i)
    assert probes.snapshot(NOW + 40, conf)[0]["obs_30d"] == 100.0


def test_skipped_runs_do_not_count_toward_availability(env, monkeypatch):
    box = script(monkeypatch)
    box["r"] = (SKIP, "stopped by design")
    conf = conf_of(raw("x"))
    go(conf, NOW)
    r = probes.snapshot(NOW, conf)[0]
    assert r["state"] == "skipped" and r["avail_30d"] is None and r["detail"] == "stopped by design"


def test_history_records_are_compact_throttled_and_immediate_on_change(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a"), raw("b", confirm=1))
    hist = lambda: [json.loads(x) for x in (core.STATE_DIR / "history.jsonl").read_text().splitlines()]
    go(conf, NOW)
    assert len(hist()) == 1 and hist()[0] == {"t": NOW, "kind": "probe", "n": 2, "up": 2, "bad": {}}
    go(conf, NOW + 60)
    assert len(hist()) == 1                                                 # throttled (history_every_s = 300)
    box["r"] = lambda p: (FAIL, "x") if p.name == "b" else (OK, "x")
    go(conf, NOW + 120)
    assert hist()[-1]["bad"] == {"b": "d"} and hist()[-1]["up"] == 1        # a confirmed change is written at once
    go(conf, NOW + 500)
    assert len(hist()) == 3
    assert core.read_history(10 ** 9, "probe")[-1]["kind"] == "probe"      # readable through the standard history API


def test_events_down_recovery_with_dedupe_keys_and_severity(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("web", title="Web", **{"class": "P2"}), raw("db", title="DB", **{"class": "P0"}), raw("quiet", severity="info"))
    go(conf, NOW)
    box["r"] = (FAIL, "HTTP 503")
    assert go(conf, NOW + 60).events == []                                  # first bad run: nothing yet
    ev = go(conf, NOW + 120).events
    by = {e["probe"]: e for e in ev}
    assert set(by) == {"web", "db"}                                         # info-severity probes never announce
    assert by["web"]["severity"] == "warn" and by["db"]["severity"] == "crit"
    assert by["web"]["to"] == "down" and by["web"]["dedupe_key"] == "probe:web" and by["web"]["detail"] == "HTTP 503"
    assert go(conf, NOW + 180).events == []                                 # no repeat while it stays down
    box["r"] = (OK, "fine")
    assert go(conf, NOW + 240).events == []
    ev = go(conf, NOW + 300).events
    rec = {e["probe"]: e for e in ev}
    assert rec["web"]["severity"] == "recovery" and rec["web"]["to"] == "up" and rec["web"]["down_s"] == 180
    assert rec["web"]["dedupe_key"] == "probe:web"


def test_events_warn_level_never_pages_as_crit(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("db", **{"class": "P0"}))
    go(conf, NOW)
    box["r"] = (WARN, "slow")
    go(conf, NOW + 60)
    ev = go(conf, NOW + 120).events
    assert ev[0]["to"] == "warn" and ev[0]["severity"] == "warn"


def test_events_queue_is_drained_exactly_once_and_can_be_requeued(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a", confirm=1))
    go(conf, NOW)
    box["r"] = (FAIL, "x")
    go(conf, NOW + 60)
    ev = probes.pop_events()
    assert [e["probe"] for e in ev] == ["a"] and probes.pop_events() == []
    probes.requeue_events(ev)
    assert [e["probe"] for e in probes.pop_events()] == ["a"]
    probes.requeue_events([])                                               # no-op
    assert probes.pop_events() == []


def test_events_flapping_is_announced_once_then_silent(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("f", confirm=1, recover=1), flap_max=4)
    got = []
    go(conf, NOW)
    for i in range(1, 9):
        box["r"] = (FAIL, "x") if i % 2 else (OK, "x")
        got += [e["to"] for e in go(conf, NOW + 60 * i).events]
    assert got.count("flapping") == 1 and got[:3] == ["down", "up", "down"]
    assert got[-1] == "flapping" or "flapping" in got                        # and nothing after it while it keeps changing
    assert got.index("flapping") == len(got) - 1
    r = probes.snapshot(NOW + 480, conf)[0]
    assert r["flapping"] is True


def test_events_storm_becomes_one_summary(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(*[raw(f"s{i}", confirm=1, **{"class": "P0"}) for i in range(10)])
    go(conf, NOW)
    box["r"] = (FAIL, "timeout")
    ev = go(conf, NOW + 60).events
    assert len(ev) == 1 and ev[0]["to"] == "storm" and ev[0]["probe"] == "*" and ev[0]["severity"] == "crit"
    assert "10 probes" in ev[0]["detail"]


def test_push_heartbeats_and_deadman(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a", kuma_push_key="key-a", confirm=1), raw("b"), deadman_key="umbrella-probes")
    rep = go(conf, NOW)
    assert ("key-a", "ok") == rep.pushes[0][:2] and rep.pushes[-1][0] == "umbrella-probes"
    box["r"] = (FAIL, "x")
    rep = go(conf, NOW + 60)
    assert ("key-a", "crit") == rep.pushes[0][:2]                            # per-probe heartbeat carries the health
    assert rep.pushes[-1][:2] == ("umbrella-probes", "ok") and "alive" in rep.pushes[-1][2]    # the dead-man only says "alive"
    assert not any(m for _k, _s, m in rep.pushes if "http" in m)             # nothing about targets


def test_push_failures_never_fail_the_run(env, monkeypatch):
    script(monkeypatch)
    conf = conf_of(raw("a", kuma_push_key="k"))

    def boom(*a):
        raise RuntimeError("kuma down")

    rep = probes.run_due(NOW, conf=conf, sleep=lambda s: None, pusher=boom)
    assert rep.ran == ["a"]


def test_push_is_a_noop_without_tokens(env, monkeypatch):
    script(monkeypatch)
    called = []
    monkeypatch.setattr(core, "sh", lambda *a, **k: called.append(a))        # a real push would run curl through core.sh
    probes.run_due(NOW, conf=conf_of(raw("a", kuma_push_key="k")), sleep=lambda s: None)
    assert called == []
    _write_conf(env, "kuma.toml", '[push]\nk = "abcdefgh12345678"\numbrella-probes = "zzzzzzzz99999999"\n')
    probes.run_due(NOW + 400, conf=conf_of(raw("a", kuma_push_key="k")), sleep=lambda s: None, force=True)
    assert len(called) == 2 and all(c[0][0] == "curl" for c in called)
    assert not any("abcdefgh12345678" in " ".join(c[0]) for c in called)     # the token goes to curl on stdin, never argv


# =========================================================================== read side
def test_how_reveals_no_secrets_query_or_credentials():
    assert probes.how(mk(target="http://127.0.0.1:7878/ping?apikey=SECRET")) == "GET :7878/ping"
    assert "SECRET" not in probes.how(mk(target="https://example.com/x?token=SECRET", external=True))
    assert probes.how(mk(type="tcp", target="10.0.0.5:5432")) == "tcp :5432"
    assert "10.0.0.5" not in probes.how(mk(type="tcp", target="10.0.0.5:5432"))
    assert probes.how(mk(type="docker", target="*")) == "every container running/healthy"
    assert probes.how(mk(type="command", target=["/usr/bin/tailscale", "--secret", "x"])) == "command tailscale"
    assert probes.how(mk(type="file_age", target="/home/ohmz/.hermes/cron/ticker_heartbeat", expect={"max_age_s": 1})) == "file age ticker_heartbeat"
    assert probes.how(mk(type="systemd", target="docker.service")) == "unit docker.service"


def test_snapshot_before_any_run_is_all_unknown(env):
    rows = probes.snapshot(NOW, conf_of(raw("a"), raw("b", paused=True)))
    assert [r["state"] for r in rows] == ["unknown", "paused"] and rows[0]["avail_30d"] is None


# =========================================================================== Kuma parity (a COPY, read-only)
def make_kuma(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute("create table monitor (id integer primary key, name text, type text, active integer, parent integer, url text, "
                "basic_auth_user text, basic_auth_pass text, push_token text, headers text)")
    con.executemany("insert into monitor values (?,?,?,?,?,?,?,?,?,?)",
                    [(i, n, t, a, par, "http://x/?token=KUMASECRET", "admin", "HUNTER2", "PUSHSECRET", "Authorization: Bearer BEARERSECRET")
                     for i, (n, t, a, par) in enumerate(rows, 1)])
    con.commit()
    con.close()


def test_kuma_monitors_selects_no_credentials_and_never_writes(tmp_path):
    db = tmp_path / "kuma.db"
    make_kuma(db, [("Radarr", "http", 1, 7), ("Plex", "keyword", 0, 7)])
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    mons = probes.kuma_monitors(str(db))
    assert [m["name"] for m in mons] == ["Radarr", "Plex"] and mons[1]["active"] is False
    blob = json.dumps(mons)
    for secret in ("KUMASECRET", "HUNTER2", "PUSHSECRET", "BEARERSECRET", "admin"):
        assert secret not in blob
    assert set(mons[0]) == {"id", "name", "type", "active", "parent"}
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before and sorted(p.name for p in tmp_path.iterdir()) == ["kuma.db"]


def test_kuma_diff_reports_unmapped_paused_and_stale(tmp_path):
    db = tmp_path / "kuma.db"
    make_kuma(db, [("Radarr", "http", 1, 7), ("Plex", "keyword", 0, 7), ("Media Stack", "group", 1, None), ("Orphan", "http", 1, None)])
    ps = [mk(target="http://127.0.0.1/", name="radarr", kuma="Radarr"), mk(target="http://127.0.0.1/", name="plex", kuma="Plex"),
          mk(target="http://127.0.0.1/", name="gone", kuma="Deleted In Kuma")]
    diff = probes.kuma_diff(str(db), ps, {"groups": {"Media Stack": "Media Stack"}})
    assert diff == {"covered": ["Media Stack", "Plex", "Radarr"], "unmapped": ["Orphan"], "paused_in_kuma": ["Plex"],
                    "stale_mapping": ["Deleted In Kuma"]}


# =========================================================================== CLI
def test_cli_validate_list_and_run(env, srv, capsys):
    _write_conf(env, "probes.toml", f'[defaults]\nattempts = 1\n[[probe]]\nname="up"\ntype="http"\ntarget="{srv.url("/ok")}"\n'
                                    f'[[probe]]\nname="down"\ntype="http"\ntarget="{srv.url("/code/500")}"\nconfirm = 1\n')
    assert probes.main(["validate"]) == 0
    assert "2 probes valid, 0 problems" in capsys.readouterr().out
    assert probes.main(["run", "--force"]) == 0
    out = capsys.readouterr().out
    assert "ran 2/2 probes" in out and re.search(r"^down\s+P2 down\b", out, re.M) and not re.search(r"^up\s+P2 up\b", out, re.M)   # a tick log lists problems only
    assert probes.main(["run", "--only", "up", "--all"]) == 0
    assert re.search(r"^up\s+P2 up\b", capsys.readouterr().out, re.M)
    assert probes.main(["run", "--only", "up"]) == 0
    assert "ran 1/2" in capsys.readouterr().out
    assert probes.main(["list"]) == 0
    _write_conf(env, "probes.toml", '[[probe]]\nname="Bad Name"\ntype="http"\ntarget="http://127.0.0.1/"\n')
    assert probes.main(["validate"]) == 1
    assert "1 problems" in capsys.readouterr().out


def test_cli_run_notify_only_delivers_in_events_mode(env, srv, capsys, monkeypatch):
    from homelab_maint.tasks import monitors
    _write_conf(env, "probes.toml", f'[[probe]]\nname="a"\ntype="http"\ntarget="{srv.url("/ok")}"\n')
    got = []
    monkeypatch.setattr(monitors, "notify_events", lambda *a, **k: got.append(1) or {"sent": 2, "dropped": 0, "retry": 0, "deferred": 0, "expired": 0})
    assert probes.main(["run", "--force", "--notify"]) == 0
    assert "notify: skipped" in capsys.readouterr().out and got == []        # default alert_mode "task": the check tier pages
    _write_conf(env, "maint.toml", '[tasks.probes]\nalert_mode = "events"\n')
    assert probes.main(["run", "--force", "--notify"]) == 0
    assert "notify: sent 2" in capsys.readouterr().out and got == [1]
    assert probes.main(["run", "--force"]) == 0 and got == [1]               # no --notify, no delivery


def test_cli_kuma_diff_and_export(env, tmp_path, capsys):
    db = tmp_path / "kuma.db"
    make_kuma(db, [("Radarr", "http", 1, None)])
    _write_conf(env, "probes.toml", '[kuma]\nignore = []\n[[probe]]\nname="radarr"\ntype="tcp"\ntarget="127.0.0.1:1"\nkuma="Radarr"\n')
    assert probes.main(["kuma-diff", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["covered"] == ["Radarr"]
    assert probes.main(["export"]) == 0
    assert json.loads(capsys.readouterr().out)["probes"][0]["name"] == "radarr"


# =========================================================================== the shipped etc/probes.toml
SHIPPED = ROOT / "etc" / "probes.toml"
# The 14 rows of the real kuma.db (read from a copy on 2026-10-02): name, type, active, parent id.
KUMA_ROWS = [("Internet", "http", 1, None), ("Plex", "keyword", 0, 7), ("Media Stack", "group", 1, None), ("Radarr", "http", 1, 7),
             ("Sonarr", "http", 1, 7), ("Prowlarr", "http", 1, 7), ("SABnzbd", "http", 1, 7), ("Deluge", "http", 1, 7),
             ("Transmission", "http", 1, 7), ("Bazarr", "http", 1, 7), ("Tautulli", "http", 1, 7), ("Seerr", "http", 1, 7),
             ("Kometa", "docker", 1, 7), ("Immich", "keyword", 1, 7)]


@pytest.fixture(scope="module")
def shipped():
    text = SHIPPED.read_text()
    doc = tomllib.loads(text)
    d, ps, errs = probes.parse(doc)
    return text, doc, d, ps, errs, {p.name: p for p in ps}


def test_shipped_config_is_valid_and_big_enough(shipped):
    _t, _doc, d, ps, errs, by = shipped
    assert errs == [] and len(ps) >= 120 and len(by) == len(ps)


def test_shipped_config_is_installable_as_trusted(shipped):
    mode = SHIPPED.stat().st_mode & 0o777
    assert mode & 0o022 == 0, oct(mode)                                      # the loader ignores group/world-writable files


def test_shipped_config_covers_every_kuma_monitor(shipped, tmp_path):
    _t, doc, _d, ps, _e, by = shipped
    db = tmp_path / "kuma.db"
    make_kuma(db, KUMA_ROWS)
    diff = probes.kuma_diff(str(db), ps, doc["kuma"])
    assert diff["unmapped"] == [] and diff["stale_mapping"] == [] and diff["paused_in_kuma"] == ["Plex"]
    assert "Media Stack" in diff["covered"] and len(diff["covered"]) == 14
    plex = by["plex"]
    assert plex.kuma_paused and not plex.paused and plex.target.endswith(":32400/identity")   # paused in Kuma, probed here
    assert by["ct-kometa"].type == "docker" and by["ct-kometa"].target == "kometa"
    assert by["immich"].method == "GET" and "/auth/" not in by["immich"].target                 # Kuma's POST monitor is not ported as a POST
    assert all(p.source == "kuma" for p in ps if p.kuma)


REQUIRED = {
    # apps named in SPEC4 S10
    "plex", "immich", "kavita", "seerr", "openwebui", "homarr", "nextcloud", "tunarr", "radarr", "sonarr", "prowlarr", "bazarr",
    "tautulli", "sabnzbd", "deluge", "transmission", "glances", "sensor-exporter", "maintenance-site", "ollama", "comfyui",
    # infrastructure
    "docker-daemon", "svc-docker", "svc-cloudflared", "cloudflared-tunnel", "svc-tailscaled", "tailscale", "svc-fail2ban", "fail2ban",
    "svc-smartd", "svc-sensor-exporter", "svc-glances", "svc-ollama", "svc-plex", "ct-fleet", "public-seerr", "public-maintenance", "internet",
    # the umbrella itself
    "umbrella-status", "umbrella-probe-plane", "umbrella-live", "umbrella-metrics-ring", "umbrella-public-export", "umbrella-www",
    "svc-beszel-hub", "svc-hm-check", "svc-hm-daily", "svc-hm-weekly",
    # Hermes through its state files
    "hermes-api", "hermes-gateway", "hermes-ticker", "hermes-watchdog-alive", "hermes-canary-alive", "hermes-wd-delivery", "hermes-sc-chat",
}


def test_shipped_config_has_every_required_probe(shipped):
    by = shipped[5]
    assert REQUIRED - set(by) == set()
    assert by["comfyui"].when_running == "comfyui"                           # only while the container is up
    assert by["maintenance-site"].target == "http://127.0.0.1:8088/api/health" and by["maintenance-site"].optional
    assert by["svc-beszel-hub"].target == "beszel-hub.service" and by["svc-beszel-hub"].optional
    assert by["umbrella-status"].target == "{STATE}/status.json" and by["umbrella-live"].target == "{STATE}/public/live.json"
    assert by["umbrella-metrics-ring"].target == "{STATE}/metrics-ring.json"
    assert by["svc-smartd"].target == "smartmontools.service"                # canonical Id: smartd.service is an alias


def test_shipped_config_probes_only_loopback_unless_external(shipped):
    from urllib.parse import urlsplit
    ps = shipped[3]
    ext_hosts = set()
    for p in ps:
        if p.type in ("http", "json") and str(p.target).startswith("http"):
            host = urlsplit(p.target).hostname
            if p.external:
                assert urlsplit(p.target).scheme == "https", p.name
                ext_hosts.add(host)
            else:
                assert host in ("127.0.0.1", "localhost"), (p.name, host)
        elif p.type == "tcp":
            assert p.external or p.target.startswith("127.0.0.1:"), p.name
        else:
            assert not p.external, p.name
    assert ext_hosts == {"1.1.1.1", "seerr.ohmzhomelab.ca", "maintainer.ohmzhomelab.ca"}


def test_shipped_config_is_read_only_and_secret_free(shipped):
    text, _doc, _d, ps, _e, _by = shipped
    assert all(p.method in ("GET", "HEAD") for p in ps)
    low = text.lower()
    for word in ("password", "passwd", "bearer", "authorization", "apikey", "api_key", "x-fan-token", "alert_transports", "alert_contacts",
                 "cookie", "token="):
        assert word not in low, word
    assert not re.search(r"://[^/\s\"']*@", text)                              # no userinfo in any URL
    assert not re.search(r"\b[A-Za-z0-9]{32,}\b", text)                          # no long opaque blobs
    verbs = {"restart", "stop", "start", "enable", "disable", "mask", "kill", "rm", "update", "prune", "delete", "reload", "reboot", "poweroff"}
    allowed = {"tailscale", "fail2ban-client", "nvidia-smi"}
    for p in ps:
        if p.type == "command":
            assert p.target[0] in allowed, p.name
            assert not verbs & set(p.target[1:]), p.name
            assert p.target[0] not in ("sh", "bash", "env", "sudo")


def test_shipped_config_never_queries_search_engines(shipped):
    for p in shipped[3]:
        assert "/search" not in str(p.target).split("?")[0] or not str(p.target).startswith("http"), p.name                          # the canary query is Hermes's job; a probe here would hammer SearXNG


def test_shipped_config_file_targets_stay_in_known_places(shipped):
    for p in shipped[3]:
        if p.type in ("file_age", "json") and not str(p.target).startswith("http"):
            assert p.target.startswith(("{STATE}/", "{RUN}/", "/home/ohmz/.hermes/")), p.name


def test_shipped_config_hermes_mirrors_do_not_double_page(shipped):
    by = shipped[5]
    mirrors = [p for n, p in by.items() if n.startswith(("hermes-wd-", "hermes-sc-"))]
    assert len(mirrors) == 13 and all(p.severity == "info" and p.source == "hermes" for p in mirrors)
    assert {p.target for p in mirrors} == {"/home/ohmz/.hermes/watchdog_state.json", "/home/ohmz/.hermes/search_canary_state.json"}
    for n in ("hermes-watchdog-alive", "hermes-canary-alive", "hermes-ticker", "hermes-api"):
        assert by[n].severity == "warn" and by[n].source == "hermes"          # nothing else watches the watchers
    keys = {m.expect["json"][0]["path"].split(".")[1] for m in mirrors if "watchdog" in m.target}
    assert keys == {"gateway", "api", "delivery", "backup", "flightclaw", "pubgate", "pubquota", "ticker", "gwrestarts", "backlog", "hermesver"}


def test_shipped_config_severity_and_cadence_policy(shipped):
    by = shipped[5]
    for n in ("docker-daemon", "svc-docker", "svc-cloudflared", "cloudflared-tunnel", "svc-ssh"):
        assert by[n].severity == "crit", n                                   # only the platform that everything stands on texts
    crit = {n for n, p in by.items() if p.severity == "crit"}
    assert len(crit) <= 8, crit                                              # a small, deliberate set (the SMS budget is 8 a day)
    for p in by.values():
        live = p.type in ("http", "tcp", "docker", "systemd") and p.cls in ("P0", "P1")      # a live service a person depends on
        assert p.interval_s >= 30 and (not live or p.interval_s <= 300 or p.name == "internet"), p.name
        assert p.interval_s <= 900, p.name
    per_min = sum(60 / p.interval_s for p in by.values() if not p.paused)
    assert per_min < 150, per_min                                            # cheap: well under three looks a second
    assert shipped[2]["deadman_key"] == "umbrella-probes" and shipped[2]["confirm"] == 2 and shipped[2]["attempts"] == 2


def test_shipped_config_runs_end_to_end_with_stubbed_runners(shipped, env, monkeypatch):
    """All 131-odd probes through the real engine (no network, no docker, no systemd) and out through monitors.json."""
    _t, doc, d, ps, errs, by = shipped
    for typ in probes.TYPES:
        monkeypatch.setitem(probes.RUNNERS, typ, lambda p, snap, now: (OK, "stub"))
    rep = go((d, ps, errs), NOW)
    assert len(rep.ran) == len(ps) and rep.elapsed_s < 5
    rows = probes.snapshot(NOW, (d, ps, errs))
    assert {r["state"] for r in rows} == {"up"}
    assert {r["source"] for r in rows} == {"native", "kuma", "hermes"} and {r["class"] for r in rows} == {"P0", "P1", "P2"}
    from homelab_maint.tasks import monitors
    out = json.dumps(monitors.export(NOW, (d, ps, errs)))
    assert len(out) < 200_000
    assert "127.0.0.1" not in out and "/home/ohmz" not in out


# =========================================================================== FIX 1: config trouble is never silent
def test_load_probes_separates_never_installed_from_present_but_unusable(env):
    assert probes.load_probes()[1:] == ([], [])                              # no file at all: quiet, nothing to report
    for body in ("", "# only a comment\n", "[defaults]\nconfirm = 3\n", "[[probe]]\nname = 'Bad Name'\n", "this = = bad"):
        _write_conf(env, "probes.toml", body)
        _d, ps, errs = probes.load_probes()
        assert ps == [] and errs, body                                       # present, but nothing runs: always an error
    _write_conf(env, "probes.toml", "")
    assert "defines no probes" in probes.load_probes()[2][0]


def test_load_probes_names_the_file_and_the_reason_for_an_untrusted_config(env):
    _write_conf(env, "probes.toml", '[[probe]]\nname="a"\ntype="tcp"\ntarget="127.0.0.1:1"\n')
    os.chmod(core.CONF_DIR, 0o775)                                           # the "routine chmod g+w /etc/homelab-maint"
    _d, ps, errs = probes.load_probes()
    assert ps == [] and len(errs) == 1 and errs[0].startswith("probes.toml: ignored") and "group/world writable" in errs[0]


def test_run_due_records_a_blind_plane_announces_it_once_and_never_pushes_alive(env, monkeypatch):
    script(monkeypatch)
    errs = ["probes.toml: ignored (not owned by root/runner, or it or its directory is group/world writable)"]
    blind = ({**probes.DEFAULTS}, [], errs)
    rep = go(blind, NOW)
    assert rep.total == 0 and rep.errors == errs and rep.ran == [] and rep.pushes == []        # no dead-man heartbeat while blind
    assert [(e["probe"], e["to"], e["severity"]) for e in rep.events] == [("*config", "config", "crit")]
    assert "MONITORING BLIND" in rep.events[0]["detail"] and "probes.toml" in rep.events[0]["detail"]
    st = probes.load_state()
    assert st["cfg"]["blind"] is True and st["cfg"]["n"] == 1 and st["run"] == {}              # `run` is NOT refreshed: the heartbeat goes stale
    assert go(blind, NOW + 60).events == []                                                    # the same problem is announced once
    assert [e["probe"] for e in probes.claim_events(NOW + 61)] == ["*config"]                  # and it is in the delivery queue
    rep = go(conf_of(raw("a")), NOW + 120)                                                     # fixed
    assert [(e["probe"], e["to"], e["severity"]) for e in rep.events] == [("*config", "up", "recovery")]
    assert probes.load_state()["cfg"] == {"n": 0, "blind": False, "t": NOW + 120} and rep.pushes[-1][0] == "umbrella-probes"


def test_run_due_a_vanished_config_is_blind_not_nothing_configured(env, monkeypatch):
    script(monkeypatch)
    go(conf_of(raw("a"), raw("b")), NOW)
    rep = go(({**probes.DEFAULTS}, [], []), NOW + 60)                        # probes.toml deleted after 2 probes ran here
    assert rep.total == 0 and any("2 probes ran before" in e for e in rep.errors) and rep.events[0]["to"] == "config"
    assert probes.load_state()["cfg"]["blind"] is True


def test_run_due_partial_config_trouble_is_a_warn_event_while_the_good_probes_still_run(env, monkeypatch):
    script(monkeypatch)
    conf = conf_of(raw("a"))
    rep = go((conf[0], conf[1], ["extra.toml: ignored (group/world writable)"]), NOW)
    assert rep.ran == ["a"] and [(e["to"], e["severity"]) for e in rep.events] == [("config", "warn")]
    assert probes.load_state()["cfg"]["blind"] is False


# =========================================================================== FIX 5: one malformed probe never breaks the rest
BAD_FIELDS = [
    {"tags": 5}, {"tags": [1]}, {"expect": True}, {"expect": 5}, {"expect": "x"}, {"class": ["x"]}, {"class": 5}, {"severity": 5},
    {"severity": ["crit"]}, {"target": "http://[::1/x"}, {"target": "http://127.0.0.1:80a0/"}, {"target": "http://127.0.0.1:99999/"},
    {"target": "http://127.0.0.1:abc/"}, {"target": "http://127.0.0.1/ a"}, {"target": "http://127.0.0.1/\n"}, {"target": 5}, {"target": None},
    {"target": ["x"]}, {"interval_s": "fast"}, {"timeout_s": True}, {"confirm": [1]}, {"interval_s": float("nan")}, {"slo": "99"},
    {"slo": 150}, {"paused": "false"}, {"optional": 1}, {"external": "yes"}, {"title": 5}, {"group": 5}, {"kuma": []},
    {"when_running": 3}, {"kuma_push_key": 5}, {"name": 5}, {"name": None}, {"method": ["GET"]}, {"source": ["native"]},
    {"expect": {"json": 5}}, {"expect": {"json": [{"path": "a", "min": "x"}]}}, {"expect": {"json": [{"path": "a", "in": "x"}]}},
    {"expect": {"json": [{"path": "a", "exists": "yes"}]}}, {"expect": {"json": [{"path": ""}]}},
]


@pytest.mark.parametrize("bad", BAD_FIELDS)
def test_build_rejects_junk_types_without_raising(bad):
    r = {"name": "t", "type": "http", "target": "http://127.0.0.1/", **bad}
    p, errs = probes.build(r, probes.DEFAULTS)
    assert p is None and errs, bad
    d, ps, perrs = probes.parse({"probe": [r, raw("good")]})                  # and through parse(): the neighbour still loads
    assert [x.name for x in ps] == ["good"] and perrs


@pytest.mark.parametrize("typ,target", [
    ("tcp", "127.0.0.1:99999"), ("tcp", "127.0.0.1:0"), ("command", ["ls", "a\0b"]), ("file_age", "{BOGUS}/x"), ("file_age", "rel/x"),
    ("systemd", "a\0b.service"), ("docker", "x" * 200)])
def test_build_rejects_targets_a_runner_would_choke_on(typ, target):
    r = {"name": "t", "type": typ, "target": target, "expect": {"max_age_s": 5} if typ == "file_age" else {}}
    assert probes.build(r, probes.DEFAULTS)[0] is None


def test_build_clamps_absurd_numbers_instead_of_raising():
    p = mk(target="http://127.0.0.1/", interval_s=10 ** 400, timeout_s=1e308, confirm=-5)
    assert (p.interval_s, p.timeout_s, p.confirm) == (86400 * 7, 30, 1)
    assert probes._num(float("nan"), 1, 2, 7) == 7 and probes._num(True, 1, 2, 7) == 7 and probes._num("5", 1, 2, 7) == 7


def test_parse_isolates_an_unexpected_exception_to_the_one_probe(monkeypatch):
    real = probes.build

    def boom(r, d):
        if r.get("name") == "boom":
            raise RuntimeError("bug")
        return real(r, d)

    monkeypatch.setattr(probes, "build", boom)
    _d, ps, errs = probes.parse({"probe": [raw("a"), raw("boom"), raw("b")]})
    assert [p.name for p in ps] == ["a", "b"] and errs == ["boom: invalid definition (RuntimeError)"]


def test_parse_defaults_are_validated_and_clamped():
    d, _ps, errs = probes.parse({"defaults": {"workers": "many", "budget_s": 10 ** 9, "attempts": 0, "retry_s": float("nan"),
                                              "deadman_key": "Bad Key!", "confirm": True}, "probe": [raw("a")]})
    assert d["workers"] == probes.DEFAULTS["workers"] and d["budget_s"] == 600 and d["attempts"] == 1
    assert d["retry_s"] == probes.DEFAULTS["retry_s"] and d["deadman_key"] == "umbrella-probes" and d["confirm"] == 2
    assert len(errs) == 4 and any("defaults.workers" in e for e in errs) and any("deadman_key" in e for e in errs)


JUNK = [None, True, False, 0, -1, 5, 10 ** 400, 1e308, float("nan"), float("inf"), "", "x", "http://127.0.0.1/", "a" * 300, "\0",
        "{STATE}/x", [], ["x"], [1], [[]], {}, {"a": 1}, {"path": "a"}, [{"path": "a", "equals": 1}], [{"path": 1}], {"json": 5},
        {"max_age_s": "x"}, {"status": [200]}, {"except": ["("]}, {"names": [1]}]
FIELDS = ["name", "title", "type", "target", "expect", "interval_s", "timeout_s", "class", "severity", "slo", "confirm", "recover", "tags",
          "group", "source", "kuma", "kuma_paused", "kuma_push_key", "optional", "paused", "when_running", "needs_root", "external",
          "insecure", "method"]


VALID_TARGET = {"http": "http://127.0.0.1:80/", "tcp": "127.0.0.1:80", "docker": "kavita", "systemd": "u.service", "command": ["true"],
                "file_age": "/tmp/x", "json": "http://127.0.0.1:80/j"}


def test_parse_never_raises_whatever_the_field_types(env):
    """Property-style: thousands of probes with random junk in random fields, flat and in groups, with junk [defaults]."""
    rnd = random.Random(20261002)
    accepted = rejected = 0
    for _ in range(8000):
        typ = rnd.choice(probes.TYPES)
        r = {"name": f"p{rnd.randrange(40)}", "type": typ, "target": VALID_TARGET[typ], **({"expect": {"max_age_s": 5}} if typ == "file_age" else {})}
        for _k in range(rnd.randrange(0, 3)):
            r[rnd.choice(FIELDS)] = rnd.choice(JUNK)
        grp = {"group": rnd.choice(JUNK), "probes": rnd.choice([[r], r, [5], None, "x", [r, 3]]), rnd.choice(FIELDS): rnd.choice(JUNK)}
        doc = {"defaults": {rnd.choice(list(probes.DEFAULTS)): rnd.choice(JUNK)}, "probe": rnd.choice([[r], r, 5, None, [r, "junk"]]),
               "group": rnd.choice([[grp], grp, "x", 7, None])}
        _d, ps, errs = probes.parse(doc)
        assert all(isinstance(e, str) for e in errs)
        for p in ps:
            accepted += 1
            assert isinstance(probes.how(p), str) and p.cls in probes.CLASSES and p.severity in ("crit", "warn", "info")
            assert 10 <= p.interval_s <= 86400 * 7 and isinstance(p.tags, tuple)
        rejected += not ps
    assert accepted > 500 and rejected > 500                                  # both branches are exercised, a lot
    for top in JUNK:                                                          # junk at the TOP level of the document
        probes.parse({"probe": top, "group": top, "defaults": top})


def test_load_probes_survives_junk_documents_and_keeps_the_good_probes(env):
    _write_conf(env, "probes.toml", 'probe = 5\n[defaults]\nworkers = "many"\n[[group]]\ngroup = "G"\nprobes = [\n'
                                    '  {name="ok", type="tcp", target="127.0.0.1:9"},\n  {name="bad", type="tcp", target="127.0.0.1:99999"},\n  5]\n')
    _write_conf(env, "probes.d/odd.toml", '[probe]\nname = "tablenotarray"\n')
    (core.CONF_DIR / "probes.d" / "binary.toml").write_bytes(b"\xff\xfe[[probe]]")
    os.chmod(core.CONF_DIR / "probes.d" / "binary.toml", 0o644)
    _d, ps, errs = probes.load_probes()
    assert [p.name for p in ps] == ["ok"]
    joined = "\n".join(errs)
    for needle in ("probe must be an array", "defaults.workers", "bad:", "not a table", "binary.toml: unreadable"):
        assert needle in joined or needle in joined.replace("a member is not a table", "not a table"), needle


def test_one_bad_probe_does_not_break_the_run_the_export_or_the_heartbeat(env, monkeypatch):
    from homelab_maint.tasks import monitors
    script(monkeypatch)
    _write_conf(env, "probes.toml", '[defaults]\nattempts = 1\n[[probe]]\nname="good"\ntype="http"\ntarget="http://127.0.0.1:1/"\n'
                                    '[[probe]]\nname="typo"\ntype="http"\ntarget="http://127.0.0.1:80a0/"\n'
                                    '[[probe]]\nname="v6"\ntype="http"\ntarget="http://[::1/x"\n[[probe]]\nname="tags"\ntype="tcp"\ntarget="127.0.0.1:2"\ntags = 5\n')
    assert probes.main(["validate"]) == 1                                    # `validate` now says what is wrong (it used to say OK for :80a0)
    rep = probes.run_due(NOW, sleep=lambda s: None, pusher=lambda *a: None)
    assert rep.ran == ["good"] and len(rep.errors) == 3
    assert [p["name"] for p in monitors.export(NOW)["probes"]] == ["good"] and monitors.heartbeat_payload(NOW + 5)["total"] == 1
    hand_made = probes.Probe(name="x", title="x", type="http", target="http://127.0.0.1:80a0/")      # even a Probe that bypassed build()
    assert probes.how(hand_made) == "GET :80/"


def test_load_state_resets_wrong_typed_fields_instead_of_trusting_them(env):
    (core.STATE_DIR / "probes.json").write_text(json.dumps({"v": 1, "events": "junk", "probes": [], "inflight": {}, "fleet": 5,
                                                            "hist_t": "x", "seq": True, "run": {"total": 3}}))
    st = probes.load_state()
    assert st["events"] == [] and st["probes"] == {} and st["inflight"] == [] and st["fleet"] == {} and st["hist_t"] == 0 and st["seq"] == 0
    assert st["run"] == {"total": 3}


# =========================================================================== FIX 4: two passes, nothing sleeps in a worker
def execute(pl, looks, monkeypatch, st=None, sleep=None, **d):
    """probes._execute over scripted looks: looks[name] is a list of (level, detail) consumed one per look; returns (verdicts, calls, slept)."""
    calls: dict[str, int] = {}
    slept: list[float] = []
    box = script(monkeypatch)

    def run(p):
        calls[p.name] = calls.get(p.name, 0) + 1
        seq = looks[p.name]
        return seq[min(calls[p.name], len(seq)) - 1]

    box["r"] = run
    dd = {**probes.DEFAULTS, "retry_s": 7, **d}
    out = probes._execute([mk(name=n, target="http://x/") for n in pl], st or {"probes": {}}, probes.Snapshot([]), NOW, dd, sleep or slept.append)
    return out, calls, slept


def test_pass_one_looks_at_everything_once_then_one_pause_then_only_the_failures(monkeypatch):
    out, calls, slept = execute(["a", "b", "c"], {"a": [(OK, "fine")], "b": [(FAIL, "blip"), (OK, "fine")], "c": [(FAIL, "x"), (FAIL, "y")]}, monkeypatch)
    assert slept == [7]                                                      # ONE pause for the whole run, in the caller's thread
    assert calls == {"a": 1, "b": 2, "c": 2}                                 # the clean probe is not looked at again
    assert out["a"][0] == OK and out["b"][0] == OK and out["c"][:2] == (FAIL, "y")      # a blip that clears inside the run never counts


def test_no_pause_when_every_probe_is_clean(monkeypatch):
    out, calls, slept = execute(["a", "b"], {"a": [(OK, "x")], "b": [(SKIP, "by design")]}, monkeypatch)
    assert slept == [] and calls == {"a": 1, "b": 1} and out["b"][0] == SKIP


def test_attempts_bounds_the_number_of_passes_and_the_best_look_wins(monkeypatch):
    out, calls, slept = execute(["a"], {"a": [(FAIL, "1"), (WARN, "2"), (FAIL, "3")]}, monkeypatch, attempts=3)
    assert slept == [7, 7] and calls == {"a": 3} and out["a"][:2] == (WARN, "2")
    out, calls, slept = execute(["a"], {"a": [(FAIL, "1"), (FAIL, "2")]}, monkeypatch, attempts=1)
    assert slept == [] and calls == {"a": 1}


def test_no_retry_pass_when_the_budget_has_no_room_for_a_pause_and_a_pass(monkeypatch):
    out, calls, slept = execute(["a"], {"a": [(FAIL, "x"), (OK, "y")]}, monkeypatch, budget_s=3, retry_s=5)
    assert slept == [] and calls == {"a": 1} and out["a"][0] == FAIL         # pass 1's real look stands


def test_a_full_outage_costs_two_passes_and_one_pause_not_the_whole_budget(env, monkeypatch):
    """The reviewer's case: 131 probes that all fail fast. Before: every worker slept retry_s, the run took the full budget and 59
    probes were judged by the budget. Now: every probe gets two looks and the run takes ~2 passes + one pause."""
    calls: dict[str, int] = {}
    lock = threading.Lock()

    def down(p, snap, now):
        time.sleep(0.01)
        with lock:
            calls[p.name] = calls.get(p.name, 0) + 1
        return FAIL, "refused"

    monkeypatch.setitem(probes.RUNNERS, "http", down)
    slept = []
    conf = conf_of(*[raw(f"p{i}") for i in range(131)])
    t0 = time.monotonic()
    rep = probes.run_due(NOW, conf=conf, force=True, sleep=slept.append, pusher=lambda *a: None)
    assert len(rep.ran) == 131 and slept == [5.0] and set(calls.values()) == {2}
    assert time.monotonic() - t0 < 5 and all("did not finish" not in r["detail"] for r in probes.snapshot(NOW, conf))


def test_probes_the_budget_never_reached_keep_their_state_and_go_first_next_time(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a"), raw("b"), raw("c"), budget_s=0.4, workers=1, attempts=1, confirm=1)
    go(conf, NOW)                                                            # all three clean: state exists, availability 1/1
    before = {n: (probes.load_state()["probes"][n]["last_run"], probes.load_state()["days"][n]) for n in "abc"}
    gate = threading.Event()
    box["r"] = lambda p: (gate.wait(10), (OK, "late"))[1] if p.name == "a" else (OK, "fine")
    rep = go(conf, NOW + 60, force=True)                                     # `a` hangs for the whole budget; b and c are never started
    assert rep.ran == ["a"]
    st = probes.load_state()
    assert st["probes"]["a"]["lvl"] == FAIL and "did not finish" in st["probes"]["a"]["detail"]      # started and never finished: a FAIL
    for n in "bc":                                                           # never looked at: NO verdict, NO availability sample, still due
        assert (st["probes"][n]["last_run"], st["days"][n]) == before[n], n
        assert st["probes"][n]["lvl"] == OK and st["probes"][n]["detail"] == "fine"
    gate.set()
    order = []
    box["r"] = lambda p: (order.append(p.name), (OK, "fine"))[1]
    go(conf, NOW + 120, force=True)
    assert order == ["b", "c", "a"]                                          # starved probes first (oldest last_run), then the rest


def test_a_probe_whose_retry_hangs_keeps_its_pass_one_verdict_not_a_budget_fail(env, monkeypatch):
    box = script(monkeypatch)
    gate = threading.Event()
    n = {"x": 0}

    def run(p):
        n["x"] += 1
        if n["x"] == 1:
            return FAIL, "refused"
        gate.wait(10)
        return OK, "late"

    box["r"] = run
    conf = conf_of(raw("x"), budget_s=1.4, retry_s=0, attempts=2, confirm=1)
    go(conf, NOW)
    r = probes.snapshot(NOW, conf)[0]
    assert r["state"] == "down" and r["detail"] == "refused"                 # the real look, not "did not finish within the run budget"
    gate.set()


def test_an_optional_probe_that_hangs_before_it_was_ever_up_is_skipped_not_failed(env, monkeypatch):
    gate = threading.Event()
    monkeypatch.setitem(probes.RUNNERS, "http", lambda p, snap, now: (gate.wait(10), (OK, "x"))[1])
    conf = conf_of(raw("o", optional=True), budget_s=0.3, attempts=1, confirm=1)
    go(conf, NOW)
    assert probes.snapshot(NOW, conf)[0]["state"] == "skipped"
    gate.set()


# =========================================================================== FIX 3: the event queue is two-phase
def put_events(*evs):
    st = probes.load_state()
    probes._queue(st, [{**e, "id": e.get("id", f"q{i}"), "t": e.get("t", NOW)} for i, e in enumerate(evs, 1)])
    probes.save_state(st)


EV = {"probe": "a", "title": "A", "to": "down", "severity": "warn", "class": "P2", "detail": "x", "dedupe_key": "probe:a"}


def test_claim_keeps_events_until_acked_most_urgent_first(env):
    put_events({**EV, "id": "r", "severity": "recovery", "to": "up"}, {**EV, "id": "w-old", "t": NOW - 50}, {**EV, "id": "c-old", "severity": "crit", "t": NOW - 40},
               {**EV, "id": "w-new", "t": NOW - 5}, {**EV, "id": "c-new", "severity": "crit", "t": NOW - 1})
    got = probes.claim_events(NOW)
    assert [e["id"] for e in got] == ["c-new", "c-old", "w-new", "w-old", "r"]       # crit first, newest first within a severity, recoveries last
    st = probes.load_state()
    assert st["events"] == [] and len(st["inflight"]) == 5 and all(e["ct"] == NOW for e in st["inflight"])   # nothing was deleted
    assert probes.claim_events(NOW + 30) == []                                       # claimed: a second deliverer does not repeat them
    probes.ack_event("c-new")
    probes.ack_event("nope")                                                         # unknown ids are harmless
    assert sorted(e["id"] for e in probes.load_state()["inflight"]) == ["c-old", "r", "w-new", "w-old"]


def test_a_claim_whose_deliverer_died_is_requeued_after_the_ttl_not_lost(env):
    put_events({**EV, "id": "1"}, {**EV, "id": "2", "probe": "b"})
    assert len(probes.claim_events(NOW)) == 2
    # ... the process is killed here: nothing acked, nothing released ...
    assert probes.claim_events(NOW + probes.CLAIM_TTL_S) == []                       # a slow deliverer still owns them
    again = probes.claim_events(NOW + probes.CLAIM_TTL_S + 1)
    assert sorted(e["id"] for e in again) == ["1", "2"] and all(e["ct"] == NOW + probes.CLAIM_TTL_S + 1 for e in again)


def test_release_failed_backs_off_and_release_unfailed_does_not(env):
    put_events({**EV, "id": "1"}, {**EV, "id": "2", "probe": "b"}, {**EV, "id": "3", "probe": "c"})
    probes.claim_events(NOW)
    probes.release_events(["1"], failed=True, now=NOW)
    probes.release_events(["2"], failed=False, now=NOW)
    st = probes.load_state()
    assert {e["id"]: (e.get("tries", 0), e.get("nb", 0)) for e in st["events"]} == {"1": (1, NOW + 60), "2": (0, 0)}
    assert [e["id"] for e in probes.claim_events(NOW + 10)] == ["2"]                  # 1 is still backing off
    assert [e["id"] for e in probes.claim_events(NOW + 61)] == ["1"]
    probes.release_events(["1"], failed=True, now=NOW + 61)
    probes.release_events(["1"], failed=True, now=NOW + 61)                           # (second call: not inflight any more, a no-op)
    assert probes.load_state()["events"][0]["nb"] == NOW + 61 + 120
    for i in range(10):
        put_events({**EV, "id": "x"})
        probes.claim_events(NOW + 10000 * i)
        probes.release_events(["x"], failed=True, now=NOW)
    assert max(e.get("nb", 0) for e in probes.load_state()["events"]) <= NOW + 600    # capped at 10 minutes


def test_pop_events_legacy_drains_queued_and_inflight(env):
    put_events({**EV, "id": "1"}, {**EV, "id": "2", "probe": "b"})
    probes.claim_events(NOW)
    put_events({**EV, "id": "3", "probe": "c"})
    assert sorted(e["id"] for e in probes.pop_events()) == ["1", "2", "3"] and probes.pop_events() == []


def test_events_get_unique_ids_and_old_queue_entries_get_one_on_claim(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("a", confirm=1), raw("b", confirm=1))
    go(conf, NOW)
    box["r"] = (FAIL, "x")
    go(conf, NOW + 60)
    ids = [e["id"] for e in probes.load_state()["events"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    st = probes.load_state()
    st["events"] = [{"probe": "old", "severity": "warn", "to": "down", "t": NOW}]                # queued by a version without ids
    probes.save_state(st)
    assert probes.claim_events(NOW)[0]["id"].startswith("e")


# =========================================================================== FIX 6: flap damping must not hide a probe that stays down
def test_a_probe_that_flaps_and_then_stays_down_is_announced_once_the_level_holds(env, monkeypatch):
    """The reviewer's case: down, up, down, up, down (permanent). The 5th confirmed change is the one that makes it 'flapping',
    so the real 'down' used to stay unannounced until the 4th-latest change left the 1 h window."""
    box = script(monkeypatch)
    conf = conf_of(raw("svc", title="Svc", **{"class": "P0"}), confirm=1, recover=1, flap_max=4)
    got = []
    for i, lvl in enumerate([FAIL, OK, FAIL, OK, FAIL] + [FAIL] * 20):
        box["r"] = (lvl, "refused")
        got += [(i, e["to"], e["severity"]) for e in go(conf, NOW + 60 * i).events]
    assert [t for _i, t, _s in got[:4]] == ["down", "up", "down", "up"]
    assert got[4][1:] == ("flapping", "warn")                                # run 4: flapping begins (the 'down' of run 4 is not announced yet)
    last = got[-1]
    assert last[1:] == ("down", "crit")                                      # announced with the CRIT severity of a P0 probe...
    assert last[0] == 4 + 10                                                 # ...as soon as the level has held 10 minutes, not an hour later
    assert [t for _i, t, _s in got].count("down") == 3


def test_a_flapping_probe_that_settles_up_gets_its_recovery_after_the_level_holds(env, monkeypatch):
    box = script(monkeypatch)
    conf = conf_of(raw("svc"), confirm=1, recover=1, flap_max=4)
    got = []
    for i, lvl in enumerate([OK, FAIL, OK, FAIL, OK] + [OK] * 20):
        box["r"] = (lvl, "fine")
        got += [(i, e["to"]) for e in go(conf, NOW + 60 * i).events]
    assert got == [(1, "down"), (2, "up"), (3, "down"), (4, "flapping"), (14, "up")]       # the up of run 4 waits until it has held 10 min


def test_flap_settle_time_scales_with_a_slow_probes_interval(env):
    d = dict(probes.DEFAULTS)
    mkst = lambda held: {"probes": {"x": {"lvl": FAIL, "since": NOW - held, "ev": 0, "fl": True, "tr": [NOW - 100 * i for i in range(1, 5)], "detail": "d"}}, "seq": 0}
    slow, quick = mk(name="x", target="http://x/", interval_s=900), mk(name="x", target="http://x/", interval_s=60)
    assert probes._events([slow], mkst(700), NOW, d) == []                                  # flapping, and the level has held only 700 s
    assert [e["to"] for e in probes._events([slow], mkst(1900), NOW, d)] == ["down"]        # 2 x 900 s = 1800 s must have passed
    assert [e["to"] for e in probes._events([quick], mkst(700), NOW, d)] == ["down"]        # 10 minutes is enough for a 1-minute probe


# =========================================================================== FIX 7a: containers that vanish
H8 = 8 * 3600                                                                # past FLEET_MIN_S (6 h): a container this old is a service
FLEET_OUT = "web|running|Up 2 hours (healthy)\ndb|running|Up 2 hours\ntmp-1|running|Up 1 minute\n"


def fleet_world(monkeypatch, text=FLEET_OUT, rc=0):
    """A scripted `docker ps -a`: mutate box["out"] / box["rc"] between runs."""
    box = {"out": text, "rc": rc}
    monkeypatch.setattr(probes, "sh", lambda cmd, timeout=60, **kw: subprocess.CompletedProcess(cmd, box["rc"], box["out"], ""))
    return box


def fleet_conf(**ex):
    return conf_of({"name": "fleet", "type": "docker", "target": "*", "expect": {"except": ["^tmp-"], **ex}}, confirm=1, recover=1, attempts=1)


def test_fleet_reports_an_established_container_that_vanished_from_docker_ps(env, monkeypatch):
    world = fleet_world(monkeypatch)
    conf = fleet_conf()
    row = lambda t: probes.snapshot(t, conf)[0]
    go(conf, NOW)
    assert row(NOW)["state"] == "up"
    go(conf, NOW + H8)                                                     # seen running for 8 h: now it is part of the baseline
    world["out"] = "db|running|Up 3 hours\n"                                 # `docker rm web` / `compose down web`: no stopped corpse is left behind
    go(conf, NOW + H8 + 60)
    r = row(NOW + H8 + 60)
    assert r["state"] == "down" and "missing: web" in r["detail"] and "tmp-1" not in r["detail"]      # tmp-* is excepted: throw-away by design
    world["out"] = FLEET_OUT                                                 # it is back
    go(conf, NOW + H8 + 120)
    assert row(NOW + H8 + 120)["state"] == "up"


def test_fleet_does_not_flag_short_lived_or_never_seen_containers(env, monkeypatch):
    world = fleet_world(monkeypatch)
    conf = fleet_conf()
    go(conf, NOW)
    world["out"] = FLEET_OUT + "oneshot|running|Up 5 seconds\n"
    go(conf, NOW + 300)
    go(conf, NOW + 600)                                                      # observed for 5 minutes only: far from established (6 h)
    world["out"] = FLEET_OUT
    go(conf, NOW + 660)
    assert probes.snapshot(NOW + 660, conf)[0]["state"] == "up"              # a `docker run --rm` helper that finished is not "missing"
    first, last = probes.load_state()["fleet"]["oneshot"]
    assert last - first == 300 < probes.FLEET_MIN_S == 6 * 3600


def test_fleet_forgets_after_the_window_and_on_demand(env, monkeypatch, capsys):
    world = fleet_world(monkeypatch)
    conf = fleet_conf(forget_after_s=1000)
    go(conf, NOW)
    go(conf, NOW + H8)
    world["out"] = "db|running|Up 3 hours\n"
    go(conf, NOW + H8 + 300)
    assert probes.snapshot(NOW + H8 + 300, conf)[0]["state"] == "down"
    go(conf, NOW + H8 + 1001)                                              # vanished for longer than forget_after_s: no longer reported
    assert probes.snapshot(NOW + H8 + 1001, conf)[0]["state"] == "up"
    conf2 = fleet_conf()                                                     # default window (3 days): `forget` ends it at once
    go(conf2, NOW + H8 + 1100)
    assert probes.snapshot(NOW + H8 + 1100, conf2)[0]["state"] == "down"
    assert probes.main(["forget", "web", "never-heard-of"]) == 0
    assert "forgot 1/2: web" in capsys.readouterr().out
    go(conf2, NOW + H8 + 1160)
    assert probes.snapshot(NOW + H8 + 1160, conf2)[0]["state"] == "up" and probes.forget(["web"]) == []


def test_fleet_failed_docker_read_teaches_nothing_and_does_not_poison_the_baseline(env, monkeypatch):
    world = fleet_world(monkeypatch)
    conf = fleet_conf()
    go(conf, NOW)
    go(conf, NOW + H8)
    before = json.dumps(probes.load_state()["fleet"], sort_keys=True)
    world["rc"], world["out"] = 1, ""
    go(conf, NOW + H8 + 100)
    r = probes.snapshot(NOW + H8 + 100, conf)[0]
    assert r["state"] == "down" and "docker not answering" in r["detail"]    # fail closed as before
    assert json.dumps(probes.load_state()["fleet"], sort_keys=True) == before


def test_fleet_explicit_names_and_baseline_switch(env, monkeypatch):
    world = fleet_world(monkeypatch)
    conf = fleet_conf(names=["must-exist"], baseline=False)
    go(conf, NOW)
    r = probes.snapshot(NOW, conf)[0]
    assert r["state"] == "down" and "missing: must-exist" in r["detail"]    # an explicit name needs no learning period
    go(conf, NOW + H8)
    world["out"] = "must-exist|running|Up 1 hour\n"
    go(conf, NOW + H8 + 60)
    assert probes.snapshot(NOW + H8 + 60, conf)[0]["state"] == "up"            # baseline=false: web/db vanishing is not reported
    for bad in ({"names": "x"}, {"names": [1]}, {"baseline": "no"}, {"forget_after_s": "3d"}):
        assert probes.build({"name": "f", "type": "docker", "target": "*", "expect": bad}, probes.DEFAULTS)[0] is None


def test_fleet_baseline_is_bounded_and_ages_out(env, monkeypatch):
    st = {"fleet": {f"c{i}": [NOW - 100, NOW - i] for i in range(520)}}
    st["fleet"]["old"] = [NOW - 90 * 86400, NOW - 60 * 86400]
    st["fleet"]["junk"] = "x"
    snap = probes.Snapshot([])
    probes._learn_fleet(st, snap, NOW)
    assert "old" not in st["fleet"] and "junk" not in st["fleet"] and len(st["fleet"]) == probes.FLEET_MAX
    assert "c0" in st["fleet"] and "c519" not in st["fleet"]                 # the longest-unseen names go first
    assert probes.Snapshot([], {"a": [1, 2], "b": "x", "c": [1], "d": [1, "x"]}).fleet == {"a": (1.0, 2.0)}


# =========================================================================== FIX 7b + glue: shipped config
def test_shipped_config_watches_the_scheduler_tick_beat_not_just_its_timer(shipped, env):
    by = shipped[5]
    t = by["umbrella-tick"]
    assert (t.type, t.target, t.cls, t.severity, t.optional) == ("json", "{RUN}/tick.json", "P0", "crit", True)
    assert t.expect == {"json": [{"path": "t", "age_max_s": 300, "age_warn_s": 150}, {"path": "jobs", "min": 1},
                                 {"path": "config_problems", "max": 0}]}
    assert by["svc-hm-tick"].type == "systemd"                               # the unit probe stays, but it proves nothing about the beat
    beat = core.RUN_DIR / "tick.json"                                        # scheduler._heartbeat() writes {"t": now, "ms": ..., ...}
    beat.write_text(json.dumps({"t": NOW - 20, "ms": 12, "running": 0, "errors": 0, "config_problems": 0, "jobs": 20}))
    assert look(t)[0] == OK
    beat.write_text(json.dumps({"t": NOW - 20, "ms": 12, "config_problems": 3, "jobs": 0}))     # a jobs.toml typo: beating, schedules nothing
    lvl, detail, _ = look(t)
    assert lvl == FAIL and "jobs" in detail
    beat.write_text(json.dumps({"t": NOW - 20, "ms": 12, "config_problems": 1, "jobs": 19}))    # one bad job skipped, the rest scheduled: still down
    lvl, detail, _ = look(t)                                                                       # (a backup that silently never runs)
    assert lvl == FAIL and "config_problems" in detail
    beat.write_text(json.dumps({"t": NOW - 200, "config_problems": 0, "jobs": 20}))
    assert look(t)[0] == WARN
    beat.write_text(json.dumps({"t": NOW - 4000, "config_problems": 0, "jobs": 20}))   # the timer is "active (waiting)" but every tick fails: no beat
    lvl, detail, _ = look(t)
    assert lvl == FAIL and "age" in detail
    beat.unlink()
    assert look(t)[0] == FAIL                                                # seen up once, then no file (reboot wipes /run): fail closed
    assert probes.attempt(t, probes.Snapshot([]), NOW, never_up=True)[0] == SKIP      # ...but optional before the first tick ever


def test_shipped_fleet_probe_uses_the_learned_baseline(shipped):
    ct = shipped[5]["ct-fleet"]
    assert ct.target == "*" and ct.expect.get("baseline", True) is True and "forget_after_s" not in ct.expect
    assert {"^comfyui$"} == set(ct.expect["except"])                                  # the stopped-by-design one stays out of it


def test_shipped_job_that_drives_the_engine_is_managed_monitor_and_not_named_like_the_task():
    """Glue contract (etc/jobs.toml is owned by the scheduler stream): the one-minute driver must be a MANAGED, `monitor` job whose
    name is NOT the registry task name `probes` (scheduler.merge_status writes tasks[<job name>]: a job called `probes` would
    overwrite the C0 task row that carries the down/crit probes with a perpetual green one), must not notify by itself, and its
    timeout must cover the engine budget plus the delivery cap."""
    from homelab_maint.tasks import monitors
    jobs = tomllib.loads((ROOT / "etc" / "jobs.toml").read_text())["job"]
    drivers = [j for j in jobs if "homelab_maint.probes" in " ".join(j["command"])]
    assert len(drivers) == 1
    j = drivers[0]
    assert j["name"] != "probes" and j["name"] not in ("probe", "monitors")
    assert j.get("mode") == "managed"                                        # an unset mode is "observe": shown, never run
    assert j.get("monitor") is True                                          # ignores PAUSE, pressure, max_concurrent, the heavy mutex
    assert j["notify"]["on_failure"] == "none" and "gates" not in j          # the engine pages through probe events, never through the job
    assert "--notify" in j["command"] and j["schedule"] == "* * * * *"
    assert j["timeout_s"] >= probes.DEFAULTS["budget_s"] + monitors.NOTIFY_BUDGET_S
