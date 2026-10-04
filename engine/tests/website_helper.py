"""Test helper (not a test module): the REAL web/app.py on a loopback port, mounted on a tmp STATE tree the way the container sees it, with a
cookie-jar client. Shared by tests/test_acks_auth.py and tests/test_notify.py so that the runner's tests exercise the website's own validators."""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def have_web() -> bool:
    return (WEB / "app.py").is_file() and (WEB / "tools" / "ack_auth_handlers.py").is_file()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                           # (dataclasses look the module up)
    spec.loader.exec_module(mod)
    return mod


def load_app():
    return load("hm_web_app_for_tests", WEB / "app.py")


class Site:
    """web/app.py serving `tmp`/static over `state`/public with `ack` mounted; `clock` is the site's notion of now; `gate` = SETUP_GATE (first run
    without auth.json serves only the setup shell: the production default)."""

    def __init__(self, app, tmp: Path, state: Path, ack: Path, clock=time.time, gate: bool = True):
        (tmp / "static").mkdir(exist_ok=True)
        for name, html in (("index.html", "dashboard"), ("setup.html", "setup shell"), ("ack.html", "ack review page")):
            (tmp / "static" / name).write_text(f"<!doctype html><title>{name}</title><p>{html}</p>")
        (state / "public").mkdir(exist_ok=True)
        cfg = app.Config(public_dir=str(state / "public"), static_dir=str(tmp / "static"), port=0, bind="127.0.0.1", auth_fail_delay=0.0,
                         ack_dir=str(ack), log=lambda line: None, clock=clock, setup_gate=gate)
        self.app, self.srv, self.jar = app, app.Server(cfg), {}
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def req(self, method: str, path: str, body=None):
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.jar.items())} if self.jar else {}
        data = json.dumps(body).encode() if body is not None else None
        if body is not None:
            h["Content-Type"] = "application/json"
            if self.app.COOKIE_CSRF not in self.jar:
                self.req("GET", "/api/csrf")
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
            h["X-CSRF-Token"] = self.jar[self.app.COOKIE_CSRF]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, data, h)
            r = c.getresponse()
            raw = r.read()
            for k, v in r.getheaders():
                if k.lower() == "set-cookie":
                    name, _, rest = v.partition("=")
                    self.jar.pop(name, None) if "Max-Age=0" in v else self.jar.__setitem__(name, rest.split(";")[0])
        finally:
            c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
