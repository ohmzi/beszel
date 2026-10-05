"""self_health: the health of the monitoring PIPELINE itself (SPEC6 section 6). Task `self_health` (C0, check tier) and the public
`self.json` that the website's top bar shows ("Monitoring pipeline: healthy / degraded: runner stopped 12 min ago").

Every other check says whether the HOST is fine; this one says whether the thing that tells you so still works:
runner -> publish -> website, plus the pieces next to it (scheduler tick, live monitor, sensor ring, rules registry, ack inbox,
Kuma heartbeat). One verdict: {"level": ok|degraded|down, "reasons": [plain language], "since": t}.

  level     Result.status  meaning
  down      crit           monitoring is BLIND or about to be: the check tier stopped (status.json older than 3 check intervals),
                           most task runs are failing, critical pages cannot be delivered (outbox stuck or full), or the state
                           directory is nearly out of space (every write is about to fail)
  degraded  warn           something in the pipeline is late, stuck or broken but alerts still flow (publish stopped, tick stopped, live
                           or sensor sampler stopped, daily/weekly missed, registry not applied, ack backlog, website unhealthy, the
                           alert transport or the Kuma push failing ...)
  ok        ok             everything that is deployed is current. Parts that are not deployed yet (older installs: no tick, no live
                           monitor, no website, no registry: no unit installed and no trace) are `info` rows, never a problem: they
                           appear in `checks`, not in `reasons`. A part whose unit IS installed owes output (see below).

ONE ROW PER PIPELINE PART (fixed order; each row = {id, title, state ok|degraded|down|info|unknown, detail, reason, hint, age_s, limit_s}):
  runner     tier_runs.check.last_run (else the newest check task, else generated_at) vs the check interval
             degraded > 1.5 x interval + grace (a run was missed), down > 3 x interval (15 min with the shipped 300 s)
  publish    manifest.json generated_at, else the mtime of overview.json (publish rewrites it on every run); overview.export_errors
  tick       newest of RUN_DIR/tick.json, status.json tick.last_run, sched.json mtime; the tick runs every minute: degraded > 5 min
  daily      tier_runs.daily.last_run vs 30 h        weekly   tier_runs.weekly.last_run vs 9 d   (never ran: only after `born` + limit)
  live       public/live.json generated_at (the daemon writes every 5 s): degraded > 90 s
  metrics    metrics-ring.json last.t (sampled every minute): degraded > 5 min
  registry   rules.d vs STATE_DIR/rules/current.json: file set and content (sha256 of the raw bytes, as registry.py records it; dotfiles
             and non-regular files are not part of the registry), `current.json.invalid` (a rejected change: cleared by the registry itself
             when the owner goes back to the applied content), generated files edited by hand, manifest registry_hash
  errors     task records with status error in the last 24 h of history.jsonl (tail read, regex, no JSON per line) + tasks in error now
  state      free space of the state (and log) filesystem, state dir size and growth per day
  acks       STATE_DIR/ack/inbox: files waiting and the oldest age
  alerts     can an alert actually be DELIVERED? notify-state.json (read as JSON, no notify import): the transport circuit breaker, critical
             pages stuck in the outbox (degraded after 15 min or an open breaker; down after outbox_ttl_s/2 or when the outbox is full)
  website    GET http://127.0.0.1:8088/api/health (3 s) + `systemctl is-active beszel-hub.service` (the OhmzMaintainer dashboard, the
             site the old maintenance-web container was retired in favour of). Optional: nothing there and never seen = "not deployed
             yet", info
  kuma       /etc/homelab-maint/kuma.toml [push] has the umbrella's own heartbeat keys (presence and token SHAPE only, never a value) and,
             when core.kuma_push records its result through note_kuma(), N pushes in a row that failed

"NEVER SEEN" IS NOT "NOT DEPLOYED". tick, live, metrics and publish are owed output as soon as their systemd unit file exists
(/etc/systemd/system/homelab-maint-{tick.timer,live.service,metrics.timer,check.timer}: a stat, no systemctl): a part whose unit is
installed and that has produced nothing after its limit + 3 min is degraded, whatever this module's own state file remembers. Only
a part with no unit and no trace is "not deployed" (info). The same install evidence anchors every "has not run YET" grace
(`born` = the earliest of the persisted first_seen and the check timer's unit file), so the grace survives a lost state file and
a CLI call that does not persist anything.

THREE MODES (assess(mode=...)); the evidence differs, the rows do not:
  export   the verdict for the website (publish, the 1-minute refresher): strict, recomputed from files
  cli      a human asked (`homelab-maint self-health`, doctor): the same, but with no benefit of the doubt: no status.json and no
           sign of an install is degraded at once (with the check timer installed minutes ago it still waits)
  runner   inside the check task itself: its own run is proof that the runner is alive. The runner row can never be `down` (the
           previous run's status.json is read, and after a reboot or an outage that is old by definition: "a monitoring gap that has
           ended", info) and the publish row only complains when publishing fell behind the runner; Kuma, the page and the SLO would
           otherwise get a crit sample after every long outage. The strict judgement of the same evidence stays in export/cli.

SAFETY (a monitor of the monitors must not depend on, or disturb, what it watches):
  * imports only core (stdlib + tomllib); no notify, publish, probes or live import: nothing that can be down is needed to say it is down
  * C0, read-only: it writes only its own state STATE_DIR/tasks/self_health.json (since, first_seen, seen flags, size samples, cache),
    the public self.json, and (note_kuma, called by core.kuma_push) STATE_DIR/kuma-state.json
  * the website's unit is read with `systemctl is-active` only (a read-only query: nothing is started, stopped or reloaded), and its
    /api/health is only ever asked with GET on a loopback address (a non-loopback `web_host` option is ignored), 3 s at most. The
    service name is only ever passed to systemctl as one argv element, never through a shell
  * every row is computed inside its own try/except: a row that cannot run is `unknown` (counts as degraded), never an exception
  * time is always `now` (ctx.now / the caller's clock): a timestamp in the future is clock trouble, never freshness
  * no secrets: kuma tokens are matched by shape and never copied; texts are ASCII and short; nothing from the website body but a
    sanitised 80-char reason

COST: < 300 ms typical. Reads a handful of small files, one tail window (<= 6 MiB) of history.jsonl, a 60 ms-budgeted walk of the
state dir, one `systemctl is-active` and one loopback GET, the two in parallel threads. The only way to exceed 300 ms is a HUNG
website or systemd (bounded by web_timeout_s = 3 s, 2.5 s for the one systemctl call).

PUBLIC self.json (export(now); glue: publish.OPTIONAL_SOURCES["self.json"] = (("tasks.self_health", "export"),); < 8 KB):
  {"schema":2,"generated_at":t,"valid_until":t+degraded_after_s,"level":"ok|degraded|down","headline":str,
   "verdict":{"level","reasons":[str],"since":t},            since = when the CURRENT level began
   "ttl":{"refresh_s":60,"degraded_after_s":180,"down_after_s":600},
                                                             FILE freshness, decoupled from the runner limits: the page must degrade
                                                             by itself when now - generated_at exceeds these (see effective(), the
                                                             tested reference of that rule). The file is rewritten every minute by the
                                                             refresher (a 1-minute timer, `--refresh`) and at the end of every tier
                                                             run, so an OLD file means the refresher is dead; "the numbers are old"
                                                             must be said by the page, not by a file that is no longer written
   "limits":{"runner_late_s":1470,"runner_down_s":2700},     the runner thresholds the verdict itself uses (informational)
   "checks":[row ...],"metrics":{scalars}}

OPTIONS ([tasks.self_health] in maint.toml; all optional, a wrong type or range falls back to the default):
  check_interval_s 300   late_factor 1.5   grace_s 120   down_factor 3.0     runner/publish limits = late: 1.5 x + grace, down: 3 x
  refresh_s 60   stale_factor 3.0   stale_down_factor 10.0                    file ttl = 3 x / 10 x the refresh period (180 / 600 s)
  tick_late_s 300   daily_max_h 30   weekly_max_d 9   live_late_s 90   metrics_late_s 300
  state_free_warn_pct 5   state_free_warn_gib 2   state_free_down_mib 256   state_max_gib 4   state_growth_warn_mib_day 512
  error_rate_warn_pct 5   error_rate_down_pct 50   error_min_runs 20   error_tasks_warn 3
  inbox_max 25   inbox_late_s 600   registry_grace_s 600   registry_check_generated true
  outbox_warn_s 900   breaker_fail_n 3   kuma_fail_n 3
  web_check true   web_host "127.0.0.1"   web_port 8088   web_path "/api/health"   web_service "beszel-hub.service"   web_timeout_s 3.0
  kuma_keys ["tier-check", "umbrella-probes"]   kuma_required false

CLI (glue: cli.PASS["self-health"] = ("tasks.self_health", "main", ())):
  python3 -m homelab_maint.tasks.self_health [--json] [--check] [--write] [--refresh] [--published]
    default prints a table (mode cli: a human asking); --json prints self.json; --check exits 0/1/2 for ok/degraded/down;
    --write persists the state and writes STATE_DIR/public/self.json (only when public/ already exists), with the fresh numbers;
    --refresh is the 1-minute unit's call (homelab-maint-selfhealth.service): the same, with the 30-minute caches for the
    expensive rows, prints nothing, exit 0 (1 only when public/ exists and the file could not be written);
    --published prints what a CONSUMER must show for the file now on disk (the age rule applied) and, with --check, exits 0/1/2.
  doctor() -> (ok, hint): the live verdict (mode cli) and the published file's freshness, for `homelab-maint doctor`.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import itertools
import json
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from .. import core
from ..core import GIB, Ctx, Result, human, read_json, sh, task

SCHEMA = 2                                      # 2: ttl is the FILE's freshness (was the runner's lateness); limits/valid_until added
SKEW_S = 300                                    # a stamp further in the future than this is clock trouble
HIST_TAIL = 6 * 1024 * 1024                     # newest history.jsonl bytes read for the error rate (24 h is ~2-5 MB)
CACHE_S = 1800                                  # export() reuses the expensive parts (history rate, dir size) this long
WALK_BUDGET_S = 0.06                            # state dir walk: time budget ...
WALK_MAX = 20000                                #            ... and entry cap; a cut walk is never trusted for growth
UNIT_DIR = Path(os.environ.get("HOMELAB_MAINT_UNITS", "/etc/systemd/system"))   # where install.sh puts the units (read-only stats)
UNIT_FILES = {"check": "homelab-maint-check.timer", "tick": "homelab-maint-tick.timer", "metrics": "homelab-maint-metrics.timer",
              "live": "homelab-maint-live.service"}
UNIT_GRACE_S = 180                              # a part whose unit is installed owes output after its own limit + this
STALE_CAP = (900.0, 3600.0)                     # a consumer never trusts a ttl longer than this (degraded, down)
LOOPBACK = {"127.0.0.1", "localhost"}
RANK = {"down": 3, "degraded": 2, "unknown": 2, "info": 1, "ok": 0}
WORD = {"ok": "ok", "degraded": "DEGRADED", "down": "DOWN"}
_statvfs = os.statvfs                           # seams for the tests
_http_timeout = (socket.timeout, TimeoutError)

DEFAULTS: dict[str, Any] = {
    "check_interval_s": 300, "late_factor": 1.5, "grace_s": 120, "down_factor": 3.0,
    "tick_late_s": 300, "daily_max_h": 30, "weekly_max_d": 9, "live_late_s": 90, "metrics_late_s": 300,
    "state_free_warn_pct": 5.0, "state_free_warn_gib": 2.0, "state_free_down_mib": 256, "state_max_gib": 4.0,
    "state_growth_warn_mib_day": 512,
    "error_rate_warn_pct": 5.0, "error_rate_down_pct": 50.0, "error_min_runs": 20, "error_tasks_warn": 3,
    "inbox_max": 25, "inbox_late_s": 600, "registry_grace_s": 600, "registry_check_generated": True,
    "web_check": True, "web_host": "127.0.0.1", "web_port": 8088, "web_path": "/api/health", "web_service": "beszel-hub.service",
    "web_timeout_s": 3.0,
    "kuma_keys": ["tier-check", "umbrella-probes"], "kuma_required": False,
    "refresh_s": 60.0, "stale_factor": 3.0, "stale_down_factor": 10.0,
    "outbox_warn_s": 900, "breaker_fail_n": 3, "kuma_fail_n": 3,
}
TITLES = {"runner": "Runner (check tier)", "publish": "Publish to website", "tick": "Scheduler tick", "daily": "Daily maintenance",
          "weekly": "Weekly maintenance", "live": "Live monitor", "metrics": "Sensor ring", "registry": "Rules registry",
          "errors": "Runner errors", "state": "State directory", "acks": "Acknowledge inbox", "alerts": "Alert delivery",
          "website": "Website",
          "kuma": "Kuma heartbeat"}
GENERATED = ("maint.toml", "routine.toml", "jobs.toml", "probes.toml", "classes.toml", "notify.toml", "ack.toml", "protected.toml")
HASHES = ("sha256", "sha1", "md5", "blake2b", "blake2s")


# --------------------------------------------------------------------------- small helpers
def _ascii(s: Any, n: int = 120) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s)).strip()[:n]


def _name(s: Any, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(s))[:n]


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _dur(s: float) -> str:
    s = max(0.0, s)
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def _row(state: str, detail: str, reason: str = "", hint: str = "", age: float | None = None, limit: float | None = None,
         nd: bool = False) -> dict:
    """nd=True marks an info row that means "this optional part is not deployed" (the summary counts those; never published)."""
    r: dict[str, Any] = {"state": state, "detail": _ascii(detail, 160)}
    if nd:
        r["nd"] = True
    if state not in ("ok", "info"):
        r["reason"] = _ascii(reason or detail, 120)
        if hint:
            r["hint"] = _ascii(hint, 110)
    if age is not None:
        r["age_s"] = int(age)
    if limit is not None:
        r["limit_s"] = int(limit)
    return r


def _jread(path: Path, cap: int = 1 << 20) -> tuple[Any, str]:
    """(object, "ok") | (None, "missing") | (None, "bad"): a file that is there but not JSON is not the same as one that is absent."""
    try:
        with open(path, "rb") as f:
            raw = f.read(cap + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "bad"
    if len(raw) > cap:
        return None, "bad"
    try:
        return json.loads(raw), "ok"
    except (ValueError, RecursionError):
        return None, "bad"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _stamp(doc: Any, key: str = "generated_at") -> float | None:
    return _num(doc.get(key)) if isinstance(doc, dict) else None


def _atomic_json(path: Path, obj: Any, mode: int) -> None:
    """Write JSON atomically through a tmp file with a UNIQUE name (the 1-minute refresher, a tier run's publish and a human can all
    write the same file; core.write_json_atomic shares one tmp name, so two writers could publish a truncated file). The tmp is a
    dotfile named like publish.py's (.pub-*.tmp): invisible to the manifest, and swept by publish if a killed writer left it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pub-self-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1, sort_keys=True, default=str)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Opts:
    """Validated [tasks.self_health] options: anything of the wrong type or outside its range is the default (fail to the shipped value)."""

    def __init__(self, get: Callable[[str, Any], Any]):
        self._get = get

    def num(self, k: str, lo: float = 0.0, hi: float = 1e12) -> float:
        v = self._get(k, DEFAULTS[k])
        v = _num(v)
        return float(v) if v is not None and lo <= v <= hi else float(DEFAULTS[k])

    def flag(self, k: str) -> bool:
        v = self._get(k, DEFAULTS[k])
        return v if isinstance(v, bool) else bool(DEFAULTS[k])

    def text(self, k: str, rx: str) -> str:
        v = self._get(k, DEFAULTS[k])
        return v if isinstance(v, str) and re.fullmatch(rx, v) else str(DEFAULTS[k])

    def keys(self, k: str) -> list[str]:
        v = self._get(k, DEFAULTS[k])
        ok = isinstance(v, list) and 0 < len(v) <= 8 and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", x) for x in v)
        return list(v) if ok else list(DEFAULTS[k])


MODES = ("export", "cli", "runner")


class Cx:
    """One assessment: the clock, the options, the persistent state and the files read at most once."""

    def __init__(self, now: float, opt: Callable[[str, Any], Any], state: dict, quick: bool, mode: str = "export"):
        self.now, self.o, self.state, self.quick = now, Opts(opt), state, quick
        self.mode = mode if mode in MODES else "export"
        self.m: dict[str, Any] = {}                                  # scalar metrics collected by the rows
        if not isinstance(state.get("seen"), dict):
            state["seen"] = {}
        fs = _num(state.get("first_seen"))
        state["first_seen"] = fs if fs is not None and fs <= now else now
        iv = self.o.num("check_interval_s", 60, 86400)
        self.check_s = iv
        self.late_s = self.o.num("late_factor", 1.0, 10.0) * iv + self.o.num("grace_s", 0, 3600)
        self.down_s = max(self.o.num("down_factor", 1.5, 100.0) * iv, 1.5 * self.late_s)
        self._memo: dict[str, Any] = {}
        # Durable evidence that this install exists, independent of this module's own state file (which a CLI call never writes and a
        # state wipe loses): the check timer's unit file. `born` = the earliest evidence; every "not run YET" grace is measured from it.
        self.install_t = self.unit_t("check")
        self.born = min(state["first_seen"], self.install_t) if self.install_t is not None else state["first_seen"]

    def unit_t(self, cid: str) -> float | None:
        """mtime of the systemd unit file the installer put in place for this part (a stat, never systemctl); None = no such unit.
        A stamp from the future (clock trouble) counts as 'installed just now': the unit exists either way."""
        name = UNIT_FILES.get(cid)
        t = _mtime(UNIT_DIR / name) if name else None
        return None if t is None else min(t, self.now)

    def once(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    @property
    def status(self) -> tuple[Any, str]:
        return self.once("status", lambda: _jread(core.STATE_DIR / "status.json", 16 << 20))

    @property
    def pub(self) -> Path:
        return core.STATE_DIR / "public"

    @property
    def manifest(self) -> Any:
        return self.once("manifest", lambda: _jread(self.pub / "manifest.json")[0])

    @property
    def overview(self) -> Any:
        return self.once("overview", lambda: _jread(self.pub / "overview.json")[0])

    def first_seen(self, cid: str) -> bool:
        """Has this optional part ever left a trace on this host? (Then its absence is a fault, not 'not deployed'.)"""
        return cid in self.state["seen"]

    def mark(self, cid: str) -> None:
        self.state["seen"].setdefault(cid, self.now)

    def age_state(self, t: float | None, late: float, down: float | None = None) -> str:
        """ok | degraded | down | skew for the age of a timestamp; None => 'none'."""
        if t is None:
            return "none"
        age = self.now - t
        if age < -SKEW_S:
            return "skew"
        if down is not None and age > down:
            return "down"
        return "degraded" if age > late else "ok"


def _tier_last(st: dict, tier: str) -> float | None:
    """When a tier last ran: tier_runs[tier].last_run, else the newest task row of that tier (older installs have no tier_runs)."""
    tr = st.get("tier_runs")
    t = _stamp(tr.get(tier), "last_run") if isinstance(tr, dict) and isinstance(tr.get(tier), dict) else None
    if t is not None:
        return t
    tasks = st.get("tasks") if isinstance(st.get("tasks"), dict) else {}
    ts = [_num(e.get("last_run")) for e in tasks.values() if isinstance(e, dict) and e.get("tier") == tier and e.get("klass") != "J"]
    ts = [x for x in ts if x is not None]
    return max(ts) if ts else None


# --------------------------------------------------------------------------- rows: runner, publish, tick, tiers
def c_runner(cx: Cx) -> dict:
    hint = "journalctl -u homelab-maint-check -n 40; systemctl status homelab-maint-check.timer"
    st, why = cx.status
    if why == "missing":
        waited = cx.now - cx.born
        if waited > cx.late_s:
            return _row("degraded", f"no status.json although the runner should have produced one ({_dur(waited)} after install)",
                        "the check tier has never produced status.json", hint)
        if cx.mode == "cli" and cx.install_t is None:       # a human is asking and nothing shows the install is young: no benefit of the doubt
            return _row("degraded", "no status.json and no check timer unit: the check tier has never run", "the check tier has never run (no status.json)", hint)
        return _row("info", "no status.json yet: waiting for the first check run")
    if why == "bad" or not isinstance(st, dict):
        return _row("degraded", "status.json is unreadable", "status.json is corrupt or unreadable", hint)
    gen = _stamp(st)
    last = _tier_last(st, "check")
    last = last if last is not None else gen
    if gen is not None:
        cx.m["status_age_s"] = int(cx.now - gen)
    if last is None:
        return _row("degraded", "status.json carries no run time", "status.json has no run time", hint)
    age = cx.now - last
    cx.m["check_age_s"] = int(age)
    iv = f"every {_dur(cx.check_s)}"
    s_ = cx.age_state(last, cx.late_s, cx.down_s)
    if s_ == "skew":
        return _row("degraded", f"last check run is stamped {_dur(-age)} in the future", "clock stepped back: run times cannot be trusted", "timedatectl status", age, cx.late_s)
    if cx.mode == "runner" and s_ in ("down", "degraded"):
        # This very run is proof that the runner is alive: what is old is the PREVIOUS run's record (after a reboot or an outage it is
        # old by definition). Report the gap that just ended; never page, never turn the verdict, Kuma or the SLO red because of it.
        cx.m["gap_s"] = int(age)
        return _row("info", f"previous check run was {_dur(age)} ago ({iv}): a monitoring gap that has ended", age=age, limit=cx.late_s)
    if s_ == "down":
        return _row("down", f"no check run for {_dur(age)} ({iv}): monitoring is blind", f"runner stopped: no check run for {_dur(age)}", hint, age, cx.down_s)
    if s_ == "degraded":
        return _row("degraded", f"last check run {_dur(age)} ago ({iv}): a run was missed", f"runner late: last check run {_dur(age)} ago", hint, age, cx.late_s)
    return _row("ok", f"last check run {_dur(age)} ago ({iv})", age=age, limit=cx.late_s)


def c_publish(cx: Cx) -> dict:
    hint = "homelab-maint publish; ls -l /var/lib/homelab-maint/public"
    pub = cx.pub
    ran = _tier_last(cx.status[0], "check") if isinstance(cx.status[0], dict) else None      # a completed check run publishes at its end
    ran_long_ago = ran is not None and cx.now - ran > cx.late_s
    if not pub.is_dir():
        if cx.first_seen("publish"):
            return _row("degraded", "the public directory is gone", "public export directory disappeared", hint)
        if ran_long_ago:
            return _row("degraded", f"the runner ran {_dur(cx.now - ran)} ago but nothing was ever published (no public directory)",
                        "the runner has never published anything", hint)
        return _row("info", "publishing is not enabled yet (no public directory)", nd=True)
    man, ov = cx.manifest, cx.overview
    t = _stamp(man)
    if t is not None and t > cx.now + SKEW_S:
        t = None                                              # a future stamp is not freshness: fall back to the file time
    src = "manifest"
    if t is None:
        t, src = _mtime(pub / "overview.json"), "overview"
    if t is None:
        if cx.first_seen("publish") or ran_long_ago or cx.now - cx.born > cx.late_s:
            return _row("degraded", "nothing has been published", "nothing has been published to the website", hint)
        return _row("info", "nothing published yet: waiting for the first run")
    cx.mark("publish")
    age = cx.now - t
    cx.m["publish_age_s"] = int(age)
    og = _stamp(ov)                                           # the runner time the website shows (overview.generated_at = status time)
    data = f"; the numbers shown are {_dur(cx.now - og)} old" if og is not None and cx.now - og > 2 * cx.check_s else ""
    if og is not None:
        cx.m["web_data_age_s"] = int(cx.now - og)
    errs = [_name(e) for e in (ov.get("export_errors") if isinstance(ov, dict) and isinstance(ov.get("export_errors"), list) else [])][:4]
    s = cx.age_state(t, cx.late_s, cx.down_s)
    if s == "skew":
        return _row("degraded", f"publish time is {_dur(-age)} in the future", "clock stepped back: publish time cannot be trusted", "timedatectl status", age, cx.late_s)
    if cx.mode == "runner" and s in ("down", "degraded") and ran is not None and ran - t <= cx.o.num("grace_s", 0, 3600):
        # Inside the check run: the last publish was made by the PREVIOUS run (right after it) and that run is the old thing; this
        # run publishes at its end. Publishing only counts as stopped when the runner ran AFTER the last publish (see below).
        return _row("info", f"last publish {_dur(age)} ago, with the previous check run: the gap has ended, this run publishes at its end", age=age, limit=cx.late_s)
    if s in ("down", "degraded"):                              # a dead publisher leaves alerts flowing: degraded, however old
        return _row("degraded", f"last publish {_dur(age)} ago ({src}): the website shows old data", f"publishing stopped {_dur(age)} ago", hint, age, cx.late_s)
    if errs:
        return _row("degraded", f"publish could not build: {', '.join(errs)}", f"publish failed to build {len(errs)} file(s)", hint, age, cx.late_s)
    return _row("ok", f"last publish {_dur(age)} ago{data}", age=age, limit=cx.late_s)


def _optional_age(cx: Cx, cid: str, t: float | None, late: float, na: str, ok_txt: str, stopped: str, hint: str) -> dict:
    """The shape shared by tick, live and metrics. No trace: 'not deployed' only when neither this module's memory NOR an installed
    systemd unit says the part exists; seen once and gone, or installed and silent past its limit, or stale = degraded."""
    if t is None or t > cx.now + SKEW_S:
        if t is not None:                                     # only future stamps: the clock, not the component, is the story
            return _row("degraded", f"{cid} stamp is {_dur(t - cx.now)} in the future", "clock stepped back: timestamps cannot be trusted", "timedatectl status")
        if cx.first_seen(cid):
            return _row("degraded", f"{stopped}: it has left no trace", f"{stopped}", hint)
        unit = cx.unit_t(cid)
        if unit is not None:                                  # the installer put its unit in place: its output is owed, whatever we remember
            noun, waited = stopped.removesuffix(" stopped"), cx.now - unit
            if waited > late + UNIT_GRACE_S:
                return _row("degraded", f"{noun} has produced nothing, though its unit was installed {_dur(waited)} ago",
                            f"{noun} is installed but has never run", hint)
            return _row("info", f"{noun} was installed {_dur(waited)} ago: waiting for its first run")
        return _row("info", na, nd=True)
    cx.mark(cid)
    age = cx.now - t
    cx.m[f"{cid}_age_s"] = int(age)
    if age > late:
        return _row("degraded", f"{stopped} {_dur(age)} ago (limit {_dur(late)})", f"{stopped} {_dur(age)} ago", hint, age, late)
    return _row("ok", f"{ok_txt} {_dur(age)} ago", age=age, limit=late)


def c_tick(cx: Cx) -> dict:
    hb = _jread(core.RUN_DIR / "tick.json", 1 << 16)[0]
    st = cx.status[0] if isinstance(cx.status[0], dict) else {}
    stamps = [v for v in (_stamp(hb, "t"), _stamp(st.get("tick"), "last_run") if isinstance(st.get("tick"), dict) else None,
                          _mtime(core.STATE_DIR / "sched.json")) if v is not None]
    valid = [v for v in stamps if v <= cx.now + SKEW_S]
    t = max(valid) if valid else (min(stamps) if stamps else None)     # only future stamps: _optional_age reports the clock, not "no tick"
    return _optional_age(cx, "tick", t, cx.o.num("tick_late_s", 60, 86400), "the scheduler tick is not installed (older install)",
                         "tick ran", "scheduler tick stopped", "systemctl status homelab-maint-tick.timer; homelab-maint tick")


def _tier_row(cx: Cx, tier: str, limit_s: float, hint: str) -> dict:
    st, why = cx.status
    word = f"{tier} maintenance"
    if not isinstance(st, dict):
        return _row("info", "no status yet")
    t = _tier_last(st, tier)
    if t is None:
        if cx.now - cx.born > limit_s:
            return _row("degraded", f"{word} has never run (limit {_dur(limit_s)})", f"{word} has never run", hint)
        return _row("info", f"{word} has not run yet")
    age = cx.now - t
    cx.m[f"{tier}_age_s"] = int(age)
    if age < -SKEW_S:
        return _row("degraded", f"{word} is stamped {_dur(-age)} in the future", "clock stepped back: run times cannot be trusted", "timedatectl status")
    if age > limit_s:
        return _row("degraded", f"{word} last ran {_dur(age)} ago (limit {_dur(limit_s)})", f"{word} last ran {_dur(age)} ago", hint, age, limit_s)
    return _row("ok", f"{word} last ran {_dur(age)} ago", age=age, limit=limit_s)


def c_daily(cx: Cx) -> dict:
    return _tier_row(cx, "daily", cx.o.num("daily_max_h", 1, 24 * 30) * 3600, "systemctl status homelab-maint-daily.timer; homelab-maint routine status")


def c_weekly(cx: Cx) -> dict:
    return _tier_row(cx, "weekly", cx.o.num("weekly_max_d", 1, 90) * 86400, "systemctl status homelab-maint-weekly.timer; homelab-maint routine status")


def _doc_time(path: Path) -> float | None:
    """generated_at inside a JSON file (the writer's own clock), else its mtime; None when the file is absent."""
    doc, why = _jread(path, 512 << 10)
    t = _stamp(doc)
    return t if t is not None else _mtime(path)


def c_live(cx: Cx) -> dict:
    return _optional_age(cx, "live", _doc_time(cx.pub / "live.json"), cx.o.num("live_late_s", 10, 86400),
                         "the live monitor is not deployed", "live.json written", "live monitor stopped",
                         "systemctl status homelab-maint-live")


def c_metrics(cx: Cx) -> dict:
    doc, why = _jread(core.STATE_DIR / "metrics-ring.json", 1 << 20)
    t = _stamp(doc.get("last"), "t") if isinstance(doc, dict) and isinstance(doc.get("last"), dict) else None
    t = t if t is not None else (_mtime(core.STATE_DIR / "metrics-ring.json") if why != "missing" else None)
    return _optional_age(cx, "metrics", t, cx.o.num("metrics_late_s", 60, 86400), "the sensor sampler is not deployed",
                         "last sensor sample", "sensor sampler stopped", "systemctl status homelab-maint-metrics.timer")


# --------------------------------------------------------------------------- row: registry
_HEX = re.compile(r"[0-9a-f]{8,128}")


def _sha_hits(rec: str, data: bytes, alg: str) -> bool:
    try:
        return len(rec) >= 8 and hashlib.new(alg, data).hexdigest().startswith(rec.lower())
    except ValueError:
        return False


def _registry_files(rd: Path) -> dict[str, Path]:
    """The registry's own view of rules.d (registry.scan_registry): `*.toml`, no dotfiles, regular files only (no symlinks, FIFOs, dirs)."""
    out: dict[str, Path] = {}
    try:
        with os.scandir(rd) as it:
            for e in it:
                try:
                    if e.name.endswith(".toml") and not e.name.startswith(".") and e.is_file(follow_symlinks=False):
                        out[e.name] = Path(e.path)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _content_diff(pairs: dict[str, str], data: dict[str, bytes], state: dict) -> tuple[list[str], bool]:
    """(names whose bytes differ from the recorded sha, verifiable). registry.py records sha256 hex of the raw bytes: 64 hex digits are
    compared with sha256, no detection needed (so an edited SOLE file is caught even with no remembered scheme). Shorter or older
    recordings (prefixes, other algorithms) are matched by the remembered or first algorithm that fits a file, sha256 when none does.
    Anything that is not hex cannot be judged: the caller says so (unknown), never 'in sync'."""
    recs = {n: pairs[n].lower() for n in data if n in pairs}
    if any(not _HEX.fullmatch(r) for r in recs.values()):
        return [], False
    if all(len(r) == 64 for r in recs.values()):
        return sorted(n for n, r in recs.items() if hashlib.sha256(data[n]).hexdigest() != r), True
    algs = [state.get("sha_alg")] if state.get("sha_alg") in HASHES else []
    algs += [a for a in HASHES if a not in algs]
    alg = next((a for a in algs if any(_sha_hits(r, data[n], a) for n, r in recs.items())), "sha256")
    state["sha_alg"] = alg
    return sorted(n for n, r in recs.items() if not _sha_hits(r, data[n], alg)), True


def c_registry(cx: Cx) -> dict:
    hint = "homelab-maint rules check; homelab-maint rules sync"
    rd, cur_p = core.CONF_DIR / "rules.d", core.STATE_DIR / "rules" / "current.json"
    on_disk = _registry_files(rd)
    cur, why = _jread(cur_p)
    if not on_disk and why == "missing":
        if cx.first_seen("registry"):
            return _row("degraded", "the rules registry has disappeared (rules.d empty, nothing recorded)", "rules registry disappeared", hint)
        return _row("info", "no rules registry on this host (the legacy config files are in use)", nd=True)
    cx.mark("registry")
    grace = cx.o.num("registry_grace_s", 0, 86400)
    if why != "ok" or not isinstance(cur, dict) or not cur.get("hash"):
        waited = cx.now - cx.state["seen"]["registry"]
        if waited <= grace:
            return _row("info", "the registry has not been synced yet")
        return _row("degraded", "the registry was never synced (no rules/current.json)", "rules registry never synced", hint)
    synced = _stamp(cur, "synced_at") or 0.0
    cx.m["rules_count"] = int(cur["rules_count"]) if isinstance(cur.get("rules_count"), int) and not isinstance(cur.get("rules_count"), bool) else 0
    # 1. rules.d vs what was synced: file set, then content
    listed = cur.get("files")
    pairs = {Path(str(f.get("name"))).name: str(f.get("sha") or "") for f in listed if isinstance(f, dict)} if isinstance(listed, list) else \
        ({Path(str(k)).name: str(v) for k, v in listed.items()} if isinstance(listed, dict) else {})
    diff = sorted(set(on_disk) ^ set(pairs))
    verifiable = True
    if not diff and pairs:
        data: dict[str, bytes] = {}
        for n, p in on_disk.items():
            try:
                if p.stat().st_size <= (4 << 20):
                    data[n] = p.read_bytes()
            except OSError:
                diff.append(n)                                    # a file that cannot be read cannot be the one that was applied
        if not diff:
            diff, verifiable = _content_diff(pairs, data, cx.state)
    edited = max([_mtime(on_disk[n]) or 0.0 for n in diff if n in on_disk] + [0.0])
    # 2. a rejected change: the registry itself says so in current.json (`invalid` is a dict while a refused edit is in rules.d and
    #    None once an applied or the unchanged content is back; the history tail is NOT used: a revert appends no history record)
    inv = cur.get("invalid")
    if isinstance(inv, dict) and inv:
        n = len(inv["errors"]) if isinstance(inv.get("errors"), list) else 0
        if diff:
            blocked = inv.get("kind") == "blocked"
            return _row("degraded", f"the last registry change was {'blocked' if blocked else 'rejected'} ({n} error(s)): the last good config is running",
                        f"a registry change was {'blocked' if blocked else 'rejected'}; running on the last good config", hint)
        pend = cx.now - max(edited, _newest(on_disk), _stamp(inv, "ts") or 0.0)      # rules.d is back to the applied content: the tick clears the flag
        if pend <= grace:
            return _row("info", f"the rejected registry edit was reverted {_dur(pend)} ago: the tick clears the flag within a minute")
        return _row("degraded", f"registry is back to the applied content but still flagged as rejected for {_dur(pend)}: the tick is not syncing",
                    "registry sync is not clearing a rejected change (is the tick running?)", hint, pend, grace)
    if not verifiable:
        return _row("unknown", "rules.d cannot be compared with the sync record: its hashes are not recognisable", "registry content could not be verified (unreadable sync record)", hint)
    if diff:
        since = max(edited, synced)
        pend = cx.now - since
        names = ", ".join(_name(n) for n in diff[:3])
        if pend <= grace:
            return _row("info", f"registry edited {_dur(pend)} ago ({names}): the tick applies it within a minute")
        return _row("degraded", f"registry changes not applied for {_dur(pend)}: {names}", f"registry is not in sync with rules.d ({names})", hint, pend, grace)
    # 3. generated files edited by hand after the sync (they carry a GENERATED header; the registry is the only place to edit)
    if cx.o.flag("registry_check_generated") and synced:
        hand = []
        for n in GENERATED:
            p = core.CONF_DIR / n
            try:
                if _mtime(p) and _mtime(p) > synced + grace and b"GENERATED from rules.d" in p.read_bytes()[:400]:
                    hand.append(n)
            except OSError:
                continue
        if hand:
            return _row("degraded", f"generated config edited by hand after the last sync: {', '.join(hand[:3])}",
                        f"generated config was edited by hand ({hand[0]}): edit the registry instead", "homelab-maint rules diff", cx.now - synced, grace)
    # 4. the website shows a different registry than the host holds
    man = cx.manifest
    mh = man.get("registry_hash") if isinstance(man, dict) else None
    if isinstance(mh, str) and mh and mh != cur.get("hash") and cx.now - synced > grace:
        return _row("degraded", "the published registry hash differs from the host's", "website shows an older registry than the host", "homelab-maint publish")
    return _row("ok", f"registry in sync, {cx.m.get('rules_count', 0)} rules, synced {_dur(cx.now - synced)} ago")


def _newest(files: dict[str, Path]) -> float:
    return max([_mtime(p) or 0.0 for p in files.values()] + [0.0])


# --------------------------------------------------------------------------- row: errors
_TASK_RX = re.compile(rb'^\{"t":\s*([0-9.eE+-]+),\s*"kind":\s*"task",\s*"task":\s*"([^"\\]*)",\s*"status":\s*"([^"\\]*)"', re.M)


def _tail(path: Path, nbytes: int) -> bytes:
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read(nbytes)
    return data[data.find(b"\n") + 1:] if start else data      # the first line of a tail window is cut: drop it


def history_errors(now: float, window_s: float = 86400.0) -> dict:
    """Task records of the last `window_s` from the tail of history.jsonl: {total, errors, by:{task: errors}, covered_s}."""
    out: dict[str, Any] = {"total": 0, "errors": 0, "by": {}, "covered_s": 0}
    try:
        data = _tail(core.STATE_DIR / "history.jsonl", HIST_TAIL)
    except OSError:
        return out
    oldest = now
    cutoff = now - window_s
    for m in _TASK_RX.finditer(data):
        t = float(m.group(1))
        if t < cutoff or t > now + SKEW_S:
            continue
        oldest = min(oldest, t)
        out["total"] += 1
        if m.group(3) == b"error":
            out["errors"] += 1
            k = _name(m.group(2).decode("ascii", "replace"))
            out["by"][k] = out["by"].get(k, 0) + 1
    out["covered_s"] = int(now - oldest) if out["total"] else 0
    out["by"] = dict(sorted(out["by"].items(), key=lambda kv: -kv[1])[:20])
    return out


def c_errors(cx: Cx) -> dict:
    hint = "homelab-maint status | grep -w error; journalctl -u homelab-maint-check -n 50"
    cached = cx.state.get("hist") if isinstance(cx.state.get("hist"), dict) else None
    if (cx.quick and cached and all(_num(cached.get(k)) is not None for k in ("t", "total", "errors", "covered_s"))
            and isinstance(cached.get("by"), dict) and 0 <= cx.now - cached["t"] < CACHE_S):
        h = cached
    else:
        h = history_errors(cx.now)
        h["t"] = cx.now
        cx.state["hist"] = h
    total, errs = int(h.get("total", 0)), int(h.get("errors", 0))
    rate = 100.0 * errs / total if total else 0.0
    cx.m.update(errors_24h=errs, runs_24h=total, error_rate_pct=round(rate, 1))
    st = cx.status[0] if isinstance(cx.status[0], dict) else {}
    tasks = st.get("tasks") if isinstance(st.get("tasks"), dict) else {}
    now_err = sorted(_name(n) for n, e in tasks.items() if isinstance(e, dict) and e.get("status") == "error")
    mods = [n for n in now_err if n.startswith("module_")]
    cx.m["tasks_in_error"] = len(now_err)
    enough = total >= cx.o.num("error_min_runs", 1, 1e6)
    if enough and rate >= cx.o.num("error_rate_down_pct", 1, 100):
        return _row("down", f"{errs} of {total} task runs failed in 24 h ({rate:.0f}%): monitoring is mostly blind", f"{rate:.0f}% of task runs failed in 24 h", hint)
    if mods:
        return _row("degraded", f"module(s) failed to import: {', '.join(mods[:3])}", f"{len(mods)} module(s) failed to import: their checks do not run", hint)
    if enough and rate >= cx.o.num("error_rate_warn_pct", 0.1, 100):
        return _row("degraded", f"{errs} of {total} task runs failed in 24 h ({rate:.0f}%)", f"{rate:.0f}% of task runs failed in 24 h", hint)
    if len(now_err) >= cx.o.num("error_tasks_warn", 1, 1000):
        return _row("degraded", f"{len(now_err)} tasks are in error now: {', '.join(now_err[:3])}", f"{len(now_err)} tasks are failing to run", hint)
    cov = f" (history covers {_dur(h.get('covered_s', 0))})" if total and h.get("covered_s", 0) < 82800 else ""
    return _row("ok", f"{errs} of {total} task runs failed in 24 h" + (f"; in error now: {', '.join(now_err[:3])}" if now_err else "") + cov)


# --------------------------------------------------------------------------- row: state dir
def tree_size(root: Path, budget_s: float = WALK_BUDGET_S, max_entries: int = WALK_MAX) -> tuple[int, bool]:
    """(bytes, complete): lstat sizes, symlinks not followed, a time and entry budget (an incomplete walk is never used for growth)."""
    total, n, end = 0, 0, time.monotonic() + budget_s
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    n += 1
                    if n > max_entries or (n & 127 == 0 and time.monotonic() > end):
                        return total, False
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def _growth(samples: list, now: float, size: int) -> float | None:
    """Bytes per day between the oldest sample of the last 48 h and `size` (>= 6 h apart), else None."""
    pts = [s for s in samples if isinstance(s, list) and len(s) == 2 and _num(s[0]) is not None and _num(s[1]) is not None
           and 0 < now - s[0] <= 172800]
    if not pts:
        return None
    t0, b0 = min(pts, key=lambda s: s[0])
    return (size - b0) / (now - t0) * 86400 if now - t0 >= 21600 else None


def c_state(cx: Cx) -> dict:
    o = cx.o
    hint = "du -xh --max-depth=1 /var/lib/homelab-maint | sort -h | tail -5; free disk space"
    down_b, warn_b, warn_pct = o.num("state_free_down_mib", 1, 1e7) * (1 << 20), o.num("state_free_warn_gib", 0.01, 1e5) * GIB, o.num("state_free_warn_pct", 0.1, 100)
    level, bits, devs = 0, [], set()
    for label, p in (("state", core.STATE_DIR), ("log", core.LOG_DIR)):
        try:
            dev, vfs = os.stat(p).st_dev, _statvfs(p)
        except OSError:
            if label == "state":
                return _row("degraded", "the state directory cannot be read", "state directory is missing or unreadable", hint)
            continue
        if dev in devs:
            continue
        devs.add(dev)
        free, total = vfs.f_bavail * vfs.f_frsize, vfs.f_blocks * vfs.f_frsize
        pct = 100.0 * free / total if total else 0.0
        lvl = 2 if free < down_b else 1 if (pct < warn_pct or free < warn_b) else 0
        level = max(level, lvl)
        bits.append(f"{label} fs {human(free)} free ({pct:.0f}%)" + (" LOW" if lvl else ""))
        if label == "state":
            cx.m.update(state_free_h=human(free), state_free_pct=round(pct, 1))
    # size and growth of the state dir (cached by export(): a walk per publish would be waste)
    cached = cx.state.get("size") if isinstance(cx.state.get("size"), list) else []
    last = cached[-1] if cached and isinstance(cached[-1], list) and len(cached[-1]) == 2 else None
    if cx.quick and last and 0 <= cx.now - last[0] < CACHE_S:
        size, complete = int(last[1]), False
    else:
        size, complete = tree_size(core.STATE_DIR)
    rate = _growth(cached, cx.now, size) if complete or cx.quick else None
    if complete and (not last or cx.now - last[0] >= 3300 or cx.now < last[0]):
        cached = [s for s in cached if isinstance(s, list) and 0 < cx.now - s[0] <= 3 * 86400][-80:] + [[int(cx.now), size]]
        cx.state["size"] = cached
    cx.m["state_size_h"] = human(size)
    bits.append(f"state dir {human(size)}" + ("" if complete or cx.quick else "+"))
    if rate is not None:
        cx.m["state_growth_mib_day"] = round(rate / (1 << 20), 1)
        bits.append(f"{rate / (1 << 20):+.0f} MiB/day")
    big, fast = size > o.num("state_max_gib", 0.01, 1e5) * GIB, rate is not None and rate > o.num("state_growth_warn_mib_day", 1, 1e7) * (1 << 20)
    detail = ", ".join(bits)
    if level == 2:
        return _row("down", f"{detail}: writes are about to fail", f"state filesystem nearly full ({human(down_b)} floor)", hint)
    if level == 1:
        return _row("degraded", detail, "state filesystem is low on space", hint)
    if big or fast:
        return _row("degraded", detail, "state directory is " + ("growing fast" if fast else "very large"), hint)
    return _row("ok", detail)


# --------------------------------------------------------------------------- row: ack inbox
def c_acks(cx: Cx) -> dict:
    hint = "homelab-maint ack process; systemctl status homelab-maint-tick.timer"
    inbox = core.STATE_DIR / "ack" / "inbox"
    if not inbox.is_dir():
        if cx.first_seen("acks"):
            return _row("degraded", "the acknowledge inbox is gone", "acknowledge inbox disappeared", hint)
        return _row("info", "acknowledgements are not enabled (no ack/inbox)")
    cx.mark("acks")
    n, oldest, rejected = 0, None, 0
    try:
        with os.scandir(inbox) as it:
            for i, e in enumerate(it):
                if i >= 500:
                    break
                try:
                    if e.is_file(follow_symlinks=False) and e.name.endswith(".json"):
                        n += 1
                        m = e.stat(follow_symlinks=False).st_mtime
                        oldest = m if oldest is None else min(oldest, m)
                    elif e.is_dir(follow_symlinks=False) and e.name == "rejected":
                        rejected = sum(1 for _ in itertools.islice(os.scandir(e.path), 1000))
                except OSError:
                    continue
    except OSError:
        return _row("degraded", "the acknowledge inbox cannot be read", "acknowledge inbox is unreadable", hint)
    age = cx.now - oldest if oldest is not None else 0.0
    cx.m.update(inbox=n, inbox_oldest_s=int(age), inbox_rejected=rejected)
    late, cap = cx.o.num("inbox_late_s", 60, 86400), cx.o.num("inbox_max", 1, 100000)
    if n and (age > late or n >= cap):
        return _row("degraded", f"{n} acknowledgement request(s) waiting, oldest {_dur(age)}", f"{n} acknowledgement(s) waiting for {_dur(age)}: the runner is not processing them", hint, age, late)
    return _row("ok", f"{n} waiting" + (f", {rejected} rejected" if rejected else ""))


# --------------------------------------------------------------------------- row: alert delivery
BREAKER_MAX_S = 3600                            # notify.py ignores a breaker 'until' further ahead than this (clock stepped back)
RETRY_DEFAULT = {"outbox_ttl_s": 43200.0, "outbox_max": 10}


def _notify_state() -> tuple[dict | None, str]:
    """The newest valid notify-state.json (STATE_DIR first, the RUN_DIR tmpfs fallback notify uses when the disk is full or read-only)."""
    best, best_m, bad = None, -1.0, False
    for d in (core.STATE_DIR, core.RUN_DIR):
        doc, why = _jread(d / "notify-state.json", 2 << 20)
        if why == "bad" or (why == "ok" and not isinstance(doc, dict)):
            bad = True
        elif why == "ok" and (_mtime(d / "notify-state.json") or 0.0) > best_m:
            best, best_m = doc, _mtime(d / "notify-state.json") or 0.0
    return best, ("ok" if best is not None else "bad" if bad else "missing")


def _retry_cfg() -> dict[str, float]:
    """[retry] outbox_ttl_s / outbox_max from notify.toml, read as TOML here (no notify import); a bad file or value = notify's defaults."""
    out = dict(RETRY_DEFAULT)
    try:
        r = tomllib.loads((core.CONF_DIR / "notify.toml").read_text()[:262144]).get("retry", {})
    except (OSError, ValueError, UnicodeDecodeError):
        return out
    for k in out:
        v = _num(r.get(k)) if isinstance(r, dict) else None
        if v is not None and v > 0:
            out[k] = v
    return out


def c_alerts(cx: Cx) -> dict:
    """Can an alert actually reach the owner? Alerts flow through notify.py; when its transport is dead the critical pages pile up in
    the outbox and the circuit breaker keeps re-opening, while every other row here can still be green."""
    hint = "homelab-maint notify doctor; homelab-maint notify flush; journalctl -u homelab-maint-tick -n 30"
    st, why = _notify_state()
    if why == "bad":
        return _row("degraded", "notify-state.json is unreadable: the alert delivery state cannot be verified", "alert delivery state is unreadable", hint)
    if st is None:
        return _row("ok", "no delivery problem recorded (no notify-state.json yet)")
    now, o = cx.now, cx.o
    retry = _retry_cfg()
    ttl, cap = retry["outbox_ttl_s"], int(retry["outbox_max"])
    warn_s = o.num("outbox_warn_s", 60, 86400)
    box = [e for e in st.get("outbox") or [] if isinstance(e, dict)] if isinstance(st.get("outbox"), list) else []
    ages = []
    for e in box:                                           # notify's own reading: a first attempt stamped in the future is brand new, a missing one is old
        t = _num(e.get("ts"))
        ages.append(max(now - t, 0.0) if t is not None else warn_s + 1)
    oldest = max(ages, default=0.0)
    bk = st.get("breaker") if isinstance(st.get("breaker"), dict) else {}
    until, fails = _num(bk.get("until")) or 0.0, int(_num(bk.get("n")) or 0)
    open_ = now < until <= now + BREAKER_MAX_S
    cx.m.update(outbox_n=len(box), outbox_oldest_s=int(oldest), breaker_open=open_)
    n_txt = f"{len(box)} critical page(s) could not be delivered, the oldest for {_dur(oldest)}"
    if box and (oldest >= ttl / 2 or len(box) >= cap):
        why_ = f"the outbox is full ({len(box)}): pages are being dropped" if len(box) >= cap else f"a critical page has waited {_dur(oldest)} for delivery"
        return _row("down", f"{n_txt}: nobody can be paged", f"alerts are not reaching you: {why_}", hint, oldest, ttl / 2)
    last = (_ascii(bk.get("why"), 60) if bk.get("why") else "no reason logged")
    if box and oldest >= warn_s:
        return _row("degraded", f"{n_txt}" + (f"; transport circuit open ({fails} failures, last: {last})" if open_ else ""),
                    f"{len(box)} critical page(s) undelivered for {_dur(oldest)}: the alert transport is failing", hint, oldest, warn_s)
    if open_:
        return _row("degraded", f"the alert transport circuit is open for {_dur(until - now)} more ({fails} failure(s), last: {last})",
                    "the alert transport is failing: alerts are being held", hint)
    if fails >= o.num("breaker_fail_n", 1, 1000) and 0 <= now - until < 7200:
        return _row("degraded", f"{fails} alert transport failures in a row, none delivered since (last: {last})", "the alert transport keeps failing", hint)
    return _row("ok", f"{len(box)} critical page(s) waiting for a retry, the oldest {_dur(oldest)}" if box else "no undelivered critical page, transport circuit closed")


# --------------------------------------------------------------------------- row: website
def service_state(name: str, timeout: float = 2.5) -> tuple[str, bool]:
    """(ActiveState, present) of a systemd unit, from one read-only `systemctl is-active`. `is-active` prints "inactive" for a unit
    that does not exist as well, so presence is decided by its exit code (4 = no such unit), never by the word alone. A systemctl
    that cannot run (no systemd, a timeout) is "unknown" and not present: the HTTP probe alone then decides."""
    r = sh(["systemctl", "is-active", name], timeout=timeout)
    if r.returncode == 4:                                      # no such unit: is-active prints "inactive" for it too, so only the code tells
        return "unknown", False
    word = _name(r.stdout, 20) or "unknown"
    return word, word != "unknown"


def http_get(host: str, port: int, path: str, timeout: float) -> tuple[int | None, bytes, str, int]:
    """(status | None, body[:4096], error word, ms). GET only, no redirects, nothing sent but a User-Agent."""
    t0 = time.perf_counter()
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    ms = lambda: int((time.perf_counter() - t0) * 1000)       # noqa: E731
    try:
        c.request("GET", path, headers={"User-Agent": "homelab-maint-self-health", "Connection": "close"})
        r = c.getresponse()
        return r.status, r.read(4096), "", ms()
    except _http_timeout:
        return None, b"", "timeout", ms()
    except ConnectionRefusedError:
        return None, b"", "refused", ms()
    except (OSError, http.client.HTTPException):
        return None, b"", "error", ms()
    finally:
        c.close()


def _probe_site(host: str, port: int, path: str, timeout: float, service: str) -> tuple[tuple[str, bool], tuple]:
    """The unit's state and the loopback GET in parallel threads, so the cost is the slower of the two, never the sum."""
    out: dict[str, Any] = {}
    jobs: dict[str, Any] = {"h": lambda: http_get(host, port, path, timeout)}
    if service:
        jobs["s"] = lambda: service_state(service, min(timeout, 2.5))

    def run(k: str) -> None:
        try:
            out[k] = jobs[k]()
        except Exception:                                         # noqa: BLE001 - a probe that crashes is "no answer"
            pass
    ts = [threading.Thread(target=run, args=(k,), daemon=True) for k in jobs]
    for t in ts:
        t.start()
    end = time.monotonic() + timeout + 0.7
    for t in ts:
        t.join(max(0.0, end - time.monotonic()))
    return out.get("s", (None, False)), out.get("h", (None, b"", "timeout", int(timeout * 1000)))


def c_website(cx: Cx) -> dict:
    """The OhmzMaintainer dashboard (the beszel-hub site the old maintenance-web container was retired in favour of): its
    /api/health over loopback and its systemd unit, judged together. A 200 while the unit is inactive is a stray server; a live unit
    with no answer is a wedged site; either one alone is the fault."""
    o = cx.o
    hint = "systemctl status beszel-hub; curl -s http://127.0.0.1:8088/api/health"
    if not o.flag("web_check"):
        return _row("info", "the website check is switched off in the config")
    host = o.text("web_host", r"[A-Za-z0-9.]{1,40}")
    host = host if host in LOOPBACK else str(DEFAULTS["web_host"])           # GET only, and only ever to this machine
    port, to = int(o.num("web_port", 1, 65535)), o.num("web_timeout_s", 0.1, 3.0)
    path = o.text("web_path", r"/[A-Za-z0-9._/-]{0,80}")
    service = o.text("web_service", r"[A-Za-z0-9@_.:-]{0,64}")
    (svc, present), (code, body, err, ms) = _probe_site(host, port, path, to, service)
    was = cx.first_seen("website")
    listening = code is not None or err in ("timeout", "error")       # a refused connection means nothing is there; a hang means something is
    deployed = was or listening or present
    if listening or present:
        cx.mark("website")
    cx.m["web"] = "answers" if code == 200 else "down" if deployed else "not deployed"
    if service and svc:
        cx.m["web_service"] = svc
    name = service or f"{host}:{port}"
    if code == 200:
        if present and svc in ("inactive", "failed", "deactivating"):
            return _row("degraded", f"{path} answers 200 but {name} is {svc}", f"website answers but {name} is {svc}", hint)
        return _row("ok", f"GET {path} 200 in {ms} ms" + (f", {name} {svc}" if service and svc else ""))
    if code is not None:
        why = ""
        try:
            d = json.loads(body)
            why = _ascii(d.get("reason") or d.get("error") or d.get("message") or d.get("status") or "", 80) if isinstance(d, dict) else ""
        except ValueError:
            pass
        return _row("degraded", f"{path} answered HTTP {code}" + (f": {why}" if why else ""),
                    f"website reports itself unhealthy (HTTP {code}{': ' + why if why else ''})", hint)
    if present:
        if svc == "activating":
            return _row("info", f"the website service {name} is starting")
        if svc == "active":
            return _row("degraded", f"{name} is active but GET {path} does not answer ({err})",
                        f"website service runs but {path} does not answer ({err})", hint)
        return _row("degraded", f"{name} is {svc}", f"website service is {svc}", hint)
    if not deployed:
        return _row("info", f"the website is not deployed yet (nothing on {host}:{port}, no {name})")
    if service and svc == "unknown":
        return _row("degraded", f"{name} is gone and nothing answers on {host}:{port}", "website has disappeared", hint)
    return _row("degraded", f"{path} does not answer ({err}) and {name} could not be asked", "website is unreachable", hint)


# --------------------------------------------------------------------------- row: Kuma heartbeat
def c_kuma(cx: Cx) -> dict:
    p = core.CONF_DIR / "kuma.toml"
    keys = cx.o.keys("kuma_keys")
    req = cx.o.flag("kuma_required")
    hint = "create /etc/homelab-maint/kuma.toml (0600) with [push] " + " / ".join(f'"{k}" = <token>' for k in keys[:2])
    try:
        st = p.stat()
        if st.st_size > 65536:
            return _row("degraded", "kuma.toml is unreasonably large", "kuma.toml is unreasonably large", hint)
        raw = p.read_text()
    except FileNotFoundError:
        return _row("degraded" if req else "info", "no Kuma heartbeat configured (kuma.toml is optional)", "no Kuma heartbeat is configured", hint)
    except PermissionError:
        return _row("info", "kuma.toml is not readable by this user: not checked")
    except (OSError, UnicodeDecodeError):
        return _row("degraded", "kuma.toml cannot be read", "kuma.toml cannot be read", hint)
    try:
        push = tomllib.loads(raw).get("push", {})
    except tomllib.TOMLDecodeError:
        return _row("degraded", "kuma.toml is not valid TOML: every Kuma push fails", "kuma.toml is not valid TOML", "fix /etc/homelab-maint/kuma.toml")
    push = push if isinstance(push, dict) else {}
    good = [k for k in keys if re.fullmatch(r"[A-Za-z0-9]{8,64}", str(push.get(k, "")))]       # the shape core.kuma_push accepts
    bad = [k for k in keys if k in push and k not in good]
    missing = [k for k in keys if k not in push]
    cx.m["kuma_keys"] = len(good)
    open_ = " (file is readable by others: chmod 600)" if st.st_mode & 0o077 else ""
    if bad:
        return _row("degraded", f"malformed token for: {', '.join(bad)} (the push is skipped)", "a Kuma push token is malformed", hint)
    failing = _kuma_failing(cx, good)
    if failing:
        k, n, why_ = failing[0]
        return _row("degraded", f"Kuma push '{k}' failed {n} times in a row (last: {why_}): the external dead-man is not hearing us",
                    f"Kuma heartbeat '{k}' is failing ({n} in a row)", "curl -fsS http://127.0.0.1:3011/ ; docker ps --filter name=kuma")
    if missing:
        return _row("degraded" if req else "info", f"no push token for: {', '.join(missing)}" + open_, f"Kuma heartbeat key(s) missing: {', '.join(missing)}", hint)
    return _row("ok", f"{len(good)}/{len(keys)} heartbeat keys configured" + open_)


# core.kuma_push records the outcome of every push here (note_kuma); only key NAMES and results are stored, never a token or a URL.
KUMA_STATE = "kuma-state.json"


def _kuma_failing(cx: Cx, keys: list[str]) -> list[tuple[str, int, str]]:
    doc, _why = _jread(core.STATE_DIR / KUMA_STATE, 1 << 16)
    rows = doc.get("keys") if isinstance(doc, dict) and isinstance(doc.get("keys"), dict) else {}
    need = int(cx.o.num("kuma_fail_n", 1, 1000))
    out = []
    for k in keys:
        r = rows.get(k) if isinstance(rows.get(k), dict) else {}
        n = int(_num(r.get("n")) or 0)
        if n >= need and (_num(r.get("fail")) or 0) >= (_num(r.get("ok")) or 0):
            out.append((k, n, _ascii(r.get("why") or "no reason logged", 40)))
    return out


def note_kuma(key: str, ok: bool, why: str = "", now: float | None = None) -> None:
    """Record the result of one Kuma push (glue: core.kuma_push calls this with curl's return code). Never raises, stores no secret:
    {"v":1,"keys":{key:{"ok":t,"fail":t,"n":consecutive failures,"why":short text}}}. A success resets the count."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(key)):
            return
        now = time.time() if now is None else float(now)
        p = core.STATE_DIR / KUMA_STATE
        doc, _w = _jread(p, 1 << 16)
        keys = doc.get("keys") if isinstance(doc, dict) and isinstance(doc.get("keys"), dict) else {}
        r = keys.get(key) if isinstance(keys.get(key), dict) else {}
        if ok:
            r = {"ok": now, "fail": r.get("fail", 0), "n": 0, "why": ""}
        else:
            why = re.sub(r"[A-Za-z0-9]{16,}", "<x>", _ascii(why or "push failed", 60))              # a token-like run never lands in a file
            r = {"ok": r.get("ok", 0), "fail": now, "n": min(int(_num(r.get("n")) or 0) + 1, 10 ** 6), "why": why}
        keys[key] = r
        _atomic_json(p, {"v": 1, "keys": dict(sorted(keys.items())[:16])}, 0o600)
    except Exception:                                      # noqa: BLE001 - recording a heartbeat result must never break the push
        pass


# --------------------------------------------------------------------------- the verdict
COMPONENTS: tuple[tuple[str, Callable[[Cx], dict]], ...] = (
    ("runner", c_runner), ("publish", c_publish), ("tick", c_tick), ("daily", c_daily), ("weekly", c_weekly), ("live", c_live),
    ("metrics", c_metrics), ("registry", c_registry), ("errors", c_errors), ("state", c_state), ("acks", c_acks),
    ("alerts", c_alerts), ("website", c_website), ("kuma", c_kuma))


def _ttl(o: Opts) -> dict:
    """FILE freshness for self.json (3 and 10 refresh periods = 180 / 600 s), never longer than the caps a consumer enforces anyway.
    Not the runner's limits: those say how late a CHECK RUN may be; this says how old the verdict itself may be."""
    r = o.num("refresh_s", 10, 3600)
    d = min(o.num("stale_factor", 1.5, 100) * r, STALE_CAP[0])
    dn = min(max(o.num("stale_down_factor", 2.0, 1000) * r, 1.5 * d), STALE_CAP[1])
    return {"refresh_s": int(r), "degraded_after_s": int(d), "down_after_s": int(dn)}


def assess(now: float, opt: Callable[[str, Any], Any], state: dict, quick: bool = False, mode: str = "export") -> dict:
    """Judge the pipeline at `now` (mode: export | cli | runner, see the module docstring). Mutates `state` (first_seen, seen, since,
    caches): the caller persists it. Never raises."""
    cx = Cx(now, opt, state, quick, mode)
    rows = []
    for cid, fn in COMPONENTS:
        try:
            r = fn(cx)
        except Exception as exc:                                  # noqa: BLE001 - one broken row must not hide the others
            r = _row("unknown", f"could not check: {type(exc).__name__}: {exc}"[:150], f"the self-check of {cid} failed to run", "homelab-maint self-health")
        r["id"], r["title"] = cid, TITLES[cid]
        rows.append(r)
    bad = sorted((r for r in rows if r["state"] in ("down", "degraded", "unknown")), key=lambda r: -RANK[r["state"]])   # stable: fixed order within a rank
    level = "down" if any(r["state"] == "down" for r in bad) else "degraded" if bad else "ok"
    prev, since = state.get("level"), _num(state.get("since"))
    if prev != level or since is None or since > now:
        since = now
    state["level"], state["since"] = level, since
    reasons = [r["reason"] for r in bad]
    cx.m.update(level=level, degraded=sum(1 for r in bad if r["state"] != "down"), down=sum(1 for r in bad if r["state"] == "down"),
                not_deployed=sum(1 for r in rows if r.get("nd")))
    return {"level": level, "since": since, "rows": rows, "bad": bad, "reasons": reasons, "metrics": cx.m, "mode": cx.mode,
            "ttl": _ttl(cx.o), "limits": {"runner_late_s": int(cx.late_s), "runner_down_s": int(cx.down_s)}}


def headline(rep: dict) -> str:
    if rep["level"] == "ok":
        return "Monitoring pipeline: healthy"
    return _ascii(f"Monitoring pipeline: {'DOWN' if rep['level'] == 'down' else 'degraded'}: {rep['reasons'][0]}", 120)


def summary(rep: dict) -> str:
    """<= 140 ASCII chars: the level, the first reasons, then a playbook-style hint if there is room (it can be an SMS)."""
    if rep["level"] == "ok":
        nd = rep["metrics"].get("not_deployed", 0)
        m = rep["metrics"]
        age = (f"a {_dur(m['gap_s'])} gap before this run has ended" if "gap_s" in m else
               f"runner {_dur(m['check_age_s'])}" if "check_age_s" in m else "first run, no history yet")
        return _ascii(f"ok: pipeline healthy ({age})" + (f"; {nd} optional part(s) not deployed" if nd else ""), 140)
    head = f"{WORD[rep['level']]}: {rep['reasons'][0]}"
    if len(rep["reasons"]) > 1 and len(head) + len(rep["reasons"][1]) + 2 <= 96:
        head += f"; {rep['reasons'][1]}"
        rest = len(rep["reasons"]) - 2
    else:
        rest = len(rep["reasons"]) - 1
    if rest > 0:
        head += f" (+{rest} more)"
    hint = rep["bad"][0].get("hint", "")
    tail = f". Try: {hint}" if hint else ""
    room = 140 - len(head)
    if room >= 24 and tail:
        head += tail[:room]
    return _ascii(head, 140)


def public(rep: dict, now: float) -> dict:
    """The self.json document (see the module docstring)."""
    checks = []
    for r in rep["rows"]:
        c = {k: r[k] for k in ("id", "title", "state", "detail") if k in r}
        for k in ("hint", "age_s", "limit_s"):
            if k in r:
                c[k] = r[k]
        checks.append(c)
    return {"schema": SCHEMA, "generated_at": now, "valid_until": now + rep["ttl"]["degraded_after_s"], "level": rep["level"],
            "headline": headline(rep), "verdict": {"level": rep["level"], "reasons": rep["reasons"], "since": rep["since"]},
            "ttl": rep["ttl"], "limits": rep["limits"], "checks": checks,
            "metrics": {k: v for k, v in rep["metrics"].items() if isinstance(v, (int, float, str, bool))}}


# --------------------------------------------------------------------------- the page rule (what a CONSUMER shows)
def effective(doc: Any, now: float) -> dict:
    """What a consumer (the web strip, a Homarr widget, curl | jq, doctor) must show for a self.json it read at `now`:
    {"level", "headline", "reasons", "stale", "age_s"}. The file is rewritten every minute by the refresher and at the end of every
    tier run; if the refresher AND the runner die nothing rewrites it, so an old `ok` must not be believed:
      age <= ttl.degraded_after_s   the file's own verdict
      age >  ttl.degraded_after_s   at least degraded: "self-health data is N old: the refresher stopped"
      age >  ttl.down_after_s       down: the state of the pipeline is unknown
    A missing, unreadable or future-dated file is degraded. A ttl in the file is never trusted beyond STALE_CAP (a tampered or
    mistyped file cannot switch the rule off) and falls back to the defaults when absent or malformed. The web strip must
    implement exactly this rule; tests/test_self_health.py (test_page_rule_*) are its fixtures."""
    gen = _stamp(doc)
    if not isinstance(doc, dict) or gen is None:
        why = "self.json is missing or unreadable: the pipeline state is unknown (is homelab-maint-selfhealth.timer running?)"
        return {"level": "degraded", "headline": _ascii("Monitoring pipeline: degraded: " + why, 160), "reasons": [why], "stale": True, "age_s": None}
    ttl = doc.get("ttl") if isinstance(doc.get("ttl"), dict) else {}
    dflt = _ttl(Opts(lambda k, d=None: d))
    d0, dn0 = dflt["degraded_after_s"], dflt["down_after_s"]
    d = _num(ttl.get("degraded_after_s"))
    dn = _num(ttl.get("down_after_s"))
    d = min(d, STALE_CAP[0]) if d is not None and d > 0 else d0
    dn = min(dn, STALE_CAP[1]) if dn is not None and dn >= d else max(dn0, d)
    own = doc.get("level") if doc.get("level") in WORD else "degraded"
    ver = doc.get("verdict") if isinstance(doc.get("verdict"), dict) else {}
    reasons = [_ascii(r, 120) for r in (ver.get("reasons") if isinstance(ver.get("reasons"), list) else []) if isinstance(r, str)][:8]
    age = now - gen
    lead = ""
    level = own
    if age < -SKEW_S:
        level, lead = ("degraded" if own == "ok" else own), f"self.json is stamped {_dur(-age)} in the future: clock trouble"
    elif age > dn:
        level, lead = "down", f"self-health data is {_dur(age)} old: the refresher and the runner have stopped, the pipeline state is unknown"
    elif age > d:
        level, lead = ("degraded" if own == "ok" else own), f"self-health data is {_dur(age)} old: the refresher stopped"
    if lead:
        reasons.insert(0, lead)
    if not lead and own == "degraded" and not reasons:
        reasons = ["the pipeline reports a problem"]
    if level == "ok":
        head = "Monitoring pipeline: healthy"
    else:
        head = _ascii(f"Monitoring pipeline: {'DOWN' if level == 'down' else 'degraded'}: {reasons[0] if reasons else 'unknown'}", 160)
    return {"level": level, "headline": head, "reasons": reasons, "stale": bool(lead), "age_s": int(age)}


def read_published(now: float | None = None) -> dict:
    """effective() of the self.json now on disk."""
    now = time.time() if now is None else float(now)
    return effective(_jread(core.STATE_DIR / "public" / "self.json", 1 << 20)[0], now)


# --------------------------------------------------------------------------- the task
@task("self_health", klass="C0", tier="check", title="Monitoring pipeline", timeout=30)
def run(ctx: Ctx) -> Result:
    try:
        rep = assess(ctx.now, ctx.opt, ctx.state, mode="runner")      # this run proves the runner is alive: see the module docstring
        res = Result({"ok": "ok", "degraded": "warn", "down": "crit"}[rep["level"]], summary(rep), alert=True)
        res.metrics = {k: v for k, v in rep["metrics"].items() if isinstance(v, (int, float, str, bool))}
        rows = sorted(rep["rows"], key=lambda r: -RANK[r["state"]])
        res.items = [{"component": r["title"], "state": r["state"], "detail": _ascii(r["detail"], 100)} | ({"hint": r["hint"]} if "hint" in r else {})
                     for r in rows[:12]]
        res.issue_key = "|".join(sorted(r["id"] for r in rep["bad"]))   # type: ignore[attr-defined]  # SPEC5: stable ack fingerprint = the SET of failing parts
        return res
    except Exception as exc:                                      # noqa: BLE001 - a self-check that cannot run is itself a finding
        return Result("warn", _ascii(f"self-check could not run: {type(exc).__name__}: {exc}", 100) + ". Try: homelab-maint self-health", alert=True)


# --------------------------------------------------------------------------- export and CLI
def _state_path() -> Path:
    return core.STATE_DIR / "tasks" / "self_health.json"


def _load_state() -> dict:
    d = read_json(_state_path(), {})
    return d if isinstance(d, dict) else {}


def _opt_getter() -> Callable[[str, Any], Any]:
    try:
        t = core.load_config().get("tasks", {}).get("self_health", {})
    except Exception:                                             # noqa: BLE001 - a broken maint.toml must not blind the monitor of the monitors
        t = {}
    t = t if isinstance(t, dict) else {}
    return lambda k, d=None: t.get(k, d)


def _fallback_doc(now: float, why: str) -> dict:
    """The document when the self-check itself crashed: degraded, with the default file ttl, never an old ok."""
    ttl = _ttl(Opts(lambda k, dflt=None: dflt))
    late = DEFAULTS["late_factor"] * DEFAULTS["check_interval_s"] + DEFAULTS["grace_s"]
    return {"schema": SCHEMA, "generated_at": now, "valid_until": now + ttl["degraded_after_s"], "level": "degraded",
            "headline": "Monitoring pipeline: degraded: " + why, "verdict": {"level": "degraded", "reasons": [why], "since": now}, "ttl": ttl,
            "limits": {"runner_late_s": int(late), "runner_down_s": int(DEFAULTS["down_factor"] * DEFAULTS["check_interval_s"])},
            "checks": [], "metrics": {}}


def export(now: float | None = None, persist: bool = True, quick: bool = True, mode: str = "export") -> dict:
    """self.json for the website. Recomputes the verdict from files (cheap: with quick=True the expensive parts, the history error
    rate and the state dir size, are reused for 30 min). persist=True stores since/seen/caches in the task's state file (the next
    check run reads them); False leaves the disk alone. mode: see assess()."""
    now = time.time() if now is None else float(now)
    state = _load_state()
    try:
        doc = public(assess(now, _opt_getter(), state, quick=quick, mode=mode), now)
    except Exception as exc:                                     # noqa: BLE001 - a broken self-check must not leave an old "ok" in place
        return _fallback_doc(now, _ascii(f"the self-check could not run: {type(exc).__name__}", 100))
    if persist:
        try:
            _atomic_json(_state_path(), state, 0o600)
        except OSError:
            pass
    return doc


def write_export(now: float | None = None, doc: dict | None = None) -> bool:
    """export() + write STATE_DIR/public/self.json atomically (0644), only when the public dir already exists (it is never created here)."""
    doc = doc if doc is not None else export(now)
    if not (core.STATE_DIR / "public").is_dir():
        return False
    try:
        _atomic_json(core.STATE_DIR / "public" / "self.json", doc, 0o644)
        return True
    except OSError:
        return False


def refresh(now: float | None = None) -> bool:
    """One tick of the 1-minute refresher unit: recompute (30-minute caches for the expensive rows), persist the state, rewrite
    public/self.json. True = written. This is what keeps the file's `generated_at` honest when the check tier is dead or hung."""
    now = time.time() if now is None else float(now)
    return write_export(now, export(now, persist=True, quick=True))


def doctor(now: float | None = None) -> tuple[bool, str]:
    """(ok, hint) for `homelab-maint doctor`: the pipeline is healthy NOW (mode cli: no benefit of the doubt) and, when the public
    export exists, the published self.json is fresh and says the same (it flags a refresher that is not running)."""
    now = time.time() if now is None else float(now)
    doc = export(now, persist=False, quick=False, mode="cli")
    ok, msgs = doc["level"] == "ok", [] if doc["level"] == "ok" else [doc["headline"]]
    if (core.STATE_DIR / "public").is_dir():
        eff = read_published(now)
        if eff["level"] != "ok":
            ok = False
            msgs.append("published self.json: " + eff["headline"])
    return ok, _ascii("; ".join(msgs) or "pipeline healthy", 300)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="homelab-maint self-health", description="health of the monitoring pipeline itself (read-only)")
    ap.add_argument("--json", action="store_true", help="print the public self.json document")
    ap.add_argument("--write", action="store_true", help="persist state and write STATE_DIR/public/self.json (only if public/ exists)")
    ap.add_argument("--refresh", action="store_true", help="the 1-minute refresher unit: --write with the 30-minute caches, silent")
    ap.add_argument("--published", action="store_true", help="what a consumer must show for the self.json on disk (age rule applied)")
    ap.add_argument("--check", action="store_true", help="exit 0 ok / 1 degraded / 2 down")
    ap.add_argument("--now", type=float, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    now = a.now if a.now is not None else time.time()
    codes = {"ok": 0, "degraded": 1, "down": 2}
    if a.refresh:
        try:
            return 0 if refresh(now) or not (core.STATE_DIR / "public").is_dir() else 1
        except Exception as exc:                                  # noqa: BLE001 - the unit fails visibly; the file just ages (and the page says so)
            print(f"self-health refresh failed: {type(exc).__name__}: {_ascii(exc, 100)}", file=sys.stderr)
            return 1
    if a.published:
        eff = read_published(now)
        if a.json:
            print(json.dumps(eff, indent=1, sort_keys=True))
        else:
            print(eff["headline"] + (f"   (file is {_dur(eff['age_s'])} old)" if eff["age_s"] is not None else ""))
            for r in eff["reasons"]:
                print(f"  - {r}")
        return codes[eff["level"]] if a.check else 0
    doc = export(now, persist=a.write, quick=False, mode="export" if a.write else "cli")    # a human asking wants the live numbers, not the cache
    if a.write:
        write_export(now, doc)
    if a.json:
        print(json.dumps(doc, indent=1, sort_keys=True))
    else:
        v = doc["verdict"]
        print(f"{doc['headline']}   (level {v['level']} since {_dur(now - v['since'])} ago)")
        for c in doc["checks"]:
            print(f"  {c['state']:<8} {c['title']:<22} {c['detail']}" + (f"\n{'':>33}try: {c['hint']}" if c.get("hint") else ""))
    return codes[doc["level"]] if a.check else 0


if __name__ == "__main__":
    sys.exit(main())
