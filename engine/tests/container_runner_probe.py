#!/usr/bin/env python3
"""Runs INSIDE a throw-away container as root (tests/test_acks_auth_container.py; never run on the host). The case no single-uid test can see:
the PRODUCTION runner path as real root against the website as uid/gid 10001 with no supplementary groups, on the container's own filesystem.

  1. `homelab-maint ack init --group 10001` lays out ack/ (0750 root:10001, inbox 1730, web.key 0640 root:10001) with real chowns; the
     website's uid can traverse ack/, read tokens.json and web.key, create (not list) in the inbox and not read bootstrap.secret; a stranger
     (uid 10002) cannot even enter ack/.
  2. `homelab-maint web bootstrap` refuses where it must (no terminal).
  3. The real web/app.py as 10001 receives the first-run setup (with an authenticator); the runner (`homelab-maint ack process`, the command
     of the ack-process job and the tick's call) answers it; auth.json is root:10001 0640; the site sees it, the owner logs in with the
     passphrase and the authenticator code; a recovery code is burned durably; `web bootstrap` then refuses (setup complete).

Prints one JSON object {check: bool}. Mounts: the repository at /repo (read-only).
"""
import http.client
import json
import os
import re
import subprocess
import sys
import time

for k, v in (("STATE", "/st"), ("LOG", "/st/log"), ("RUN", "/st/run"), ("CONF", "/st/conf")):
    os.environ[f"HOMELAB_MAINT_{k}"] = v
os.environ.update(HOMELAB_MAINT_NO_SYSLOG="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH="/repo")
sys.dont_write_bytecode = True
sys.path[:0] = ["/repo", "/repo/web"]

WEB_GID, PORT = 10001, 18081
PASS, SECRET = "a long enough passphrase for the probe", "probe-bootstrap-secret-0123456789"
out: dict[str, bool] = {}


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def as_uid(uid: int, code: str) -> subprocess.CompletedProcess:
    """Run python code as uid:uid with NO supplementary groups: what the container user (or a stranger) is."""
    return subprocess.run([sys.executable, "-B", "-c", code], user=uid, group=uid, extra_groups=[], capture_output=True, text=True)


def runner(*args: str) -> subprocess.CompletedProcess:
    return sh(sys.executable, "-B", "-m", "homelab_maint.cli", *args, cwd="/repo")


class Site:
    def __init__(self):
        env = {"PATH": os.environ["PATH"], "PUBLIC_DIR": "/st/public", "ACK_DIR": "/st/ack", "STATIC_DIR": "/repo/web/static", "BIND": "127.0.0.1",
               "PORT": str(PORT), "PYTHONDONTWRITEBYTECODE": "1", "OUTER_AUTH": "access"}
        self.p = subprocess.Popen([sys.executable, "-B", "/repo/web/app.py"], user=WEB_GID, group=WEB_GID, extra_groups=[], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.req("GET", "/healthz")
                return
            except OSError:
                time.sleep(0.1)
        raise SystemExit("the website did not start")

    def req(self, method, path, body=None, jar=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
        h = {"Content-Type": "application/json"}
        if jar is not None:
            if "csrf" not in jar:
                jar["csrf"] = self.req("GET", "/api/csrf")[1]["csrf"]
            h["Cookie"] = f"__Host-mw_csrf={jar['csrf']}"
            h["X-CSRF-Token"] = jar["csrf"]
        c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {}

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()


def main() -> int:
    for d in ("/st", "/st/log", "/st/run", "/st/conf", "/st/public"):
        os.makedirs(d, mode=0o755, exist_ok=True)
    with open("/st/public/overview.json", "w") as f:
        json.dump({"generated_at": time.time(), "headline": "probe"}, f)
    os.chmod("/st/public/overview.json", 0o644)

    # ---- 1. the layout, from the command install.sh's twin
    r = runner("ack", "init", "--group", str(WEB_GID))
    out["init_exits_0"] = r.returncode == 0 and "created web.key" in r.stdout
    st = lambda p: os.stat(p)                                                                              # noqa: E731
    out["ack_is_0750_root_web_gid"] = (st("/st/ack").st_mode & 0o7777, st("/st/ack").st_uid, st("/st/ack").st_gid) == (0o750, 0, WEB_GID)
    out["inbox_is_1730_root_web_gid"] = (st("/st/ack/inbox").st_mode & 0o7777, st("/st/ack/inbox").st_gid) == (0o1730, WEB_GID)
    out["web_key_is_0640_root_web_gid"] = (st("/st/ack/web.key").st_mode & 0o777, st("/st/ack/web.key").st_gid) == (0o640, WEB_GID)
    with open("/st/ack/bootstrap.secret", "w") as f:
        f.write(SECRET + "\n")
    os.chmod("/st/ack/bootstrap.secret", 0o600)
    out["site_uid_reads_tokens_and_the_key"] = as_uid(WEB_GID, "open('/st/ack/tokens.json').read(); open('/st/ack/web.key').read()").returncode == 0
    out["site_uid_cannot_read_the_bootstrap_secret"] = as_uid(WEB_GID, "open('/st/ack/bootstrap.secret').read()").returncode != 0
    out["site_uid_creates_in_the_inbox_but_cannot_list_it"] = (
        as_uid(WEB_GID, "open('/st/ack/inbox/.probe','w').write('x')").returncode == 0 and as_uid(WEB_GID, "import os; os.listdir('/st/ack/inbox')").returncode != 0)
    os.unlink("/st/ack/inbox/.probe")
    out["a_stranger_cannot_enter_ack"] = as_uid(10002, "import os; os.listdir('/st/ack')").returncode != 0 and as_uid(10002, "open('/st/ack/tokens.json').read()").returncode != 0

    # ---- 2. the bootstrap command refuses a non-terminal
    r = runner("web", "bootstrap")
    out["bootstrap_refuses_a_pipe_and_prints_nothing"] = r.returncode == 1 and SECRET not in r.stdout + r.stderr and "not a terminal" in r.stderr

    site = Site()
    try:
        # ---- 3. the real first-run flow through the production command
        jar: dict = {}
        code, state = site.req("GET", "/api/auth/state")
        out["setup_required_at_first"] = code == 200 and state.get("setup_required") is True
        code, res = site.req("POST", "/api/auth/setup", {"bootstrap": SECRET, "passphrase": PASS, "totp": True}, jar)
        out["setup_queued"] = code == 202 and re.fullmatch(r"[0-9a-f]{8}", res.get("rid", "")) is not None
        rid = res["rid"]
        r = runner("ack", "process")
        out["runner_command_exits_0"] = r.returncode == 0
        st_ = os.stat("/st/ack/auth.json")
        out["auth_json_is_0640_root_web_gid"] = (st_.st_uid, st_.st_gid, st_.st_mode & 0o777) == (0, WEB_GID, 0o640)
        out["auth_json_readable_by_10001"] = as_uid(WEB_GID, "open('/st/ack/auth.json').read()").returncode == 0
        out["auth_json_unreadable_for_a_stranger"] = as_uid(10002, "open('/st/ack/auth.json').read()").returncode != 0
        code, state = site.req("GET", f"/api/auth/state?rid={rid}")
        out["site_sees_a_usable_auth_json"] = state.get("setup_required") is False and "error" not in state and state.get("totp") is True
        out["site_reports_ok_to_the_setup_screen"] = state.get("result") == {"ok": True, "reason": "ok", "kind": "auth_setup"}
        code, health = site.req("GET", "/healthz")
        out["healthz_has_no_ack_problem"] = not any("auth.json" in w or "setup is not complete" in w for w in health.get("warnings", []))
        import app
        now_code = app.hotp(app.b32decode(res["totp"]["secret"]), int(time.time() // 30))
        code, login = site.req("POST", "/api/auth/login", {"passphrase": PASS, "totp": now_code}, jar)
        out["owner_logs_in_with_the_authenticator"] = code == 200 and login.get("authenticated") is True
        out["secrets_are_not_in_auth_json"] = SECRET not in open("/st/ack/auth.json").read() and PASS not in open("/st/ack/auth.json").read()
        site.req("POST", "/api/auth/logout", {}, jar)
        code, login = site.req("POST", "/api/auth/login", {"passphrase": PASS, "totp": res["recovery"][0]}, jar)
        out["recovery_code_login"] = code == 200 and login.get("recovery_used") is True
        runner("ack", "process")
        st_ = os.stat("/st/ack/auth.json")
        out["burn_is_durable_and_keeps_the_group"] = (len(json.load(open("/st/ack/auth.json"))["recovery"]) == 7 and st_.st_gid == WEB_GID
                                                       and as_uid(WEB_GID, "open('/st/ack/auth.json').read()").returncode == 0)
        r = runner("web", "bootstrap")
        out["bootstrap_refuses_once_setup_is_complete"] = r.returncode == 1 and SECRET not in r.stdout + r.stderr and "already complete" in r.stderr
        log = "".join(open(os.path.join(dp, f), errors="replace").read() for dp, _d, fs in os.walk("/st") for f in fs
                      if f not in ("bootstrap.secret", "auth.json") and os.path.getsize(os.path.join(dp, f)) < 1 << 20)
        out["no_secret_in_any_log_or_state_file"] = SECRET not in log and PASS not in log
    finally:
        site.stop()
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
