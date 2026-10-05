"""notify: the ONE path by which homelab-maint tells the owner anything (SPEC4 S7).

    send(Event(kind, severity, title, summary, details, facts, status, dedupe_key, task), cfg=None, now=None) -> Delivery

Kinds: alert, recovery, maintenance, digest_daily, report_weekly, incident_open, incident_resolved, ack_expired, test.
Everything about a message lives here or in notify_templates.py: routing, quiet hours, dedupe, budgets, escalation,
rendering (SMS / subject / plain / HTML in the Ohmz Cloud palette), delivery, and the delivery log. The debounce
state machine (confirm N runs, reminders) stays in core.Notifier; HermesNotifier (below) is that class with delivery
through notify.send: it builds an Event (alert_event / recovery_event), remembers a recovery that could not be delivered,
and retries it. It is what cli.cmd_run should use, as `HermesNotifier(cfg, defer=True)` with `.deliver()` called AFTER the
state lock is released (evaluate() then only queues, so a dead transport never holds cli's flock). `notifier_send` is the plain
function form (see its docstring for what its return value means: do not patch core.Notifier._send with it).

Pipeline (each step can end it with a reason that is logged):
  config -> route (kind x severity, per-task override, maintenance `facts.significant`) -> warn escalation ->
  recovery SMS only if the problem was texted -> mute file -> quiet hours (non-critical lose their SMS) ->
  dedupe window / covered-by-another-kind (the alert pager and the incident ledger speak once, not twice) ->
  budgets (per kind, total, SMS; critical bypasses the per-kind and total caps, never the hard cap) ->
  CLAIM (state, under a short flock) -> render -> transport (outside the lock) -> COMMIT or ROLLBACK + log + audit.
A failed delivery is rolled back, so it consumes no budget and no dedupe slot and the caller can retry next run.
A claim whose sender died is dropped after CLAIM_TTL_S. A malformed notify.toml never blocks delivery (defaults).
A CRITICAL alert/incident_open that could not be delivered at all (every leg failed, the transport timed out, or the circuit
below is open) is also copied into the OUTBOX (Delivery.queued, state key "outbox"): replayed oldest first by flush_pending
through this same pipeline (so a page the owner got meanwhile by another path is held by dedupe/coverage, not sent twice), with
back-off (outbox_gap_s, doubling, at most an hour) until it lands or outbox_ttl_s after its first attempt (then logged as
"expired" and audited "outbox-expired"). The copy is sanitised and bounded (secrets scrubbed, <= 16 KB). A fresh message for the
same problem first delivers the older queued messages of OTHER kinds (an alert before its recovery).

ACKNOWLEDGED ISSUES (SPEC5, config [ack]). The owner can say "I understand this error and I'm okay with it" for 90 days:
  * the email of an alert / incident_open (and of a still-failing ack_expired notice) carries an Acknowledge button and the plain
    line `Acknowledge (90 days): <url>`. The URL is `<base_url>/ack?id=<fp>&t=<token>&d=<days>&s=<warn|crit>` (SPEC5 S8; the email also prints
    `Issue ID: <fp>`; `d` is [ack] days rounded DOWN to 7/30/90/365 and capped by the runner's own [ack] max_days (no allowed length fits: no
    link, Issue ID only); tests/test_notify.py proves the chain: link -> the site's parse rules -> token hash in ack/tokens.json -> signed inbox
    request -> acks.process_inbox); the token is issued by acks.issue_token (256 random bits,
    only its SHA-256 is stored, valid token_ttl_days, single use, one fresh token per email, always for the same fingerprint). It opens a
    CONFIRMATION page; nothing is acknowledged by a GET. The token exists in exactly one place here: the email bodies handed to the
    primary transport (stdin of the child). It is never logged, audited, exported, stored in notify-state.json or the outbox, never in an
    SMS, and the legacy bridge fallback (a 0644 temp file) gets a copy of the plain text WITHOUT the link; a token whose email certainly did not go
    out (email leg failed, bridge fallback used) is revoked (acks.revoke_token). Previews, `notify render`,
    dry runs and TESTs show an inert placeholder link (when the button is on, see below) and never issue a token.
  * an acknowledged fingerprint is HELD before routing: alert, incident_open and (for the same episode) recovery and incident_resolved
    are not sent, nothing is claimed (no budget, no dedupe slot, no escalation count), and the delivery log says
    `suppressed: acknowledged until <date>` (skipped = "acknowledged", handled = True so callers do not retry). A higher severity
    than the acknowledged one is not held (acks.is_acked decides). maintenance, digests, reports, ack_expired and TESTs are never held.
  * ack_expired is the one notice sent when an acknowledgement ends (`notify_expired`, `send_expired`): still failing (with a fresh
    button, at the issue's CURRENT severity) or no longer occurring. More than [ack] notice_group_over (2) ending in one run are ONE
    "N acknowledgements ended" email (no button: whatever still fails alerts again, each with its own). A notice that cannot be delivered
    waits in the outbox, in its own class (`notices_max`): it can never push a queued critical page out, and pages are replayed first.
  * WHICH TASKS AND SEVERITIES (`_ack_allowed` = acks.ackable, so etc/ack.toml [ack] is the ONE policy for the pager, the dashboard, the inbox
    and the CLI): only an alert whose fingerprint names the ERROR gets a button and a hold: Result.issue_key, a [key.<task>] rule in
    etc/ack.toml, or an explicit allow_tasks entry. acks' [ack] severities (warn only by default) narrows that further: a CRIT alert carries no
    button and no Issue-ID line, `_ack_offer` refusing it before a token is minted. The number-blind text fallback (the same fingerprint at 8
    sectors and at 8000) is never offered or honoured; smart_event and `job:*` are denied outright. The notify.toml [ack] policy keys (require_rule, allow_tasks, deny_*) are deprecated: they
    only apply when the acks module has no `ackable`. A ruled task stays number-blind about SIZE by design (SPEC5 S2: only a worse severity
    re-alerts): the accepted trade-off. issue_token gets the fingerprint's mode, so an explicit-key task keeps its button.
  * RELEASE: core.Notifier counts a held alert as sent, so HermesNotifier remembers (notify-state.json ackfp[key].held) which alerts were
    held and, when the acknowledgement is gone (un-acknowledged, expired, module missing) OR the problem now has another fingerprint (a
    decade worse, a longer or different list, at the same severity), clears the task's "already alerted" state so this very check run alerts
    again instead of waiting for a reminder (`_hold_released(key, nc, now, cur_fp)`, called from HermesNotifier.evaluate).
  * THE BUTTON WAITS FOR THE SITE ([ack] button = auto | true | false): "auto" shows the button/link once STATE_DIR/ack/web_ready exists
    (ack_web's deploy), until then an email carries the Issue ID line only and no token is minted. A link that opens a 404 is worse than none.
  * FAIL CLOSED: no acks module, a disabled [ack], an error from the module, a fingerprint that is not 16 hex, an `until` that is not in
    the future, `facts.ack = false`: the message is sent exactly as before. The fingerprint is `facts.ack_fp` (the caller computed it
    from the Result), else acks.fingerprint(task, summary, severity); both only for a task that passes _ack_allowed.

Delivery uses ONLY the owner's Hermes transports (SMS carrier gateway + Gmail in ~ohmz/.hermes), never copying
their credentials, and never importing ohmz-writable code into a root process:
  * primary: `runuser -u ohmz -- python3 -m homelab_maint.notify --child` (when running as root; direct otherwise)
    reads one JSON message on stdin (so bodies never appear in argv) and calls `alert_transports.send_report`, which
    already has the right shape (own SMS line, subject, plain body, HTML part, channel list, partial success).
  * fallback: the legacy bridge `backup-notify-hermes.py <handle> <subject> <sms> <detail-file>` (plain text, both
    channels), used only if the primary itself broke before any channel reported AND both channels were wanted.
  * partial success is success, like the bridge: SMS landed + email failed => ok=True, the failed leg is logged
    and audited. If the HTML cannot be rendered (or is over 95 KB) the email goes as plain text.
  * a CRITICAL alert/incident page whose SMS leg failed while the email went is not finished: the text alone is retried
    (flush_pending: <= [retry] sms_attempts in all, sms_gap_s apart, inside sms_window_s) and until it lands that page
    "covers" nothing (a later incident_open for the same problem is not swallowed). The email is never re-sent.
  * transport circuit breaker: when the transport itself fails (no channel reported, or a timeout) it is not tried again
    for [transport] breaker_s (state: notify-state.json "breaker"); callers get Delivery(skipped="breaker", handled=False)
    at once instead of waiting 90 s per message. The legacy-bridge fallback gets fallback_timeout_s, not the full timeout.
    A TEST always probes the transport.
Under pytest the real transports refuse to run (tests inject fakes), so a test can never send a message.
Callers that must not wait can pass `transport=` (any callable(Message, config) -> TransportResult); that is also how a
webhook/ntfy channel would be added later.

"What to do" in an alert email: details.todo from the caller, else notify.toml [todo.<task>], else the task's playbook
(etc/playbooks.toml through incidents.playbook_for, the same text as the Incidents tab).

Files (no secrets, no addresses), in the first usable directory of _dirs(): STATE_DIR, else RUN_DIR (tmpfs: still there when
/ is full), else $XDG_RUNTIME_DIR/homelab-maint (a user unit):
notify-state.json (budget/dedupe/escalation/breaker/pending/outbox, 0600: the outbox holds sanitised copies of undelivered critical
pages until they land), notify.lock, notifications.jsonl (delivery log: ts, kind,
severity, title, channels, ok, note, dedupe_key, legs; no bodies). The budgets and the hard cap therefore keep working when
the primary directory is read-only or the disk is full, and for a hook that runs as the owner (not shared with root's).
Audit (core.audit, task "notify") keeps the records alert_path_health and publish already read:
action "send" outcome "sent" | "failed rc=N <reason>"; a failed CHANNEL of a delivered message is its own action "send" row
"failed rc=1 <reason>" with target "<kind>: <leg> leg" (written right after the "sent" row), and every SMS retry writes one
more; action "budget-exhausted" outcome "dropped" (at most once an hour per kind and key); actions "outbox-expired" and
"outbox-overflow" outcome "dropped" (a queued critical page that was given up). leg_health() is the exact view.

Kill switch: an empty file CONF_DIR/NOTIFY_MUTE silences everything except critical alerts/incidents. The global
PAUSE file does NOT mute notifications (you still want to hear about problems while maintenance is paused).
Run the CLI as root (`sudo homelab-maint notify-test`). As another user the primary directory is not writable: the
message still goes (a silent alert path is worse than a repeated one) and is counted against that user's own fallback state.

CLI (also what `homelab-maint notify-test` should call):
  python3 -m homelab_maint.notify test [KIND[.SEV]...] [--dry-run]    one labelled TEST per kind, routed like the real thing
  python3 -m homelab_maint.notify route [KIND[.SEV]...] [--now EPOCH] which channels each kind/severity uses (no send)
  python3 -m homelab_maint.notify render [KIND...] --out DIR          write sample .html/.txt previews (no send)
  python3 -m homelab_maint.notify export                              notifications.json for the website
  python3 -m homelab_maint.notify send KIND SEVERITY TITLE [SUMMARY] [--task T] [--key K] [--fact L=V] [--detail-file F] [--dry-run]
                                                                      one event from a shell hook / OnFailure unit (exit 0 when sent,
                                                                      held by policy, or a critical page queued for replay; 1 otherwise)
  python3 -m homelab_maint.notify flush                               replay queued critical pages and retry the SMS leg of pages whose text
                                                                      failed (every send(), HermesNotifier.deliver() and the tick can call it)
  python3 -m homelab_maint.notify legs                                per-channel health from the delivery log (alert_path_health reads this)
  python3 -m homelab_maint.notify doctor                              read-only self-check of the delivery path
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import select
import socket
import stat
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import core
from . import notify_templates as T
from .core import sh

_REAL_SH = sh                                    # captured so the pytest guard can tell a fake from the real thing
KINDS = T.KINDS
SEVERITIES = T.SEVERITIES
CHANNELS = ("sms", "email")

# --------------------------------------------------------------------------- configuration
DEFAULTS: dict[str, Any] = {
    "transport": {"kind": "hermes", "handle": "", "user": "", "scripts_dir": "/home/ohmz/StudioProjects/ai-stack/scripts",
                  "bridge": "", "fallback": "bridge", "timeout_s": 90, "fallback_timeout_s": 20, "breaker_s": 300},
    "site": {"url": "https://maintainer.ohmzhomelab.ca", "host_label": "", "sms_prefix": "homelab",
             "subject_prefix": "[homelab] ", "masthead": "Maintenance"},
    "routes": {"alert": {"crit": "both", "warn": "email", "info": "email"}, "recovery": "both",
               "maintenance": "email", "digest_daily": "email", "report_weekly": "email",
               "incident_open": {"crit": "both", "warn": "email", "info": "email"}, "incident_resolved": "both",
               "ack_expired": "email", "test": "mirror"},
    "significant": {"maintenance": "both"},
    "task_routes": {},
    "todo": {},
    "escalation": {"warn_sms_after": 2, "recovery_sms_only_if_texted": True, "episode_ttl_h": 72},
    "dedupe": {"window_s": {"alert": 21600, "recovery": 3600, "maintenance": 3600, "digest_daily": 72000,
                            "report_weekly": 432000, "incident_open": 21600, "incident_resolved": 3600, "ack_expired": 604800,
                            "test": 0},
               # The alert pager and the incident ledger both describe one problem: whichever speaks first covers the
               # other (same key, within cover_window_s, and only if it was at least as severe). One page, not two.
               "covered_by": {"alert": ["incident_open"], "incident_open": ["alert"],
                              "recovery": ["incident_resolved"], "incident_resolved": ["recovery"]},
               "cover_window_s": 1800},
    "budget": {"per_day": {"alert": 8, "recovery": 8, "maintenance": 6, "digest_daily": 2, "report_weekly": 2,
                           "incident_open": 6, "incident_resolved": 6, "ack_expired": 10, "test": 20},
               "total_per_day": 30, "hard_cap_per_day": 40, "sms_per_day": 10, "crit_sms_reserve": 5,
               "crit_bypass": True},
    "quiet_hours": {"enabled": True, "window": "23:30-07:00", "tz": "", "suppress": ["sms"]},
    "mute_file": "NOTIFY_MUTE",
    "log": {"keep_days": 90, "max_bytes": 2097152, "max_lines": 5000},
    # A critical page whose SMS leg failed (the email went) is not "done": the text is retried on its own, the failed leg only.
    # A critical page that could not be delivered AT ALL (transport down, circuit open) waits in the outbox and is replayed
    # (backing off from outbox_gap_s to an hour) until it lands or outbox_ttl_s has passed since its first attempt.
    "retry": {"sms_attempts": 3, "sms_window_s": 3000, "sms_gap_s": 300, "outbox_ttl_s": 43200, "outbox_gap_s": 300, "outbox_max": 10,
              "notices_max": 20},
    # Acknowledged issues (SPEC5). `days` and `escalation_breaks` only word the email: the rule itself lives in acks.py / etc/ack.toml
    # (a test keeps `days` in step with etc/ack.toml). suppress_kinds / button_kinds are filtered against a hard whitelist in code.
    # button: "auto" = the link/button appears once STATE_DIR/ack/web_ready exists (the hub serves /ack, so its deploy creates it),
    # true = always, false = never. Until then an email carries only the text line `Issue ID` (never a button that opens a 404).
    # require_rule / allow_tasks / deny_tasks / deny_prefixes: DEPRECATED fallback copy; etc/ack.toml [ack] decides (acks.ackable, see _ack_allowed).
    "ack": {"enabled": True, "button": "auto", "base_url": "https://maintainer.ohmzhomelab.ca",
            "mint_url": "http://127.0.0.1:8088/api/beszel/maintenance/ack/mint", "token_ttl_days": 30, "days": 90,
            "button_text": "Acknowledge for {days} days", "escalation_breaks": True,
            "suppress_kinds": ["alert", "recovery", "incident_open", "incident_resolved"],
            "button_kinds": ["alert", "incident_open", "ack_expired"],
            "require_rule": True, "allow_tasks": [], "deny_tasks": ["smart_event"], "deny_prefixes": ["job:"],
            "notice_group_over": 2},
}
_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")


def _merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def load_config(cfg: dict | None = None) -> dict:
    """Defaults < /etc/homelab-maint/notify.toml (or cfg["notify"], or a notify-shaped cfg) < nothing else.
    maint.toml [global] supplies the handle and bridge when notify.toml leaves them empty. A broken file
    means defaults (alerts must still go out) and `_config_error` is set so the delivery note says so."""
    nc = copy.deepcopy(DEFAULTS)
    user: dict = {}
    err = ""
    if isinstance(cfg, dict) and isinstance(cfg.get("notify"), dict):
        user = cfg["notify"]
    elif isinstance(cfg, dict) and ("routes" in cfg or "transport" in cfg or "budget" in cfg):
        user = cfg
    else:
        try:
            user = core.load_toml(core.CONF_DIR / "notify.toml")
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            err = f"notify.toml unreadable ({type(exc).__name__}); defaults used"
    _merge(nc, user)
    bad = _fix_shapes(nc)
    if bad and not err:
        err = f"notify config: {', '.join(bad)}; defaults used for those"
    g = cfg.get("global", {}) if isinstance(cfg, dict) and isinstance(cfg.get("global"), dict) else {}
    t = nc["transport"]
    t["handle"] = str(t["handle"] or g.get("notify_handle") or "ohmz")
    t["bridge"] = str(t["bridge"] or g.get("bridge") or "/usr/local/sbin/backup-notify-hermes.py")
    t["user"] = str(t["user"] or t["handle"])
    if err:
        nc["_config_error"] = err
    return nc


_TABLES = ("transport", "site", "routes", "significant", "task_routes", "todo", "escalation", "dedupe", "budget", "quiet_hours",
           "log", "retry", "ack", "dedupe.window_s", "dedupe.covered_by", "budget.per_day")


def _fix_shapes(nc: dict) -> list[str]:
    """A typo such as `[budget] per_day = 5` must not turn every alert into an exception: a table that is not a table is
    replaced by its default (and named in the delivery note), and `quiet_hours.suppress = "sms"` becomes a list."""
    bad: list[str] = []
    for path in _TABLES:
        node, dflt, parts = nc, DEFAULTS, path.split(".")
        for part in parts[:-1]:
            node, dflt = node[part], dflt[part]
        if not isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = copy.deepcopy(dflt[parts[-1]])
            bad.append(f"[{path}] must be a table")
    sup = nc["quiet_hours"].get("suppress")
    if not isinstance(sup, (list, tuple)):
        nc["quiet_hours"]["suppress"] = [x for x in str(sup or "sms").replace("+", ",").split(",") if x.strip()] or ["sms"]
    return bad


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _chan(v) -> set[str] | None:
    """Route value -> set of channels; None if it is not understood (callers then fail safe to email)."""
    if v is None:
        return None
    s = ",".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
    s = s.strip().lower().replace("+", ",").replace(" ", "")
    if s in ("both", "all"):
        return set(CHANNELS)
    if s in ("none", "off", "false", "0"):
        return set()
    parts = [p for p in s.split(",") if p]
    return set(parts) if parts and all(p in CHANNELS for p in parts) else None


# --------------------------------------------------------------------------- data
@dataclass
class Event:
    kind: str
    severity: str = ""             # unset -> derived from `status`; with both set the more severe wins (never demoted)
    title: str = ""
    summary: str = ""
    details: Any = None            # str | list[str] | {"text","done","todo","timeline","log","sections"}
    facts: dict | None = None      # label -> value table; reserved: significant, host, link, tiles, was, sev, as, escalate, notified
    status: str | None = None      # task status word (ok/info/warn/crit/error/skipped); drives severity when that is unset
    dedupe_key: str | None = None
    task: str | None = None


@dataclass
class Delivery:
    kind: str = ""
    severity: str = ""
    ok: bool = False               # at least one channel delivered
    handled: bool = False          # ok, or intentionally not sent by policy (dedupe/none/quiet/muted): do not retry
    skipped: str = ""              # reason code when nothing was attempted
    channels: list = field(default_factory=list)    # delivered
    attempted: list = field(default_factory=list)
    legs: dict = field(default_factory=dict)        # channel -> sent|failed|skipped|unknown
    note: str = ""
    dedupe_key: str = ""
    why: list = field(default_factory=list)         # routing trace
    rendered: dict | None = None                    # only for dry runs
    queued: bool = False                            # not delivered YET: a durable copy waits in the outbox and is replayed (do not re-send)


@dataclass
class Message:
    handle: str
    sms: str
    subject: str
    plain: str
    html: str
    channels: list
    ack_url: str = ""              # the bearer link inside plain/html (real sends only): never logged, see plain_safe
    plain_safe: str = ""           # plain WITHOUT the acknowledge block: what a transport that leaves the body on disk may write


@dataclass
class TransportResult:
    ok: bool = False
    legs: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    fatal: str = ""
    rc: int | None = None
    via: str = ""


Transport = Callable[[Message, dict], TransportResult]


# --------------------------------------------------------------------------- redaction (delivery log, error text)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"\+\d[\d\s().-]{7,18}\d|\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}|(?<![\d.])\d{3}[\s.-]\d{3}[\s.-]\d{4}(?![\d.])"
                       r"|(?<![\d.,])1?\d{10}(?![\d.,])")
_URLQ_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s?#]*)[?#]\S*")
_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b|[A-Za-z0-9_+=]{32,}")
# An acknowledgement token is 43 characters of [A-Za-z0-9_-]: the `-` splits it for _BLOB_RE, so it gets its own rule (T.line has
# already turned `/ack?...&t=<token>` into `...&t=<redacted>`, and _URLQ_RE cuts any URL query; this catches a bare one).
_ACKTOK_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")


def redact_log(text, n: int = 160) -> str:
    """For the delivery log and error reasons: single line, secrets, e-mail addresses, phone numbers, URL
    queries/userinfo and long opaque blobs removed. Applied again when the log is exported."""
    s = T.line(text, 2000)                          # one clean line, credentials already scrubbed (T.line does it)
    s = _EMAIL_RE.sub("<addr>", s)
    s = _URLQ_RE.sub(r"\1?<q>", s)
    s = _ACKTOK_RE.sub("<token>", s)                # BEFORE the phone rule: ten digits inside a token must not split it and leave the rest readable
    s = _PHONE_RE.sub("<phone>", s)
    s = _BLOB_RE.sub("<token>", s)
    return s if len(s) <= n else s[:max(n - 3, 0)].rstrip() + "..."


# --------------------------------------------------------------------------- state (budget, dedupe, escalation)
_EMPTY_STATE = {"v": 1, "sent": [], "dedupe": {}, "esc": {}}
_OPTIONAL = ("breaker", "pending", "outbox", "ackfp")  # keys that exist only while they hold something (an idle state file stays minimal)


def _dirs() -> list[Path]:
    """Where notify keeps its state and delivery log, best first. 1. STATE_DIR. 2. RUN_DIR (tmpfs under /run: writable by
    root and still there when / is 100% full). 3. $XDG_RUNTIME_DIR/homelab-maint (a user unit such as stack-alert@, where 1
    and 2 are root-owned; its budgets are then that user's own, not shared with root's). A fallback is never a reason to stop
    paging or to stop counting: the budgets, the dedupe window and the hard cap keep working from whichever is usable."""
    out = [core.STATE_DIR, core.RUN_DIR]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isabs(xdg):
        out.append(Path(xdg) / "homelab-maint")
    return [d for i, d in enumerate(out) if d not in out[:i]]


def _valid(st) -> bool:
    return (isinstance(st, dict) and st.get("v") == 1 and isinstance(st.get("sent"), list)
            and isinstance(st.get("dedupe"), dict) and isinstance(st.get("esc"), dict))


def _load_state(dirs: list[Path]) -> dict:
    """The newest valid state file among the candidate directories (a fallback written during an outage is newer than the
    stale primary, and the next successful write to the primary supersedes it again)."""
    best, best_m = None, -1
    for d in dirs:
        p = d / "notify-state.json"
        try:
            m = p.stat().st_mtime_ns
        except OSError:
            continue
        st = core.read_json(p, None)
        if m > best_m and _valid(st):
            best, best_m = st, m
    return best if best is not None else copy.deepcopy(_EMPTY_STATE)


def _peek_state() -> dict:
    """Read-only view (doctor, health): nothing is created, locked or written."""
    return _load_state(_dirs())


@contextlib.contextmanager
def _state():
    """Exclusive flock + read-modify-write of notify-state.json in the first usable directory (see _dirs). Fails OPEN: if no
    directory can be locked the caller gets an in-memory state and the message is still sent (a silent alert path is
    worse than a repeated one); a write that fails (disk full) moves on to the next directory."""
    dirs, lk, locked = _dirs(), None, None
    for i, d in enumerate(dirs):
        try:
            d.mkdir(parents=True, exist_ok=True)
            lk = open(d / "notify.lock", "a")
            fcntl.flock(lk, fcntl.LOCK_EX)
            locked = i
            break
        except OSError:
            if lk:
                lk.close()
            lk = None
    st = _load_state(dirs) if lk else copy.deepcopy(_EMPTY_STATE)
    before = json.dumps(st, sort_keys=True)
    try:
        yield st
        for k in _OPTIONAL:
            if not st.get(k):
                st.pop(k, None)
        if lk and json.dumps(st, sort_keys=True) != before:
            for d in dirs[locked:]:
                try:
                    core.write_json_atomic(d / "notify-state.json", st, 0o600)
                    break
                except OSError:
                    continue
    finally:
        if lk:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()


def _age(now: float, ts) -> float:
    """Seconds since ts; a timestamp from the future (clock stepped back) counts as expired, not as forever-fresh."""
    try:
        a = now - float(ts)
    except (TypeError, ValueError):
        return float("inf")
    return a if a >= -300 else float("inf")


CLAIM_TTL_S = 600        # a claim is held while the transport runs (<= its timeout); an older one belongs to a dead process


def _prune(st: dict, nc: dict, now: float) -> None:
    """Drop expired rows. A claim (non-empty marker) never committed or rolled back within CLAIM_TTL_S is dropped too,
    so a crash mid-send cannot suppress that alert for the whole dedupe window."""
    st["sent"] = [r for r in st["sent"] if isinstance(r, list) and len(r) >= 4 and _age(now, r[0]) < 86400
                  and not (r[3] and _age(now, r[0]) > CLAIM_TTL_S)]
    st["dedupe"] = {k: v for k, v in st["dedupe"].items() if isinstance(v, dict) and _age(now, v.get("ts")) < 7 * 86400
                    and not (v.get("c") and _age(now, v.get("ts")) > CLAIM_TTL_S)}
    ttl = _num(nc["escalation"].get("episode_ttl_h"), 72) * 3600
    st["esc"] = {k: v for k, v in st["esc"].items() if isinstance(v, dict) and _age(now, v.get("last")) < ttl}
    r = nc["retry"]
    win, tries = _num(r.get("sms_window_s"), 3000), int(_num(r.get("sms_attempts"), 3))
    pend = [p for p in st.get("pending") or [] if isinstance(p, dict) and isinstance(p.get("sms"), str)
            and _age(now, p.get("ts")) < win and int(_num(p.get("n"), 0)) < tries][-10:]
    for p in pend:
        if p.get("claim") and _age(now, p.get("last")) > CLAIM_TTL_S:        # the process that was retrying it died
            p.pop("claim")
    st["pending"] = pend
    live = {f"{p.get('kind')}|{p.get('key')}" for p in pend}
    for k, v in st["dedupe"].items():             # once its text is no longer being retried a page covers nothing special
        if v.get("sp") and k not in live:
            v.pop("sp")
    box = []
    for e in st.get("outbox") or []:              # the outbox: malformed rows go; a claim whose sender died is released; expiry is _flush_outbox's (it logs it)
        if not (isinstance(e, dict) and isinstance(e.get("kind"), str) and isinstance(e.get("key"), str) and isinstance(e.get("snap"), dict)
                and isinstance(e["snap"].get("title"), str)) or _oage(now, e.get("ts")) >= 2 * _num(nc["retry"].get("outbox_ttl_s"), 43200):
            continue
        if e.get("claim") and _age(now, e.get("claim_ts")) > CLAIM_TTL_S:
            e.pop("claim")
            e.pop("claim_ts", None)
        box.append(e)
    st["outbox"] = _cap_outbox(box, nc)[0]
    memo = st.get("ackfp") if isinstance(st.get("ackfp"), dict) else {}      # key -> {"fp","sev","ts"}: which fingerprint an episode's alerts had
    st["ackfp"] = {k: v for k, v in memo.items() if isinstance(k, str) and isinstance(v, dict) and _FP_RE.match(str(v.get("fp") or ""))
                   and _age(now, v.get("ts")) < ACK_MEMO_S}


# --------------------------------------------------------------------------- routing decision (pure over state)
@dataclass
class Decision:
    channels: list = field(default_factory=list)
    skip: str = ""
    handled: bool = False
    why: list = field(default_factory=list)
    key: str = ""
    escalated: bool = False
    notified: int = 0


def _kind_sev(ev: Event) -> tuple[str, str, str]:
    kind = ev.kind if ev.kind in KINDS else "alert"
    facts = ev.facts if isinstance(ev.facts, dict) else {}
    tmpl = kind
    if kind == "test":
        a = facts.get("as")
        tmpl = a if a in KINDS and a != "test" else "alert"
    return kind, tmpl, T.norm_severity(ev.severity, ev.status, tmpl)


def _key(ev: Event) -> str:
    k = redact_log(ev.dedupe_key, 80) or redact_log(ev.task, 60) or re.sub(r"[^a-z0-9]+", "-", str(ev.title or "").lower()).strip("-")[:60]
    return k or "unnamed"


def in_quiet_hours(now: float, qh: dict) -> bool:
    if not qh.get("enabled"):
        return False
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(qh.get("window", "")))
    if not m:
        return False
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    if a == b or max(a, b) > 24 * 60:
        return False
    tz = None
    if qh.get("tz"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(str(qh["tz"]))
        except Exception:                      # unknown zone: fall back to host local time
            tz = None
    t = datetime.fromtimestamp(now, tz)
    mins = t.hour * 60 + t.minute
    return a <= mins < b if a < b else (mins >= a or mins < b)


def _base_route(nc: dict, tmpl: str, sev: str, task: str, facts: dict) -> tuple[set[str], bool, list[str]]:
    r = nc["routes"].get(tmpl)
    if isinstance(r, dict):
        r = r.get(sev, r.get("default"))
    ch = _chan(r)
    why = []
    if ch is None:
        ch = {"email"}
        why.append(f"route {tmpl}.{sev} not understood: email only")
    explicit = False
    if tmpl == "maintenance" and facts.get("significant"):
        extra = _chan(nc["significant"].get("maintenance"))
        if extra:
            ch |= extra
            why.append("significant maintenance")
    if tmpl in ("alert", "incident_open") and task and task in nc["task_routes"]:
        tr = _chan(nc["task_routes"][task])
        if tr is not None:
            ch, explicit = set(tr), True
            why.append(f"task route {task}")
    return ch, explicit, why


def decide(ev: Event, nc: dict, st: dict, now: float) -> Decision:
    """Everything that can stop or reshape a message, as a pure function of (event, config, state, time)."""
    kind, tmpl, sev = _kind_sev(ev)
    facts = ev.facts if isinstance(ev.facts, dict) else {}
    d = Decision(key=_key(ev))
    crit = tmpl in ("alert", "incident_open") and sev == "crit"
    esc = st["esc"].get(d.key) if isinstance(st["esc"].get(d.key), dict) else None
    d.notified = int(esc.get("n", 0)) if esc and kind != "test" else 0

    ch, explicit, why = _base_route(nc, tmpl, sev, str(ev.task or ""), facts)
    if kind == "test" and str(nc["routes"].get("test", "mirror")).lower() != "mirror":
        ch = _chan(nc["routes"]["test"]) or ch
    d.why = [f"route {tmpl}.{sev} -> {'+'.join(c for c in CHANNELS if c in ch) or 'none'}"] + why
    e = nc["escalation"]

    # A warning is email-only until it has been notified `warn_sms_after` times (it persisted through a reminder),
    # then it also texts. facts.escalate forces it. A task route is explicit and is never escalated.
    after = int(_num(e.get("warn_sms_after"), 2))
    if kind != "test" and tmpl in ("alert", "incident_open") and sev == "warn" and "sms" not in ch and not explicit:
        if facts.get("escalate") or (after > 0 and d.notified + 1 >= after):
            ch.add("sms")
            d.escalated = True
            d.why.append(f"warn escalated to sms (notification #{d.notified + 1})")

    # A recovery text only makes sense if the owner was texted about the problem.
    if kind != "test" and tmpl in ("recovery", "incident_resolved") and "sms" in ch and e.get("recovery_sms_only_if_texted", True):
        texted = bool(esc.get("sms")) if esc else (facts.get("was") == "crit" if facts.get("was") else None)
        if texted is False:
            ch.discard("sms")
            d.why.append("recovery: the problem was never texted, no sms")

    mute = core.CONF_DIR / str(nc.get("mute_file") or "NOTIFY_MUTE")
    if kind != "test" and not crit and mute.exists():
        d.skip, d.handled = "muted", True
        d.why.append("NOTIFY_MUTE present")
        return d

    if kind != "test" and not crit and ch and in_quiet_hours(now, nc["quiet_hours"]):
        gone = ch & set(map(str, nc["quiet_hours"].get("suppress") or ["sms"]))
        if gone:
            ch -= gone
            d.why.append(f"quiet hours: dropped {'+'.join(sorted(gone))}")
            if not ch:
                d.skip, d.handled = "quiet-hours", True
                return d

    if not ch:
        d.skip, d.handled = "route-none", True
        return d

    win = _num(nc["dedupe"]["window_s"].get(kind), 0)
    prev = st["dedupe"].get(f"{kind}|{d.key}")
    if kind != "test" and win > 0 and isinstance(prev, dict) and _age(now, prev.get("ts")) < win and prev.get("sev") == sev:
        d.skip, d.handled = "dedupe", True
        d.why.append(f"same {kind}/{sev} for '{d.key}' {int(_age(now, prev.get('ts')) // 60)} min ago (window {int(win // 60)} min)")
        return d

    cw = _num(nc["dedupe"].get("cover_window_s"), 1800)
    rank = {"ok": 0, "info": 1, "warn": 2, "crit": 3}
    covers = nc["dedupe"].get("covered_by")
    for other in ((covers or {}).get(kind) or []) if kind != "test" and isinstance(covers, dict) else []:
        prev = st["dedupe"].get(f"{other}|{d.key}")
        # a page whose text is still being retried ("sp") has not reached the phone: it must not stand in for a later alert
        if isinstance(prev, dict) and not prev.get("sp") and _age(now, prev.get("ts")) < cw and rank.get(prev.get("sev"), 0) >= rank.get(sev, 0):
            d.skip, d.handled = "covered", True
            d.why.append(f"'{d.key}' already sent as {other} {int(_age(now, prev.get('ts')) // 60)} min ago")
            return d

    b = nc["budget"]
    recent = [r for r in st["sent"] if _age(now, r[0]) < 86400]
    if len(recent) >= int(_num(b.get("hard_cap_per_day"), 40)):
        d.skip = "budget"
        d.why.append("hard daily cap reached")
        return d
    if not (crit and b.get("crit_bypass", True)):
        cap = int(_num(b["per_day"].get(kind), 10))
        if sum(1 for r in recent if r[1] == kind) >= cap:
            d.skip = "budget"
            d.why.append(f"daily budget for {kind} reached ({cap})")
            return d
        if len(recent) >= int(_num(b.get("total_per_day"), 30)):
            d.skip = "budget"
            d.why.append("total daily budget reached")
            return d
    if "sms" in ch:
        cap = int(_num(b.get("sms_per_day"), 10)) + (int(_num(b.get("crit_sms_reserve"), 5)) if crit else 0)
        if sum(1 for r in recent if r[2]) >= cap:
            ch.discard("sms")
            d.why.append(f"sms budget reached ({cap}/day): email only")
            if not ch:
                d.skip = "budget"
                return d
    d.channels = [c for c in CHANNELS if c in ch]
    return d


# --------------------------------------------------------------------------- log + export
def _log(rec: dict, nc: dict) -> None:
    """Append one row to the first directory (see _dirs) where the log can be written; never raises."""
    for d in _dirs():
        p = d / "notifications.jsonl"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            continue
        try:
            if p.stat().st_size > int(_num(nc["log"].get("max_bytes"), 2 << 20)):
                keep_s = _num(nc["log"].get("keep_days"), 90) * 86400
                rows = [ln for ln in p.read_text().splitlines() if _line_ts(ln) >= time.time() - keep_s]
                rows = rows[-int(_num(nc["log"].get("max_lines"), 5000)):]
                tmp = p.with_suffix(".tmp")
                tmp.write_text("\n".join(rows) + "\n")
                os.chmod(tmp, 0o644)
                os.replace(tmp, p)
        except OSError:
            pass
        return


def _line_ts(ln: str) -> float:
    try:
        return float(json.loads(ln).get("ts", 0))
    except (ValueError, AttributeError, TypeError):
        return 0.0


def _record(ev: Event, kind: str, sev: str, key: str, d: Delivery, now: float) -> dict:
    rec = {"ts": round(now, 1), "kind": kind, "severity": sev, "title": redact_log(ev.title, 100),
           "channels": d.channels, "ok": d.ok or d.handled, "note": redact_log(d.note, 160), "dedupe_key": key}
    if d.legs:
        rec["legs"] = {k: str(v)[:12] for k, v in d.legs.items()}
    if d.skipped:
        rec["skipped"] = d.skipped
    return rec


def _tail(path: Path, nbytes: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = data.splitlines()
    return rows[1:] if size > nbytes and rows else rows


def _read_log(now: float) -> list[dict]:
    """Delivery-log rows from every directory a row can be in (the fallbacks hold rows written while the primary was
    unwritable), oldest first; garbage lines, non-dicts and rows from the far future are skipped."""
    recs: list[dict] = []
    for d in _dirs():
        for ln in _tail(d / "notifications.jsonl", 2 << 20):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and r["ts"] <= now + 3600:
                recs.append(r)
    recs.sort(key=lambda r: r["ts"])
    return recs


def leg_health(now: float | None = None, window_s: float = 86400) -> dict:
    """Per-channel health from the delivery log, for the meta-monitor (alert_path_health) and the website:
    {"sms": {"ok": n, "fails": n, "last_ok": ts|None, "last_fail": ts|None, "broken": bool}, "email": {...}}.
    A channel is `broken` when its LAST outcome in the window is a failure: a later email-only message never hides a dead
    SMS gateway (the audit's overall "sent" row cannot say which leg worked)."""
    now = time.time() if now is None else float(now)
    out = {c: {"ok": 0, "fails": 0, "last_ok": None, "last_fail": None, "broken": False} for c in CHANNELS}
    last: dict[str, str] = {}
    for r in _read_log(now):
        if now - r["ts"] > window_s:
            continue
        for leg, word in (r.get("legs") or {}).items() if isinstance(r.get("legs"), dict) else ():
            if leg not in out or word not in ("sent", "failed"):
                continue
            o = out[leg]
            if word == "sent":
                o["ok"] += 1
                o["last_ok"], last[leg] = r["ts"], "ok"
            else:
                o["fails"] += 1
                o["last_fail"], last[leg] = r["ts"], "fail"
    for leg, o in out.items():
        o["broken"] = last.get(leg) == "fail"
    return out


def export(now: float | None = None) -> dict:
    """notifications.json for the website: last 100 deliveries (redacted again), counts for 24 h / 7 d, failures."""
    now = time.time() if now is None else float(now)
    recs = _read_log(now)

    def row(r: dict) -> dict:
        out = {"ts": r["ts"], "kind": redact_log(r.get("kind"), 24), "severity": redact_log(r.get("severity"), 8),
               "title": redact_log(r.get("title"), 100), "ok": bool(r.get("ok")),
               "channels": [c for c in (r.get("channels") or []) if c in CHANNELS], "note": redact_log(r.get("note"), 160)}
        if r.get("skipped"):
            out["skipped"] = redact_log(r["skipped"], 20)
        return out

    counts: dict[str, dict] = {}
    for label, span in (("24h", 86400), ("7d", 7 * 86400)):
        win = [r for r in recs if now - r["ts"] <= span]
        counts[label] = {"total": len(win),
                         "sent": sum(1 for r in win if r.get("channels") and r.get("ok")),
                         "failed": sum(1 for r in win if not r.get("ok")),
                         "skipped": sum(1 for r in win if r.get("skipped")),
                         "suppressed": sum(1 for r in win if r.get("skipped") == "acknowledged"),
                         "sms": sum(1 for r in win if "sms" in (r.get("channels") or [])),
                         "email": sum(1 for r in win if "email" in (r.get("channels") or []))}
    fails = [r for r in recs if not r.get("ok") or any(v == "failed" for v in (r.get("legs") or {}).values())]
    by_kind: dict[str, int] = {}
    for r in recs:
        if now - r["ts"] <= 86400 and r.get("channels"):
            by_kind[redact_log(r.get("kind"), 24)] = by_kind.get(redact_log(r.get("kind"), 24), 0) + 1
    st = _peek_state()
    box = [e for e in st.get("outbox") or [] if isinstance(e, dict)]
    waiting = {"pages": sum(1 for e in box if e.get("kind") not in NOTICE_KINDS),
               "texts": sum(1 for p in st.get("pending") or [] if isinstance(p, dict))}          # counts only: no bodies, no titles
    if any(e.get("kind") in NOTICE_KINDS for e in box):
        waiting["notices"] = sum(1 for e in box if e.get("kind") in NOTICE_KINDS)
    return {"schema": 1, "generated_at": now, "recent": [row(r) for r in recs[-100:]][::-1], "counts": counts, "waiting": waiting,
            "failures": {"24h": sum(1 for r in fails if now - r["ts"] <= 86400),
                         "7d": sum(1 for r in fails if now - r["ts"] <= 7 * 86400),
                         "last": row(fails[-1]) if fails else None},
            "by_kind_24h": by_kind, "legs": leg_health(now)}


# --------------------------------------------------------------------------- transports
_NOTE_PATTERNS = (
    ("sent", re.compile(r"(sms|email) sent\b", re.I)),
    ("failed", re.compile(r"(sms|email) (?:FAILED|NOT SENT)\b", re.I)),
    ("skipped", re.compile(r"(sms|email) skipped\b", re.I)),
    ("not-requested", re.compile(r"(sms|email) not requested\b", re.I)),
)


def reduce_notes(notes, wanted=()) -> dict:
    """send_report's free-text notes (they contain phone numbers, addresses and the SMS body) -> per-channel
    outcome words plus short REDACTED reasons. The raw notes are discarded here and never logged or returned."""
    legs: dict[str, str] = {}
    errors: dict[str, str] = {}
    fatal = ""
    joined = ""
    for raw in notes or []:
        n = " ".join(str(raw).split())
        joined += " " + n
        if n.lower().startswith("no transport configured"):
            fatal = "no transport configured (alert_transports.env missing)"
        for word, pat in _NOTE_PATTERNS:
            m = pat.match(n)
            if m and m.group(1).lower() not in legs:
                leg = m.group(1).lower()
                legs[leg] = word
                if word in ("failed", "skipped"):
                    errors[leg] = redact_log(n.split(":", 1)[1] if ":" in n else n.split(" ", 2)[-1], 100)
                elif "UNVERIFIABLE" in n:
                    errors[leg] = "unverifiable domain (no MX record)"
                break
    for leg in wanted or ():
        if leg not in legs:
            legs[leg] = "skipped" if "not enabled in ALERT_CHANNELS" in joined else "unknown"
            if legs[leg] == "skipped":
                errors[leg] = "channel not enabled in ALERT_CHANNELS"
    return {"ok": any(v == "sent" for v in legs.values()), "legs": legs, "errors": errors, "fatal": fatal}


def _home(user: str) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return f"/home/{user}"


def _as_user(user: str, argv: list[str]) -> list[str]:
    """Root jobs must run Hermes as the owner: its config resolves through ~ohmz. Non-root runs directly."""
    return ["runuser", "-u", user, "--"] + argv if (user and user != "root" and os.geteuid() == 0) else argv


def _run(cmd: list[str], timeout: int, input_: str | None, env: dict):
    import subprocess
    if "PYTEST_CURRENT_TEST" in os.environ and sh is _REAL_SH:           # a test must never reach the real world
        return subprocess.CompletedProcess(cmd, 126, "", "real transport blocked under pytest")
    return sh(cmd, timeout=timeout, input_=input_, env=env)


def _parse_child(r) -> TransportResult:
    if r.returncode == 124:
        return TransportResult(False, fatal="transport timed out", rc=124, via="hermes")
    data = None
    for ln in reversed((r.stdout or "").strip().splitlines()):
        try:
            data = json.loads(ln)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        tail = redact_log((r.stderr or r.stdout or "")[-300:] or f"child exited rc={r.returncode} without a result", 140)
        return TransportResult(False, fatal=tail, rc=r.returncode, via="hermes")
    legs = {k: str(v) for k, v in (data.get("legs") or {}).items() if k in CHANNELS}
    return TransportResult(bool(data.get("ok")), legs, {k: redact_log(v, 100) for k, v in (data.get("errors") or {}).items()},
                           redact_log(data.get("fatal"), 140), r.returncode, "hermes")


def hermes_transport(msg: Message, nc: dict) -> TransportResult:
    """Primary transport: Hermes `alert_transports.send_report` run as the handle's user in a small child."""
    t = nc["transport"]
    if not _USER_RE.match(t["user"]) or not _USER_RE.match(t["handle"]):
        return TransportResult(False, fatal="invalid transport user/handle in notify.toml", via="hermes")
    payload = json.dumps({"handle": t["handle"], "sms": msg.sms, "subject": msg.subject, "plain": msg.plain,
                          "html": msg.html or "", "channels": msg.channels, "scripts_dir": t["scripts_dir"]})
    cmd = _as_user(t["user"], [sys.executable or "/usr/bin/python3", "-m", "homelab_maint.notify", "--child"])
    env = {"HOME": _home(t["user"]), "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return _parse_child(_run(cmd, int(_num(t["timeout_s"], 90)), payload, env))


def bridge_transport(msg: Message, nc: dict) -> TransportResult:
    """Fallback: the legacy bridge the backups use. Plain text only; it sends on every channel Hermes enables."""
    import tempfile
    t = nc["transport"]
    if not _USER_RE.match(t["user"]) or not _USER_RE.match(t["handle"]) or not os.path.isabs(t["bridge"]):
        return TransportResult(False, fatal="invalid bridge settings in notify.toml", via="bridge")
    detail = None
    try:
        core.RUN_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=core.RUN_DIR, prefix="notify-", suffix=".txt", delete=False) as f:
            # a 0644 file for a moment: never the acknowledge link (plain_safe is the copy without it; if that could not be built, cut the URL out)
            f.write(msg.plain_safe or (msg.plain.replace(msg.ack_url, "<link omitted>") if msg.ack_url else msg.plain))
            detail = f.name
        os.chmod(detail, 0o644)                  # the bridge runs as the handle's user
        cmd = _as_user(t["user"], [t["bridge"], t["handle"], msg.subject, msg.sms, detail])
        r = _run(cmd, int(_num(t["timeout_s"], 90)), None, {"HOME": _home(t["user"])})
    except OSError as exc:
        return TransportResult(False, fatal=redact_log(f"bridge setup failed: {exc.strerror}", 120), via="bridge")
    finally:
        if detail:
            with contextlib.suppress(OSError):
                os.unlink(detail)
    if r.returncode == 124:
        return TransportResult(False, fatal="bridge timed out", rc=124, via="bridge")
    red = reduce_notes((r.stdout or "").splitlines(), ())
    fatal = "" if red["legs"] else redact_log((r.stderr or r.stdout or "")[-200:] or f"bridge rc={r.returncode}", 140)
    return TransportResult(r.returncode == 0 and red["ok"], red["legs"], red["errors"], fatal or red["fatal"], r.returncode, "bridge")


def child_main(stdin=None, stdout=None, send_report: Callable | None = None) -> int:
    """`--child` entry, runs as the handle's user. One JSON message in, one JSON result out; never raises."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        data = json.loads(stdin.read(4_000_000))
        channels = [c for c in data.get("channels", []) if c in CHANNELS]
        if send_report is None:
            os.environ["HOME"] = _home(pwd.getpwuid(os.geteuid()).pw_name)    # alert_transports expands ~ at import
            sd = str(data.get("scripts_dir") or "")
            if sd:
                sys.path.insert(0, sd)
            import alert_transports as at                                     # noqa: PLC0415 (the owner's own code)
            send_report = at.send_report
        ok, notes = send_report(str(data["handle"]), str(data.get("sms", "")), str(data.get("subject", "")),
                                str(data.get("plain", "")), html=data.get("html") or None, channels=channels)
        res = reduce_notes(notes, channels)
        res["ok"] = bool(ok)
    except Exception as exc:                                                   # noqa: BLE001
        res = {"ok": False, "legs": {}, "errors": {}, "fatal": redact_log(f"{type(exc).__name__}: {exc}", 140)}
    stdout.write(json.dumps(res) + "\n")
    return 0 if res["ok"] else 1


# --------------------------------------------------------------------------- send
def _audit(*a) -> None:
    core.audit(*a)


def playbook_lines(task: str) -> list[str]:
    """"What to do" for an alert from etc/playbooks.toml (incidents.playbook_for): the first checks, the first safe fixes
    and one thing to avoid. A `$ ` command line becomes "Run: ...". Never raises; no playbook module means no lines."""
    try:
        from . import incidents
        pb = incidents.playbook_for(task)
    except Exception:                                                          # noqa: BLE001
        return []

    def one(prefix: str, x: str) -> str:
        return f"Run: {x[2:]}" if x.startswith("$ ") else f"{prefix}{x}"
    out = [one("", x) for x in pb.get("checks", [])[:3]] + [one("Fix: ", x) for x in pb.get("fixes", [])[:2]]
    out += [x if re.match(r"(?i)(do not|don't|never)\b", x) else f"Avoid: {x}" for x in pb.get("avoid", [])[:1]]
    return [x for x in out if x.strip()]


playbook_loader: Callable[[str], list[str]] = playbook_lines      # tests / other sources replace this


def _todo_for(ev: Event, nc: dict):
    """Precedence: details.todo (the caller, applied in templates) > notify.toml [todo.<task>] > the task's playbook >
    notify.toml [todo] _default."""
    task = str(ev.task or "")
    if task and nc["todo"].get(task):
        return nc["todo"][task]
    try:
        lines_ = playbook_loader(task) if task else []
    except Exception:                                                          # noqa: BLE001
        lines_ = []
    return lines_ or nc["todo"].get("_default")


# --------------------------------------------------------------------------- acknowledged issues (SPEC5)
ACK_SUPPRESS = ("alert", "recovery", "incident_open", "incident_resolved")      # the ONLY kinds an acknowledgement may silence
ACK_BUTTON = ("alert", "incident_open", "ack_expired")                           # the ONLY kinds that may carry an Acknowledge button
ACK_MEMO_S = 60 * 86400          # how long an episode's fingerprint is remembered without news (a held reminder refreshes it daily)
ACK_LOG_S = 3600                 # a held message is logged/audited at most once an hour per kind and key
_FP_RE = re.compile(r"[0-9a-f]{16}\Z")


def acks_loader():
    """The acknowledgements module (homelab_maint/acks.py) or None when it is absent or broken. None means exactly the behaviour
    before this feature: nothing is held and no button is offered. Tests and other sources replace this function."""
    try:
        from . import acks
        return acks
    except Exception:                                                          # noqa: BLE001 (ImportError, a SyntaxError in acks, ...)
        return None


def _as_list(v) -> list[str]:
    return [str(x).strip() for x in v] if isinstance(v, (list, tuple)) else [x.strip() for x in str(v or "").replace("+", ",").split(",")]


def ack_cfg(nc: dict) -> dict:
    """[ack] with every value clamped and the kind lists cut down to the whitelists, so a typo can only mean "less", never "more"."""
    a = nc.get("ack") if isinstance(nc.get("ack"), dict) else {}
    site = nc.get("site") if isinstance(nc.get("site"), dict) else {}
    b = a.get("button", "auto")
    b = b.strip().lower() if isinstance(b, str) else b
    return {"enabled": a.get("enabled", True) is True,                        # only a real `true`: "false", 0 or a typo disables
            "base": str(a.get("base_url") or site.get("url") or ""),
            # Ohmz fork: when mint_url is set the acknowledgement token is minted by the NEW site (the
            # hub), not here; the request is signed with key_file (ack/web.key, the runner's shared secret).
            "mint_url": str(a.get("mint_url") or ""),
            "key_file": str(a.get("key_file") or "/var/lib/homelab-maint/ack/web.key"),
            "button_mode": "on" if b is True or b in ("true", "on", "yes", "1") else "auto" if b == "auto" else "off",     # a typo: no button
            "ttl": int(min(max(_num(a.get("token_ttl_days"), 30), 1), 90)),
            "days": int(min(max(_num(a.get("days"), 90), 1), 365)),
            "text": str(a.get("button_text") or T.ACK_DEFAULT_TEXT),
            "escalates": a.get("escalation_breaks", True) is not False,
            "suppress": tuple(k for k in _as_list(a.get("suppress_kinds")) if k in ACK_SUPPRESS),
            "button": tuple(k for k in _as_list(a.get("button_kinds")) if k in ACK_BUTTON),
            "require_rule": a.get("require_rule", True) is not False,         # only a real `false` opens the number-blind fallback to every task
            "allow": tuple(x for x in _as_list(a.get("allow_tasks")) if x),
            "deny": tuple(x for x in _as_list(a.get("deny_tasks")) if x),
            "deny_prefix": tuple(x for x in _as_list(a.get("deny_prefixes")) if x),
            "group_over": int(min(max(_num(a.get("notice_group_over"), 2), 1), 50))}


def web_ready() -> bool:
    """Does the maintenance site serve the confirmation page the button opens? ack_web's deploy drops STATE_DIR/ack/web_ready
    (a plain file); a link to a site without /ack would be a 404 in the middle of a page."""
    try:
        return (core.STATE_DIR / "ack" / "web_ready").is_file()
    except OSError:
        return False


def button_on(cfg: dict) -> bool:
    return cfg["button_mode"] == "on" or (cfg["button_mode"] == "auto" and web_ready())


def _fp_str(x) -> str:
    """A fingerprint as 16 lowercase hex characters (a str, or an object with .fp / .id), else "": a doubt is no fingerprint."""
    s = x if isinstance(x, str) else getattr(x, "fp", None) or getattr(x, "id", None) or ""
    s = str(s).strip().lower()
    return s if _FP_RE.match(s) else ""


def _ack_sev(sev) -> str:
    """The severity an acknowledgement is compared at: crit stays crit, info and warn are warn, ok (a recovery) has none."""
    s = str(sev or "").strip().lower()
    return "crit" if s == "crit" else "warn" if s in ("warn", "info") else ""


def _ack_sev_ok(mod, sev) -> bool:
    """May an issue at this severity be acknowledged at all (acks [ack] severities, warn only by default)? True also when the module or the
    severity is unknown: the runner still refuses a non-acknowledgeable request, this only keeps the WORDING of an "ended" notice honest."""
    f = getattr(mod, "severity_allowed", None)
    if not callable(f) or not sev:
        return True
    try:
        return bool(f(sev))
    except Exception:  # noqa: BLE001
        return True


def _held_until(info) -> float:
    """When the acknowledgement ends, from whatever is_acked returned (a dict or an object): a finite number, else 0 (= not held)."""
    v = info.get("until") if isinstance(info, dict) else getattr(info, "until", None)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    return float(v) if v == v and 0 < v < 1e12 else 0.0


@dataclass
class AckHold:
    fp: str = ""                   # the event's fingerprint ("" = it cannot be acknowledged)
    sev: str = ""                  # the severity it was compared at (warn | crit)
    until: float = 0.0             # > 0: an acknowledgement is in force until then and the message is held


def _denied(task: str, cfg: dict) -> bool:
    """A task the owner (or the built-in default) never lets an acknowledgement touch: smart_event (a smartd hook whose text carries the
    severity in its numbers) and the `job:*` keys of the scheduler. The deny list beats everything, the rules and `allow_tasks` included."""
    return task in cfg["deny"] or any(task.startswith(p) for p in cfg["deny_prefix"])


def _task_denied(mod, task: str, cfg: dict) -> bool:
    """Is this task on the deny list that beats everything? The acks module's (ack.toml) when it has `ackable`: "explicit" is its most
    permissive mode, so only a deny entry (or an unusable name) says no to it. Else notify.toml's own copy."""
    ackable = getattr(mod, "ackable", None)
    if callable(ackable):
        try:
            return not ackable(task, "explicit")
        except Exception:                                                      # noqa: BLE001
            return True
    return _denied(task, cfg)


def _ack_allowed(mod, task: str, fpobj, cfg: dict) -> bool:
    """May this task's alert be offered a button, and held once acknowledged? Only when its fingerprint names THE ERROR and not just
    its wording: a Result.issue_key (mode "explicit"), a deliberate per-task rule in etc/ack.toml ("task", "regex", or "text" with a
    [key.<task>] table), or an explicit `allow_tasks` entry. The fallback for every other task is the number-blind text of the summary,
    which keeps the same fingerprint (and the same severity) while a WAL, a disk or a failure count grows 10x: such an acknowledgement
    would silence a worsening problem for the whole period, so it is neither offered nor honoured here (`require_rule = false` lifts it).
    Any doubt (no mode, a rules file that cannot be read) is "no". Rules that ARE number-blind by design (a disk's mount names, a
    unit's name) stay the accepted trade-off of SPEC5 S2: the acknowledgement says "I know about this exact error", not "at this size".
    ONE POLICY: when the acks module has `ackable` (the production one), the answer is ack.toml's, the same as the dashboard, the inbox, the
    CLI and status.json use, so the site never says "acknowledged" while the alert pages. The notify.toml [ack] policy keys below are only
    the fallback for a module without it (deprecated: ack.toml [ack] decides)."""
    if not task:
        return False
    ackable = getattr(mod, "ackable", None)
    if callable(ackable):
        try:
            return bool(ackable(task, fpobj))
        except Exception:                                                      # noqa: BLE001
            return False
    if _denied(task, cfg):
        return False
    if not cfg["require_rule"] or task in cfg["allow"]:
        return True
    mode = str(getattr(fpobj, "mode", "") or "")
    if mode in ("explicit", "task", "regex"):
        return True
    if mode == "text":
        try:
            return task in (mod.load_config().get("key") or {})
        except Exception:                                                      # noqa: BLE001
            return False
    return False


def _event_fp(mod, ev: Event, sev: str, facts: dict, cfg: dict) -> str:
    """The fingerprint this event may be acknowledged by, else "". facts.ack_fp (the caller had the Result and applied the same policy,
    see _result_fp), else acks.fingerprint(task, summary, severity) if the task passes _ack_allowed. An ack_expired notice must bring
    its own: its summary is not the error's. A denied task has none, whatever the caller supplied."""
    task = str(ev.task or "").strip()
    if task and _task_denied(mod, task, cfg):
        return ""
    pre = _fp_str(facts.get("ack_fp"))
    if pre or ev.kind == "ack_expired":
        return pre
    if not task:
        return ""
    fo = mod.fingerprint(task, str(ev.summary or ""), _ack_sev(sev))
    return _fp_str(fo) if _ack_allowed(mod, task, fo, cfg) else ""


ACK_MODES = ("explicit", "task", "regex", "text")


def _fp_mode(mod, ev: Event, sev: str, facts: dict) -> str | None:
    """How the fingerprint of this event was made (Fp.mode), for issue_token's policy check, so an explicit-key task keeps its button:
    facts.ack_mode when the caller had the Result (HermesNotifier), else the mode of the fingerprint recomputed from the summary. None =
    unknown (a caller-supplied id without its mode): issue_token then looks the failing issue up itself."""
    m = str(facts.get("ack_mode") or "")
    if m in ACK_MODES:
        return m
    if _fp_str(facts.get("ack_fp")):
        return None
    try:
        return str(getattr(mod.fingerprint(str(ev.task or "").strip(), str(ev.summary or ""), _ack_sev(sev)), "mode", "") or "") or None
    except Exception:                                                          # noqa: BLE001
        return None


def _ack_gate(mod, ev: Event, st: dict, tmpl: str, sev: str, key: str, now: float, cfg: dict) -> AckHold:
    """Is this message about an acknowledged issue? Alerts and incidents: by their fingerprint. A recovery or a resolved incident has
    no error text of its own: it is held when the alerts of THAT episode (remembered per key in state["ackfp"]) are, at the severity
    they last had. Any exception, odd value or expired acknowledgement means "not held": it fails closed (the message is sent)."""
    try:
        facts = ev.facts if isinstance(ev.facts, dict) else {}
        if tmpl in ("alert", "incident_open"):
            fp, asev = _event_fp(mod, ev, sev, facts, cfg), _ack_sev(sev)
        else:
            memo = st["ackfp"].get(key) if isinstance(st.get("ackfp"), dict) else None
            fp, asev = (_fp_str(memo.get("fp")), _ack_sev(memo.get("sev"))) if isinstance(memo, dict) else ("", "")
        if not fp or not asev:
            return AckHold()
        info = mod.is_acked(fp, asev, now)
        until = _held_until(info) if info else 0.0
        return AckHold(fp, asev, until if until > now else 0.0)
    except Exception:                                                          # noqa: BLE001
        return AckHold()


def _ack_check(ev: Event, nc: dict, kind: str, tmpl: str, sev: str, now: float, dry_run: bool) -> tuple[Delivery | None, AckHold]:
    """The hold, BEFORE routing, dedupe, budgets and the claim: a held message costs nothing (no budget, no dedupe slot, no escalation
    count, no SMS) and the Delivery says why. Returns (Delivery, hold) when held, else (None, hold) where hold.fp is the fingerprint to
    remember if the alert is delivered. Without an acks module this does not even take the state lock."""
    cfg = ack_cfg(nc)
    facts = ev.facts if isinstance(ev.facts, dict) else {}
    if kind == "test" or not cfg["enabled"] or tmpl not in cfg["suppress"] or ("ack" in facts and not T.truthy(facts["ack"])):
        return None, AckHold()
    mod = acks_loader()
    if mod is None:
        return None, AckHold()
    key, say = _key(ev), False
    with _state() as st:
        _prune(st, nc, now)
        hold = _ack_gate(mod, ev, st, tmpl, sev, key, now, cfg)
        if not hold.until:
            return None, hold
        when = time.strftime("%Y-%m-%d", time.localtime(hold.until))
        d = Delivery(kind=kind, severity=sev, ok=False, handled=True, skipped="acknowledged", dedupe_key=key,
                     note=f"suppressed: acknowledged until {when}", why=[f"acknowledged until {when} (fingerprint {hold.fp})"])
        if dry_run:
            return d, hold
        if tmpl in ("alert", "incident_open"):
            st.setdefault("ackfp", {})[key] = {"fp": hold.fp, "sev": hold.sev, "ts": now, "held": 1}     # its recovery is held too; `held`: a later
            #                                                         release (un-acknowledge, expiry) must re-open core.Notifier's "already alerted" state
        else:                                                                  # the episode ends here, quietly: forget it like a delivery would
            st["ackfp"].pop(key, None)
            st["esc"].pop(key, None)
            for k in ("alert", "incident_open"):
                st["dedupe"].pop(f"{k}|{key}", None)
            _drop_pending(st, key)
        slot = st["dedupe"].get(f"ack|{kind}|{key}")
        say = not (isinstance(slot, dict) and _age(now, slot.get("ts")) < ACK_LOG_S)
        if say:
            st["dedupe"][f"ack|{kind}|{key}"] = {"ts": now, "sev": sev}
            _log(_record(ev, kind, sev, key, d, now), nc)
    with contextlib.suppress(Exception):
        mod.record_suppressed(hold.fp, now)                                    # the counter on the acknowledged issue, outside our lock
    if say:
        _audit("notify", "suppressed", f"{kind}: {redact_log(ev.title, 60)}", 0, f"acknowledged until {when}")
    return d, hold


def _link_days(mod, cfg: dict) -> int:
    """The `d=` the link carries (SPEC5 S8): [ack] days snapped DOWN to a length the site accepts (7/30/90/365) and capped by the runner's own
    [ack] max_days, so a link can keep or lower the default and never ask for more than the inbox will take. 0 = no allowed length fits inside
    what the runner accepts (min_days..max_days): then there is no link at all, because the request it makes would be refused every time."""
    lo, hi = 1, 365
    try:
        a = mod.load_config()["ack"]
        lo, hi = int(_num(a.get("min_days"), 1)), int(_num(a.get("max_days"), 365))
    except Exception:                                                          # noqa: BLE001 (a module without a config: the site's own range)
        pass
    d = T.snap_days(min(cfg["days"], hi))
    return d if lo <= d <= hi else 0


def _mint_via_hub(cfg: dict, fp: str, sev: str, days: int, title: str, notes: list[str]) -> str:
    """Ask the NEW site (the hub) to mint an acknowledgement token (Ohmz fork). The request is signed with
    ack/web.key (HMAC-SHA256 over the exact body), the same secret the runner already shares, so only the
    engine and the hub can mint. Any failure returns "" and the caller sends the page without the button,
    exactly as it does when a local mint fails -- a broken hub never blocks an alert."""
    try:
        body = json.dumps({"fp": fp, "sev": sev, "days": days, "host": socket.gethostname(),
                           "title": T.line(title, 100)}, separators=(",", ":")).encode()
        key = Path(cfg["key_file"]).read_bytes()
        sig = hmac.new(key, body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(cfg["mint_url"], data=body,
                                     headers={"Content-Type": "application/json", "X-HM-Signature": sig})
        with urllib.request.urlopen(req, timeout=5) as r:
            tok = json.loads(r.read().decode("utf-8", "replace")).get("token", "")
        return tok if isinstance(tok, str) and tok else ""
    except Exception as exc:                                                   # noqa: BLE001 (never lose an alert because the hub is down)
        notes.append(f"ack link unavailable (hub mint failed: {type(exc).__name__}): sent without the Acknowledge button")
        return ""


def _ack_offer(ev: Event, nc: dict, dec: "Decision", now: float, preview: bool, notes: list[str]) -> dict | None:
    """The Acknowledge button for this email: {"url", "id", "days", ...} for T.prepare, or None. Only an email that is really going out
    gets a real token (issued here, one per email); a preview, a dry run and a TEST get the inert placeholder. Anything unusual (no
    module, the module raising, an unusable token or base URL) means no button and the page is sent as before, with a note in the log;
    the Issue ID is still printed when the fingerprint is known (it is not a secret: it is what `homelab-maint ack add` takes).
    The button itself waits for the site (button_on: [ack] button = auto|true|false): until /ack exists the email carries the Issue ID
    line only, and no token is minted. A task that fails _ack_allowed has neither the button nor an Issue ID."""
    cfg = ack_cfg(nc)
    kind, tmpl, sev = _kind_sev(ev)
    facts = ev.facts if isinstance(ev.facts, dict) else {}
    asev = _ack_sev(sev)
    if (not cfg["enabled"] or "email" not in dec.channels or tmpl not in cfg["button"] or not asev or ("ack" in facts and not T.truthy(facts["ack"]))
            or (tmpl == "ack_expired" and not T.truthy(facts.get("still_failing")))):
        return None
    mod = acks_loader()
    if mod is None:
        return None
    sev_ok = getattr(mod, "severity_allowed", None)                            # acks' [ack] severities: crit always alerts, no button
    if callable(sev_ok):
        try:
            if not sev_ok(asev):
                return None
        except Exception:                                                      # noqa: BLE001 (no button, never a button that cannot work)
            return None
    try:
        fp = _event_fp(mod, ev, sev, facts, cfg)
    except Exception:                                                          # noqa: BLE001
        fp = ""
    if not button_on(cfg):                                                     # the site cannot take the click yet: text only
        return {"id": fp, "url": ""} if fp else None
    if not fp and str(ev.task or "").strip() and (preview or kind == "test"):  # a task that may not be acknowledged: its preview shows no button either
        return None
    days = _link_days(mod, cfg)                                                # what `d=` will say: never more than the runner accepts
    if not days:
        notes.append("ack link unavailable ([ack] days / the runner's min_days..max_days leave no allowed length): sent without the Acknowledge button")
        return {"id": fp, "url": ""} if fp else None
    if preview or kind == "test":                                              # inert: a placeholder token, never issued, never valid
        url = T.ack_url(cfg["base"], T.PREVIEW_TOKEN, fp or T.PREVIEW_ID, days, asev)
        return {"url": url, "id": fp or T.PREVIEW_ID, "days": days, "ttl_days": cfg["ttl"], "text": cfg["text"],
                "escalates": cfg["escalates"], "demo": True} if url else None
    if not fp:                                                                 # nothing to fingerprint: nothing to acknowledge
        return None
    if cfg.get("mint_url"):                                                    # the NEW site mints the token
        token = _mint_via_hub(cfg, fp, asev, days, str(ev.title or ""), notes)
        if not token:
            return {"id": fp, "url": ""}
    else:
        try:
            token = mod.issue_token(fp, str(ev.task or ""), T.line(ev.title, 100), T.line(facts.get("ack_summary") or ev.summary, 200),
                                    asev, now, ttl_days=cfg["ttl"], mode=_fp_mode(mod, ev, sev, facts))
        except Exception as exc:                                               # noqa: BLE001
            notes.append(f"ack link unavailable ({type(exc).__name__}): sent without the Acknowledge button")
            return {"id": fp, "url": ""}
    url = T.ack_url(cfg["base"], token, fp, days, asev)
    if not url:
        notes.append("ack link unavailable (unusable token or [ack] base_url): sent without the Acknowledge button")
        return {"id": fp, "url": ""}
    return {"url": url, "id": fp, "days": days, "ttl_days": cfg["ttl"], "text": cfg["text"], "escalates": cfg["escalates"], "demo": False}


def _hold_released(key: str, nc: dict, now: float, cur_fp: str = "") -> bool:
    """Were the alerts of this key held for an acknowledgement (notify-state.json ackfp[key].held) that is no longer in force? Read-only:
    the caller clears the mark with _clear_held once its own state is saved. Still in force, or nothing was ever held: False.
    `cur_fp` is the fingerprint of the problem as it fails NOW: a different one (a decade worse, a longer list, another unit) is another
    error than the one acknowledged, released at once even at the same severity; core counts a held alert as sent, so without this a
    worsening would wait for the 24 h reminder. "" (unknown) compares nothing."""
    memo = (_peek_state().get("ackfp") or {}).get(key)
    if not (isinstance(memo, dict) and memo.get("held")):
        return False
    if cur_fp and cur_fp != _fp_str(memo.get("fp")):
        return True
    cfg, mod = ack_cfg(nc), acks_loader()
    if mod is not None and cfg["enabled"]:
        try:
            info = mod.is_acked(_fp_str(memo.get("fp")), _ack_sev(memo.get("sev")), now)
            return not (bool(info) and _held_until(info) > now)
        except Exception:                                                      # noqa: BLE001
            return True
    return True


def _clear_held(key: str) -> None:
    with _state() as st:
        m = (st.get("ackfp") or {}).get(key)
        if isinstance(m, dict):
            m.pop("held", None)


def _forget_alert_dedupe(key: str) -> None:
    """The hold ended (un-acknowledged, expired, or another error): the alert that re-opens must not be swallowed by the 6 h window of a page that
    went out BEFORE the acknowledgement (a short reminder interval, or a flap, puts one inside it), or "alerting resumes at once" would be a lie."""
    with _state() as st:
        (st.get("dedupe") or {}).pop(f"alert|{key}", None)


def _revoke_unused_token(msg: Message, res: TransportResult, now: float) -> None:
    """The token in msg.ack_url went into an email. If that email certainly did NOT go out (the email leg failed or was skipped, nothing
    was delivered, or the legacy bridge sent its link-free copy instead), the token is revoked: an unsent link is a valid bearer
    token nobody holds, and every retry would otherwise leave another behind. A timeout may have half-sent it, and a transport that
    says ok without naming legs is assumed to have delivered: both keep it (a dead button in a mail the owner did get is worse)."""
    if not msg.ack_url:
        return
    went = res.via != "bridge" and (res.legs.get("email") == "sent" or (res.ok and not res.legs))
    if went or res.rc == 124:
        return
    m = T._ACK_URL_RE.match(msg.ack_url)
    mod = acks_loader()
    if m is None or mod is None or not callable(getattr(mod, "revoke_token", None)):
        return
    with contextlib.suppress(Exception):
        mod.revoke_token(m[2], now)


def _result_ack(name: str, res, level: int, nc: dict | None = None) -> tuple[str, str]:
    """HermesNotifier: (fingerprint, mode) of a confirmed problem, from the task's Result (it knows issue_key), else ("", ""). The same
    policy as _event_fp: a task that is denied, or has neither an issue_key nor a rule, gets none (and so no button and no hold)."""
    mod = acks_loader()
    if mod is None or res is None or level <= 0:
        return "", ""
    try:
        sev = "crit" if level >= 2 else "warn"
        fo = mod.fingerprint(name, res, sev)
        if not _ack_allowed(mod, str(name or "").strip(), fo, ack_cfg(nc or load_config())):
            return "", ""
        mode = str(getattr(fo, "mode", "") or "")
        return _fp_str(fo), mode if mode in ACK_MODES else ""
    except Exception:                                                          # noqa: BLE001
        return "", ""


def _result_fp(name: str, res, level: int, nc: dict | None = None) -> str:
    return _result_ack(name, res, level, nc)[0]


def _render(ev: Event, nc: dict, dec: Decision, now: float, preview: bool = True) -> tuple[Message, list[str]]:
    """SMS, subject, plain, HTML. Each part is guarded: a failing HTML renderer sends plain text, a failing
    anything-else falls back to a bare title/summary. An unreadable message beats none.
    `preview` (the default, so a caller that forgets it cannot spend a token): previews, dry runs and tests show the inert placeholder
    link; only _send's real delivery passes preview=False and gets a freshly issued token (one per email) in plain and html."""
    site, notes = nc["site"], []
    sms = subject = plain = html_ = plain_safe = ""
    todo = _todo_for(ev, nc)
    m = None
    ack_url = ""
    offer = None
    try:
        offer = _ack_offer(ev, nc, dec, now, preview, notes)
    except Exception as exc:                                                   # noqa: BLE001 (no button is better than no alert)
        notes.append(f"ack offer failed ({type(exc).__name__}): sent without the Acknowledge button")
    try:
        m = T.prepare(ev, site=site, todo_default=todo, now=now, ack=offer)
        m.notified = dec.notified + 1 if (dec.notified or dec.escalated) else 0
        if offer and not offer.get("demo"):
            ack_url = m.ack_url
    except Exception as exc:                                                   # noqa: BLE001
        notes.append(f"prepare failed ({type(exc).__name__})")
    for name, fn in (("sms", lambda: T.build_sms(m, site["sms_prefix"])),
                     ("subject", lambda: T.build_subject(m, site["subject_prefix"])),
                     ("plain", lambda: T.build_plain(m))):
        try:
            val = fn()
        except Exception as exc:                                               # noqa: BLE001
            val = ""
            notes.append(f"{name} render failed ({type(exc).__name__})")
        if name == "sms":
            sms = val
        elif name == "subject":
            subject = val
        else:
            plain = val
    if ack_url and m is not None:                              # the copy for transports that leave the body on disk: no bearer link
        with contextlib.suppress(Exception):
            plain_safe = T.build_plain(dataclasses.replace(m, ack_url=""))
    if "email" in dec.channels:
        try:
            html_ = T.build_html(m, site["masthead"])
            if len(html_) > 95_000:                                            # Gmail clips messages near 102 KB
                html_ = ""
                notes.append("html too large: plain text sent")
        except Exception as exc:                                               # noqa: BLE001
            html_ = ""
            notes.append(f"html render failed ({type(exc).__name__}): plain text sent")
    title, summ = T.line(ev.title, 100), T.line(ev.summary, 300)
    sms = sms or T.sms_clean(f"{site['sms_prefix']}: {ev.kind} {title}")[:T.SMS_LIMIT]
    subject = subject or T.ascii_fold(f"{site['subject_prefix']}{ev.kind}: {title}")[:110]
    plain = plain or f"{title}\n{summ}\n"
    return Message(nc["transport"]["handle"], sms, subject, plain, html_, list(dec.channels), ack_url, plain_safe), notes


def _note(res: TransportResult, degraded: list[str], extra: str = "") -> str:
    parts = []
    for leg in CHANNELS:
        if leg in res.legs:
            s = f"{leg} {res.legs[leg]}"
            if res.errors.get(leg):
                s += f" ({res.errors[leg]})"
            parts.append(s)
    if not parts and res.fatal:
        parts.append(res.fatal)
    if res.via == "bridge":
        parts.append("via bridge fallback")
    return "; ".join(parts + degraded + ([extra] if extra else []))


def send(event: Event, cfg: dict | None = None, now: float | None = None, *, transport: Transport | None = None,
         fallback: Transport | None = None, dry_run: bool = False) -> Delivery:
    """Route, render and deliver one event. Never raises. `transport`/`fallback` are injectable (tests, other
    transports); by default the Hermes child with the legacy bridge as the fallback. dry_run renders and routes
    but sends nothing and records nothing.
    A CRITICAL alert/incident that cannot be delivered is not lost: it is queued in the outbox (Delivery.queued) and replayed
    until it lands (see flush_pending). Older queued messages of another kind for the same problem go out first (an alert before
    its recovery), and after the message itself everything else that is owed (queued pages, SMS retries) gets its turn."""
    now = time.time() if now is None else float(now)
    kind = str(getattr(event, "kind", ""))
    try:
        if not dry_run and kind != "test" and _peek_state().get("outbox"):
            with contextlib.suppress(Exception):
                _flush_outbox(load_config(cfg), now, transport, fallback, only_key=_key(event), skip_kind=kind)
        d = _send(event, cfg, now, transport, fallback, dry_run)
    except Exception as exc:                                                   # noqa: BLE001
        return Delivery(kind=kind, note=f"notify error: {type(exc).__name__}: {redact_log(str(exc), 80)}")
    if not dry_run and d.skipped != "breaker":
        flush_pending(cfg, now, transport=transport, fallback=fallback)
    return d


# --------------------------------------------------------------------------- transport circuit breaker
BREAKER_MAX_S = 3600


def _breaker_until(st: dict, now: float) -> float:
    """Epoch until which the transport is considered dead, else 0. A value implausibly far ahead (the clock stepped
    back) is ignored instead of silencing the owner for days."""
    bk = st.get("breaker")
    until = _num(bk.get("until"), 0) if isinstance(bk, dict) else 0.0
    return until if now < until <= now + BREAKER_MAX_S else 0.0


def _breaker_update(st: dict, res: TransportResult, now: float, nc: dict) -> None:
    """After a transport call. Any delivery closes the breaker. A transport-level failure (no channel reported, or a
    timeout) opens it for [transport] breaker_s, so N pending alerts cost one timeout, not N of them, and the caller's
    state lock is held for seconds instead of minutes. A failure the channels themselves reported (every leg
    `failed`) is quick and is retried normally, so it does not open it."""
    if res.ok:
        st.pop("breaker", None)
    elif not res.legs or res.rc == 124:
        secs = min(max(_num(nc["transport"].get("breaker_s"), 300), 0.0), BREAKER_MAX_S)
        if secs > 0:
            prev = st.get("breaker") if isinstance(st.get("breaker"), dict) else {}
            st["breaker"] = {"until": now + secs, "n": int(_num(prev.get("n"), 0)) + 1,
                             "why": redact_log(res.fatal or "no channel reported", 80)}


# --------------------------------------------------------------------------- pending SMS retries (critical pages)
def _add_pending(st: dict, kind: str, key: str, sev: str, sms: str, now: float) -> None:
    rows = [p for p in st.get("pending") or [] if not (isinstance(p, dict) and p.get("kind") == kind and p.get("key") == key)]
    rows.append({"kind": kind, "key": key, "sev": sev, "sms": str(sms)[:T.SMS_LIMIT], "ts": now, "last": now, "n": 1})
    st["pending"] = rows[-10:]


def _drop_pending(st: dict, key: str) -> None:
    """The owner's phone was reached for this problem (by any message): its pending text is moot."""
    st["pending"] = [p for p in st.get("pending") or [] if isinstance(p, dict) and p.get("key") != key]
    for k, v in st["dedupe"].items():
        if k.partition("|")[2] == key:
            v.pop("sp", None)


def flush_pending(cfg: dict | None = None, now: float | None = None, *, transport: Transport | None = None,
                  fallback: Transport | None = None) -> list[Delivery]:
    """Everything that failed earlier and is owed to the owner. Called after every send(), by HermesNotifier.deliver() and by
    `notify flush` (the scheduler tick can call it every minute: with nothing pending it is one short locked read). Two parts:
      1. the OUTBOX: critical alert/incident pages that could not be delivered at all (see _flush_outbox);
      2. the SMS leg of critical pages whose text failed while the email went (the failed leg only, never the whole message):
         at most [retry] sms_attempts tries in all, at least sms_gap_s apart, within sms_window_s of the first.
    Both respect the transport breaker, the budgets and the hard cap, and a failed try consumes no budget. Never raises."""
    now = time.time() if now is None else float(now)
    out: list[Delivery] = []
    try:
        nc = load_config(cfg)
    except Exception:                                                          # noqa: BLE001
        return out
    for part in (lambda: _flush_outbox(nc, now, transport, fallback), lambda: _flush(nc, now, transport)):
        try:
            out += part()
        except Exception:                                                      # noqa: BLE001 (one part failing must not stop the other)
            pass
    return out


def _flush(nc: dict, now: float, transport) -> list[Delivery]:
    if nc["transport"].get("kind") == "none" and transport is None:
        return []
    gap, tries = _num(nc["retry"].get("sms_gap_s"), 300), int(_num(nc["retry"].get("sms_attempts"), 3))
    work: list[tuple[dict, str]] = []
    with _state() as st:
        _prune(st, nc, now)
        if not st.get("pending") or _breaker_until(st, now):
            return []
        b = nc["budget"]
        recent = [r for r in st["sent"] if _age(now, r[0]) < 86400]
        room = min(int(_num(b.get("hard_cap_per_day"), 40)) - len(recent),
                   int(_num(b.get("sms_per_day"), 10)) + int(_num(b.get("crit_sms_reserve"), 5)) - sum(1 for r in recent if r[2]))
        for p in st["pending"]:
            if room <= 0 or len(work) >= 3:
                break
            if p.get("claim") or _age(now, p.get("last")) < gap:
                continue
            claim = secrets.token_hex(4)
            before = dict(p)
            p["claim"], p["last"], p["n"] = claim, now, int(_num(p.get("n"), 0)) + 1
            st["sent"].append([now, p["kind"], 1, claim])
            work.append((before, claim))
            room -= 1
    primary = transport or {"bridge": bridge_transport}.get(nc["transport"].get("kind"), hermes_transport)
    out: list[Delivery] = []
    dead = False
    for p, claim in work:
        if dead:                           # the transport just failed outright: the rest wait for the next run, un-tried and un-counted
            with _state() as st:
                st["sent"] = [r for r in st["sent"] if not (len(r) >= 4 and r[3] == claim)]
                for q in st.get("pending") or []:
                    if isinstance(q, dict) and q.get("claim") == claim:
                        q.pop("claim")
                        q["n"], q["last"] = p.get("n", 1), p.get("last", now)
            continue
        p["n"] = int(_num(p.get("n"), 0)) + 1          # `p` is the entry as it was before the claim: this try is number n
        msg = Message(nc["transport"]["handle"], p["sms"], "[homelab] SMS retry", p["sms"], "", ["sms"])
        try:
            res = primary(msg, nc)
        except Exception as exc:                                               # noqa: BLE001
            res = TransportResult(False, fatal=redact_log(f"{type(exc).__name__}: {exc}", 120))
        ok = res.legs.get("sms") == "sent" or (res.ok and not res.legs)
        dead = not res.ok and (not res.legs or res.rc == 124)
        why = res.errors.get("sms") or res.fatal or "no reason logged"
        d = Delivery(kind=p["kind"], severity=p["sev"], ok=ok, handled=ok, channels=["sms"] if ok else [], attempted=["sms"],
                     legs=dict(res.legs) or {"sms": "sent" if ok else "failed"}, dedupe_key=p["key"],
                     note="sms retry sent" if ok else f"sms retry failed ({why}), attempt {p['n']} of {tries}"
                     + ("; giving up" if p["n"] >= tries else ""))
        with _state() as st:
            _breaker_update(st, res, now, nc)
            if ok:
                for r in st["sent"]:
                    if len(r) >= 4 and r[3] == claim:
                        r[3] = ""
                _drop_pending(st, p["key"])
                esc = st["esc"].get(p["key"])
                if isinstance(esc, dict):
                    esc["sms"] = True                                          # so its recovery texts too
            else:
                st["sent"] = [r for r in st["sent"] if not (len(r) >= 4 and r[3] == claim)]
                for q in st.get("pending") or []:
                    if isinstance(q, dict) and q.get("claim") == claim:
                        q.pop("claim")
            _log(_record(Event(p["kind"], p["sev"], f"SMS retry: {p['key']}"), p["kind"], p["sev"], p["key"], d, now), nc)
        _audit("notify", "send", f"{p['kind']}: sms retry", 0, "sent" if ok else f"failed rc={res.rc or 1} {why}".strip())
        out.append(d)
    return out


# --------------------------------------------------------------------------- the outbox (a page that could not be delivered at all)
OUTBOX_ENTRY_BYTES = 16_000          # one stored event stays small: the bulkiest parts (sections, text, log) are dropped first
OUTBOX_KINDS = ("alert", "incident_open")
NOTICE_KINDS = ("ack_expired",)      # informational notices that share the outbox's storage but never its capacity (see _cap_outbox)
_NO_ATTEMPT = ("breaker", "budget")  # replay outcomes that did not actually try the transport: they do not use up an attempt


def _outbox_eligible(kind: str, sev: str) -> bool:
    """A page nobody may lose: a CRITICAL alert or incident. A warning is re-sent by its own reminder; tests, digests and
    recoveries are not queued from here (HermesNotifier(defer=True) queues everything it sends through enqueue())."""
    return kind in OUTBOX_KINDS and sev == "crit"


def _oage(now: float, ts) -> float:
    """Age of an outbox entry. Unlike _age, a clock that stepped back (a first-attempt time in the future) must not EXPIRE a
    critical page: it counts as brand new. A missing or non-numeric time is infinitely old."""
    try:
        return max(now - float(ts), 0.0)
    except (TypeError, ValueError):
        return float("inf")


def _gap_s(nc: dict, n: int) -> float:
    """Wait before attempt n+1: outbox_gap_s, doubling per failed attempt, at most an hour (a permanent fault must not log 144 rows)."""
    return min(_num(nc["retry"].get("outbox_gap_s"), 300) * 2 ** max(int(n) - 1, 0), 3600.0)


def _freeze(ev: Event, kind: str, sev: str) -> dict:
    """A bounded, JSON-safe, SANITISED copy of an event for notify-state.json (0600): everything goes through the cleaners the
    renderer uses (control characters out, credentials scrubbed, lines clipped), so a raw log or a quoted password never lands in
    the state file. thaw() turns it back into an Event that renders the same way."""
    snap = {"kind": kind, "sev": sev, "title": T.line(ev.title, 100), "summary": T.line(ev.summary, 600), "status": T.line(ev.status, 12) or None,
            "task": T.line(ev.task, 60) or None, "facts": T.clean_facts(ev.facts), "details": T.clean_details(ev.details)}
    while len(json.dumps(snap, default=str)) > OUTBOX_ENTRY_BYTES and snap["details"]:
        for k in ("sections", "text", "log", "timeline", "done", "todo"):
            if snap["details"].pop(k, None) is not None:
                break
    return snap


def _thaw(e: dict, now: float) -> Event:
    """The stored event, plus a facts row saying it is late (its email would otherwise read as if it were happening now)."""
    s = e["snap"]
    facts = dict(s.get("facts") or {})
    late = _oage(now, e.get("ts"))
    if 60 < late < float("inf"):
        facts["Delayed delivery"] = f"first attempt {time.strftime('%H:%M', time.localtime(float(e['ts'])))} ({T.dur(late)} ago), sent now"
    return Event(str(s.get("kind") or e["kind"]), str(s.get("sev") or ""), str(s.get("title") or ""), str(s.get("summary") or ""),
                 s.get("details") or None, facts or None, s.get("status"), e["key"], s.get("task"))


def _cap_outbox(box: list[dict], nc: dict) -> tuple[list[dict], list[dict]]:
    """(kept, squeezed out) for an outbox that may be over its caps. Two independent classes, so one can never push the other out:
    PAGES (everything but a notice) keep at most [retry] outbox_max entries, NOTICES (ack_expired) at most notices_max. Inside a class
    the oldest goes first, except that an entry being replayed right now is kept if possible and a critical alert/incident goes last:
    a flood of warnings, recoveries or expiry notices must never evict the page the owner has not got yet. Order is preserved."""
    r = nc["retry"]
    caps = {False: max(int(_num(r.get("outbox_max"), 10)), 1), True: max(int(_num(r.get("notices_max"), 20)), 1)}
    gone: set[int] = set()
    for notice, cap in caps.items():
        grp = [e for e in box if (e.get("kind") in NOTICE_KINDS) is notice]
        order = sorted(range(len(grp)), key=lambda i: (bool(grp[i].get("claim")), _outbox_eligible(str(grp[i].get("kind")), str((grp[i].get("snap") or {}).get("sev"))), i))
        gone |= {id(grp[i]) for i in order[:max(len(grp) - cap, 0)]}
    return [e for e in box if id(e) not in gone], [e for e in box if id(e) in gone]


def _outbox_put(st: dict, nc: dict, ev: Event, kind: str, sev: str, key: str, now: float, why: str, tried: bool) -> list[dict]:
    """Add (or refresh) the entry for this kind + key under the state lock. Order is delivery order: an entry is refreshed in
    place only if it is the LAST one for that key (a newer event for the same problem never jumps ahead of a later one).
    Returns the entries squeezed out by outbox_max (the caller audits them)."""
    box = [e for e in st.get("outbox") or [] if isinstance(e, dict)]
    snap = _freeze(ev, kind, sev)
    last = next((e for e in reversed(box) if e.get("key") == key), None)
    if last is not None and last.get("kind") == kind:
        last["snap"], last["why"] = snap, why
        last.pop("claim", None)
        last.pop("claim_ts", None)
        if tried:
            last["n"], last["last"] = int(_num(last.get("n"), 0)) + 1, now
    else:
        box.append({"kind": kind, "key": key, "snap": snap, "ts": now, "last": now if tried else 0, "n": 1 if tried else 0, "why": why})
    st["outbox"], over = _cap_outbox(box, nc)
    return over


def _outbox_drop(st: dict, kind: str, key: str) -> None:
    """The same kind of message for the same problem was just delivered: its older queued copies are moot."""
    if st.get("outbox"):
        st["outbox"] = [e for e in st["outbox"] if not (isinstance(e, dict) and e.get("kind") == kind and e.get("key") == key)]


def _dropped(over: list[dict]) -> None:
    for e in over:
        _audit("notify", "outbox-overflow", f"{e.get('kind')}: {redact_log((e.get('snap') or {}).get('title'), 60)}", 0, "dropped")


def enqueue(event: Event, cfg: dict | None = None, now: float | None = None) -> bool:
    """Durably queue an event for the next flush_pending(): no network, no routing, no budget (those are decided when it is
    delivered, in order). For callers that must not wait on the transport while they hold a lock (HermesNotifier(defer=True)).
    True = it is stored; False = it could not be (no writable state directory): the caller keeps the responsibility."""
    now = time.time() if now is None else float(now)
    try:
        nc = load_config(cfg)
        kind, _tmpl, sev = _kind_sev(event)
        key = _key(event)
        with _state() as st:
            _prune(st, nc, now)
            over = _outbox_put(st, nc, event, kind, sev, key, now, "queued", tried=False)
        _dropped(over)
        return any(e.get("kind") == kind and e.get("key") == key for e in _peek_state().get("outbox") or [])
    except Exception:                                                          # noqa: BLE001
        return False


def _flush_outbox(nc: dict, now: float, transport, fallback, *, only_key: str | None = None, skip_kind: str | None = None) -> list[Delivery]:
    """Replay the outbox, oldest first, through the SAME pipeline as a fresh send (routing, dedupe, budget, rendering, commit),
    outside any lock. An entry leaves when it was delivered, or held on purpose by policy (dedupe, covered, quiet hours, mute: a
    page the owner already got by another path is not sent again), or after outbox_ttl_s since its first attempt (logged and
    audited as expired). A failed replay stays, backing off (see _gap_s); an open transport breaker is not even tried.
    `only_key`/`skip_kind`: send() uses them so that older messages of OTHER kinds for the same problem (an alert before its
    recovery) are delivered first, whatever their gap. Never raises."""
    if nc["transport"].get("kind") == "none" and transport is None:
        return []
    ttl = _num(nc["retry"].get("outbox_ttl_s"), 43200)
    work: list[tuple[dict, str]] = []
    gone: list[dict] = []
    with _state() as st:
        _prune(st, nc, now)
        box = st.get("outbox") or []
        gone = [e for e in box if _oage(now, e.get("ts")) >= ttl]
        st["outbox"] = box = [e for e in box if _oage(now, e.get("ts")) < ttl]
        if box and not _breaker_until(st, now):
            for e in sorted(box, key=lambda x: x.get("kind") in NOTICE_KINDS):         # pages first (stable: oldest first), notices after them
                if len(work) >= max(int(_num(nc["retry"].get("outbox_max"), 10)), 1):
                    break
                if e.get("claim") or (only_key is not None and (e["key"] != only_key or e["kind"] == skip_kind)):
                    continue
                if only_key is None and e.get("last") and _age(now, e["last"]) < _gap_s(nc, int(_num(e.get("n"), 0))):
                    continue
                tok = secrets.token_hex(4)
                e["claim"], e["claim_ts"] = tok, now
                work.append((copy.deepcopy(e), tok))
    for e in gone:
        title = redact_log((e.get("snap") or {}).get("title"), 100)
        _log({"ts": round(now, 1), "kind": e["kind"], "severity": (e.get("snap") or {}).get("sev", ""), "title": title, "channels": [], "ok": False,
              "note": f"gave up: never delivered within {int(ttl // 3600)} h of the first attempt", "dedupe_key": e["key"], "skipped": "expired"}, nc)
        _audit("notify", "outbox-expired", f"{e['kind']}: {title[:60]}", 0, "dropped")
    out: list[Delivery] = []
    dead = False
    released: list[str] = []
    for e, tok in work:
        if dead:                                       # the transport just failed outright: the rest wait for the next run, untried
            released.append(tok)
            continue
        try:
            d = _send(_thaw(e, now), nc, now, transport, fallback, False, replay=True)
        except Exception as exc:                                               # noqa: BLE001
            d = Delivery(kind=e["kind"], note=f"notify error: {type(exc).__name__}: {redact_log(str(exc), 80)}")
        dead = d.skipped == "breaker" or (not d.ok and not d.handled and not d.legs and not d.skipped)
        with _state() as st:
            row = next((x for x in st.get("outbox") or [] if isinstance(x, dict) and x.get("claim") == tok), None)
            if row is not None:                        # (a delivery inside _send already removed it)
                if d.ok or d.handled:
                    st["outbox"].remove(row)
                else:
                    row.pop("claim", None)
                    row.pop("claim_ts", None)
                    row["last"] = now
                    if d.skipped not in _NO_ATTEMPT:
                        row["n"], row["why"] = int(_num(row.get("n"), 0)) + 1, redact_log(d.note, 100)
        out.append(d)
    if released:
        with _state() as st:
            for x in st.get("outbox") or []:
                if isinstance(x, dict) and x.get("claim") in released:
                    x.pop("claim", None)
                    x.pop("claim_ts", None)
    return out


def _send(ev: Event, cfg, now: float, transport, fallback, dry_run: bool, replay: bool = False) -> Delivery:
    """One event through the whole pipeline. `replay`: it comes from the outbox, which manages its own entry (a failed replay
    is not queued a second time)."""
    nc = load_config(cfg)
    kind, tmpl, sev = _kind_sev(ev)
    d = Delivery(kind=kind, severity=sev)
    if nc["transport"].get("kind") == "none" and transport is None:
        d.skipped, d.handled, d.note = "disabled", True, "notifications disabled ([transport] kind = none)"
        d.dedupe_key = _key(ev)
        with _state():
            _log(_record(ev, kind, sev, d.dedupe_key, d, now), nc)
        return d

    held, hold = _ack_check(ev, nc, kind, tmpl, sev, now, dry_run)          # an acknowledged issue is held BEFORE routing and budgets
    if held is not None:
        return held

    until, over = 0.0, []
    with _state() as st:
        _prune(st, nc, now)
        dec = decide(ev, nc, st, now)
        d.dedupe_key, d.why = dec.key, dec.why
        until = _breaker_until(st, now) if not (dec.skip or dry_run or kind == "test") else 0.0      # a TEST always probes the transport
        if dec.skip or dry_run:
            d.skipped, d.handled, d.attempted = dec.skip or "dry-run", bool(dec.skip and dec.handled) or dry_run, dec.channels
            d.note = (dec.why[-1] if dec.why else dec.skip) if dec.skip else "dry run: nothing sent"
            if not dry_run:
                noisy = False
                if dec.skip == "budget":              # the caller retries every run: say so once an hour, not every run
                    th = st["dedupe"].get(f"budget|{kind}|{dec.key}")
                    noisy = isinstance(th, dict) and _age(now, th.get("ts")) < 3600
                    if not noisy:
                        st["dedupe"][f"budget|{kind}|{dec.key}"] = {"ts": now, "sev": sev}
                        _audit("notify", "budget-exhausted", f"{kind}: {redact_log(ev.title, 60)}", 0, "dropped")
                if not noisy:
                    _log(_record(ev, kind, sev, dec.key, d, now), nc)
        elif until:                                   # the transport is known to be dead: do not wait for it again
            d.skipped, d.handled, d.attempted = "breaker", False, list(dec.channels)
            d.note = (f"transport circuit open, next attempt in {int((until - now) // 60) + 1} min "
                      f"(last failure: {(st.get('breaker') or {}).get('why') or 'no reason logged'})")
            if not replay and _outbox_eligible(kind, sev):          # not lost: a durable copy is replayed when the circuit closes
                over = _outbox_put(st, nc, ev, kind, sev, dec.key, now, "transport circuit open", tried=False)
                d.queued, d.note = True, d.note + "; queued for retry"
            th = st["dedupe"].get(f"breaker|{kind}|{dec.key}")
            if not (isinstance(th, dict) and _age(now, th.get("ts")) < 900):      # every run retries: log it every 15 min, not every run
                st["dedupe"][f"breaker|{kind}|{dec.key}"] = {"ts": now, "sev": sev}
                _log(_record(ev, kind, sev, dec.key, d, now), nc)
        else:
            claim = secrets.token_hex(4)
            fkey = f"{kind}|{dec.key}"
            prev = st["dedupe"].get(fkey)
            st["sent"].append([now, kind, 1 if "sms" in dec.channels else 0, claim])
            st["dedupe"][fkey] = {"ts": now, "sev": sev, "c": claim}
    _dropped(over)
    if dec.skip or dry_run or until:
        if dry_run:
            msg, _n = _render(ev, nc, dec, now)
            d.rendered = {"subject": msg.subject, "sms": msg.sms, "plain": msg.plain, "html": msg.html}
        return d

    d.attempted = list(dec.channels)
    msg, degraded = _render(ev, nc, dec, now, preview=False)                 # the one place a real acknowledge token is issued
    if nc.get("_config_error"):
        degraded.append(nc["_config_error"])
    primary = transport or {"bridge": bridge_transport}.get(nc["transport"].get("kind"), hermes_transport)
    fb = fallback if (transport or fallback) else (bridge_transport if nc["transport"].get("fallback") == "bridge" else None)
    try:
        res = primary(msg, nc)
    except Exception as exc:                                                   # noqa: BLE001
        res = TransportResult(False, fatal=redact_log(f"{type(exc).__name__}: {exc}", 120))
    # The primary broke before any channel reported (not a timeout: that may have half-sent). Try the bridge, but only
    # if it can honour the route: it always sends on every enabled channel, so it is for sms+email messages only. The
    # fallback gets a much shorter timeout than the primary: it is the second chance, not a second 90 s wait.
    if fb and not res.ok and res.fatal and not res.legs and res.rc != 124 and set(dec.channels) == set(CHANNELS):
        try:
            res2 = fb(msg, {**nc, "transport": {**nc["transport"], "timeout_s": _num(nc["transport"].get("fallback_timeout_s"), 20)}})
            if res2.ok:
                res = res2
            else:
                res.fatal = f"{res.fatal}; fallback: {res2.fatal or 'failed'}"[:200]
        except Exception as exc:                                               # noqa: BLE001
            res.fatal = f"{res.fatal}; fallback raised {type(exc).__name__}"[:200]

    _revoke_unused_token(msg, res, now)                                      # a link that reached nobody must not stay valid
    delivered = [c for c in dec.channels if res.legs.get(c) == "sent"]
    if res.ok and not delivered:
        delivered = list(dec.channels)           # the transport said ok but did not say which leg: assume what we asked
    d.ok, d.handled, d.channels, d.legs = bool(res.ok and delivered), bool(res.ok and delivered), delivered, dict(res.legs)
    d.note = ("replayed from the outbox; " if replay else "") + _note(res, degraded)
    # A critical page that reached the inbox but not the phone is half done: the text is retried on its own.
    sms_lost = (kind in ("alert", "incident_open") and sev == "crit" and "sms" in dec.channels and res.legs.get("sms") == "failed")

    with _state() as st:
        _prune(st, nc, now)
        _breaker_update(st, res, now, nc)
        if d.ok:
            for r in st["sent"]:
                if len(r) >= 4 and r[3] == claim:
                    r[2] = 1 if "sms" in delivered else 0
                    r[3] = ""
            slot = {"ts": now, "sev": sev}
            if "sms" in delivered:
                _drop_pending(st, dec.key)
            elif sms_lost:
                _add_pending(st, kind, dec.key, sev, msg.sms, now)
                slot["sp"] = 1                                                # covers nothing: the phone has not been reached
                d.note += "; the text will be retried"
            st["dedupe"][fkey] = slot
            if not replay:                                                     # an older queued copy of this very message is moot
                _outbox_drop(st, kind, dec.key)                                # (a replay removes only its own entry: a NEWER one for the key stays)
            e = st["esc"]
            if kind in ("alert", "incident_open"):
                if hold.fp:                                          # remember what this episode's fingerprint is: an acknowledgement added
                    st.setdefault("ackfp", {})[dec.key] = {"fp": hold.fp, "sev": hold.sev, "ts": now}     # later still holds its recovery
                for k in ("recovery", "incident_resolved"):          # a new episode: its recovery must not look like a repeat
                    st["dedupe"].pop(f"{k}|{dec.key}", None)
                cur = e.get(dec.key) if isinstance(e.get(dec.key), dict) else {"n": 0, "first": now, "sms": False}
                e[dec.key] = {"n": int(cur.get("n", 0)) + 1, "first": cur.get("first", now), "last": now, "sev": sev,
                              "sms": bool(cur.get("sms")) or "sms" in delivered}
            elif kind in ("recovery", "incident_resolved"):
                e.pop(dec.key, None)
                (st.get("ackfp") or {}).pop(dec.key, None)
                for k in ("alert", "incident_open"):
                    st["dedupe"].pop(f"{k}|{dec.key}", None)
        else:                                                                 # roll the claim back: nothing was delivered
            st["sent"] = [r for r in st["sent"] if not (len(r) >= 4 and r[3] == claim)]
            if prev is None:
                st["dedupe"].pop(fkey, None)
            else:
                st["dedupe"][fkey] = prev
            if not replay and _outbox_eligible(kind, sev):                    # a critical page is never just lost: replayed until it lands
                over = _outbox_put(st, nc, ev, kind, sev, dec.key, now, redact_log(d.note, 100), tried=True)
                d.queued, d.note = True, d.note + "; queued for retry"
        _log(_record(ev, kind, sev, dec.key, d, now), nc)
    _dropped(over)
    target = f"{kind}: {redact_log(ev.title, 60)}"
    if d.ok:
        _audit("notify", "send", target, 0, "sent")
        # One failed-"send" row per dead channel, right after the "sent" row: alert_path_health reads action "send" and
        # sees a leg that is down (the overall row cannot say which leg worked). leg_health() is the exact per-channel view.
        for leg in CHANNELS:
            if res.legs.get(leg) == "failed":
                _audit("notify", "send", f"{kind}: {leg} leg", 0, f"failed rc=1 {res.errors.get(leg, '')}".strip())
    else:
        _audit("notify", "send", target, 0, f"failed rc={res.rc or 1} {d.note}".strip())          # a failure never reads rc=0
    return d


# --------------------------------------------------------------------------- helpers for callers (core.Notifier, jobs, incidents, reports)
def alert_event(task: str, title: str, status: str, summary: str, *, playbook=None, facts: dict | None = None,
                details: dict | None = None) -> Event:
    """core.Notifier: a confirmed problem. status crit/error -> crit, anything else -> warn."""
    det = dict(details or {})
    if playbook:
        det["todo"] = playbook
    return Event("alert", "crit" if status in ("crit", "error") else "warn", title, summary, det or None,
                 dict(facts or {}) or None, status, dedupe_key=task, task=task)


def recovery_event(task: str, title: str, summary: str = "", *, was: str | None = None, facts: dict | None = None,
                   details=None) -> Event:
    f = dict(facts or {})
    if was:
        f["was"] = was                            # "crit" | "warn": decides whether a recovery text is sent
    return Event("recovery", "ok", title, summary or f"{title} is back to normal", details, f or None, "ok",
                 dedupe_key=task, task=task)


def maintenance_event(task: str, title: str, summary: str, *, done=None, freed_bytes: int = 0, significant: bool = False,
                      facts: dict | None = None, severity: str = "ok") -> Event:
    f = dict(facts or {})
    if freed_bytes:
        f.setdefault("Freed", core.human(freed_bytes))
    if significant:
        f["significant"] = True
    return Event("maintenance", severity, title, summary, {"done": done} if done else None, f or None, None,
                 dedupe_key=f"maint-{task}", task=task)


def digest_event(kind: str, title: str, summary: str, *, severity: str = "ok", digest_text=None, tiles=None, link: str = "",
                 sections=None, period: str = "") -> Event:
    """digest_daily / report_weekly from reports.py: digest_text (<= 600 chars) becomes the highlights."""
    f: dict[str, Any] = {}
    if tiles:
        f["tiles"] = tiles
    if link:
        f["link"] = link
    det: dict[str, Any] = {"text": digest_text}
    if sections:
        det["sections"] = sections
    return Event(kind, severity, title, summary, det, f or None, None, dedupe_key=f"{kind}-{period or time.strftime('%Y-%m-%d')}")


def _tone(grade) -> str:
    return {"A": "ok", "B": "ok", "C": "info", "D": "warn", "F": "crit"}.get(str(grade or "").upper()[:1], "")


def report_event(doc: dict, link: str = "") -> Event:
    """A reports.py document (SPEC3 S5: health, highlights, actions, incidents, capacity, upcoming, digest_text) ->
    digest_daily / report_weekly. Every field is optional: a half-empty report still produces a readable email."""
    doc = doc if isinstance(doc, dict) else {}
    kind = "report_weekly" if doc.get("kind") == "weekly" else "digest_daily"
    h = doc.get("health") if isinstance(doc.get("health"), dict) else {}
    inc = doc.get("incidents") if isinstance(doc.get("incidents"), dict) else {}
    act = doc.get("actions") if isinstance(doc.get("actions"), dict) else {}
    cap = doc.get("capacity") if isinstance(doc.get("capacity"), dict) else {}
    grade, score = h.get("grade"), h.get("score")
    tiles: list = []
    if score is not None or grade:
        tiles.append([f"{grade or ''} {score if score is not None else ''}".strip(), "health", _tone(grade)])
    for label, val in (("incidents", inc.get("opened")), ("open now", inc.get("open_now"))):
        if isinstance(val, int):
            tiles.append([str(val), label, "warn" if (label == "open now" and val) else ""])
    if isinstance(act.get("freed_bytes"), (int, float)):
        tiles.append([core.human(act["freed_bytes"]) if act["freed_bytes"] else "0 B", "freed", ""])
    paras = [x for x in (doc.get("highlights") or []) if isinstance(x, str)][:8] or [str(doc.get("digest_text") or doc.get("headline") or "")]
    sections: list = []
    mounts = [(m.get("mount"), f"{m.get('free_h', '?')} free" + (f", {m['days_to_full']:.0f} days to full" if isinstance(m.get("days_to_full"), (int, float)) else ""))
              for m in (cap.get("mounts") or []) if isinstance(m, dict) and m.get("mount")][:6]
    if mounts:
        sections.append({"title": "Capacity", "kv": mounts})
    recs = [x for x in (cap.get("recommendations") or []) if isinstance(x, str)][:5]
    if recs:
        sections.append({"title": "Recommendations", "lines": recs})
    up = [(u.get("when"), u.get("what")) for u in (doc.get("upcoming") or []) if isinstance(u, dict)][:6]
    if up:
        sections.append({"title": "Coming up", "kv": up})
    sev = "warn" if (inc.get("open_now") or _tone(grade) in ("warn", "crit")) else ("info" if _tone(grade) == "info" else "ok")
    rid = str(doc.get("id") or time.strftime("%Y-%m-%d"))
    facts: dict[str, Any] = {"tiles": tiles[:4], "link": link or (f"#/reports/{rid}" if re.fullmatch(r"[0-9A-Za-z-]{1,16}", rid) else "#/reports")}
    return Event(kind, sev, str(doc.get("headline") or f"{'Weekly report' if kind == 'report_weekly' else 'Daily digest'} {rid}")[:100],
                 str(doc.get("digest_text") or doc.get("headline") or "")[:600],
                 {"text": paras, "sections": sections}, facts, None, f"{kind}-{rid}")


# --------------------------------------------------------------------------- the notice when an acknowledgement ends
def _day(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def ack_expired_event(item: dict, *, still_failing: bool | None = None, button_days: int | None = None, button: bool = True,
                      ackable: bool = True) -> Event:
    """The ONE notice for an acknowledgement that has ended. `item` is an acks.json record (SPEC5 S3: task, title, summary, severity,
    acked_at, until, by, note, count_suppressed) plus `fp` (and optionally `days`, `still_failing`, `now_severity`). Still failing: a
    warn/crit notice with a fresh Acknowledge button (the fingerprint travels in facts.ack_fp; the original error text in facts.ack_summary).
    Its severity, "Now" row and the button's `s=` are the issue's CURRENT severity (`now_severity`, from status.json), the recorded one only
    when the status says nothing. `button_days` is what the button promises ("again for another 90 days"), not the length of the ended
    acknowledgement; `button=False` words the todo for an email that has no button (the site cannot take the click yet); `ackable=False`
    words it for an issue that cannot be acknowledged at all (a crit issue: `[ack] severities` allows no button and no CLI `ack add`).
    No longer occurring: an "ok" notice with nothing to click. dedupe_key `ack-expired-<fp>-<end time in hex>` makes it one notice per acknowledgement."""
    item = item if isinstance(item, dict) else {}
    fp, task = _fp_str(item.get("fp")), T.line(item.get("task"), 60)
    title = T.line(item.get("title"), 100) or task or "Acknowledged issue"
    summary = T.line(item.get("summary"), 200)
    failing = T.truthy(item.get("still_failing")) if still_failing is None else bool(still_failing)
    was = _ack_sev(item.get("severity")) or "warn"
    sev = (_ack_sev(item.get("now_severity")) or was) if failing else "ok"
    try:
        days = int(round((float(item["until"]) - float(item["acked_at"])) / 86400))
    except (KeyError, TypeError, ValueError, OverflowError):
        days = 0
    days = int(item["days"]) if isinstance(item.get("days"), int) and not isinstance(item.get("days"), bool) and item["days"] > 0 else days or 90
    again = int(button_days) if isinstance(button_days, int) and not isinstance(button_days, bool) and button_days > 0 else days
    ended = _day(item.get("until"))
    head = f"The {days}-day acknowledgement of this exact error ended on {ended}"
    if failing:
        text = f"{head} and it is still failing" + (f": {summary}" if summary else ".")
        if not ackable:                                        # a crit issue: no button, no `ack add`, nothing to promise
            todo = [f"Fix it: a {sev} issue always alerts, so it cannot be acknowledged again.",
                    "Until it clears, this exact error alerts like any other problem."]
        else:
            how = "with the button above" if button else (f"with: homelab-maint ack add {fp}" if fp else "on the maintenance site")
            todo = [f"Fix it, or acknowledge it again for another {again} days {how}.",
                    "Until you do, it alerts again like any other problem."]
    else:
        text = f"{head}. It is no longer occurring, so nothing needs doing."
        todo = ["Nothing to do: alerts for this exact error are active again, in case it comes back."]
    held = item.get("count_suppressed")
    facts: dict[str, Any] = {"Acknowledged": f"{days} day{'s' if days != 1 else ''}, by {T.line(item.get('by'), 12) or 'unknown'}", "Acknowledged on": _day(item.get("acked_at")),
                             "Ended": ended}
    if isinstance(held, int) and not isinstance(held, bool) and held >= 0:
        facts["Held back"] = f"{held} alert{'s' if held != 1 else ''} and emails"
    facts["Now"] = (f"still failing ({sev})" + (f", acknowledged as {was}" if sev != was else "")) if failing else "no longer occurring"
    if T.line(item.get("note"), 200):
        facts["Your note"] = T.line(item.get("note"), 200)
    facts.update({"still_failing": failing, "link": "#/health"})
    if fp:
        facts["ack_fp"] = fp
    if summary:
        facts["ack_summary"] = summary
    try:                                           # the end time (hex: a run of ten digits would be read as a phone number by the log redactor) makes the key one
        tag = "-" + format(int(float(item["until"])), "x") if 0 < float(item["until"]) < 1e12 else ""        # per ACKNOWLEDGEMENT, so a re-acknowledged issue
    except (KeyError, TypeError, ValueError, OverflowError):                                                  # that ends again within a week still gets its notice
        tag = ""
    return Event("ack_expired", sev, title, text, {"todo": todo}, facts, None, f"ack-expired-{fp or task or 'unknown'}{tag}"[:80], task or None)


GROUP_LIST = 12                                    # rows the grouped notice names (the rest is counted)


def ack_expired_group_event(items: list[dict], *, button: bool = False, ackable: bool = True) -> Event:
    """ONE notice for several acknowledgements that ended in the same run (acks made in one sitting end together): a list with each
    one's title, Issue ID, current state and what it was, never one email (and one slice of the daily budget) per acknowledgement.
    It carries no button (one token binds one issue): whatever is still failing alerts again as a normal alert, each with its own
    button, and `homelab-maint ack add <Issue ID>` acknowledges one again at once."""
    rows = [i for i in items if isinstance(i, dict)]
    failing = [i for i in rows if T.truthy(i.get("still_failing"))]
    gone = [i for i in rows if not T.truthy(i.get("still_failing"))]
    worst = max((_ack_sev(i.get("now_severity")) or _ack_sev(i.get("severity")) or "warn" for i in failing), key=lambda x: x == "crit", default="")
    sev = worst or "ok"
    n = len(rows)

    def row(i: dict) -> str:
        fp = _fp_str(i.get("fp"))
        cur = (_ack_sev(i.get("now_severity")) or _ack_sev(i.get("severity")) or "warn") if T.truthy(i.get("still_failing")) else ""
        what = T.line(i.get("summary"), 100)
        return (f"{T.line(i.get('title') or i.get('task'), 60) or 'Acknowledged issue'}" + (f" ({cur})" if cur else "") + (f": {what}" if what else "")
                + (f" - Issue ID {fp}" if fp else ""))
    secs = []
    for label, grp in (("Still failing", failing), ("No longer occurring", gone)):
        if grp:
            ls = [row(i) for i in grp[:GROUP_LIST]]
            if len(grp) > GROUP_LIST:
                ls.append(f"+{len(grp) - GROUP_LIST} more: see the Acknowledged issues list on the site")
            secs.append({"title": label, "lines": ls})
    ends = sorted(float(i["until"]) for i in rows if isinstance(i.get("until"), (int, float)) and not isinstance(i.get("until"), bool))
    summary = (f"Ended {_day(ends[-1])}: " if ends else "") + f"{len(failing)} still failing, {len(gone)} no longer occurring."
    todo = ["Anything still failing alerts again like any other problem" + (", each with its own Acknowledge button." if button else "."),
            "To acknowledge one again right away: homelab-maint ack add <Issue ID> (or the Acknowledged issues list on the maintenance site)."]
    if not ackable:                                                # at least one is crit: do not promise a re-acknowledgement that would be refused
        todo[-1] = "A warning can be acknowledged again with homelab-maint ack add <Issue ID>; a critical issue cannot be acknowledged and always alerts."
    ident = "".join(sorted(f"{_fp_str(i.get('fp'))}{int(i['until']) if isinstance(i.get('until'), (int, float)) and not isinstance(i.get('until'), bool) and 0 < i['until'] < 1e12 else 0}"
                           for i in rows))
    tag = "".join(chr(97 + int(c, 16)) for c in hashlib.sha1(ident.encode()).hexdigest()[:12])      # letters only: a digit run would be read as a phone number
    return Event("ack_expired", sev, f"{n} acknowledgements ended", summary, {"todo": todo, "sections": secs},
                 {"link": "#/health", "still_failing": False}, None, f"ack-expired-group-{tag}", None)


def _status_entry(item: dict, status: dict | None):
    """(status.json entry of the item's task | None, its CURRENT severity warn|crit | "" when it is not failing or does not page)."""
    task = str(item.get("task") or "")
    t = (status.get("tasks") or {}).get(task) if isinstance(status, dict) and isinstance(status.get("tasks"), dict) else None
    if not isinstance(t, dict) or t.get("alert") is False:
        return None, ""
    return t, {"warn": "warn", "crit": "crit", "error": "crit"}.get(str(t.get("status") or ""), "")


def _still_failing(item: dict, status: dict | None) -> bool:
    """Is the EXACT acknowledged error still being reported? The task's current status entry is fingerprinted the same way the
    acknowledgement was; when that cannot be done and the task is failing, say "still failing" (the safe thing to tell the owner)."""
    task = str(item.get("task") or "")
    t, sev = _status_entry(item, status)
    if t is None or not sev:
        return False
    mod, fp = acks_loader(), _fp_str(item.get("fp"))
    cur = _fp_str(t.get("fp"))                                                  # acks.mark_entry stamps every failing entry with its exact fingerprint
    if cur and fp:
        return cur == fp
    if mod is not None and fp:
        res = core.Result(sev, str(t.get("summary") or ""), metrics=t.get("metrics") if isinstance(t.get("metrics"), dict) else {},
                          items=t.get("items") if isinstance(t.get("items"), list) else [])
        with contextlib.suppress(Exception):
            res.issue_key = t.get("issue_key")                                  # (a Result field once acks lands; harmless before)
        for arg in (res, str(t.get("summary") or "")):
            try:
                now_fp = _fp_str(mod.fingerprint(task, arg, sev))
            except Exception:                                                  # noqa: BLE001
                continue
            if now_fp:
                return now_fp == fp
    return True


def expired_items(expired, now: float | None = None, status: dict | None = None) -> list[dict]:
    """acks.expire()'s result -> the items send_expired wants. Each element is a fingerprint (its record is read, read-only, from
    STATE_DIR/acks.json, SPEC5 S3) or already a record dict. A fingerprint with no record is skipped: without the task and title there
    is nothing honest to say. `status` is status.json (read when not given) and decides still failing / no longer occurring."""
    if status is None:
        status = core.read_json(core.STATE_DIR / "status.json", {})
    store = core.read_json(core.STATE_DIR / "acks.json", {})
    recs = store.get("acks") if isinstance(store, dict) and isinstance(store.get("acks"), dict) else {}
    out: list[dict] = []
    for x in expired or []:
        rec = dict(x) if isinstance(x, dict) else dict(recs.get(_fp_str(x)) or {}) if _fp_str(x) else {}
        fp = _fp_str(rec.get("fp") or (x if isinstance(x, str) else ""))
        if not fp or not rec.get("task"):
            continue
        rec["fp"] = fp
        rec["still_failing"] = _still_failing(rec, status) if rec.get("still_failing") is None else T.truthy(rec["still_failing"])
        cur = _status_entry(rec, status)[1]
        if rec["still_failing"] and cur:                  # the notice speaks of the severity the issue has NOW (a warn ack, a crit issue: crit)
            rec["now_severity"] = cur
        out.append(rec)
    return out


def send_expired(items, cfg: dict | None = None, now: float | None = None, *, transport: Transport | None = None,
                 fallback: Transport | None = None) -> list[Delivery]:
    """One ack_expired notice per item, through the normal pipeline (routes, budget, dedupe); MORE than [ack] notice_group_over (2) of them
    in one call become ONE grouped notice (ack_expired_group_event: acknowledgements made together end together, and a dozen emails would
    spend the daily budget the real alerts need). The expiry is reported by acks.expire() only once, so a notice that could not be delivered
    (transport down, budget) is NOT left to chance: it is queued (enqueue: its own capacity, never a critical page's) and replayed by
    flush_pending. Never raises."""
    now = time.time() if now is None else float(now)
    out: list[Delivery] = []
    try:
        ac = ack_cfg(load_config(cfg))
    except Exception:                                                          # noqa: BLE001
        ac = ack_cfg({})
    rows = [i for i in items or [] if isinstance(i, dict)]
    try:
        mod = acks_loader()
        days = _link_days(mod, ac) if mod is not None else 0                   # the days the fresh button will carry (0: it will not exist)
        btn = button_on(ac) and bool(days)

        def _ok(i: dict) -> bool:                                             # may this item's issue be acknowledged again at all (crit: no)
            return _ack_sev_ok(mod, _ack_sev(i.get("now_severity")) or _ack_sev(i.get("severity")))
        evs = ([ack_expired_group_event(rows, button=btn, ackable=all(_ok(i) for i in rows))] if len(rows) > ac["group_over"]
               else [ack_expired_event(i, button_days=days or T.snap_days(ac["days"]), button=btn and _ok(i), ackable=_ok(i)) for i in rows])
    except Exception as exc:                                                   # noqa: BLE001
        return [Delivery(kind="ack_expired", note=f"notify error: {type(exc).__name__}")]
    for ev in evs:
        try:
            d = send(ev, cfg, now, transport=transport, fallback=fallback)
            if not (d.ok or d.handled) and enqueue(ev, cfg, now):
                d.queued, d.note = True, f"{d.note}; queued for retry".lstrip("; ")
            out.append(d)
        except Exception as exc:                                               # noqa: BLE001
            out.append(Delivery(kind="ack_expired", note=f"notify error: {type(exc).__name__}"))
    return out


def notify_expired(now: float | None = None, cfg: dict | None = None, *, expired=None, status: dict | None = None,
                   transport: Transport | None = None, fallback: Transport | None = None) -> list[Delivery]:
    """What the runner calls after acks.process_inbox: end the acknowledgements that are due (acks.expire) and tell the owner, once each.
    `expired` (fingerprints or records) skips the acks.expire call, for a caller that already ended them. Without an acks module: []."""
    now = time.time() if now is None else float(now)
    if expired is None:
        mod = acks_loader()
        if mod is None:
            return []
        try:
            expired = mod.expire(now)
        except Exception:                                                      # noqa: BLE001
            return []
    return send_expired(expired_items(expired, now, status), cfg, now, transport=transport, fallback=fallback)


def notifier_send(cfg: dict | None, name: str, title: str, status: str, summary: str, now: float, *, recovery: bool = False,
                  was: str | None = None, facts: dict | None = None, details: Any = None) -> bool:
    """The adapter core.Notifier calls instead of its old `_send` (the debounce state machine stays in core).
    True = the owner was told OR policy deliberately held the message (dedupe, quiet hours, mute, route none), so the
    Notifier records it as handled; False = delivery failed, the budget is spent or the transport breaker is open.
    CAUTION: core.Notifier.evaluate honours that False for ALERTS only (the next run retries). For a RECOVERY it sets
    alerted=0 whatever _send returned, so a recovery that fails is lost unless the Notifier remembers it: use
    HermesNotifier (below), which does, instead of patching core.Notifier._send with this function."""
    return send(_core_event(name, title, status, summary, recovery=recovery, was=was, facts=facts, details=details), cfg, now).handled


def _core_event(name: str, title: str, status: str, summary: str, *, recovery: bool = False, was: str | None = None,
                facts: dict | None = None, details: Any = None) -> Event:
    return (recovery_event(name, title, summary, was=was, facts=facts, details=details) if recovery
            else alert_event(name, title, status, summary, facts=facts, details=details if isinstance(details, dict) else None))


RECOVERY_RETRY_S = 12 * 3600
INLOCK_TIMEOUT_S, INLOCK_FALLBACK_S = 45, 10      # transport timeouts while the caller holds a lock (HermesNotifier without defer)


def _capped(cfg: dict | None) -> dict:
    """The loaded notify config with the transport timeouts cut down, for a caller that holds a lock while it sends: a dead
    transport then costs at most INLOCK_TIMEOUT_S + INLOCK_FALLBACK_S once (the breaker stops the repeats), not 90 + 20 s."""
    nc = load_config(cfg)
    t = nc["transport"]
    t["timeout_s"] = min(_num(t.get("timeout_s"), 90), INLOCK_TIMEOUT_S)
    t["fallback_timeout_s"] = min(_num(t.get("fallback_timeout_s"), 20), INLOCK_FALLBACK_S)
    return nc


class HermesNotifier(core.Notifier):
    """core.Notifier with delivery through notify.send, plus the two things core's own _send never had:
      * a recovery that could not be delivered is remembered (`recovery_pending` in the task's alerts.json entry) and
        retried every run until it goes out, the problem comes back, or RECOVERY_RETRY_S has passed. core.evaluate
        clears `alerted` after a recovery send whatever the result, so without this an outage of the mail path at the
        moment a check recovers leaves the owner with a CRIT page and never an OK;
      * every run first retries what failed earlier: the SMS leg of critical pages that reached the inbox but not the phone,
        and critical pages that could not be delivered at all (the outbox, see flush_pending).
    The debounce / reminder state machine is core's, unchanged: this class only overrides what is sent and remembers what
    failed. cli.cmd_run uses it by `from .notify import HermesNotifier as Notifier`.

    NETWORK WORK UNDER A LOCK. cli.cmd_run calls evaluate() inside its blocking state flock. Two ways to keep a dead transport
    from holding that lock for minutes:
      * defer=True (what cmd_run should use): evaluate() only QUEUES what the state machine decides to send (enqueue(): a short
        write to notify-state.json, no network; core is told "sent" because the queue is durable and replayed in order until it
        lands), and `deliver()`, called AFTER the lock is released, does the sending and the retries. If deliver() is never
        reached (the run was killed, a caller forgot) nothing is lost: the next flush_pending() (any send(), `notify flush`, the
        next run's deliver()) sends it.
      * defer=False (the default, and what a caller that does not know about deliver() gets): sends inline, but with the
        transport timeouts capped at INLOCK_TIMEOUT_S / INLOCK_FALLBACK_S, and only the first dead transport costs them."""

    def __init__(self, cfg: dict, defer: bool = False):
        super().__init__(cfg)
        self.cfg = cfg
        self.defer = bool(defer)
        self._nc = _capped(cfg)
        self._ctx: tuple[str, Any] = ("", None)
        self._tried: tuple[bool, bool, str] | None = None     # (was a recovery, was delivered, was) of the send made inside evaluate()
        self._flushed = False
        self._now = time.time()
        self._released: list[str] = []                         # keys whose held alerts this run re-opened (see _reopen_if_released)

    def _deliver(self, name: str, title: str, summary: str, now: float, *, recovery: bool, level: int, was: str | None = None,
                 res=None) -> bool:
        try:
            # An alert carries the exact fingerprint of the task's Result (it knows issue_key): the summary alone cannot say it.
            fp, mode = ("", "") if recovery else _result_ack(name, res, level, self._nc)
            facts = {"ack_fp": fp, **({"ack_mode": mode} if mode else {})} if fp else None
            if self.defer:
                return enqueue(_core_event(name, title, "crit" if level >= 2 else "warn", summary, recovery=recovery, was=was, facts=facts),
                               self.cfg, now)
            return notifier_send(self._nc, name, title, "crit" if level >= 2 else "warn", summary, now, recovery=recovery, was=was, facts=facts)
        except Exception:                                                      # noqa: BLE001
            return False

    def deliver(self, now: float | None = None, *, transport: Transport | None = None) -> list[Delivery]:
        """Send what evaluate() queued and retry what failed earlier. Call it AFTER the state lock is released (and after the
        Kuma heartbeat): with a dead transport it can take up to the full transport timeout once. Never raises."""
        return flush_pending(self.cfg, now, transport=transport)

    def _reopen_if_released(self, name: str, res: core.Result) -> None:
        """core.evaluate counts a HELD alert as sent (alerted = level, last_sent = now), so when the acknowledgement ends (un-acknowledge
        from the CLI or the site, expiry, a removed acks module) nothing would alert again until the next 24 h reminder, which each held
        reminder pushed further out. For a task that is failing now and whose alerts were held (the `held` mark _ack_check leaves in
        notify-state.json), clear core's "already alerted" state once the hold is gone: this very run alerts again. Runs inside cmd_run's
        state lock, on alerts.json, which only this notifier owns. Any doubt means "released" (an extra alert, never a silent one)."""
        st = self.s["tasks"].get(name)
        if (not isinstance(st, dict) or int(st.get("alerted", 0) or 0) <= 0 or int(st.get("level", 0) or 0) <= 0
                or not res.alert or core.LEVELS.get(res.status, 0) <= 0):
            return
        try:
            key = _key(Event("alert", dedupe_key=name, task=name))
            if _hold_released(key, self._nc, self._now, _result_fp(name, res, core.LEVELS.get(res.status, 0), self._nc)):
                st["alerted"], st["last_sent"] = 0, 0
                self._released.append(key)                                     # the `held` mark is cleared by save(), once alerts.json says so
                _forget_alert_dedupe(key)
                core.audit("notify", "ack-released", name, 0, "alerting resumes: the acknowledgement is no longer in force")
        except Exception:                                                      # noqa: BLE001
            pass

    def save(self) -> None:
        """core's save of alerts.json, then the `held` marks of the alerts it just re-opened are cleared: a run that dies before this
        point leaves them set, so the next run re-opens them again (a repeat, never a silence)."""
        super().save()
        keys, self._released = self._released, []
        for key in keys:
            with contextlib.suppress(Exception):
                _clear_held(key)

    def evaluate(self, name: str, title: str, res: core.Result, now: float) -> None:
        if not self._flushed and not self.defer:               # (a deferring notifier flushes in deliver(), outside the lock)
            self._flushed = True
            flush_pending(self._nc, now)
        self._now = now
        self._reopen_if_released(name, res)
        before = self.s["tasks"].get(name) or {}
        pend = before.get("recovery_pending") if isinstance(before.get("recovery_pending"), dict) else None
        self._ctx, self._tried = (title, res), None
        super().evaluate(name, title, res, now)
        st = self.s["tasks"][name]
        tried = self._tried
        if tried and tried[0]:                                 # a recovery message was just attempted
            if tried[1]:
                st.pop("recovery_pending", None)
            else:
                st["recovery_pending"] = {"ts": now, "last": now, "n": 1, "title": title[:100], "summary": str(res.summary)[:200], "was": tried[2]}
        elif tried or int(st.get("level", 0) or 0) > 0:        # the problem is (again) active: an old recovery is moot
            st.pop("recovery_pending", None)
        elif pend and not (res.alert and core.LEVELS.get(res.status, 0) > 0):
            age = now - float(pend.get("ts", now))
            if age >= RECOVERY_RETRY_S:
                st.pop("recovery_pending", None)
                core.audit("notify", "recovery-expired", name, 0, "dropped")
            elif self._deliver(name, str(pend.get("title") or title), str(pend.get("summary") or ""), now, recovery=True, level=0,
                               was=str(pend.get("was") or "warn")):
                st.pop("recovery_pending", None)
            else:
                pend["n"], pend["last"] = int(pend.get("n", 1)) + 1, now

    def _send(self, name: str, subject: str, body: str, now: float) -> bool:
        title, res = self._ctx
        st = self.s["tasks"].get(name) or {}
        level = int(st.get("level", 0) or 0)
        recovery = level == 0                                  # core sets the new level before it sends a recovery
        was = "crit" if int(st.get("alerted", 0) or 0) >= 2 else "warn"
        ok = self._deliver(name, title or name, str(getattr(res, "summary", "") or ""), now, recovery=recovery, level=level, was=was, res=res)
        self._tried = (recovery, ok, was)
        return ok


def doctor(cfg: dict | None = None) -> list[tuple[str, bool, str]]:
    """Read-only self-check of the notification path for `homelab-maint doctor` -> [(label, ok, hint)]. It never sends
    anything and never reads a credential: the Hermes config file is only stat()ed."""
    nc = load_config(cfg)
    t = nc["transport"]
    home = Path(_home(t["user"]))
    env = home / ".hermes" / "alert_transports.env"
    rows = [("notify.toml readable and well formed", "_config_error" not in nc, nc.get("_config_error", "")),
            ("transport user and handle valid", bool(_USER_RE.match(t["user"]) and _USER_RE.match(t["handle"])), f"{t['user']!r} / {t['handle']!r}"),
            ("Hermes alert_transports.py present", (Path(t["scripts_dir"]) / "alert_transports.py").is_file(), t["scripts_dir"]),
            ("Hermes transport config present (not read)", env.is_file(), str(env)),
            ("state dir writable (budgets, delivery log)", os.access(core.STATE_DIR, os.W_OK),
             f"{core.STATE_DIR} is read-only for this user: budgets and the log fall back to a tmpfs directory ({'; '.join(map(str, _dirs()[1:])) or 'none'})"
             " and are not shared with root. Hooks that should share root's budgets must run as root"),
            ("notifications not muted", not (core.CONF_DIR / str(nc.get("mute_file") or "NOTIFY_MUTE")).exists(),
             "NOTIFY_MUTE present: only critical alerts are sent")]
    st, now = _peek_state(), time.time()
    until = _breaker_until(st, now)
    rows.append(("transport circuit closed", not until,
                 f"open for {int((until - now) // 60) + 1} more min after: {(st.get('breaker') or {}).get('why') or 'no reason logged'}"))
    rows.append(("no critical text waiting for a retry", not st.get("pending"),
                 f"{len(st.get('pending') or [])} text(s) retried every {int(_num(nc['retry'].get('sms_gap_s'), 300) // 60)} min "
                 "(the email went, the SMS leg failed)"))
    box = [e for e in st.get("outbox") or [] if isinstance(e, dict) and e.get("kind") not in NOTICE_KINDS]     # (a queued expiry notice is no page)
    oldest = min(max((_oage(now, e.get("ts")) for e in box), default=0), 10 ** 9)
    rows.append(("no critical page waiting in the outbox", not box,
                 f"{len(box)} page(s) could not be delivered (oldest first tried {T.dur(oldest)} ago), replayed with back-off for up to "
                 f"{int(_num(nc['retry'].get('outbox_ttl_s'), 43200) // 3600)} h: {'; '.join(str(e.get('why') or '?')[:50] for e in box[:3])}"))
    ac = ack_cfg(nc)
    rows.append(("acknowledgements available (button + suppression)", not ac["enabled"] or acks_loader() is not None,
                 "homelab_maint/acks.py is missing or does not import: alerts are sent as before, with no Acknowledge button"))
    rows.append((f"acknowledge button: {'on' if button_on(ac) else 'off until the site serves /ack'}", True,
                 "[ack] button = auto offers it once STATE_DIR/ack/web_ready exists (ack_web's deploy), true always, false never"))
    rows.append(("acknowledge link base URL usable", not ac["enabled"] or bool(T.ack_url(ac["base"], T.PREVIEW_TOKEN, T.PREVIEW_ID, ac["days"], "warn")),
                 f"[ack] base_url {ac['base']!r} must be an http(s) URL; without it no Acknowledge button is sent"))
    if t.get("fallback") == "bridge" or t.get("kind") == "bridge":
        rows.append(("legacy bridge executable", os.access(t["bridge"], os.X_OK), t["bridge"]))
    if t.get("kind") == "none":
        rows.append(("transport enabled", False, '[transport] kind = "none": nothing is sent'))
    return rows


# --------------------------------------------------------------------------- samples, notify-test, CLI
def sample_events() -> dict[str, Event]:
    """Realistic example of every kind and the interesting severities (also the previews and notify-test)."""
    return {
        "alert.crit": Event("alert", "crit", "Disk space", "/ is 4% free (11.2 GiB left), about 3 days until full",
                            {"todo": ["Find what grew: du -xh --max-depth=1 /var | sort -h | tail",
                                      "Preview the safe cleanups: homelab-maint run --tier daily --dry-run",
                                      "Do not prune Docker volumes."],
                             "log": ["/ 11.2 GiB free of 280.0 GiB (4%)", "trend -3.9 GiB/day over 7 d"]},
                            {"Mount": "/", "Free": "11.2 GiB of 280.0 GiB", "Days until full": 3,
                             "Fastest grower": "/var/lib/docker (+3.1 GiB/day)", "link": "#/capacity"},
                            "crit", "sample-disk", "disk_forecast"),
        "alert.warn": Event("alert", "warn", "Services", "1 failed unit: nginx.service (failed 2 h ago)",
                            {"todo": ["systemctl status nginx.service", "journalctl -u nginx.service -n 50"]},
                            {"Failed units": 1, "Oldest failure": "2 h ago"}, "warn", "sample-units", "failed_units"),
        "recovery": Event("recovery", "ok", "Disk space", "/ is back to 31% free after the daily cleanup",
                          {"done": ["Docker build cache pruned: 21.2 GiB freed"]},
                          {"Was": "4% free", "Now": "31% free", "Problem lasted": "5h02m", "was": "crit"},
                          "ok", "sample-disk", "disk_forecast"),
        "maintenance": Event("maintenance", "ok", "Daily cleanup finished", "3 cleanups ran, 24.6 GiB freed (report mode skipped 2)",
                             {"done": ["Docker build cache pruned: 21.2 GiB freed", "Old snap revisions removed: 3.1 GiB",
                                       "Kavita logs trimmed: 0.3 GiB"]},
                             {"tiles": [["24.6 GiB", "freed", "ok"], ["3", "actions"], ["0", "refused"], ["41 s", "took"]],
                              "Mode": "apply (docker_cache, snap_revisions, retention)", "significant": False,
                              "link": "#/maintenance"}, "ok", "sample-daily", "daily"),
        "digest_daily": Event("digest_daily", "ok", "All clear", "Nothing needed attention in the last 24 hours.",
                              {"text": ["All 31 checks healthy at the last run.", "Daily cleanup freed 24.6 GiB.",
                                        "Backups: system 2 days ago, Immich 3 days ago, stack 6 h ago."]},
                              {"tiles": [["A", "health", "ok"], ["31/31", "checks"], ["0", "incidents"], ["24.6 GiB", "freed"]],
                               "link": "#/health"}, None, "sample-daily-digest"),
        "report_weekly": Event("report_weekly", "info", "Week 40", "One warning cleared on its own; storage trend steady.",
                               {"text": ["Health score 96 (A). Worst moment: memory PSI 12% on Tuesday, handled without a restart.",
                                         "Space reclaimed this week: 61.4 GiB."],
                                "sections": [{"title": "Capacity", "kv": [["/", "31% free, 38 days to full"],
                                                                         ["/media/SandiskSSD", "52% free"]]},
                                             {"title": "Recommendations", "lines": ["Archive the 10.8 GiB kometa backup tarball."]}]},
                               {"tiles": [["96", "score", "ok"], ["1", "incidents"], ["61.4 GiB", "freed"], ["99.8%", "uptime"]],
                                "link": "#/reports"}, None, "sample-weekly"),
        "incident_open": Event("incident_open", "crit", "Plex media mount missing",
                               "Plex would regenerate Media on the root disk",
                               {"timeline": [["22:01", "check turned critical"], ["22:16", "confirmed on the second run"]],
                                "todo": ["Check the bind mount: findmnt \"/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server/Media\"",
                                         "Re-mount from /media/SandiskSSD/plex before Plex starts scanning."]},
                               {"sev": "SEV2", "Impact": "Plex preview thumbnails would fill /", "link": "#/incidents"},
                               "crit", "sample-incident-1", "plex_media_mount_check"),
        "incident_resolved": Event("incident_resolved", "ok", "Plex media mount missing", "Mounted again; Plex scan resumed normally.",
                                   {"timeline": [["22:01", "detected"], ["22:16", "alert sent"], ["22:31", "mount restored"]]},
                                   {"Time to detect": "15m00s", "Time to resolve": "30m00s", "link": "#/incidents", "was": "crit"},
                                   "ok", "sample-incident-1", "plex_media_mount_check"),
        "ack_expired.failing": Event("ack_expired", "warn", "Services",
                                     "The 90-day acknowledgement of this exact error ended on 2026-10-02 and it is still failing: "
                                     "1 failed unit: nginx.service (failed 2 h ago)",
                                     {"todo": ["Fix it, or acknowledge it again for another 90 days with the button above.",
                                               "Until you do, it alerts again like any other problem."]},
                                     {"Acknowledged": "90 days, by email", "Acknowledged on": "2026-07-04", "Ended": "2026-10-02",
                                      "Held back": "14 alerts and emails", "Now": "still failing (warn)", "still_failing": True,
                                      "ack_fp": T.PREVIEW_ID, "ack_summary": "1 failed unit: nginx.service (failed 2 h ago)",
                                      "link": "#/health"}, None, "sample-ack-expired-1", "failed_units"),
        "ack_expired.gone": Event("ack_expired", "ok", "Backups",
                                  "The 90-day acknowledgement of this exact error ended on 2026-10-02. It is no longer occurring, so nothing needs doing.",
                                  {"todo": ["Nothing to do: alerts for this exact error are active again, in case it comes back."]},
                                  {"Acknowledged": "90 days, by web", "Acknowledged on": "2026-07-04", "Ended": "2026-10-02",
                                   "Held back": "3 alerts and emails", "Now": "no longer occurring", "still_failing": False,
                                   "link": "#/health"}, None, "sample-ack-expired-2", "backup_freshness"),
    }


def send_test(kinds=None, cfg: dict | None = None, now: float | None = None, *, transport: Transport | None = None,
              fallback: Transport | None = None, dry_run: bool = False) -> list[Delivery]:
    """One clearly labelled TEST per requested kind ("alert", "alert.warn", "recovery", ...; default: all). A test is
    routed exactly like the real kind/severity (so you see which channels it really uses) but is exempt from quiet
    hours and never touches dedupe/escalation state."""
    samples = sample_events()
    keys: list[str] = []
    out: list[Delivery] = []
    for want in (list(kinds) if kinds else list(samples)):
        hit = [k for k in samples if k == want or k.split(".")[0] == want]       # "alert" = alert.crit + alert.warn
        if not hit:
            out.append(Delivery(kind=str(want), note=f"unknown kind {str(want)[:30]!r} (known: {', '.join(samples)})"))
        keys += [k for k in hit if k not in keys]
    for k in keys:
        s = samples[k]
        facts = dict(s.facts or {})
        facts["as"] = s.kind
        facts.pop("significant", None)
        out.append(send(Event("test", s.severity, s.title, s.summary, s.details, facts, s.status, f"test-{k}", s.task),
                        cfg, now, transport=transport, fallback=fallback, dry_run=dry_run))
    return out


DETAIL_BYTES = 20_000


def _tail_text(buf: bytes, total: int, nbytes: int) -> str:
    """The last `nbytes` of a stream as text starting at a line boundary: the fragment cut by the byte window goes."""
    cut = total > nbytes
    text = buf[-nbytes:].decode("utf-8", "replace")
    if cut:
        i = text.find("\n")
        if 0 <= i < len(text) - 1:
            text = text[i + 1:]
    return text


def read_tail(path: str, nbytes: int = DETAIL_BYTES) -> tuple[str, str]:
    """-> (text, problem). The last `nbytes` of a REGULAR file, or ("", why) when it cannot be used. It never blocks and
    never reads a whole log: the file is opened O_NONBLOCK (a FIFO opens at once and is then refused, instead of
    hanging the hook), fstat must say regular file (no device, directory or socket), and only the tail is read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0))
    except OSError as exc:
        return "", exc.strerror or type(exc).__name__
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "", "not a regular file"
        start = max(0, st.st_size - nbytes)
        os.lseek(fd, start, os.SEEK_SET)
        buf = b""
        while len(buf) < 2 * nbytes:                                  # a log that is still growing cannot make this unbounded
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
        return _tail_text(buf, start + len(buf), nbytes), ""
    except OSError as exc:
        return "", exc.strerror or type(exc).__name__
    finally:
        os.close(fd)


def read_stdin_tail(nbytes: int = DETAIL_BYTES, wait_s: float = 10.0, limit: int = 1 << 20) -> tuple[str, str]:
    """-> (text, problem): the tail of what a pipe delivers on stdin (`systemctl status ... | notify send ... --detail-file -`).
    A writer that never closes the pipe cannot hang the hook: after `wait_s` seconds what has arrived is used."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):                      # not a real descriptor (a test, an embedding)
        try:
            data = sys.stdin.read(limit) or ""
        except OSError as exc:
            return "", exc.strerror or "stdin unreadable"
        b = data.encode("utf-8", "replace")
        return _tail_text(b, len(b), nbytes), ""
    buf, total, deadline = b"", 0, time.monotonic() + wait_s
    try:
        while True:                                                    # to EOF: it is the END of the stream that is wanted
            left = deadline - time.monotonic()
            if left <= 0:
                return _tail_text(buf, total, nbytes), "stdin did not close, used what arrived"
            if not select.select([fd], [], [], min(left, 1.0))[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            buf = (buf + chunk)[-2 * nbytes:]
    except OSError as exc:
        return _tail_text(buf, total, nbytes), exc.strerror or "stdin unreadable"
    return _tail_text(buf, total, nbytes), ""


def _cli_send(argv: list[str]) -> int:
    """`notify send KIND SEVERITY TITLE [SUMMARY]`: one event from a shell hook (a systemd OnFailure unit, a legacy script
    that is being migrated) through the same routes, templates and budgets as everything else. A hook must fail OPEN: a
    log that cannot be read (the run died before writing it, which is exactly what an OnFailure hook is for) is noted in
    the message and the message is sent anyway."""
    import argparse
    ap = argparse.ArgumentParser(prog="homelab-maint notify send", description=_cli_send.__doc__)
    ap.add_argument("kind", choices=[k for k in KINDS if k != "test"])
    ap.add_argument("severity", choices=list(SEVERITIES))
    ap.add_argument("title")
    ap.add_argument("summary", nargs="?", default="")
    ap.add_argument("--task", help="check/job name: playbook lookup, per-task route, and the default dedupe key")
    ap.add_argument("--key", help="dedupe key (default: the task, else the title)")
    ap.add_argument("--fact", action="append", default=[], metavar="LABEL=VALUE", help="a row in the facts table (repeatable)")
    ap.add_argument("--detail-file", metavar="PATH|-", help="a log excerpt for the email: the LAST 20 KB of a regular file, or '-' for stdin "
                                                            "(a missing or unreadable file is noted in the email, never a reason not to send)")
    ap.add_argument("--done", action="append", default=[], metavar="TEXT", help="a line under 'What was done' (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="render and route, send nothing, record nothing")
    a = ap.parse_args(argv)
    if any("=" not in f or not f.partition("=")[0].strip() for f in a.fact):
        ap.error("--fact must be LABEL=VALUE")
    facts: dict[str, Any] = {k.strip(): v.strip() for k, _s, v in (f.partition("=") for f in a.fact)}
    det: dict[str, Any] = {}
    if a.done:
        det["done"] = a.done
    if a.detail_file:
        text, why = read_stdin_tail() if a.detail_file == "-" else read_tail(a.detail_file)
        if why:
            print(f"detail {a.detail_file}: {why}" + (" (sending without it)" if not text else ""), file=sys.stderr)
        det["log"] = text if text else [f"(log unavailable: {redact_log(why, 60) or 'empty'})"]
    d = send(Event(a.kind, a.severity, a.title, a.summary, det or None, facts or None, None, a.key, a.task), dry_run=a.dry_run)
    what = "sent via " + "+".join(d.channels) if d.ok else "queued for retry" if d.queued else f"not sent ({d.skipped or 'failed'})"
    print(f"{d.kind} {d.severity}: {what}" + (f" - {d.note}" if d.note else ""))
    if a.dry_run and d.rendered:
        print(f"subject: {d.rendered['subject']}\nsms ({len(d.rendered['sms'])}): {d.rendered['sms']}")
    # Queued is a success for the hook: the page is durable and replayed by the next flush. A non-zero exit would also mark the
    # OnFailure unit failed, and failed_units would then report a transient mail outage until someone resets it.
    return 0 if (d.ok or d.handled or d.queued) else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--child" in argv:
        return child_main()
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "test":
        dry = "--dry-run" in rest
        res = send_test([a for a in rest if not a.startswith("--")] or None, dry_run=dry)
        for d in res:
            print(f"{d.kind:<8} {d.severity:<5} ok={d.ok!s:<5} via={'+'.join(d.channels or d.attempted) or '-':<10} "
                  f"{d.skipped or ''} {d.note}")
            if dry and d.rendered:
                print(f"    subject: {d.rendered['subject']}\n    sms ({len(d.rendered['sms'])}): {d.rendered['sms']}")
        return 0 if all(d.ok or d.handled for d in res) else 1            # non-zero when a test message did not go out
    if cmd == "route":
        now = float(rest[rest.index("--now") + 1]) if "--now" in rest else time.time()       # --now EPOCH: what-if (tests, quiet hours)
        names = [a for i, a in enumerate(rest) if not a.startswith("--") and not (i and rest[i - 1] == "--now")]
        nc, st = load_config(), copy.deepcopy(_EMPTY_STATE)
        if in_quiet_hours(now, nc["quiet_hours"]):
            print(f"(quiet hours: non-critical SMS is held until {str(nc['quiet_hours'].get('window', '')).split('-')[-1]}; criticals still text)")
        for k, ev in sample_events().items():
            if names and not any(k == a or k.split(".")[0] == a for a in names):
                continue
            dec = decide(ev, nc, st, now)
            print(f"{k:<20} -> {'+'.join(dec.channels) or dec.skip}  ({'; '.join(dec.why)})")
        return 0
    if cmd == "render":
        out = Path(rest[rest.index("--out") + 1]) if "--out" in rest else Path.cwd()
        out.mkdir(parents=True, exist_ok=True)
        nc = load_config()
        nc["ack"]["button"] = True                          # local files for looking at the design: the button is shown (and inert) whatever the site's state
        names = [a for a in rest if not a.startswith("--") and a != str(out)]
        for k, ev in sample_events().items():
            if names and not any(k == a or k.split(".")[0] == a for a in names):
                continue
            dec = decide(ev, nc, copy.deepcopy(_EMPTY_STATE), time.time())
            dec.channels = list(CHANNELS)
            msg, _n = _render(ev, nc, dec, time.time())
            (out / f"{k}.html").write_text(msg.html)
            (out / f"{k}.txt").write_text(f"{msg.subject}\n\nSMS ({len(msg.sms)}): {msg.sms}\n\n{msg.plain}")
            print(f"wrote {out / (k + '.html')}")
        return 0
    if cmd == "export":
        print(json.dumps(export(), indent=1))
        return 0
    if cmd == "flush":
        res = flush_pending()
        for d in res:
            print(f"{d.kind:<8} {d.severity:<5} ok={d.ok!s:<5} {d.skipped or '':<8} {d.note}")
        print(f"{len(res)} queued message(s) / text(s) retried" if res else "nothing pending (or not due yet)")
        return 0 if all(d.ok or d.handled for d in res) else 1
    if cmd == "legs":
        print(json.dumps(leg_health(), indent=1))
        return 0
    if cmd == "send":
        return _cli_send(rest)
    if cmd == "doctor":
        rows = doctor()
        for label, ok, hint in rows:
            print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f"  ({hint})" if not ok and hint else ""))
        return 0 if all(ok for _l, ok, _h in rows) else 1
    print("CLI (also" + __doc__.split("CLI (also", 1)[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
