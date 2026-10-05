"""Tiny read-only JSON server for the Homarr widgets: 127.0.0.1:<www_port> (default 9111).

Routes (GET only): /overview /disk /jobs /guard /reclaim /status  /thermal /load /metrics /heartbeat /pipeline
  * /thermal and /load are the 7-day sensor widgets (payloads_metrics, fed by the metrics ring); /metrics is the raw ring export;
    /heartbeat is the dead-man's switch for an external watcher (tasks.monitors.heartbeat_payload; HTTP 200 whatever "ok" says,
    so a Kuma keyword monitor can match '"ok":true'). None of them needs status.json;
  * /pipeline is the monitoring pipeline (tasks/self_health): the effective verdict a consumer must show, plus every part whose
    state is not ok or info. It reads STATE_DIR/public/self.json, so a widget can name the broken stage instead of only saying
    that something is wrong;
  * every answer is HTTP 200 JSON, because Homarr shows a red triangle for any non-200; trouble (no status file,
    unreadable file, payload bug) is reported in-band as {"error": ..., "stale": true};
  * unknown paths 404 and every other verb 405; there is no file or directory serving at all;
  * /status is status.json minus the C2 plans and task tracebacks.
The server only ever reads STATE_DIR/status.json (the unit mounts it read-only) and only binds to loopback.

  python3 -m homelab_maint.server [--port N] [--bind 127.0.0.1] [--status PATH] [--now EPOCH | --preview]
--preview freezes the clock 3 minutes after the status file's generated_at, so a saved sample never goes stale.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import payloads, payloads_metrics
from .core import CONF_DIR, STATE_DIR, load_toml, read_json

DEFAULT_PORT = 9111
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class StatusSource:
    """status.json reader with an mtime cache (the file is replaced atomically by the runner)."""

    def __init__(self, path: Path, now: float | None = None, preview: bool = False):
        self.path, self.fixed_now, self.preview = Path(path), now, preview
        self._sig: tuple | None = None
        self._data: dict | None = None

    def load(self) -> dict | None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._sig = self._data = None
            return None
        sig = (st.st_mtime_ns, st.st_size)
        if sig != self._sig:
            data = read_json(self.path)
            self._data = data if isinstance(data, dict) else None
            self._sig = sig
        return self._data

    def now(self, status: dict | None) -> float | None:
        if self.fixed_now is not None:
            return self.fixed_now
        if self.preview and status and isinstance(status.get("generated_at"), (int, float)):
            return float(status["generated_at"]) + 180
        return None                                   # real clock


def _encode(body: dict) -> bytes:
    # errors="replace": a lone surrogate (valid JSON as \udcff, e.g. a non-UTF-8 file name a task copied into a
    # label) becomes "?" instead of raising out of the handler and dropping the connection with no response.
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace")


def render(route: str, source: StatusSource) -> bytes:
    """Body for one route; never raises (trouble becomes an in-band error object)."""
    try:
        status = source.load()
        if route == "status":
            body = payloads.public_status(status) if status else payloads.error_payload("no status yet")
        else:
            body = payloads.build(route, status or {}, source.now(status))
            if status is None:
                body["error"] = "no status yet"
        return _encode(body)                  # inside the try: serialising is part of "never raises"
    except Exception as exc:  # noqa: BLE001 - a widget must always get a 200
        print(f"homelab-maint-www: {route}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _encode(payloads.error_payload(f"payload failure: {type(exc).__name__}"))


EXTRA_ROUTES = (*payloads_metrics.ROUTES, "metrics", "heartbeat")   # answered from the ring / probe state, not from status.json


def render_extra(route: str, source: StatusSource) -> bytes:
    """Body for one EXTRA_ROUTES route; never raises (the clock is frozen only by --now, like the status routes)."""
    try:
        now = source.fixed_now
        if route in payloads_metrics.ROUTES:
            body = payloads_metrics.build(route, now)                  # never raises: trouble is an in-band error + stale
        elif route == "metrics":
            body = payloads_metrics.load_export(now) or payloads.error_payload("no metrics yet")
        else:
            from .tasks import monitors                                  # lazy: the widgets do not need the probe engine
            body = monitors.heartbeat_payload(now)
        return _encode(body)
    except Exception as exc:  # noqa: BLE001 - a poller must always get a 200
        print(f"homelab-maint-www: {route}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _encode(payloads.error_payload(f"payload failure: {type(exc).__name__}"))


PIPELINE_ROUTE = "pipeline"


def render_pipeline(source: StatusSource) -> bytes:
    """The monitoring pipeline for a widget: the verdict a consumer must show, plus the parts that are not healthy.

    self_health.effective() is the page rule the web strip implements too (a stale "ok" is not believed); this adds the
    rows, so a board can name the stage that is broken instead of only saying that something is. Never raises: a widget
    must always get a 200."""
    try:
        from .tasks import self_health                       # lazy: the widgets need no check engine at import
        now = source.fixed_now if source.fixed_now is not None else time.time()
        doc = read_json(STATE_DIR / "public" / "self.json")
        body = dict(self_health.effective(doc, now))
        rows = doc.get("checks") if isinstance(doc, dict) else None
        body["unhealthy"] = [
            {"id": r.get("id"), "title": r.get("title"), "state": r.get("state")}
            for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict) and r.get("state") not in ("ok", "info")
        ]
        return _encode(body)
    except Exception as exc:  # noqa: BLE001 - a widget must always get a 200
        print(f"homelab-maint-www: pipeline: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _encode(payloads.error_payload(f"payload failure: {type(exc).__name__}"))


class Handler(BaseHTTPRequestHandler):
    source: StatusSource                    # set on the subclass by make_server()
    server_version = "homelab-maint"
    sys_version = ""
    protocol_version = "HTTP/1.0"           # close after each answer; polls are infrequent
    timeout = 10                            # slow or idle clients cannot pin a thread

    def _send(self, code: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlsplit(self.path).path.strip("/")
        if route == PIPELINE_ROUTE:
            self._send(200, render_pipeline(self.source))
        elif route in payloads.ROUTES or route == "status":
            self._send(200, render(route, self.source))
        elif route in EXTRA_ROUTES:
            self._send(200, render_extra(route, self.source))
        else:
            self._send(404, b'{"error":"not found"}')

    def _not_allowed(self) -> None:
        self._send(405, b'{"error":"method not allowed"}', {"Allow": "GET"})

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def log_message(self, fmt: str, *args) -> None:   # widgets poll every 10-60 s: keep the journal quiet
        pass


def make_server(bind: str, port: int, source: StatusSource) -> ThreadingHTTPServer:
    if bind not in LOOPBACK and not ipaddress.ip_address(bind).is_loopback:
        raise ValueError(f"refusing to bind {bind}: loopback only")
    handler = type("BoundHandler", (Handler,), {"source": source})
    if ":" in bind:                                   # ::1
        return type("Server6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})((bind, port), handler)
    return ThreadingHTTPServer((bind, port), handler)


def configured_port() -> int:
    try:
        return int(load_toml(CONF_DIR / "maint.toml").get("global", {}).get("www_port", DEFAULT_PORT))
    except (TypeError, ValueError, OSError):
        return DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="homelab-maint-www", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, help="default: [global] www_port from maint.toml, else 9111")
    ap.add_argument("--bind", default="127.0.0.1", help="loopback addresses only")
    ap.add_argument("--status", default=str(STATE_DIR / "status.json"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--now", type=float, help="freeze the clock at this epoch (testing)")
    g.add_argument("--preview", action="store_true", help="freeze the clock 3 min after generated_at")
    a = ap.parse_args(argv)
    try:
        srv = make_server(a.bind, a.port if a.port is not None else configured_port(),
                          StatusSource(Path(a.status), a.now, a.preview))
    except (ValueError, OSError) as exc:
        print(f"homelab-maint-www: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    print(f"homelab-maint-www: serving {a.status} on {a.bind}:{srv.server_address[1]}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
