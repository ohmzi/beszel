"""probes: the ONE monitoring plane of homelab-maint (SPEC4 S10).

Declarative probes (etc/probes.toml + /etc/homelab-maint/probes.d/*.toml) -> debounced states -> history, SLO inputs,
events, Kuma push heartbeats and the monitors.json inventory. Pure stdlib. Nothing here changes the host: probes only
READ (HTTP GET/HEAD, TCP connect, `docker ps -a`, `systemctl show`, files, and argv lists written in the config).

Config (all keys optional unless stated)
    [defaults]   workers=8 budget_s=45 timeout_s=5 interval_s=300 confirm=2 recover=2 retry_s=5 attempts=2
                 flap_window_s=3600 flap_max=4 storm_max=8 history_every_s=300 keep_days=30 deadman_key="umbrella-probes"
    [[probe]]    name* (a-z0-9_.-) title type* target* expect interval_s timeout_s class(P0-P3) severity(crit|warn|info)
                 slo(%) confirm recover tags group source(native|kuma|hermes) kuma kuma_paused kuma_push_key
                 optional paused when_running(container) needs_root external insecure method(GET|HEAD)
    [[group]]    any probe key as a default for its `probes = [ {name=..., target=...}, ... ]` members (member wins).
    [kuma]       groups = { "Media Stack" = "Media Stack" }  # Kuma group monitors carried by a probe `group`; ignore = [...]
  types and `target` / `expect`:
    http      "http(s)://host:port/path"   status=[200,"200-299"] regex not_regex json=[rules] max_ms (redirects NOT followed)
    tcp       "host:port"                  (connect only)
    docker    container name | "@daemon" | "*" (whole fleet)   state="running" health="ignore" except=[regex]
              fleet only: names=[must exist] baseline=true forget_after_s=259200 (a container seen running for >= 6 h that then
              vanishes from `docker ps -a` is reported "missing" until it is forgotten; `probes forget NAME` forgets at once)
    systemd   "unit.service"               active=["active"]
    command   ["argv", ...] (no shell)     ok_codes=[0] regex not_regex show_output   (run as the runner's user; needs_root skips otherwise)
    file_age  "/path" or glob (newest)     max_age_s warn_age_s min_size epoch_content   ({STATE} {LOG} {RUN} {CONF} expand)
    json      file path (or http URL)      json=[rules] max_age_s (file mtime) detail_path
  json rule = {path="a.*.b", <op>...}: equals in warn_in not_in exists regex min max age_max_s age_warn_s (`*` = every dict value/list item).
  optional=true: a probe that has NEVER been seen up is "skipped" (not deployed yet), never an alert; once seen up it is normal.

State machine (per probe, persisted in STATE_DIR/probes.json)
    A RUN gives each due probe one verdict, in TWO PASSES: every due probe is looked at once; then, after ONE `retry_s` pause,
    only the probes that were not clean are looked at again (up to `attempts` looks in all); the best look wins, so a blip that
    clears inside the run never counts. Verdict level ok(0) | warn(1) | fail(2). Nothing sleeps inside a worker, so a full outage
    costs about pass + retry_s + pass, not the whole budget. A probe the budget ran out before STARTING gets no verdict at all
    (its state is untouched and it is first in line next tick); only a probe that started and then did not finish is a FAIL.
    A new level becomes the CONFIRMED level only after `confirm` consecutive RUN verdicts (going worse) or `recover` (going
    better), exactly like core.Notifier and the Hermes watchdog: "2 bad runs to go down, 2 good to recover". The streak counts
    ANY run on the same side of the confirmed level (WARN, FAIL, WARN ... all count as "worse") and confirms the mildest level
    seen in it (the best the probe has shown over the streak), so a service that alternates between two non-OK levels is
    reported at the lower one instead of staying green. Alternating good and bad runs still never confirm. The cadence is
    therefore the scheduler tick (1 min), not the 15-min check tier. >= flap_max confirmed changes inside flap_window_s =
    "flapping": counted as warn, quiet while the level keeps changing, BUT a level that has held for max(10 min, 2 x interval)
    is still announced (a crash-looping service that then stays down is not hidden behind its own flapping). A run with
    > storm_max changes (a host stall makes everything fail at once) emits ONE summary event instead of a page per probe.
Events are delivered in two phases (claim_events -> ack_event / release_events): a claimed event stays in the state file until
    it is acknowledged, and a claim older than CLAIM_TTL_S (the deliverer died: TERM, KILL, OOM, job timeout) is requeued, so a
    page is delayed by a crash, never lost. Config trouble is never silent: a probes.toml that is present but untrusted,
    unreadable or defines no valid probe (or vanished after probes ran) is "monitoring blind", a distinct event and an
    `error` task result; only "never installed" is a quiet info.
Availability is TIME-honest: a sample counts once per probe interval, and when the engine itself did not run (host off, tick dead,
    reboot: the gap since the probe's last sample exceeds GAP_FACTOR x max(interval_s, the engine's own run period)) the missing
    samples are booked as FAILED and as "unobserved" (spread over the days they fell on), so a power cut burns SLO budget instead
    of leaving 100%. Every availability figure has an observed-% next to it (share of expected samples really taken). Time a
    probe was paused or removed from the config is not booked. State is not thrown away on a config error: the state of a probe
    that is missing from the valid config (typo, untrusted probes.d file, removed) is kept GONE_KEEP_S (7 days) and only then
    dropped. A clock that steps backwards cannot park a probe: a last_run in the future makes it due at once.
Safety: targets come only from root-owned, non-group/world-writable config (an untrusted file is ignored and reported); http
targets must be non-public addresses unless the probe says external=true; redirects are never followed; no request bodies,
headers or cookies; response bodies are read (<= 64 KiB) but never stored; details are scrubbed + truncated to 120 chars; a
timeout, an unparsable answer, a missing tool or an exception is a FAIL (fail closed). Files named by file_age/json probes live
in user-writable places while the engine may run as root, so they go through ONE bounded reader (read_small: no symlinks, regular
files only, <= 1 MiB, never blocks) and no parse error text or file content ever reaches a detail. Concurrency is a bounded thread pool with a
global budget; Kuma push heartbeats are GET-only (core.kuma_push) and are no-ops without a token in kuma.toml.
CLI: python3 -m homelab_maint.probes run [--force] [--only a,b] [--notify] [--all] | list | validate | export | forget NAME... | kuma-diff COPY_OF_KUMA_DB
     `run` is what the scheduler tick should call every minute (it honours every probe's own interval, so an idle call costs ~10 ms);
     --notify also delivers per-probe events when [tasks.probes] alert_mode = "events".
"""
from __future__ import annotations

import errno
import fcntl
import glob as _glob
import hashlib
import ipaddress
import json
import math
import os
import queue
import re
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import core

OK, WARN, FAIL, SKIP = 0, 1, 2, -1
WORD = {OK: "up", WARN: "warn", FAIL: "down"}
TYPES = ("http", "tcp", "docker", "systemd", "command", "file_age", "json")
CLASSES = ("P0", "P1", "P2", "P3")
SEV_BY_CLASS = {"P0": "crit", "P1": "warn", "P2": "warn", "P3": "info"}
MAX_BODY = 65536
NAME_RX = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}")
UNIT_RX = re.compile(r"[A-Za-z0-9:_.@\\-]+\.(service|timer|socket|mount|target|path)")
CT_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
OPS = {"equals", "in", "warn_in", "not_in", "exists", "regex", "min", "max", "age_max_s", "age_warn_s"}
NUM_OPS, LIST_OPS = ("min", "max", "age_max_s", "age_warn_s"), ("in", "warn_in", "not_in")
EXPECT_KEYS = {
    "http": {"status", "regex", "not_regex", "json", "max_ms"}, "tcp": set(),
    "docker": {"state", "health", "except", "names", "baseline", "forget_after_s"}, "systemd": {"active"},
    "command": {"ok_codes", "regex", "not_regex", "show_output"},
    "file_age": {"max_age_s", "warn_age_s", "min_size", "epoch_content"},
    "json": {"json", "max_age_s", "detail_path", "status", "regex", "not_regex", "max_ms"},
}
DEFAULTS: dict[str, Any] = dict(workers=8, budget_s=45, timeout_s=5, interval_s=300, confirm=2, recover=2, retry_s=5.0,
                                attempts=2, flap_window_s=3600, flap_max=4, storm_max=8, history_every_s=300,
                                keep_days=30, deadman_key="umbrella-probes")
BOUNDS = {"workers": (1, 64), "budget_s": (0.05, 600), "timeout_s": (0.5, 30), "interval_s": (10, 86400 * 7), "confirm": (1, 10),
          "recover": (1, 10), "retry_s": (0, 60), "attempts": (1, 5), "flap_window_s": (60, 86400), "flap_max": (2, 100),
          "storm_max": (1, 1000), "history_every_s": (0, 86400), "keep_days": (1, 365)}
KEY_RX = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
CLAIM_TTL_S = 120          # a claimed-but-unacknowledged event is requeued after this long (its deliverer died)
BACKOFF_MAX_S = 600        # longest back-off after a failed delivery
SKEW_S = 5                 # clock differences up to this are noise; more than this in the FUTURE means the clock stepped back
GAP_FACTOR = 2.5           # no sample for more than this x the expected spacing = the engine was not running: book the gap
PERIOD_CAP_S = 3600        # a run-to-run spacing above this is an outage, not the engine's cadence, and is never learned as one
GONE_KEEP_S = 7 * 86400    # state of a probe missing from the VALID config (typo, untrusted file, removed) is kept this long
MAX_FILE = 1 << 20         # the most a probe reads from any one file
SETTLE_MIN_S = 600         # a flapping probe's level must hold this long (or 2 x its interval) before it is announced anyway
FLEET_MIN_S = 6 * 3600     # a container counts as "established" (its disappearance is reported) after this long running: services run for days,
                           # a long one-off job container must not be learned as a service
FLEET_FORGET_S = 3 * 86400  # a vanished established container is reported "missing" for this long, then forgotten
FLEET_KEEP_S = 30 * 86400
FLEET_MAX = 500


def sh(cmd, timeout=60, **kw):
    """core.sh through a late-bound name, so tests can patch either `probes.sh` or `core.sh`."""
    return core.sh(cmd, timeout=timeout, **kw)


def _state_path() -> Path:
    return core.STATE_DIR / "probes.json"


# --------------------------------------------------------------------------- definitions
@dataclass
class Probe:
    name: str
    title: str
    type: str
    target: Any
    expect: dict = field(default_factory=dict)
    interval_s: int = 300
    timeout_s: float = 5.0
    cls: str = "P2"
    severity: str = "warn"
    slo: float | None = None
    confirm: int = 2
    recover: int = 2
    tags: tuple = ()
    group: str = ""
    source: str = "native"
    kuma: str = ""
    kuma_paused: bool = False
    kuma_push_key: str = ""
    optional: bool = False
    paused: bool = False
    when_running: str = ""
    needs_root: bool = False
    external: bool = False
    insecure: bool = False
    method: str = "GET"


def _fnum(v: Any, default: float = 0.0) -> float:
    """float(v) for a value read back from the state file; junk (hand edit, old version) is `default`, never an exception."""
    return float(v) if _is_num(v) else default


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    """Clamp a TOML number into [lo, hi]; junk (strings, bools, NaN, inf, absurd ints) falls back to `default`, never raises."""
    return float(min(max(v, lo), hi)) if _is_num(v) else default


def _check_rules(rules: Any, errs: list[str], name: str = "") -> None:
    pre = f"{name}: " if name else ""
    if not isinstance(rules, list) or not all(isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"] for r in rules):
        errs.append(f"{pre}expect.json must be a list of {{path=...}} rules")
        return
    for r in rules:
        bad = set(r) - OPS - {"path"}
        if bad or not (set(r) & OPS):
            errs.append(f"{pre}rule {r.get('path')}: unknown or missing op {sorted(bad)}")
        for k in NUM_OPS:                      # a str compared with a number would be a TypeError at 03:00, which reads as FAIL
            if k in r and not _is_num(r[k]):
                errs.append(f"{pre}rule {r['path']}: {k} must be a number")
        for k in LIST_OPS:
            if k in r and not isinstance(r[k], list):
                errs.append(f"{pre}rule {r['path']}: {k} must be a list")
        if "exists" in r and not isinstance(r["exists"], bool):
            errs.append(f"{pre}rule {r['path']}: exists must be true or false")
        if "regex" in r:
            try:
                re.compile(r["regex"])
            except (re.error, TypeError):
                errs.append(f"{pre}rule {r['path']}: bad regex")


def _check_expect(name: str, ex: dict, errs: list[str]) -> None:
    """Type-check expect values so a typo is a config error at load time, not a TypeError at 03:00 (which would read as FAIL)."""
    for k in ("max_age_s", "warn_age_s", "min_size", "max_ms", "forget_after_s"):
        if k in ex and not _is_num(ex[k]):
            errs.append(f"{name}: expect.{k} must be a number")
    if "status" in ex and not (isinstance(ex["status"], list) and ex["status"] and all(
            (isinstance(x, int) and not isinstance(x, bool)) or (isinstance(x, str) and re.fullmatch(r"\d{3}(-\d{3})?", x))
            for x in ex["status"])):
        errs.append(f"{name}: expect.status must be a list of codes or 'NNN-NNN' ranges")
    if "ok_codes" in ex and not (isinstance(ex["ok_codes"], list) and all(isinstance(x, int) and not isinstance(x, bool) for x in ex["ok_codes"])):
        errs.append(f"{name}: expect.ok_codes must be a list of integers")
    if "active" in ex and not (isinstance(ex["active"], list) and ex["active"] and all(isinstance(x, str) for x in ex["active"])):
        errs.append(f"{name}: expect.active must be a list of unit states")
    if "except" in ex:
        try:
            ok = isinstance(ex["except"], list) and all(isinstance(x, str) and re.compile(x) is not None for x in ex["except"])
        except re.error:
            ok = False
        if not ok:
            errs.append(f"{name}: expect.except must be a list of valid regexes")
    if "names" in ex and not (isinstance(ex["names"], list) and all(isinstance(x, str) and CT_RX.fullmatch(x) for x in ex["names"])):
        errs.append(f"{name}: expect.names must be a list of container names")
    for k in ("show_output", "epoch_content", "baseline"):
        if k in ex and not isinstance(ex[k], bool):
            errs.append(f"{name}: expect.{k} must be true or false")
    for k in ("state", "health", "detail_path"):
        if k in ex and not isinstance(ex[k], str):
            errs.append(f"{name}: expect.{k} must be a string")


def _flag(raw: dict, k: str, name: str, errs: list[str]) -> bool:
    v = raw.get(k, False)
    if not isinstance(v, bool):                # bool("false") is True: a quoted flag would silently invert itself
        errs.append(f"{name}: {k} must be true or false")
        return False
    return v


def _text(raw: dict, k: str, name: str, errs: list[str], default: str = "") -> str:
    v = raw.get(k, default)
    if not isinstance(v, str):
        errs.append(f"{name}: {k} must be a string")
        return default
    return v


def _check_target(name: str, typ: str, target: Any, errs: list[str]) -> None:
    """Shape of `target` per probe type. Everything that a runner would otherwise raise on (bad port, bad IPv6, NUL, control
    characters) is rejected here, so a typo is one invalid probe, never a probe that errors on every run."""
    if typ in ("http", "json") and isinstance(target, str) and target.startswith(("http://", "https://")):
        if not re.fullmatch(r"[\x21-\x7e]+", target):
            errs.append(f"{name}: URL must be printable ASCII without spaces")
            return
        try:
            u = urlsplit(target)
            host, _port = u.hostname, u.port       # u.port raises ValueError for :80a0 / :99999; urlsplit for "http://[::1/x"
        except ValueError:
            errs.append(f"{name}: malformed URL (bad host or port)")
            return
        if not host or u.username or u.password:
            errs.append(f"{name}: URL needs a host and must not embed credentials")
    elif typ == "http":
        errs.append(f"{name}: http target must be an http(s) URL")
    elif typ == "tcp":
        m = re.fullmatch(r"([A-Za-z0-9._:\[\]-]+):(\d{1,5})", target) if isinstance(target, str) else None
        if not m or not 1 <= int(m[2]) <= 65535:
            errs.append(f"{name}: tcp target must be host:port (port 1-65535)")
    elif typ == "docker":
        if not (isinstance(target, str) and (target in ("@daemon", "*") or CT_RX.fullmatch(target))):
            errs.append(f"{name}: docker target must be a container name, @daemon or *")
    elif typ == "systemd":
        if not (isinstance(target, str) and UNIT_RX.fullmatch(target)):
            errs.append(f"{name}: systemd target must be a unit name")
    elif typ == "command":
        if not (isinstance(target, list) and target and all(isinstance(a, str) and a and "\0" not in a for a in target)):
            errs.append(f"{name}: command target must be a non-empty argv list")
    elif typ in ("file_age", "json"):
        if not (isinstance(target, str) and "\0" not in target and os.path.isabs(_expand(target))):
            errs.append(f"{name}: file target must be an absolute path (or start with {{STATE}}, {{LOG}}, {{RUN}}, {{CONF}})")


def build(raw: dict, d: dict) -> tuple[Probe | None, list[str]]:
    """Validate one raw table. Returns (probe, []) or (None, errors): an invalid probe never runs. Raises only on a bug: parse()
    still isolates anything unexpected to this one probe."""
    errs: list[str] = []
    name = raw.get("name", "")
    if not (isinstance(name, str) and NAME_RX.fullmatch(name)):
        return None, [f"{str(name)[:40]!r}: name must match {NAME_RX.pattern}"]
    typ, target, expect = raw.get("type"), raw.get("target"), raw.get("expect")
    if typ not in TYPES:
        return None, [f"{name}: type must be one of {TYPES}"]
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        errs.append(f"{name}: expect must be a table")
        expect = {}
    if set(expect) - EXPECT_KEYS[typ]:
        errs.append(f"{name}: unknown expect keys {sorted(set(expect) - EXPECT_KEYS[typ])}")
    cls = raw.get("class", "P2")
    if not (isinstance(cls, str) and cls in CLASSES):
        errs.append(f"{name}: class must be one of {CLASSES}")
        cls = "P2"
    sev = raw.get("severity") or SEV_BY_CLASS[cls]
    if not (isinstance(sev, str) and sev in ("crit", "warn", "info")):
        errs.append(f"{name}: severity must be crit|warn|info")
    method = str(raw.get("method", "GET")).upper()
    if method not in ("GET", "HEAD"):
        errs.append(f"{name}: only GET and HEAD are allowed")
    src = raw.get("source", "native")
    if not (isinstance(src, str) and src in ("native", "kuma", "hermes")):
        errs.append(f"{name}: source must be native|kuma|hermes")
    key = raw.get("kuma_push_key", "")
    if key and not (isinstance(key, str) and KEY_RX.fullmatch(key)):
        errs.append(f"{name}: bad kuma_push_key")
    for k in ("interval_s", "timeout_s", "confirm", "recover"):
        if k in raw and not _is_num(raw[k]):
            errs.append(f"{name}: {k} must be a number")
    if "slo" in raw and not (_is_num(raw["slo"]) and 0 <= raw["slo"] <= 100):
        errs.append(f"{name}: slo must be a percentage")
    tags = raw.get("tags", [])
    if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        errs.append(f"{name}: tags must be a list of strings")
        tags = []
    flags = {k: _flag(raw, k, name, errs) for k in ("kuma_paused", "optional", "paused", "needs_root", "external", "insecure")}
    title, group, kuma, wr = (_text(raw, k, name, errs, dv) for k, dv in (("title", name), ("group", ""), ("kuma", ""), ("when_running", "")))
    _check_target(name, typ, target, errs)
    _check_expect(name, expect, errs)
    if typ == "file_age" and "max_age_s" not in expect:
        errs.append(f"{name}: file_age needs expect.max_age_s")
    if typ in ("json", "http") and "json" in expect:
        _check_rules(expect["json"], errs, name)
    if typ == "http" and method == "HEAD" and ({"regex", "not_regex", "json"} & set(expect)):
        errs.append(f"{name}: a HEAD probe has no body to match")
    if typ == "json" and not (expect.get("json") or "max_age_s" in expect):
        errs.append(f"{name}: json probe needs expect.json rules or max_age_s")
    for k in ("regex", "not_regex"):
        if k in expect:
            try:
                re.compile(expect[k])
            except (re.error, TypeError):
                errs.append(f"{name}: bad expect.{k}")
    if errs:
        return None, errs
    g = lambda k: raw.get(k, d[k])  # noqa: E731
    return Probe(
        name=name, title=title[:60], type=typ, target=target, expect=dict(expect),
        interval_s=int(_num(g("interval_s"), 10, 86400 * 7, 300)), timeout_s=_num(g("timeout_s"), 0.5, 30, 5.0),
        cls=cls, severity=sev, slo=raw["slo"] if "slo" in raw else None,
        confirm=int(_num(g("confirm"), 1, 10, 2)), recover=int(_num(g("recover"), 1, 10, 2)),
        tags=tuple(tags), group=group, source=src, kuma=kuma, kuma_paused=flags["kuma_paused"],
        kuma_push_key=key or "", optional=flags["optional"], paused=flags["paused"], when_running=wr,
        needs_root=flags["needs_root"], external=flags["external"], insecure=flags["insecure"], method=method), []


def _clean_defaults(dd: dict, errs: list[str]) -> dict:
    """[defaults] with every value checked and clamped: a junk value is reported and the shipped default stays."""
    d = dict(DEFAULTS)
    for k, v in dd.items():
        if k not in DEFAULTS:
            continue
        if k == "deadman_key":
            if isinstance(v, str) and (v == "" or KEY_RX.fullmatch(v)):
                d[k] = v
            else:
                errs.append("defaults.deadman_key must be a Kuma push key (or empty)")
        elif _is_num(v):
            lo, hi = BOUNDS[k]
            d[k] = _num(v, lo, hi, DEFAULTS[k])
        else:
            errs.append(f"defaults.{k} must be a number")
    return d


def _tables(v: Any, what: str, errs: list[str]) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        errs.append(f"{what} must be an array of tables ([[{what}]])")
        return []
    return v


def parse(doc: dict) -> tuple[dict, list[Probe], list[str]]:
    """(defaults, probes, errors) from a parsed TOML document. Pure, and total: whatever types the document holds, it returns;
    an invalid probe is reported by name and the others still load. Used by load_probes and by the tests."""
    errs: list[str] = []
    dd = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    d = _clean_defaults(dd, errs)
    raws: list[dict] = []
    for i, r in enumerate(_tables(doc.get("probe"), "probe", errs)):
        if isinstance(r, dict):
            raws.append(r)
        else:
            errs.append(f"probe #{i + 1}: not a table")
    for gi, g in enumerate(_tables(doc.get("group"), "group", errs)):
        if not isinstance(g, dict):
            errs.append(f"group #{gi + 1}: not a table")
            continue
        gname = str(g.get("group", f"#{gi + 1}"))[:30]
        base = {k: v for k, v in g.items() if k != "probes"}
        members = g.get("probes", [])
        if not isinstance(members, list):
            errs.append(f"group {gname}: probes must be a list")
            continue
        for m in members:
            if not isinstance(m, dict):
                errs.append(f"group {gname}: a member is not a table")
                continue
            bx, mx = base.get("expect"), m.get("expect")
            ex = {**bx, **mx} if isinstance(bx, dict) and isinstance(mx, dict) else (mx if mx is not None else bx)
            raws.append({**base, **m, **({"expect": ex} if ex is not None else {})})
    probes, seen = [], set()
    for i, r in enumerate(raws):
        who = str(r.get("name", f"probe #{i + 1}"))[:48]
        try:
            p, e = build(r, d)
        except Exception as exc:  # noqa: BLE001 - one malformed probe must never take the other probes down with it
            p, e = None, [f"{who}: invalid definition ({type(exc).__name__})"]
        if p and p.name in seen:
            p, e = None, [f"{p.name}: duplicate name"]
        if p:
            seen.add(p.name)
            probes.append(p)
        errs += e
    return d, probes, errs


def _trusted(p: Path) -> bool:
    """Config can name commands (run as root by the check tier), so only a file owned by root-or-us that nobody else can write,
    inside a directory nobody else can write, is believed."""
    try:
        st, dst = p.stat(), p.parent.stat()
    except OSError:
        return False
    return st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022 and not dst.st_mode & 0o022


def load_probes() -> tuple[dict, list[Probe], list[str]]:
    """probes.toml + probes.d/*.toml from CONF_DIR. A file that does not exist is "nothing configured" (quiet); a file that
    exists but is untrusted, unreadable or leaves no valid probe is an ERROR in the list (the task turns it into "monitoring
    blind", it is never mistaken for an empty config)."""
    doc: dict = {"probe": [], "group": [], "kuma": {}}
    errs: list[str] = []
    main = core.CONF_DIR / "probes.toml"
    files = [main, *sorted((core.CONF_DIR / "probes.d").glob("*.toml"))]
    present = False
    for f in files:
        if not f.exists():
            continue
        present = True
        if not _trusted(f):
            errs.append(f"{f.name}: ignored (not owned by root/runner, or it or its directory is group/world writable)")
            continue
        try:
            with open(f, "rb") as fh:
                part = tomllib.load(fh)
        except (OSError, ValueError, RecursionError) as exc:      # TOMLDecodeError and UnicodeDecodeError are ValueErrors
            errs.append(f"{f.name}: unreadable ({type(exc).__name__})")
            continue
        if f is main:
            doc["defaults"] = part.get("defaults", {})
            doc["kuma"] = part.get("kuma", {}) if isinstance(part.get("kuma"), dict) else {}
        for key in ("probe", "group"):
            doc[key] += _tables(part.get(key), key, errs)
    d, probes, perrs = parse(doc)
    errs += perrs
    if present and not probes and not errs:
        errs.append("probes.toml: present but defines no probes")
    return d, probes, errs


# --------------------------------------------------------------------------- scrubbing
_SECRET_QS = re.compile(r"((?:^|[?&;,\s])(?:token|key|apikey|api_key|secret|passw\w*|auth\w*|sig\w*)=)[^&\s]*", re.I)
_BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{28,}")


def scrub(s: Any, n: int = 120) -> str:
    """One short ASCII line with credentials, userinfo, token-looking blobs and query strings removed."""
    t = re.sub(r"\s+", " ", str(s))
    t = re.sub(r"(?<=://)[^/@\s]+@", "", t)
    t = _SECRET_QS.sub(r"\1[redacted]", t)
    t = _BEARER.sub(r"\1 [redacted]", t)
    t = _LONG_TOKEN.sub("[redacted]", t)
    return re.sub(r"[^\x20-\x7e]", "?", t)[:n].strip()


def _why(exc: BaseException) -> str:
    r = getattr(exc, "reason", None) or exc
    return scrub(getattr(r, "strerror", None) or str(r) or type(r).__name__, 60)


# --------------------------------------------------------------------------- json rules
def jpath(obj: Any, path: str) -> list[tuple[str, Any]]:
    """Dotted path with `*` (every dict value / list item) and list indexes -> [(concrete.path, value)]; [] if absent."""
    cur: list[tuple[str, Any]] = [("", obj)]
    for part in path.split("."):
        nxt: list[tuple[str, Any]] = []
        for k, v in cur:
            sub = f"{k}.{part}" if k else part
            if part == "*":
                items = v.items() if isinstance(v, dict) else enumerate(v) if isinstance(v, list) else ()
                nxt += [(f"{k}.{i}" if k else str(i), x) for i, x in items]
            elif isinstance(v, dict) and part in v:
                nxt.append((sub, v[part]))
            elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
                nxt.append((sub, v[int(part)]))
        cur = nxt
    return cur


def _epoch(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def _eq(a: Any, b: Any) -> bool:
    return a is b if isinstance(b, bool) or isinstance(a, bool) else a == b


def _judge(v: Any, r: dict, now: float) -> tuple[int, str]:
    """Worst verdict of one value against every op in a rule."""
    lvl, why = OK, ""

    def bad(l: int, msg: str) -> None:
        nonlocal lvl, why
        if l > lvl:
            lvl, why = l, msg

    if "equals" in r and not _eq(v, r["equals"]):
        bad(FAIL, f"{scrub(v, 30)!r} != {r['equals']!r}")
    if "in" in r and not any(_eq(v, x) for x in r["in"]):
        bad(WARN if any(_eq(v, x) for x in r.get("warn_in", [])) else FAIL, f"{scrub(v, 30)!r} not in {r['in']}")
    if "not_in" in r and any(_eq(v, x) for x in r["not_in"]):
        bad(FAIL, f"{scrub(v, 30)!r} is forbidden")
    if "regex" in r and not re.search(r["regex"], str(v)):
        bad(FAIL, "does not match")
    num = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    for op, cmp in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
        if op in r and (num is None or cmp(num, r[op])):
            bad(FAIL, f"{scrub(v, 20)} violates {op} {r[op]}")
    if "age_max_s" in r or "age_warn_s" in r:
        ep = _epoch(v)
        if ep is None:
            bad(FAIL, "not a timestamp")
        else:
            age = max(now - ep, 0.0)
            if "age_max_s" in r and age > r["age_max_s"]:
                bad(FAIL, f"age {int(age)}s > {r['age_max_s']}s")
            elif "age_warn_s" in r and age > r["age_warn_s"]:
                bad(WARN, f"age {int(age)}s > {r['age_warn_s']}s")
    return lvl, why


def eval_rules(obj: Any, rules: list[dict], now: float) -> tuple[int, str]:
    """Apply json rules; the worst level wins and its first reason is reported (`path: reason`)."""
    lvl, why = OK, ""
    for r in rules:
        vals = jpath(obj, r["path"])
        if r.get("exists") is False:
            l, w = (FAIL, f"{r['path']} present") if vals else (OK, "")
        elif not vals:
            l, w = FAIL, f"{r['path']} missing"
        else:
            l, w = OK, ""
            for k, v in vals:
                vl, vw = _judge(v, r, now)
                if vl > l:
                    l, w = vl, f"{k}: {vw}"
        if l > lvl:
            lvl, why = l, w
    return lvl, why


# --------------------------------------------------------------------------- shared per-run reads
class Snapshot:
    """One `docker ps -a` and one `systemctl show` per pass, shared by every probe that needs them. `fleet` is the persisted
    baseline of containers seen running ({name: (first_seen, last_seen)}), read-only here: the engine updates it after the run."""

    def __init__(self, units: list[str], fleet: dict | None = None):
        self._units, self._lock = sorted(set(units)), threading.Lock()
        self._docker: Any = "unset"
        self._sysd: Any = "unset"
        self.last_docker: dict[str, tuple[str, str]] | None = None      # the last SUCCESSFUL read, survives reset()
        self.fleet: dict[str, tuple[float, float]] = {
            n: (float(v[0]), float(v[1])) for n, v in (fleet or {}).items()
            if isinstance(n, str) and isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_num(x) for x in v)}

    def reset(self) -> None:
        """Forget the cached reads, so the retry pass looks at the host again instead of re-reading the same stale answer."""
        with self._lock:
            self._docker = self._sysd = "unset"

    def docker(self) -> dict[str, tuple[str, str]] | None:
        """{name: (state, health)} for every container, health in healthy|unhealthy|starting|''; None = docker failed."""
        with self._lock:
            if self._docker == "unset":
                r = sh(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=20)
                out: dict[str, tuple[str, str]] | None = None
                if r.returncode == 0:
                    out = {}
                    for ln in r.stdout.splitlines():
                        parts = ln.split("|", 2)
                        if len(parts) == 3 and parts[0].strip():
                            m = re.search(r"\((healthy|unhealthy|health: starting)\)", parts[2])
                            h = {"healthy": "healthy", "unhealthy": "unhealthy"}.get(m.group(1) if m else "", "starting" if m else "")
                            out[parts[0].split(",")[0].strip()] = (parts[1].strip().lower(), h)
                    self.last_docker = out
                self._docker = out
            return self._docker

    def units(self) -> dict[str, dict[str, str]] | None:
        with self._lock:
            if self._sysd == "unset":
                self._sysd = None
                if self._units:
                    r = sh(["systemctl", "show", "--property=Id,LoadState,ActiveState,SubState,Result", *self._units], timeout=20)
                    if r.returncode == 0:
                        blocks = [dict(ln.split("=", 1) for ln in b.splitlines() if "=" in ln) for b in r.stdout.split("\n\n")]
                        self._sysd = {b["Id"]: b for b in blocks if "Id" in b} or None
            return self._sysd


# --------------------------------------------------------------------------- probe runners: (level, detail)
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # a 3xx is an answer to judge, never a hop to follow (SSRF)
        return None


_OPENERS: dict[tuple[bool, bool], urllib.request.OpenerDirector] = {}
_OPENER_LOCK = threading.Lock()


def _opener(https: bool, insecure: bool) -> urllib.request.OpenerDirector:
    """One shared opener per (https, insecure): no proxies, no redirects, no cookies, no auth handlers. Built by hand because
    build_opener() constructs an HTTPSHandler (and loads the CA bundle, ~13 ms of CPU) even for plain http, per probe, per run."""
    with _OPENER_LOCK:
        o = _OPENERS.get((https, insecure))
        if o is None:
            o = urllib.request.OpenerDirector()
            hs: list = [urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(), _NoRedirect(),
                        urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()]
            if https:
                ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()   # noqa: SLF001 (self-signed local https only)
                hs.append(urllib.request.HTTPSHandler(context=ctx))
            for h in hs:
                o.add_handler(h)
            _OPENERS[(https, insecure)] = o
        return o


def _expand(path: str) -> str:
    return (path.replace("{STATE}", str(core.STATE_DIR)).replace("{LOG}", str(core.LOG_DIR))
            .replace("{RUN}", str(core.RUN_DIR)).replace("{CONF}", str(core.CONF_DIR)))


def _host_ok(host: str, external: bool) -> None:
    """SSRF belt and braces on top of 'targets only from config': refuse public addresses unless external=true."""
    if external:
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise OSError("cannot resolve host") from None
    for info in infos:
        if ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise OSError("public address refused (set external=true)")


def _in_status(code: int, spec: list) -> bool:
    for s in spec:
        if isinstance(s, int) and s == code:
            return True
        if isinstance(s, str) and (m := re.fullmatch(r"(\d{3})-(\d{3})", s)) and int(m[1]) <= code <= int(m[2]):
            return True
    return False


def _p_http(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    u = urlsplit(p.target)
    try:
        _host_ok(u.hostname or "", p.external)
    except OSError as exc:                       # keep the reason ("public address refused"): it tells the owner what to fix
        return FAIL, _why(exc)
    opener = _opener(u.scheme == "https", p.insecure)
    req = urllib.request.Request(p.target, method=p.method, headers={"User-Agent": "homelab-maint-probe/1"})
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=p.timeout_s) as r:
            code, body = r.status, (r.read(MAX_BODY) if p.method == "GET" else b"")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read(MAX_BODY) if p.method == "GET" else b""
        except OSError:
            body = b""
    except (urllib.error.URLError, OSError) as exc:
        return FAIL, _why(exc)
    ms = int((time.monotonic() - t0) * 1000)
    ex = p.expect
    if not _in_status(code, ex.get("status", ["200-299"])):
        return FAIL, f"HTTP {code} (want {ex.get('status', ['200-299'])})"
    text = body.decode("utf-8", "replace")
    if "regex" in ex and not re.search(ex["regex"], text):
        return FAIL, f"HTTP {code} but body does not match"
    if "not_regex" in ex and re.search(ex["not_regex"], text):
        return FAIL, f"HTTP {code} but body matches a failure pattern"
    if "json" in ex:
        try:
            lvl, why = eval_rules(json.loads(text), ex["json"], now)
        except ValueError:
            return FAIL, f"HTTP {code} but body is not JSON"
        if lvl:
            return lvl, f"HTTP {code} {why}"
    if "max_ms" in ex and ms > ex["max_ms"]:
        return WARN, f"HTTP {code} slow: {ms} ms (limit {ex['max_ms']})"
    return OK, f"HTTP {code} {ms} ms"


def _p_tcp(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    host, _, port = p.target.rpartition(":")
    host = host.strip("[]")
    try:
        _host_ok(host, p.external)
    except OSError as exc:
        return FAIL, _why(exc)
    try:
        with socket.create_connection((host, int(port)), timeout=p.timeout_s):
            return OK, f"tcp {port} open"
    except OSError as exc:
        return FAIL, _why(exc)


def _fleet_missing(p: Probe, cts: dict, snap: Snapshot, now: float, skip: list) -> list[str]:
    """Containers that SHOULD exist but are absent from `docker ps -a` (a `docker rm` / `compose down` leaves no stopped corpse
    to judge): the explicit expect.names, plus every established container of the learned baseline that vanished within
    forget_after_s. Names matching `except` are not baseline-checked (throw-away containers come and go by design)."""
    ex = p.expect
    want = set(ex.get("names", []))
    if ex.get("baseline", True):
        forget = float(ex.get("forget_after_s", FLEET_FORGET_S))
        want |= {n for n, (first, last) in snap.fleet.items()
                 if last - first >= FLEET_MIN_S and now - last <= forget and not any(r.search(n) for r in skip)}
    return sorted(n for n in want if n not in cts)


def _p_docker(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    cts = snap.docker()
    if cts is None:
        return FAIL, "docker not answering"
    if p.target == "@daemon":
        return OK, f"docker answers, {len(cts)} containers"
    want = p.expect.get("state", "running")
    if p.target == "*":
        skip = [re.compile(x, re.I) for x in p.expect.get("except", [])]
        bad: dict[str, list[str]] = {}
        gone = _fleet_missing(p, cts, snap, now, skip)
        if gone:
            bad["missing"] = gone
        for n, (state, health) in sorted(cts.items()):
            if any(r.search(n) for r in skip):
                continue
            if state != "running":
                bad.setdefault(state, []).append(n)
            elif health == "unhealthy":
                bad.setdefault("unhealthy", []).append(n)
        if bad:
            return FAIL, scrub("; ".join(f"{k}: {', '.join(v[:3])}{'+%d' % (len(v) - 3) if len(v) > 3 else ''}" for k, v in bad.items()))
        return OK, f"{len(cts)} containers fine"
    c = cts.get(p.target)
    if c is None:
        return FAIL, "no such container"
    state, health = c
    if state != want:
        return FAIL, state
    if p.expect.get("health") != "ignore":
        if health == "unhealthy":
            return FAIL, "unhealthy"
        if health == "starting":
            return WARN, "health check starting"
    return OK, "running" + (" healthy" if health == "healthy" else "")


def _p_systemd(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    units = snap.units()
    pr = (units or {}).get(p.target)
    if units is None:
        return FAIL, "systemctl not answering"
    if pr is None or pr.get("LoadState") == "not-found":
        return FAIL, "unit not found"
    st = pr.get("ActiveState", "?")
    if st in p.expect.get("active", ["active"]):
        return OK, f"{st} ({pr.get('SubState', '?')})"
    return FAIL, f"{st} ({pr.get('SubState', '?')}) result={pr.get('Result', '?')}"


def _p_command(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    if p.needs_root and os.geteuid() != 0:
        return SKIP, "needs root"
    r = sh(list(p.target), timeout=int(p.timeout_s) + 1)
    if r.returncode == 124:
        return FAIL, "timed out"
    if r.returncode == 127:
        return FAIL, "command not found"
    out = ((r.stdout or "") + (r.stderr or ""))[:MAX_BODY]
    ex = p.expect
    show = f": {scrub(out.strip().splitlines()[0] if out.strip() else '', 60)}" if ex.get("show_output") else ""
    if r.returncode not in ex.get("ok_codes", [0]):
        return FAIL, f"exit {r.returncode}{show}"
    if "regex" in ex and not re.search(ex["regex"], out):
        return FAIL, f"exit {r.returncode}, output does not match"
    if "not_regex" in ex and re.search(ex["not_regex"], out):
        return FAIL, "output matches a failure pattern"
    return OK, f"exit {r.returncode}{show}"


class Unsafe(Exception):
    """A file the engine refuses to read. The text is fixed (never taken from the file), so nothing can leak through a detail."""


def read_small(path: str, limit: int = MAX_FILE, content: bool = True) -> tuple[bytes, os.stat_result]:
    """(contents <= `limit` bytes, fstat) of a REGULAR file, or raises OSError / Unsafe. THE reader for every file a probe names:
    those live in user-writable places (~/.hermes) while the engine may run as root, so the leaf is opened O_NOFOLLOW (a symlink is
    refused, never followed: no /dev/zero, /etc/passwd or /proc detour), O_NONBLOCK (a FIFO cannot park a worker), judged by fstat on
    the OPEN descriptor (no check-then-use race: it must be a regular file) and read at most limit + 1 bytes (a file that grows
    or lies about st_size still stops). content=False only vets the file and returns its fstat (mtime/size probes).
    Directory components of the path are followed as usual; the config is root-owned, the hardening is about the leaf."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Unsafe("symlink refused") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe("not a regular file")
        if not content:
            return b"", st
        if st.st_size > limit:
            raise Unsafe("file too large")
        chunks, n = [], 0
        while n <= limit:
            b = os.read(fd, limit + 1 - n)
            if not b:
                break
            chunks.append(b)
            n += len(b)
        if n > limit:
            raise Unsafe("file too large")
        return b"".join(chunks), st
    finally:
        os.close(fd)


def _newest(path: str) -> str | None:
    """The path itself, or for a glob the newest REGULAR file (lstat: a symlink never matches, so it cannot steer the probe)."""
    if not any(c in path for c in "*?["):
        return path
    best, best_t = None, -1.0
    for f in _glob.glob(path):
        try:
            st = os.lstat(f)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_mtime > best_t:
            best, best_t = f, st.st_mtime
    return best


def _p_file_age(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    ex, path = p.expect, _newest(_expand(p.target))
    if path is None:
        return FAIL, "no matching file"
    epoch = bool(ex.get("epoch_content"))
    try:
        data, st = read_small(path, content=epoch)
    except Unsafe as exc:
        return FAIL, f"unsafe file: {exc}"
    except OSError as exc:
        return FAIL, f"unreadable ({_why(exc)})"
    if epoch:
        try:                                              # never echo the content or the parser's message: it is not ours
            t = float(data.decode("ascii").strip())
        except ValueError:
            return FAIL, "content is not a number"
        if not math.isfinite(t):                          # "nan" / "inf" parse fine and would compare as fresh: fail closed
            return FAIL, "content is not a number"
        age = now - t
    else:
        age = now - st.st_mtime
    if st.st_size < ex.get("min_size", 0):
        return FAIL, f"file too small ({st.st_size} B)"
    age = max(age, 0.0)
    if age > ex["max_age_s"]:
        return FAIL, f"age {int(age)}s > {ex['max_age_s']}s"
    if "warn_age_s" in ex and age > ex["warn_age_s"]:
        return WARN, f"age {int(age)}s > {ex['warn_age_s']}s"
    return OK, f"age {int(age)}s"


def _p_json(p: Probe, snap: Snapshot, now: float) -> tuple[int, str]:
    if p.target.startswith(("http://", "https://")):
        return _p_http(p, snap, now)
    ex, path = p.expect, _expand(p.target)
    try:
        data, st = read_small(path)
        doc = json.loads(data)
    except Unsafe as exc:
        return FAIL, f"unsafe file: {exc}"
    except OSError as exc:
        return FAIL, f"unreadable ({_why(exc)})"
    except (ValueError, RecursionError):                  # UnicodeDecodeError is a ValueError; a 1 MiB "[[[[..." is a RecursionError
        return FAIL, "not valid JSON"
    if "max_age_s" in ex and now - st.st_mtime > ex["max_age_s"]:
        return FAIL, f"file age {int(now - st.st_mtime)}s > {ex['max_age_s']}s"
    lvl, why = eval_rules(doc, ex.get("json", []), now)
    if lvl and ex.get("detail_path"):
        extra = [scrub(v, 50) for _k, v in jpath(doc, ex["detail_path"])]
        why = f"{why} [{'; '.join(extra[:2])}]" if extra else why
    return lvl, why or "ok"


RUNNERS: dict[str, Callable[[Probe, Snapshot, float], tuple[int, str]]] = {
    "http": _p_http, "tcp": _p_tcp, "docker": _p_docker, "systemd": _p_systemd, "command": _p_command,
    "file_age": _p_file_age, "json": _p_json}


def attempt(p: Probe, snap: Snapshot, now: float, never_up: bool = False) -> tuple[int, str, int]:
    """One observation (level, detail, ms). Never raises; any surprise is a FAIL (fail closed)."""
    t0 = time.monotonic()
    try:
        cts = snap.docker() if p.when_running else {}
        if p.when_running and cts is not None and cts.get(p.when_running, ("", ""))[0] != "running":
            lvl, detail = SKIP, f"{p.when_running} not running (by design)"
        else:
            lvl, detail = RUNNERS[p.type](p, snap, now)
    except Exception as exc:  # noqa: BLE001
        lvl, detail = FAIL, f"probe error: {type(exc).__name__}"
    if p.optional and lvl == FAIL and never_up:
        lvl, detail = SKIP, f"not deployed yet ({detail})"
    return lvl, scrub(detail), int((time.monotonic() - t0) * 1000)


# --------------------------------------------------------------------------- state machine
def step(s: dict, lvl: int, now: float, p: Probe) -> bool:
    """Feed one run verdict into per-probe state `s`. True when the CONFIRMED level changed.
    First sight: a clean verdict is believed at once; a problem has no confirmed level (state "unknown", never counted as up)
    until it has been seen `confirm` runs in a row, exactly like a later change.
    A streak is a run of verdicts on the SAME SIDE of the confirmed level, not of one identical level: going worse, every run
    above the confirmed level counts and the streak confirms the MILDEST level in it (WARN, FAIL, WARN, FAIL -> WARN after
    `confirm` runs, never green); going better, every run below it counts and the streak confirms the HIGHEST level in it. A
    run at the confirmed level, or on the other side of it, restarts the count: good/bad alternation still never confirms."""
    first = s.get("lvl") is None
    if first and lvl == OK:
        s.update(lvl=OK, since=now, pend=OK, streak=0)
        return False
    cur = OK if first else s["lvl"]
    if lvl == cur:
        s["pend"], s["streak"] = lvl, 0
        return False
    worse, pend = lvl > cur, s.get("pend")
    if _fnum(s.get("streak")) > 0 and _is_num(pend) and pend != cur and (pend > cur) == worse:
        s["streak"] = int(_fnum(s.get("streak"))) + 1
        s["pend"] = min(pend, lvl) if worse else max(pend, lvl)
    else:
        s["pend"], s["streak"] = lvl, 1
    if s["streak"] < (p.confirm if worse else p.recover):
        return False
    s.update(lvl=s["pend"], since=now, streak=0)
    if not first:
        s["tr"] = (s.get("tr", []) + [now])[-12:]            # the initial confirmation is not a flap
    return True


def _flapping(s: dict, now: float, d: dict) -> bool:
    return sum(1 for t in s.get("tr", []) if now - t <= d["flap_window_s"]) >= d["flap_max"]


def state_of(p: Probe, s: dict | None) -> str:
    if p.paused:
        return "paused"
    if not s or s.get("lvl") is None and not s.get("skip"):
        return "unknown"
    if s.get("skip"):
        return "skipped"
    return WORD[s["lvl"]]


def eff_severity(p: Probe, lvl: int) -> str:
    """What a confirmed level means for paging: a warn is never worse than warn; severity=info never pages."""
    return "info" if p.severity == "info" else ("crit" if lvl == FAIL and p.severity == "crit" else "warn")


# --------------------------------------------------------------------------- persistence
_STATE_TYPES = {"probes": dict, "days": dict, "events": list, "inflight": list, "run": dict, "fleet": dict, "cfg": dict,
                "gone": dict, "hist_t": (int, float), "seq": int, "cfg_sig": str, "cfg_t": (int, float)}


def _blank() -> dict:
    return {"v": 1, "probes": {}, "days": {}, "events": [], "inflight": [], "run": {}, "hist_t": 0, "fleet": {}, "cfg": {},
            "gone": {}, "seq": 0, "cfg_sig": "", "cfg_t": 0}


def load_state() -> dict:
    st = core.read_json(_state_path(), None)
    if not isinstance(st, dict) or st.get("v") != 1:
        return _blank()
    for k, v in _blank().items():
        if not isinstance(st.get(k), _STATE_TYPES.get(k, object)) or isinstance(st.get(k), bool):
            st[k] = v                                        # a wrong-typed field (hand edit, old version) is reset, never trusted
    return st


def save_state(st: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, separators=(",", ":"), default=str))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


@contextmanager
def _locked(wait_s: float = 0.0):
    """Exclusive flock on STATE_DIR/probes.lock; yields False when it could not be taken within wait_s."""
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(core.STATE_DIR / "probes.lock", "w")
    got, end = False, time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
            break
        except OSError:
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
    try:
        yield got
    finally:
        if got:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _day(t: float) -> int:
    return int(time.strftime("%Y%m%d", time.localtime(t)))


def _good_row(r: Any) -> bool:
    """A per-day availability row: [day, ok, total] (+ unobserved, only once a gap was booked on that day)."""
    return isinstance(r, list) and len(r) >= 3 and all(_is_num(x) for x in r[:3]) and (len(r) == 3 or _is_num(r[3]))


def _book(rows: list, day: int, ok: int, total: int, unobs: int = 0) -> None:
    """Add samples to `day`'s row (created in day order when missing). The 4th column exists only for days with a booked gap."""
    row = next((r for r in reversed(rows) if r[0] == day), None)
    if row is None:
        row = [day, 0, 0]
        rows.append(row)
        rows.sort(key=lambda r: r[0])
    row[1] += ok
    row[2] += total
    if unobs:
        if len(row) == 3:
            row.append(0)
        row[3] += unobs


def _trim_days(st: dict, name: str, now: float, keep_days: int) -> None:
    cut = _day(now - (keep_days + 5) * 86400)
    st["days"][name] = [r for r in st["days"][name] if r[0] >= cut]


def _bump_day(st: dict, name: str, now: float, ok: bool, keep_days: int) -> None:
    _book(st["days"].setdefault(name, []), _day(now), int(ok), 1)
    _trim_days(st, name, now, keep_days)


def _midnights(a: float, b: float) -> list[float]:
    """Local midnights strictly between a and b (at most one per day of the span)."""
    out, d = [], datetime.fromtimestamp(a).date()
    while True:
        d += timedelta(days=1)
        t = datetime.combine(d, datetime.min.time()).timestamp()
        if t >= b:
            return out
        out.append(t)


def _book_gap(st: dict, name: str, prev: float, now: float, expected: float, keep_days: int) -> int:
    """The engine took no sample of this probe between `prev` and `now`. When that is longer than GAP_FACTOR x the expected spacing
    (the probe's interval or the engine's own run period, whichever is longer) the samples that should have been taken are booked
    as FAILED and UNOBSERVED, spread over the local days the gap covered (a 3-day outage is not all booked on today). Returns how
    many. Time while the probe was paused/removed never gets here (the caller clears the anchor); a clock that moved backwards has
    a negative gap and books nothing."""
    gap = now - prev
    if expected <= 0 or gap <= GAP_FACTOR * expected:
        return 0
    gap = min(gap, (keep_days + 5) * 86400.0)                  # nothing older than the retained history matters
    n = int(gap // expected) - 1
    if n <= 0:
        return 0
    a = now - gap
    edges, done = [a, *_midnights(a, now), now], 0
    rows = st["days"].setdefault(name, [])
    for lo, hi in zip(edges, edges[1:]):
        cum = n if hi == now else int(n * (hi - a) / gap)      # cumulative share: the days add up to exactly n
        if cum > done:
            _book(rows, _day(lo), 0, cum - done, cum - done)
            done = cum
    return n


def _window(rows: list, now: float, days: int) -> list:
    cut = _day(now - (days - 1) * 86400)
    return [r for r in rows if _good_row(r) and r[0] >= cut]


def availability(rows: list, now: float, days: int) -> float | None:
    """% of the window's expected samples that were OK; unobserved samples (the engine was not running) count as not OK."""
    w = _window(rows, now, days)
    tot = sum(r[2] for r in w)
    return round(100.0 * sum(r[1] for r in w) / tot, 2) if tot else None


def observed(rows: list, now: float, days: int) -> float | None:
    """% of the window's expected samples that were really taken: 100 means every figure above is a measurement; less means the
    availability includes time nobody was watching (booked as down)."""
    w = _window(rows, now, days)
    tot = sum(r[2] for r in w)
    return round(100.0 * (tot - sum(r[3] for r in w if len(r) > 3)) / tot, 2) if tot else None


def _retain(st: dict, names: set[str], now: float) -> None:
    """Keep state across config trouble. Only probes in the VALID config are live; the state of any other name (a typo that makes
    its definition invalid, a probes.d file that is momentarily untrusted, a probe taken out) is kept for GONE_KEEP_S, then
    dropped, so fixing the config restores its confirmed level, announcements and 30-day history instead of starting blank.
    Such a probe is marked `fresh` (its gap is not booked as downtime when it comes back: it was not being monitored on purpose,
    or the config error is reported on its own). Paused probes are marked the same way."""
    # a wrong-typed entry (hand edit, old version) is reset, never trusted; junk rows of a live probe's history are skipped
    st["probes"] = {n: v for n, v in st["probes"].items() if isinstance(v, dict)}
    st["days"] = {n: [r for r in v if _good_row(r)] if n in names else v for n, v in st["days"].items() if isinstance(v, list)}
    gone = st["gone"]
    for n in set(st["probes"]) | set(st["days"]):
        if n in names:
            gone.pop(n, None)
            continue
        if not _is_num(gone.get(n)):
            gone[n] = now
        if now - gone[n] > GONE_KEEP_S:
            st["probes"].pop(n, None)
            st["days"].pop(n, None)
        elif isinstance(st["probes"].get(n), dict):
            st["probes"][n]["fresh"] = True
    for n in [n for n in gone if n not in st["probes"] and n not in st["days"]]:
        del gone[n]


def _learn_period(st: dict, now: float) -> float:
    """The engine's own run-to-run spacing (EMA of the last runs), so a deployment where the engine runs less often than a probe's
    interval (check tier only, no 1-min tick) does not read as an outage on every run. A spacing above PERIOD_CAP_S is an
    outage, not a cadence, and is never learned."""
    run = st["run"]
    prev, per = _fnum(run.get("last_start")), _fnum(run.get("period"))
    dt = now - prev
    if prev and 0 < dt <= PERIOD_CAP_S:
        per = dt if per <= 0 else 0.7 * per + 0.3 * dt
    return per


# --------------------------------------------------------------------------- the engine
@dataclass
class RunReport:
    ran: list[str] = field(default_factory=list)        # probes that got a verdict this run (not those the budget never reached)
    locked: bool = False
    errors: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    elapsed_s: float = 0.0
    total: int = 0


def is_due(p: Probe, s: dict | None, now: float) -> bool:
    """Due when 90 % of the interval has passed since the last run. A last_run in the FUTURE (the clock stepped backwards: RTC or
    NTP correction, VM resume) is due at once; otherwise the plane would sit idle for exactly the size of the step while
    probes.json kept being rewritten and every freshness probe stayed green."""
    if p.paused:
        return False
    age = now - _fnum((s or {}).get("last_run"))
    return age < -SKEW_S or age >= p.interval_s * 0.9


def run_due(now: float | None = None, *, maint_cfg: dict | None = None, conf: tuple | None = None, force: bool = False,
            only: set[str] | None = None, sleep: Callable[[float], None] = time.sleep,
            pusher: Callable[..., Any] | None = None) -> RunReport:
    """Run every probe whose interval has elapsed (all of them with force; `only` = just those names), apply the
    debounced state machine, persist, and return what happened. Safe to call from several processes: one wins the lock.
    With no usable probes the plane is BLIND: that is recorded (and announced once) instead of being mistaken for "all quiet",
    and no dead-man heartbeat is pushed, so Kuma notices the pushes stopping."""
    t_start = time.monotonic()
    now = time.time() if now is None else float(now)
    d, probes, errs = conf if conf is not None else load_probes()
    errs = list(errs)
    if not probes and not errs:                      # nothing configured: quiet, unless probes ran here before (file vanished)
        prev = int(_fnum(load_state()["run"].get("total")))
        if prev:
            errs.append(f"probes.toml missing or empty, but {prev} probes ran before (delete probes.json if intended)")
    rep = RunReport(errors=errs, total=len(probes))
    if not probes and not errs:
        return rep
    with _locked() as got:
        if not got:
            rep.locked = True
            return rep
        st = load_state()
        st["cfg"] = {"n": len(errs), "blind": not probes, "t": now}
        cfg_events = _config_events(st, errs, not probes, now)
        if not probes:
            _queue(st, cfg_events)
            save_state(st)
            rep.events = cfg_events
            rep.elapsed_s = round(time.monotonic() - t_start, 2)
            return rep
        names = {p.name for p in probes}
        _retain(st, names, now)                      # NOT a prune: state of a probe missing from the valid config outlives a config typo
        for p in probes:
            if p.paused and isinstance(st["probes"].get(p.name), dict):
                st["probes"][p.name]["fresh"] = True     # time spent paused is neither up nor down
        period = _learn_period(st, now)
        due = [p for p in probes if (p.name in only if only else is_due(p, st["probes"].get(p.name), now) or force)]
        due = [p for p in due if not p.paused]
        due.sort(key=lambda p: _fnum(st["probes"].get(p.name, {}).get("last_run")))   # starved probes first (stable: config order otherwise)
        dn = {p.name for p in due}
        snap = Snapshot([p.target for p in probes if p.type == "systemd" and p.name in dn], st["fleet"])
        results = _execute(due, st, snap, now, d, sleep)
        changes = _apply(due, results, st, now, d, period)
        _learn_fleet(st, snap, now)
        rep.ran = [p.name for p in due if p.name in results]
        rep.events = _events(probes, st, now, d) + cfg_events
        _queue(st, rep.events)
        _history(probes, st, now, d, bool(changes))
        st["run"] = {"last_start": now, "last_end": now + (time.monotonic() - t_start), "ran": len(rep.ran), "total": len(probes),
                     "period": round(period, 1)}
        save_state(st)
    _push(probes, [p for p in due if p.name in rep.ran], st, maint_cfg or {}, d, pusher)
    rep.elapsed_s = round(time.monotonic() - t_start, 2)
    return rep


def _fan_out(items: list[Probe], look: Callable[[Probe], tuple], workers: int, end: float) -> tuple[dict[str, tuple], set[str]]:
    """One look at each of `items` on up to `workers` DAEMON threads, none started after the monotonic deadline `end`.
    Returns (finished results, names that were started). A daemon thread cannot hold the process at exit, which a
    ThreadPoolExecutor worker stuck in the kernel would; a probe still running at `end` is abandoned, not waited for."""
    if not items:
        return {}, set()
    todo: queue.SimpleQueue = queue.SimpleQueue()
    for p in items:
        todo.put(p)
    out: dict[str, tuple] = {}
    began: set[str] = set()
    lock = threading.Lock()

    def work() -> None:
        while time.monotonic() < end:
            try:
                p = todo.get_nowait()
            except queue.Empty:
                return
            with lock:
                began.add(p.name)
            try:
                r = look(p)
            except Exception as exc:  # noqa: BLE001
                r = (FAIL, f"probe error: {type(exc).__name__}", 0)
            with lock:
                out[p.name] = r

    threads = [threading.Thread(target=work, name=f"probe-{i}", daemon=True) for i in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    while True:                                         # an abandoned worker must not start probes nobody will read
        try:
            todo.get_nowait()
        except queue.Empty:
            break
    with lock:
        return dict(out), set(began)


def _execute(due: list[Probe], st: dict, snap: Snapshot, now: float, d: dict, sleep: Callable) -> dict[str, tuple]:
    """Verdicts for the due probes: {name: (level, detail, ms)}. Pass 1 looks at every probe once. If anything was not clean
    and the budget allows, ONE `retry_s` pause follows (here, in the caller's thread: no worker ever sleeps) and pass 2 re-looks
    only at those (repeated up to `attempts` looks); the best look wins. The global budget covers all passes. A probe the budget
    never reached has NO entry (no verdict, no availability sample, state untouched); a probe that started and never finished
    is a FAIL, but only if it has no earlier real look."""
    budget = float(d["budget_s"])
    end = time.monotonic() + budget
    never_up = {p.name: st["probes"].get(p.name, {}).get("last_ok") is None for p in due}
    look = lambda p: attempt(p, snap, now, never_up[p.name])  # noqa: E731
    best: dict[str, tuple] = {}
    began_first: set[str] = set()
    todo = list(due)
    for i in range(max(1, int(d["attempts"]))):
        if i:
            todo = [p for p in todo if best.get(p.name, (OK,))[0] in (WARN, FAIL)]
            if not todo or end - time.monotonic() <= float(d["retry_s"]) + 1.0:
                break                                    # nothing to re-check, or no budget left for a pause plus a pass
            sleep(d["retry_s"])
            snap.reset()                                 # re-read docker/systemd: the retry must see the host as it is NOW
        done, began = _fan_out(todo, look, int(d["workers"]), end)
        if i == 0:
            began_first = began
        for n, o in done.items():
            if n not in best or o[0] <= best[n][0]:
                best[n] = o
    for p in due:
        if p.name not in best and p.name in began_first:        # started, never finished: the only budget-made failure
            lvl, why = (SKIP, "not deployed yet (did not finish)") if p.optional and never_up[p.name] else (FAIL, "did not finish within the run budget")
            best[p.name] = (lvl, why, int(budget * 1000))
    return best


def _apply(due: list[Probe], results: dict[str, tuple], st: dict, now: float, d: dict, period: float = 0.0) -> list[tuple[Probe, int, int]]:
    """Feed each probe's run verdict into its persisted state. Returns [(probe, old_level, new_level)] for confirmed changes.
    A probe without a verdict (the budget never reached it) is left exactly as it was and stays due. Before the sample is counted,
    a long silence since the probe's previous sample (host off, tick dead) is booked as failed + unobserved time (_book_gap);
    `period` is the engine's own run spacing, so an engine that runs less often than a probe's interval is not read as down."""
    changes = []
    for p in due:
        if p.name not in results:
            continue
        s = st["probes"].setdefault(p.name, {})
        prev, fresh = s.get("last_run"), s.pop("fresh", False)
        lvl, detail, ms = results[p.name]
        s.update(last_run=now, detail=detail, ms=ms)
        if lvl == SKIP:
            s["skip"] = detail
            continue
        s.pop("skip", None)
        old = s.get("lvl") or 0
        s["raw"] = lvl
        s["last_ok" if lvl < FAIL else "last_fail"] = now
        if step(s, lvl, now, p):
            changes.append((p, old, lvl))
        if not fresh and _is_num(prev):
            _book_gap(st, p.name, float(prev), now, max(float(p.interval_s), period), int(d["keep_days"]))
        _bump_day(st, p.name, now, lvl < FAIL, int(d["keep_days"]))
    return changes


def _learn_fleet(st: dict, snap: Snapshot, now: float) -> None:
    """Remember which containers have been seen running (and since when), from this run's `docker ps -a` if there was one.
    A failed read teaches nothing. Old names are dropped after FLEET_KEEP_S; the table is bounded."""
    cts = snap.last_docker
    fleet = st["fleet"]
    if cts is not None:
        for n, (state, _h) in cts.items():
            if state == "running":
                f = fleet.get(n)
                fleet[n] = [f[0] if isinstance(f, list) and len(f) == 2 and _is_num(f[0]) else now, now]
    for n in [n for n, f in fleet.items() if not (isinstance(f, list) and len(f) == 2 and _is_num(f[1])) or now - f[1] > FLEET_KEEP_S]:
        del fleet[n]
    if len(fleet) > FLEET_MAX:
        for n in sorted(fleet, key=lambda n: fleet[n][1])[:len(fleet) - FLEET_MAX]:
            del fleet[n]


def forget(names: list[str]) -> list[str]:
    """Drop containers from the fleet baseline (the owner removed them on purpose). Returns the names that were known."""
    with _locked(5.0) as got:
        if not got:
            return []
        st = load_state()
        known = [n for n in names if st["fleet"].pop(n, None) is not None]
        if known:
            save_state(st)
        return known


def _stamp(st: dict, events: list[dict], now: float) -> list[dict]:
    """Give each new event its time, a dedupe key and a unique id (the handle delivery acknowledges)."""
    for e in events:
        st["seq"] += 1
        e.update(t=now, id=f"e{st['seq']}")
        e.setdefault("dedupe_key", f"probe:{e['probe']}")
    return events


def _queue(st: dict, events: list[dict]) -> None:
    st["events"] = (st["events"] + events)[-50:]


def _config_events(st: dict, errs: list[str], blind: bool, now: float) -> list[dict]:
    """Config trouble is itself an event: announced once per distinct set of problems (crit when no probe runs at all), and once
    more when it clears. Queued by the caller; stamped here."""
    sig = hashlib.sha1((("B|" if blind else "P|") + "|".join(sorted(errs))).encode()).hexdigest()[:12] if errs else ""
    prev = st.get("cfg_sig", "")
    if sig == prev:
        return []
    st["cfg_sig"] = sig
    if not sig:
        return _stamp(st, [{"probe": "*config", "title": "Probe configuration", "from": "config", "to": "up", "severity": "recovery",
                            "class": "-", "detail": "probe configuration is valid again", "down_s": int(now - st["cfg_t"]) if st.get("cfg_t") else 0}], now)
    st["cfg_t"] = now
    first = scrub(errs[0], 90)
    return _stamp(st, [{"probe": "*config", "title": "Probe configuration", "to": "config", "severity": "crit" if blind else "warn",
                        "class": "-", "detail": (f"MONITORING BLIND: {first}" if blind else f"{len(errs)} problems: {first}")}], now)


def _events(probes: list[Probe], st: dict, now: float, d: dict) -> list[dict]:
    """Announce confirmed changes (info-severity probes are silent; a storm becomes one summary event). A flapping probe is
    quiet while its level keeps changing, but a level that has HELD for max(SETTLE_MIN_S, 2 x interval) is announced anyway:
    flap damping must never hide a probe that crash-looped and then stayed down."""
    out = []
    for p in probes:
        s = st["probes"].get(p.name)
        if not s or s.get("lvl") is None or p.severity == "info":
            continue
        fl = _flapping(s, now, d)
        if fl and not s.get("fl"):
            out.append({"probe": p.name, "title": p.title, "to": "flapping", "severity": "warn", "detail": "state keeps changing"})
        s["fl"] = fl
        if s["lvl"] == s.get("ev", 0):
            continue
        if fl and now - s.get("since", now) < max(SETTLE_MIN_S, 2 * p.interval_s):
            continue
        down_for = max(0, int(now - _fnum(s.get("ann_t"), now))) if s["lvl"] == OK else 0     # >= 0 even if the clock stepped back
        out.append({"probe": p.name, "title": p.title, "from": WORD[s.get("ev", 0)], "to": WORD[s["lvl"]],
                    "severity": "recovery" if s["lvl"] == OK else eff_severity(p, s["lvl"]), "class": p.cls,
                    "detail": s.get("detail", ""), "down_s": down_for})
        s["ev"], s["ann_t"] = s["lvl"], now
    if len(out) > d["storm_max"]:
        worst = "crit" if any(e["severity"] == "crit" for e in out) else "warn"
        out = [{"probe": "*", "title": "Many probes changed state", "to": "storm", "severity": worst,
                "detail": f"{len(out)} probes changed state in one run (host stall?)"}]
    return _stamp(st, out, now)


def _history(probes: list[Probe], st: dict, now: float, d: dict, changed: bool) -> None:
    """One compact `kind:"probe"` record per run (throttled): {"n": probes counted, "up": n up, "bad": {name: "d"|"w"}}."""
    if not changed and now - st["hist_t"] < d["history_every_s"]:
        return
    bad, n = {}, 0
    for p in probes:
        s = st["probes"].get(p.name)
        if p.paused or not s or s.get("lvl") is None or s.get("skip"):
            continue
        n += 1
        if s["lvl"]:
            bad[p.name] = "w" if s["lvl"] == WARN else "d"
    try:
        core.append_history({"t": now, "kind": "probe", "n": n, "up": n - len(bad), "bad": bad})
        st["hist_t"] = now
    except OSError:
        pass


def _push(probes: list[Probe], due: list[Probe], st: dict, mcfg: dict, d: dict, pusher: Callable | None) -> None:
    """GET-only Kuma push heartbeats (no-ops without a token): per probe with kuma_push_key, plus the umbrella dead-man's switch."""
    push = pusher or core.kuma_push
    try:
        for p in due:
            s = st["probes"].get(p.name) or {}
            if p.kuma_push_key and s.get("lvl") is not None:
                push(mcfg, p.kuma_push_key, "crit" if s["lvl"] == FAIL else "ok", f"{p.name}: {s.get('detail', '')}")
        if due and d.get("deadman_key"):
            # The dead-man's switch says "the plane is alive", not "everything is up": it is always ok when it is sent, and
            # Kuma raises the alarm when the pushes STOP. Health travels through the per-probe kuma_push_key heartbeats.
            lv = [(st["probes"].get(p.name) or {}).get("lvl") for p in probes if not p.paused]
            down, ok = sum(1 for x in lv if x == FAIL), sum(1 for x in lv if x == OK)
            push(mcfg, d["deadman_key"], "ok", f"alive {ok} up {down} down")
    except Exception:  # noqa: BLE001 - a heartbeat problem must never fail the monitoring run
        pass


# --------------------------------------------------------------------------- event delivery (two-phase)
def claim_events(now: float | None = None, limit: int = 50) -> list[dict]:
    """Phase 1: hand the pending events to ONE deliverer without deleting them. A claimed event stays in the state file
    (`inflight`, with its claim time) until ack_event() / release_events(); a claim older than CLAIM_TTL_S belongs to a
    deliverer that died (TERM, KILL, OOM, job timeout) and is requeued first, so a crash delays a page and never drops it.
    Returned most urgent first: crit, then warn, then recoveries, newest first within each. Events still backing off after a
    failed delivery (`nb` in the future) stay queued."""
    now = time.time() if now is None else float(now)
    with _locked(5.0) as got:
        if not got:
            return []
        st = load_state()
        # abs(): a claim stamped in the FUTURE (the clock stepped back since) would otherwise never expire until the clock catches up
        stale = [e for e in st["inflight"] if isinstance(e, dict) and abs(now - _fnum(e.get("ct"))) > CLAIM_TTL_S]
        st["inflight"] = [e for e in st["inflight"] if isinstance(e, dict) and e not in stale]
        st["events"] = [e for e in stale + st["events"] if isinstance(e, dict)]
        for e in st["events"]:
            if not e.get("id"):                                    # an event queued by an older version: give it a handle
                st["seq"] += 1
                e["id"] = f"e{st['seq']}"
        # a back-off further ahead than the longest one we ever set came from a clock that has since stepped back: not waiting for it
        ready = [e for e in st["events"] if _fnum(e.get("nb")) <= now or _fnum(e.get("nb")) - now > BACKOFF_MAX_S][:limit]
        ids = {e["id"] for e in ready}
        st["events"] = [e for e in st["events"] if e["id"] not in ids]
        for e in ready:
            e["ct"] = now
        st["inflight"] += ready
        save_state(st)
        rank = {"crit": 0, "warn": 1, "recovery": 2}
        return sorted((dict(e) for e in ready), key=lambda e: (rank.get(e.get("severity"), 3), -_fnum(e.get("t"))))


def ack_event(eid: Any) -> None:
    """Phase 2: the event was delivered (or deliberately dropped by policy): delete it. Waits for the lock, because a probe
    run may hold it for up to budget_s; a missed ack only means the claim expires and notify's dedupe absorbs the repeat."""
    with _locked(20.0) as got:
        if got:
            st = load_state()
            st["inflight"] = [e for e in st["inflight"] if not (isinstance(e, dict) and e.get("id") == eid)]
            save_state(st)


def release_events(ids: list, failed: bool, now: float | None = None) -> None:
    """Put claimed events back in the queue. failed=True is a delivery that did not go through: it backs off (1, 2, 4 ... 10 min)
    so a dead transport is not hammered every minute; failed=False is "no time left this run": retried at once."""
    if not ids:
        return
    now = time.time() if now is None else float(now)
    with _locked(20.0) as got:
        if not got:
            return
        st = load_state()
        back = [e for e in st["inflight"] if isinstance(e, dict) and e.get("id") in ids]
        st["inflight"] = [e for e in st["inflight"] if e not in back]
        for e in back:
            e.pop("ct", None)
            if failed:
                e["tries"] = int(_fnum(e.get("tries"))) + 1
                e["nb"] = now + min(60 * 2 ** (e["tries"] - 1), BACKOFF_MAX_S)
        st["events"] = (back + st["events"])[-50:]
        save_state(st)


def pop_events() -> list[dict]:
    """LEGACY one-shot drain (queued and in-flight): it deletes before anyone delivered, so a caller that dies loses the events.
    notify_events uses claim_events / ack_event / release_events instead."""
    with _locked(5.0) as got:
        if not got:
            return []
        st = load_state()
        ev, st["events"], st["inflight"] = st["events"] + st["inflight"], [], []
        if ev:
            save_state(st)
        return ev


def requeue_events(events: list[dict]) -> None:
    """Put events back (oldest first) when their delivery failed, so the next run retries instead of losing the page."""
    if not events:
        return
    with _locked(5.0) as got:
        if got:
            st = load_state()
            st["events"] = (list(events) + st["events"])[-50:]
            save_state(st)


# --------------------------------------------------------------------------- read side (monitors.json, task, live)
def how(p: Probe) -> str:
    """A short description that reveals no host, path outside the URL path, query or credentials. Total: it never raises."""
    try:
        if p.type in ("http", "json") and str(p.target).startswith("http"):
            u = urlsplit(p.target)
            try:
                port = u.port
            except ValueError:                           # build() rejects these, but a hand-made Probe must not break the export
                port = None
            port = port or (443 if u.scheme == "https" else 80)
            return f"{p.method} :{port}{u.path or '/'}" if u.hostname in ("127.0.0.1", "localhost", "::1") else f"{p.method} {u.scheme} {u.hostname}{u.path or '/'}"
        if p.type == "tcp":
            return f"tcp :{str(p.target).rpartition(':')[2]}"
        if p.type == "docker":
            return {"@daemon": "docker daemon answers", "*": "every container running/healthy"}.get(p.target, "container state + healthcheck")
        if p.type == "systemd":
            return f"unit {p.target}"
        if p.type == "command":
            return f"command {os.path.basename(p.target[0])}"
        return f"{'file age' if p.type == 'file_age' else 'json state'} {os.path.basename(str(p.target))}"
    except Exception:  # noqa: BLE001
        return f"{p.type} probe"


def snapshot(now: float | None = None, conf: tuple | None = None) -> list[dict]:
    """Every probe merged with its last state, read-only. This is THE data other modules (live, web, SLO) should use."""
    now = time.time() if now is None else float(now)
    d, probes, _errs = conf if conf is not None else load_probes()
    st = load_state()
    rows = []
    for p in probes:
        s = st["probes"].get(p.name) or {}
        state = state_of(p, s)
        days = st["days"].get(p.name, [])
        rows.append({
            "name": p.name, "title": p.title, "type": p.type, "group": p.group, "class": p.cls, "severity": p.severity,
            "source": p.source, "kuma": p.kuma, "kuma_paused": p.kuma_paused, "tags": list(p.tags), "optional": p.optional,
            "state": state, "lvl": s.get("lvl") if state in ("up", "warn", "down") else None, "since": s.get("since"),
            "last_run": s.get("last_run"), "interval_s": p.interval_s, "ms": s.get("ms"),
            "detail": s.get("skip") or s.get("detail", ""), "flapping": bool(s.get("fl")), "how": how(p), "slo": p.slo,
            "avail_today": availability(days, now, 1), "avail_7d": availability(days, now, 7),
            "avail_30d": availability(days, now, 30), "pending": s.get("streak", 0),
            "obs_today": observed(days, now, 1), "obs_7d": observed(days, now, 7), "obs_30d": observed(days, now, 30),
            "kuma_push_key": bool(p.kuma_push_key)})
    return rows


# --------------------------------------------------------------------------- Kuma parity (reads a COPY of kuma.db, never writes)
def kuma_monitors(db_copy: str) -> list[dict]:
    """name/type/active/parent of every Kuma monitor from a COPY of kuma.db (credential columns are never selected)."""
    con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        return [{"id": i, "name": n, "type": t, "active": bool(a), "parent": pa}
                for i, n, t, a, pa in con.execute("select id, name, type, active, parent from monitor order by id")]
    finally:
        con.close()


def kuma_diff(db_copy: str, probes: list[Probe], kuma_cfg: dict) -> dict:
    """Which Kuma monitors a probe (or a probe `group`) carries, which are unmapped, and which are paused in Kuma."""
    have = {p.kuma for p in probes if p.kuma}
    groups, ignore = set((kuma_cfg.get("groups") or {}).keys()), set(kuma_cfg.get("ignore") or [])
    mons = kuma_monitors(db_copy)
    return {"covered": sorted(m["name"] for m in mons if m["name"] in have or m["name"] in groups),
            "unmapped": sorted(m["name"] for m in mons if m["name"] not in have | groups | ignore),
            "paused_in_kuma": sorted(m["name"] for m in mons if not m["active"]),
            "stale_mapping": sorted(have - {m["name"] for m in mons})}


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "list"
    if cmd == "validate":
        _d, probes, errs = load_probes()
        print(f"{len(probes)} probes valid, {len(errs)} problems")
        for e in errs:
            print(f"  {e}")
        return 1 if errs else 0
    if cmd == "kuma-diff" and len(a) == 2:
        _d, probes, _e = load_probes()
        print(json.dumps(kuma_diff(a[1], probes, _kuma_cfg()), indent=1))
        return 0
    if cmd == "export":
        from .tasks import monitors
        print(json.dumps(monitors.export(), separators=(",", ":")))
        return 0
    if cmd == "forget" and len(a) >= 2:              # the owner removed these containers on purpose: stop reporting them missing
        known = forget(a[1:])
        print(f"forgot {len(known)}/{len(a) - 1}: {', '.join(known) or '-'}")
        return 0
    if cmd == "run":
        only = set(a[a.index("--only") + 1].split(",")) if "--only" in a else None
        cfg = core.load_config()
        rep = run_due(force="--force" in a, only=only, maint_cfg=cfg)
        print(f"ran {len(rep.ran)}/{rep.total} probes in {rep.elapsed_s}s" + (" (another run holds the lock)" if rep.locked else ""))
        for e in rep.errors:
            print(f"  config: {e}")
        if not rep.total and rep.errors:
            print("  MONITORING BLIND: no probe is running")
        if "--notify" in a:                      # the scheduler tick: deliver per-probe events (only when the owner opted in)
            tcfg = cfg.get("tasks", {}).get("probes", {})
            if tcfg.get("alert_mode") == "events":
                from .tasks import monitors
                kw = {"budget_s": float(tcfg["notify_budget_s"])} if _is_num(tcfg.get("notify_budget_s")) else {}
                r = monitors.notify_events(**kw)
                print(f"notify: sent {r['sent']}, dropped {r['dropped']}, retry {r['retry']}, deferred {r.get('deferred', 0)}, expired {r['expired']}")
            else:
                print("notify: skipped (alert_mode is not 'events', the check tier pages on the task level)")
        rows = snapshot()
        for r in rows if "--all" in a else [r for r in rows if r["state"] in ("down", "warn", "unknown") or r["flapping"]]:
            print(f"{r['state']:<8} {r['class']} {r['name']:<28} {r['detail'][:70]}")
        return 0
    for r in snapshot():
        print(f"{r['state']:<8} {r['class']} {r['name']:<28} {r['detail'][:70]}")
    return 0


def _kuma_cfg() -> dict:
    return core.load_toml(core.CONF_DIR / "probes.toml").get("kuma", {})


if __name__ == "__main__":
    sys.exit(main())
