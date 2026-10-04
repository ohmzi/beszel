"""homelab-maint core: config, state, locking, task contract, alerting, audit.

Design rules (enforced here so individual tasks cannot skip them):
  * Tasks are small functions returning a `Result`; they never write status files,
    send alerts, or decide whether apply mode is on. The runner does.
  * Every mutation goes through `Ctx.act(...)`, which checks the kill switch, the
    per-run caps, the protected-workload list, and writes an audit record.
  * A task class is C0 (read-only), C1 (safe auto-clean, apply allowed) or C2
    (plan only; applied by `homelab-maint approve`).
  * An empty or unparsable selector means NOTHING is selected, never everything.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CONF_DIR = Path(os.environ.get("HOMELAB_MAINT_CONF", "/etc/homelab-maint"))
STATE_DIR = Path(os.environ.get("HOMELAB_MAINT_STATE", "/var/lib/homelab-maint"))
LOG_DIR = Path(os.environ.get("HOMELAB_MAINT_LOG", "/var/log/homelab-maint"))
RUN_DIR = Path(os.environ.get("HOMELAB_MAINT_RUN", "/run/homelab-maint"))

GIB = 1024 ** 3
LEVELS = {"ok": 0, "info": 0, "warn": 1, "crit": 2, "error": 2, "skipped": 0}


# --------------------------------------------------------------------------- results
@dataclass
class Result:
    status: str = "ok"                    # ok | info | warn | crit | skipped | error
    summary: str = ""                     # one line, <= 140 chars, ASCII (goes into SMS)
    metrics: dict[str, Any] = field(default_factory=dict)   # small scalars for widgets
    items: list[dict[str, Any]] = field(default_factory=list)  # top N rows for tables
    reclaimed_bytes: int = 0              # bytes actually freed this run (apply mode)
    plan: dict[str, Any] | None = None    # C2: what *would* be done, hashed for approval
    alert: bool = True                    # False: show in dashboard but never page
    issue_key: str | None = None          # SPEC5: stable identity of THIS error for acknowledgements; None = derive it from the summary (etc/ack.toml)


def ikey(**sets: Any) -> str | None:
    """SPEC5: a Result.issue_key built from the FULL sets of what fails, e.g. ikey(units=stale, exited=names) -> "exited:b;units:a,c". Names
    only, sorted and escaped (so two different sets can never read alike): a changed set is another error, a volatile number (an age, a
    percentage, a count of restarts) is never in it. A magnitude that matters (a backup a month late, +8000 sectors) goes in as its decade,
    e.g. "b2". None when nothing fails (the task then has nothing to acknowledge)."""
    esc = lambda s: str(s).replace("%", "%25").replace(";", "%3B").replace(",", "%2C")   # noqa: E731  (":" is safe: a kind never contains one)
    parts = [f"{k}:{','.join(sorted({esc(x) for x in v}))}" for k, v in sorted(sets.items()) if v]
    return ";".join(parts) or None


@dataclass
class Task:
    name: str
    klass: str                            # "C0" | "C1" | "C2"
    tier: str                             # "check" (15 min) | "daily" | "weekly" | "monthly" (monthly window, driven by the tick)
    run: Callable[["Ctx"], Result]
    title: str = ""                       # human label for the dashboard
    timeout: int = 300                    # seconds; SIGALRM guard
    needs_root: bool = False


REGISTRY: dict[str, Task] = {}
DUPLICATES: list[tuple[str, str, str]] = []   # (task, module that lost, module that now owns the name): `doctor` and a test read it


def task(name: str, klass: str, tier: str, title: str = "", timeout: int = 300,
         needs_root: bool = False):
    """Decorator used by every module in homelab_maint/tasks/ (and reports.py, routine.py)."""
    def deco(fn: Callable[["Ctx"], Result]) -> Callable[["Ctx"], Result]:
        old = REGISTRY.get(name)
        if old is not None and getattr(old.run, "__module__", "") != getattr(fn, "__module__", ""):
            DUPLICATES.append((name, getattr(old.run, "__module__", "?"), getattr(fn, "__module__", "?")))   # a re-import of one module is no clash
        REGISTRY[name] = Task(name, klass, tier, fn, title or name, timeout, needs_root)
        return fn
    return deco


# --------------------------------------------------------------------------- config
def load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_config() -> dict:
    cfg = load_toml(CONF_DIR / "maint.toml")
    cfg.setdefault("global", {})
    cfg.setdefault("tasks", {})
    cfg.setdefault("caps", {})
    prot = load_toml(CONF_DIR / "protected.toml")
    if not prot.get("patterns"):
        # No protected list means we cannot tell what is safe: protect everything (fail closed).
        prot = {**prot, "patterns": [".*"], "_missing": True}
    cfg["protected"] = prot
    return cfg


def jdump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def write_json_atomic(path: Path, obj: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- shell helpers
def sh(cmd: list[str] | str, timeout: int = 60, check: bool = False,
       input_: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, never raising on non-zero unless check=True; timeout => rc 124."""
    shell = isinstance(cmd, str)
    if not shell and cmd[:1] == ["logger"] and os.environ.get("HOMELAB_MAINT_NO_SYSLOG"):   # tests and scratch runs: the host journal stays clean
        return subprocess.CompletedProcess(cmd, 0, "", "")
    e = dict(os.environ)
    e.update({"LC_ALL": "C", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
    if env:
        e.update(env)
    try:
        return subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout,
                              check=check, input=input_, env=e)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", (exc.stderr or "") + "timeout")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


# --------------------------------------------------------------------------- context
class CapExceeded(Exception):
    pass


class Ctx:
    """Everything a task may touch. Constructed fresh per task per run."""

    def __init__(self, cfg: dict, task_name: str, apply: bool, now: float | None = None):
        self.cfg = cfg
        self.name = task_name
        self.now = now if now is not None else time.time()
        self.tcfg: dict = cfg.get("tasks", {}).get(task_name, {})
        self.global_apply = apply
        self.apply = apply and self.tcfg.get("mode", "report") == "apply" and not paused(task_name)
        self.state_path = STATE_DIR / "tasks" / f"{task_name}.json"
        self.state: dict = read_json(self.state_path, {}) or {}
        self._deleted_bytes = 0
        self._actions = 0
        caps = cfg.get("caps", {})
        self.cap_bytes = int(float(self.tcfg.get("max_gib_per_run", caps.get("max_gib_per_run", 40))) * GIB)
        self.cap_items = int(self.tcfg.get("max_items_per_run", caps.get("max_items_per_run", 500)))
        self.protected = cfg.get("protected", {})
        self.dry_sample = _int_or(cfg.get("global", {}).get("audit_dry_sample"), DRY_SAMPLE)
        self._dry: dict[str, list[int]] = {}               # action -> [would-do count, bytes, unlisted count, unlisted bytes]

    # -- state ---------------------------------------------------------------------------
    def save_state(self) -> None:
        write_json_atomic(self.state_path, self.state, 0o600)

    # -- config helpers ------------------------------------------------------------------
    def opt(self, key: str, default: Any = None) -> Any:
        return self.tcfg.get(key, default)

    # -- protection ----------------------------------------------------------------------
    def is_protected(self, *names: str) -> bool:
        """True if any name matches a protected pattern (container, process, unit or path)."""
        pats = self.protected.get("patterns", [])
        # Per-task, explicit, auditable exemptions (e.g. `caps` exists to put a ceiling on tunarr, which
        # the global list protects from being killed). Matches here skip the global patterns only.
        unprotect = [u for u in self.tcfg.get("unprotect", []) if isinstance(u, str)]
        for n in names:
            if any(_rx(u, n) for u in unprotect):
                continue
            for p in pats:
                try:
                    if re.search(p, n or "", re.I):
                        return True
                except re.error:
                    return True   # a broken pattern protects, it never exposes
        return False

    # -- mutation gate -------------------------------------------------------------------
    def act(self, what: str, target: str, size: int, fn: Callable[[], Any],
            protect_names: tuple[str, ...] = (), outcome: str = "done") -> bool:
        """Perform (or, in dry-run, describe) one mutating action. Returns True if executed. `outcome` is the audit label of a success:
        "done" is what the change log and the reports count as a change; an action that only STARTS something whose result is confirmed
        later (a launched relief) passes another word, and the task audits "done" itself once the outcome is known.

        Refuses when: apply is off, kill switch present, caps would be exceeded, target is
        protected, or `target` is empty. Every attempt is written to audit.jsonl.
        """
        if not target or not str(target).strip():
            audit(self.name, what, target, size, "refused-empty-selector")
            return False
        if self.is_protected(target, *protect_names):
            audit(self.name, what, target, size, "refused-protected")
            return False
        if not self.apply:
            self._would(what, target, size)
            return False
        if paused(self.name):            # a PAUSE dropped in while a long task is running
            audit(self.name, what, target, size, "refused-paused")
            self.apply = False
            return False
        if self._actions + 1 > self.cap_items or self._deleted_bytes + max(size, 0) > self.cap_bytes:
            audit(self.name, what, target, size, "refused-cap")
            raise CapExceeded(f"{self.name}: per-run cap reached "
                              f"({human(self._deleted_bytes)} / {self.cap_items} items)")
        try:
            ret = fn()
        except Exception as exc:  # noqa: BLE001
            audit(self.name, what, target, size, f"failed: {exc}")
            raise
        if isinstance(ret, int) and not isinstance(ret, bool) and ret >= 0:
            size = ret                   # exact bytes freed reported by the action itself
        self._actions += 1
        self._deleted_bytes += max(size, 0)
        audit(self.name, what, target, size, outcome)
        return True

    def _would(self, what: str, target: str, size: int) -> None:
        """A report-mode decision. The first `dry_sample` per action are audited one by one (a reader can see WHAT it would do); the rest are
        only counted and `flush_dry` writes ONE row for them. A daily run used to write ~230 rows (retention, qos_classes) for nothing."""
        d = self._dry.setdefault(what, [0, 0, 0, 0])
        d[0] += 1
        d[1] += max(size, 0)
        if d[0] <= self.dry_sample:
            audit(self.name, what, target, size, "dry-run")
        else:
            d[2] += 1
            d[3] += max(size, 0)

    def flush_dry(self) -> None:
        """One aggregate row per action whose would-do rows were not all listed (run_task calls it when the task ends)."""
        for what, d in self._dry.items():
            if d[2]:
                audit(self.name, what, f"(+{d[2]} more, not listed)", d[3], "dry-run", n=d[2])
        self._dry.clear()

    @property
    def freed(self) -> int:
        return self._deleted_bytes


def _rx(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text or "", re.I) is not None
    except re.error:
        return False


def approved(task_name: str, hash_: str) -> bool:
    """True if `homelab-maint approve` recorded this exact plan hash."""
    return (STATE_DIR / "approvals" / f"{task_name}.{hash_}").exists()


# --------------------------------------------------------------------------- audit/log
DRY_SAMPLE = 20                  # [global] audit_dry_sample: report-mode rows of one action listed individually per task run (see Ctx._would)
SYSLOG_MAX = 200                 # attempts of one task run that reach syslog line by line; audit.jsonl always has every one
_syslog: list[str] | None = None  # while a task runs: attempts wait here and leave in ONE `logger` call (run_task), not one fork each


def _int_or(v: Any, default: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def audit(task_name: str, what: str, target: str, size: int, outcome: str, n: int = 1) -> None:
    """One row of audit.jsonl. `n` > 1: the row stands for n would-do decisions (an aggregate, see Ctx.flush_dry)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "task": task_name, "action": what,
           "target": str(target), "bytes": size, "outcome": outcome, **({"n": n} if n > 1 else {})}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "audit.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    if outcome == "dry-run":      # syslog is for what was attempted: a report-mode run lists "would" rows (one fork each) nobody reads there
        return
    line = f"{task_name} {what} {outcome} {target} {size}"
    if _syslog is None:
        sh(["logger", "-t", "homelab-maint", line], timeout=5)
    else:
        _syslog.append(line)


def _syslog_flush(prev: list[str] | None) -> None:
    """End of a task run: send what it audited to syslog with ONE `logger` (lines from stdin), or hand it to the enclosing run."""
    global _syslog
    lines, _syslog = _syslog or [], prev
    if prev is not None:
        prev.extend(lines)
        return
    if not lines:
        return
    if len(lines) > SYSLOG_MAX:
        lines = lines[:SYSLOG_MAX - 1] + [f"{len(lines) - SYSLOG_MAX + 1} more attempts of this run: see audit.jsonl"]
    body = "".join(re.sub(r"[\x00-\x1f\x7f]", " ", ln)[:900] + "\n" for ln in lines)   # no control char can forge a second syslog line
    try:
        sh(["logger", "-t", "homelab-maint"], timeout=10, input_=body)
    except OSError:
        pass                                                   # no syslog is never a reason to fail a task run


def paused(task_name: str | None = None) -> bool:
    if (CONF_DIR / "PAUSE").exists():
        return True
    return bool(task_name and (CONF_DIR / f"PAUSE.{task_name}").exists())


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(jdump(plan).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- locking / timeout
class Locked(Exception):
    pass


class tier_lock:
    def __init__(self, tier: str):
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RUN_DIR / f"{tier}.lock"

    def __enter__(self):
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Locked(str(self.path)) from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


class _Timeout(BaseException):
    """BaseException on purpose: task code with `except Exception` must not swallow the alarm."""


def _alarm(_sig, _frm):
    raise _Timeout()


def run_task(t: Task, cfg: dict, apply: bool) -> tuple[Result, float]:
    global _syslog
    ctx = Ctx(cfg, t.name, apply)
    started = time.time()
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(t.timeout)
    outer, _syslog = _syslog, []                              # this run's attempts leave in one `logger` call (_syslog_flush)
    try:
        if t.klass == "C0":
            ctx.apply = False         # C0 can never mutate, whatever the config says
        res = t.run(ctx)
        if not isinstance(res, Result):
            res = Result("error", f"{t.name} returned {type(res).__name__}, not Result")
        res.reclaimed_bytes = max(res.reclaimed_bytes, ctx.freed)
    except CapExceeded as exc:
        res = Result("warn", f"cap reached: {exc}"[:140], reclaimed_bytes=ctx.freed)
    except _Timeout:
        res = Result("error", f"timed out after {t.timeout}s", reclaimed_bytes=ctx.freed)
    except Exception as exc:  # noqa: BLE001
        res = Result("error", f"{type(exc).__name__}: {exc}"[:140])
        res.metrics["traceback"] = traceback.format_exc()[-600:]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
        ctx.flush_dry()
        _syslog_flush(outer)
    try:
        ctx.save_state()
    except OSError:
        pass
    return res, time.time() - started


# --------------------------------------------------------------------------- alerting
class Notifier:
    """State-change alerts: the debounce state machine (confirm-before-alert, reminders, flap damping).

    DELIVERY is notify.py's (SPEC4 S7): routes, quiet hours, dedupe, budgets (notify.toml [budget]), templates and the delivery
    log live there and `_send` below is only the adapter. cli.cmd_run uses notify.HermesNotifier (this class + a durable queue
    that is flushed after the state lock is released); this class alone sends inline. If notify cannot be imported or raises,
    `_send_bridge` (the original Hermes bridge call and its own daily budget) still gets the page out: an alert is never lost
    to a broken template module.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._ctx: tuple[str, Any] = ("", None)             # (title, Result) of the evaluate() in progress, for the adapter
        g = cfg.get("global", {})
        self.bridge = g.get("bridge", "/usr/local/sbin/backup-notify-hermes.py")
        self.handle = g.get("notify_handle", "ohmz")
        self.confirm = int(g.get("alert_confirm_runs", 2))
        self.reminder_s = int(float(g.get("alert_reminder_hours", 24)) * 3600)
        self.daily_budget = int(g.get("alert_daily_budget", 8))
        self.path = STATE_DIR / "alerts.json"
        self.s = read_json(self.path, {"tasks": {}, "sent": []}) or {"tasks": {}, "sent": []}

    def evaluate(self, name: str, title: str, res: Result, now: float) -> None:
        """Debounced state machine. A new level only becomes the *confirmed* level after it has been
        seen `confirm` runs in a row, so one-run blips never page; a confirmed problem pages once, then
        reminds every `reminder_s`; a confirmed recovery sends one OK message."""
        st = self.s["tasks"].setdefault(name, {"level": 0, "pending": 0, "pending_streak": 0,
                                               "alerted": 0, "last_sent": 0})
        self._ctx = (title, res)
        lvl = LEVELS.get(res.status, 0) if res.alert else 0
        if lvl == st["level"]:
            st["pending"], st["pending_streak"] = lvl, 0          # steady state: nothing pending
        else:
            if st.get("pending") == lvl:
                st["pending_streak"] += 1
            else:
                st["pending"], st["pending_streak"] = lvl, 1
            if st["pending_streak"] >= self.confirm:               # confirmed change of level
                st["level"], st["pending_streak"] = lvl, 0
                if lvl == 0 and st["alerted"]:
                    self._send(name, f"OK {title}: recovered", f"{title} recovered. {res.summary}", now)
                    st["alerted"], st["last_sent"] = 0, now
        if st["level"] > 0 and (st["alerted"] != st["level"] or now - st["last_sent"] >= self.reminder_s):
            tag = "CRIT" if st["level"] >= 2 else "WARN"
            if self._send(name, f"{tag} {title}", f"{title}: {res.summary}", now):
                st["alerted"], st["last_sent"] = st["level"], now

    def _send(self, name: str, subject: str, body: str, now: float) -> bool:
        """Adapter to notify.notifier_send. True = the owner was told OR policy deliberately held the message (dedupe, quiet
        hours, mute); False = failed or over budget, so evaluate() retries an alert next run."""
        title, res = self._ctx
        st = self.s["tasks"].get(name) or {}
        level = int(st.get("level", 0) or 0)                 # evaluate() sets the new level before it sends a recovery
        try:
            from . import notify
            return notify.notifier_send(self.cfg, name, title or name, "crit" if level >= 2 else "warn",
                                        str(getattr(res, "summary", "") or body), now, recovery=level == 0,
                                        was="crit" if int(st.get("alerted", 0) or 0) >= 2 else "warn")
        except Exception:  # noqa: BLE001 - never lose an alert to a broken notify module
            return self._send_bridge(name, subject, body, now)

    def _acked_hold(self, name: str, now: float) -> bool:
        """SPEC5: True when this ALERT is for an error the owner acknowledged (exact fingerprint, severity at or below the acknowledged one).
        Only the legacy bridge path below asks (notify.send holds acknowledged messages itself). Any doubt = False = send."""
        try:
            from . import acks
            _title, res = self._ctx
            level = int((self.s["tasks"].get(name) or {}).get("level", 0) or 0)
            sev = "crit" if level >= 2 else "warn"
            return level > 0 and res is not None and acks.is_acked(acks.fingerprint(name, res, sev), sev, now) is not None
        except Exception:  # noqa: BLE001
            return False

    def _send_bridge(self, name: str, subject: str, body: str, now: float) -> bool:
        if self._acked_hold(name, now):                      # reached only when notify.py is broken: an acknowledged alert still stays silent
            audit("notify", "suppressed", name, 0, "acknowledged")
            return True
        self.s["sent"] = [t for t in self.s["sent"] if now - t < 86400]
        if len(self.s["sent"]) >= self.daily_budget:
            audit("notify", "budget-exhausted", name, 0, "dropped")
            return False
        sms = re.sub(r"[^\x20-\x7e]", "?", body)[:130]
        cmd = ["runuser", "-u", self.handle, "--", self.bridge, self.handle,
               f"homelab-maint: {subject}"[:120], sms]
        r = sh(cmd, timeout=90, env={"HOME": f"/home/{self.handle}"})
        ok = r.returncode == 0
        audit("notify", "send", f"{name}: {subject}", 0,
              "sent" if ok else f"failed rc={r.returncode} {((r.stderr or '') + (r.stdout or ''))[-160:].strip() or 'no reason logged'}")
        if ok:
            self.s["sent"].append(now)
        return ok

    def save(self) -> None:
        write_json_atomic(self.path, self.s, 0o600)


def kuma_push(cfg: dict, key: str, status: str, msg: str) -> None:
    """GET-only push heartbeat to Uptime Kuma v1 (POST returns 404 on v1).

    The URL contains the push token, so it is passed to curl on stdin (`-K -`), never in argv.
    """
    from urllib.parse import quote
    g = cfg.get("global", {})
    token = load_toml(CONF_DIR / "kuma.toml").get("push", {}).get(key)
    if not token or not re.fullmatch(r"[A-Za-z0-9]{8,64}", str(token)):
        return
    base = g.get("kuma_url", "http://127.0.0.1:3011").rstrip("/")
    q = f"status={'up' if status in ('ok', 'info', 'skipped') else 'down'}&msg={quote(re.sub(r'[^A-Za-z0-9._ -]', '_', msg)[:80])}"
    r = sh(["curl", "-fsS", "-m", "8", "-K", "-"], timeout=12, input_=f'url = "{base}/api/push/{token}?{q}"\n')
    try:                                                      # self_health's Kuma row: a heartbeat that stopped landing is a monitoring fault
        from .tasks import self_health
        self_health.note_kuma(key, r.returncode == 0, f"curl exit {r.returncode}")     # 'curl exit N', not 'rc=N': publish cuts text after rc=
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- history
HISTORY_MAX_BYTES = 40 * 1024 * 1024


def append_history(rec: dict, keep_days: int = 14) -> None:
    """Append one compact JSON line. Trims (atomically, under a lock) only when the file is large:
    first by age, then by dropping the oldest half if age alone did not get it under the cap."""
    p = STATE_DIR / "history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
    with open(p, "a") as f:
        f.write(line)
    try:
        if p.stat().st_size <= HISTORY_MAX_BYTES:
            return
        with open(STATE_DIR / "history.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            cutoff = time.time() - keep_days * 86400
            lines = [ln for ln in p.read_text().splitlines() if _ts(ln) >= cutoff]
            if sum(len(x) + 1 for x in lines) > HISTORY_MAX_BYTES:
                lines = lines[len(lines) // 2:]
            tmp = p.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            os.replace(tmp, p)
    except OSError:
        pass


def _ts(line: str) -> float:
    try:
        return float(json.loads(line).get("t", 0))
    except ValueError:
        return 0.0


def read_history(since_s: float, kind: str | None = None) -> list[dict]:
    p = STATE_DIR / "history.jsonl"
    out: list[dict] = []
    cutoff = time.time() - since_s
    try:
        with open(p) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("t", 0) >= cutoff and (kind is None or r.get("kind") == kind):
                    out.append(r)
    except OSError:
        pass
    return out
