"""acks: acknowledged known issues (SPEC5). "I understand this error and I'm okay with it": that EXACT error stops paging for N days.

    fingerprint(task, result|summary|status-entry, severity) -> Fp  16 hex = sha1(task|issue_key)[:16] (a str subclass; "" = none)
    is_acked(fp, severity, now) -> AckInfo | None      the one question the notifier asks (read-only, lock-free, fails closed)
    apply_to_status(status, now) / mark_entry(...)     status.json entries get `fp` and, when covered, `acked: {...}`; overall skips them
    record_suppressed(fp, now)                          counter for "held back N alerts"
    issue_token(fp, task, title, summary, severity, now, ttl_days) -> token   plaintext returned ONCE; only sha256 is stored
    process_inbox(now) -> Report      the web container's requests (signed, strictly validated, rate limited); expire(now) -> [fp]
    add(...) / remove(...) / export_public(now) / export_tokens(now) / main(argv)   python3 -m homelab_maint.acks process|list|...

Files (STATE_DIR): acks.json 0600 (acks + token hashes + small bookkeeping), acks.jsonl audit trail, acks.lock flock,
public/acks.json 0644 (no tokens, no hashes), ack/tokens.json 0644 (hash -> {fp, exp, used, ...}: the website's verify-only view),
ack/inbox/ (written by the container, the ONLY write surface it has), ack/web.key (HMAC key, root + container group only).

WHAT AN ACKNOWLEDGEMENT IS. It silences THAT fingerprint at the acknowledged severity or lower, for `days` (default 90). A worse
severity (warn -> crit) is not covered. Everything else about the issue stays true: status.json keeps the real status (the entry only
gains `acked`), incidents stay open ("acknowledged"), SLOs are untouched. Expiry resumes alerting and yields ONE notice.

FINGERPRINT = which error it is, never how big it is, EXCEPT where a problem can only get worse without ever changing severity (a backup
that is a day or a year late is "warn" both times; so are 2 and 8000 reallocated sectors): there a rule names a magnitude group and its
DECADE (1-9, 10-99, 100-999 ...) is part of the error, so the next decade alerts once more. issue_key is Result.issue_key when a task sets
one, else a per-task rule from etc/ack.toml (the unit names, mount names, probe names, with the COUNT of what the task cut off; derived from
every task's real summary format), else normalize(summary) (standalone numbers, sizes, percentages, durations, times, hex ids replaced by #;
digits that belong to a name stay). Different failed unit/mount/probe/disk = different fingerprint. A status of "error" (the check itself
failed) is never the same error as a condition it reports.

WHICH TASKS AND SEVERITIES: policy_ok / ackable() is the one task rule (explicit issue_key, [key.<task>] rule or allow_tasks, minus
deny_tasks/deny_prefixes); severity_allowed() narrows it by severity ([ack] severities, warn only by default, so a CRIT issue always alerts
and "error" counts as crit). add(), the inbox, issue_token(), mark_entry(), is_acked(), notify's button and the public export all use both,
so an issue that is not ackable has no id, no button, no token, no acknowledgement, and notify holds nothing for it.

FAIL CLOSED, everywhere: an unreadable or corrupt store, a broken rule, an unknown severity, an exception, a doubt of any kind means
"not acknowledged" (the alert goes out); a request that is not exactly right is refused and quarantined, never half-applied.
Secrets: tokens are 256-bit random, only SHA-256(token) is ever written; signatures use hmac.compare_digest; nothing from a request
is ever executed or used to build a path; every string that reaches a public file is redacted.

THE WEBSITE'S LOGIN rides the same inbox: process_inbox registers the `auth_setup` / `auth_change` handlers of acks_auth (first-run setup proved by
a key derived from ack/bootstrap.secret, ack/auth.json written 0640 for the site's group, recovery codes burned durably) before it reads the first
request; `homelab-maint web bootstrap` (web_main) shows the secret once on a root terminal; auth_state() is what doctor reads.

LOCKS: acks.lock guards acks.json. Order is state.lock (cli.cmd_run) -> acks.lock, never the reverse: process_inbox releases the
acks lock before it touches status.json. Readers take no lock (every write is an atomic replace).
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import functools
import hashlib
import hmac
import itertools
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import core

V = 1
FP_RE = re.compile(r"[0-9a-f]{16}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")          # secrets.token_urlsafe(32)
NAME_RE = re.compile(r"\d{10,14}-[0-9a-f]{8}\.json\Z")  # inbox file: <epoch_ms>-<8hex>.json
RANK = {"warn": 1, "crit": 2}
BY = ("email", "web", "cli")
MAX_STORE = 8 << 20                                      # a store beyond this is treated as corrupt (the cap on acks/tokens keeps it ~0.3 MB)
LOG_MAX = 2 << 20                                        # acks.jsonl is compacted to its newest half above this
SEEN_KEEP_S = 7200                                       # request digests remembered (replay guard); the +-skew window is 20 minutes
PUBLIC_REJECT_S = 3600                                   # how long a refused (authentic) web request stays listed in public/acks.json
TMP_MAX_AGE = 600                                        # leftovers of a killed writer older than this are swept
LIST_MAX = 20000                                         # directory entries looked at per inbox run (a flooded directory costs a bounded listing)

# --------------------------------------------------------------------------- configuration
# The baseline lives HERE (versioned with the code, so a missing or stale /etc copy can only mean "less specific", never "silenced").
# /etc/homelab-maint/ack.toml overrides it per [ack]/[inbox] key and per [key.<task>] table; tests keep etc/ack.toml (the commented
# template) equal to this text. A broken override file is ignored as a whole: the alert path must fail towards ALERTING.
_BASELINE = r'''
[ack]
days = 90
min_days = 1
max_days = 365
severities = ["warn"]
escalation_breaks = true
max_acks_per_day = 50
max_active = 500
token_ttl_days = 30
max_tokens = 500
expired_keep_days = 30
notice_attempts = 5
suppress_log_s = 3600
require_rule = true
allow_tasks = []
deny_tasks = ["smart_event"]
deny_prefixes = ["job:"]
[inbox]
max_bytes = 2048
skew_s = 600
max_files_per_run = 100
rejected_keep = 200
require_signature = true
unsigned_sources = []
[key.disk_forecast]
mode = "regex"
regex = ['(?:^|; )(/[^;]*?) \d+% free']
[key.failed_units]
mode = "regex"
regex = ['(\d+) failed unit\(s\)', 'failed unit\(s\): ([^;]*)', '(\d+) unexpected exited', 'unexpected exited: ([^;]*)', '(\d+) unhealthy', 'unhealthy: ([^;]*)', '(\d+) restarting', 'restarting: ([^;]*)']
[key.backup_freshness]
mode = "regex"
regex = ['(?:^|; )([\w.-]+ [A-Z_]+) \(\S+ ago\)', '(?:^|; )([\w.-]+) (?P<mag>\S+) old \(limit']
[key.docker_df]
mode = "task"
[key.memory_health]
mode = "regex"
regex = ['(memory stall)', '\b(available)\b', '(swap-in) (?P<mag>\d+) pages', '(?P<mag>\d+) (oom kill)']
[key.plex_media_mount_check]
mode = "regex"
regex = ['(is not mounted)', 'mounted from [^,]+, (expected under \S+)']
[key.smart_trend]
mode = "regex"
split = "; "
alias = ["devices", "dev", "model"]
regex = ['([A-Za-z][\w-]*) \+(?P<mag>\d+)', '\d+(C)\b', '(smartd data) \d+h old', '(unreadable)', '(no parsable rows)']
[key.alert_path_health]
mode = "regex"
regex = ['(bridge missing|smartd hook broken)', '\d+ (smart hook|notifier) sends failed', '(smart hook|notifier) log unreadable']
[key.growth_watch]
mode = "regex"
regex = ['^(growth over limit|growth blind)', '([^\s,;:]+) (?P<mag>[+-]?\d+(?:\.\d+)?) GiB/d', 'cut off\): ([^;]*)', 'blind >\S+: (\d+)/']
[key.stuck_detector]
mode = "regex"
regex = ['(\d+) actionable', 'actionable(?:, restarted \S+)?: (\S+) (?P<mag>\d[\d.]* ?[KMGT]?i?B)']
[key.orphan_report]
mode = "regex"
regex = ['(?P<mag>\d+) (idle gradle daemon|orphan emulator|stray server|orphan crashpad handler|zombie|long-running emulator)', '\((?P<mag>\d[\d.]* ?[KMGT]?i?B) anon\)']
[key.pressure_state]
mode = "regex"
regex = ['^(L\d)\b', '\b(memory stall|memory wait|avail|swap-in|io stall|io wait|cpu wait|vram)\b']
[key.pressure_response]
mode = "regex"
regex = ['\d+ (failed)\b', '(throttle\(s\) not restored)', '(ALERT ONLY)']
[key.probes]
mode = "regex"
regex = ['(\d+) down:', 'down: ([^;]*)', '(\d+) degraded:', 'degraded: ([^;]*)', '(\d+) flapping:', 'flapping: ([^;]*)', '(\d+ bad probe defs)']
[key.os_jobs]
mode = "regex"
regex = ['(?:attention: |; )([\w.@-]+ (?:overdue|failed|inactive|unknown))', '(\d+) of \d+ need attention']
[key.docker_prune_exposure]
mode = "regex"
regex = ['deletes container (.+?)(?= \+ image|;|$)', '\+ image (.+?)(?= \(|;|$)']
[key.immich_recycle]
mode = "regex"
regex = ['(restart of \S+ failed)', '(\S+ restarted but is \S+)']
[key.comfyui_idle_reclaim]
mode = "regex"
regex = ['(restart of \S+ failed)']
'''
_INT_KEYS = {"ack": {"days": (1, 365), "min_days": (1, 365), "max_days": (1, 365), "max_acks_per_day": (1, 10000),
                     "max_active": (1, 5000), "token_ttl_days": (1, 90), "max_tokens": (10, 5000), "expired_keep_days": (1, 365),
                     "notice_attempts": (1, 20), "suppress_log_s": (0, 86400)},
             "inbox": {"max_bytes": (256, 16384), "skew_s": (30, 3600), "max_files_per_run": (1, 1000), "rejected_keep": (0, 5000)}}
_BOOL_KEYS = {"ack": ("escalation_breaks", "require_rule"), "inbox": ("require_signature",)}
_LIST_KEYS = ("allow_tasks", "deny_tasks", "deny_prefixes")        # [ack]: which tasks may be acknowledged at all (see policy_ok)
_MODES = ("text", "task", "regex")
MAX_PATTERNS, MAX_PATTERN_LEN = 10, 240
_IDENT = re.compile(r"[A-Za-z0-9_]{1,40}\Z")


def _tbl(text: str) -> dict:
    try:
        d = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


_BASE = _tbl(_BASELINE)                                  # parsed once; load_config copies what it changes


def _rule(raw: Any) -> dict | None:
    """One [key.<task>] table -> a normalised rule, or None when it is not usable (the caller keeps the baseline rule).
    Beyond mode/regex/sort: `split` (regex mode: the summary is cut into segments at this separator, each segment is "<entity> <detail>" and
    every part is tagged with the entity) and `alias = [list, from, to]` (rename the entity through status metrics: metrics[list] is a list of
    dicts and the entity whose `from` field equals the name becomes its `to` field, e.g. the kernel name sda -> model + serial)."""
    if not isinstance(raw, dict) or raw.get("mode", "text") not in _MODES:
        return None
    rx = raw.get("regex", [])
    if raw.get("mode") == "regex":
        if not isinstance(rx, list) or not 0 < len(rx) <= MAX_PATTERNS:
            return None
        for p in rx:
            if not isinstance(p, str) or not 0 < len(p) <= MAX_PATTERN_LEN:
                return None
            try:
                re.compile(p, re.I)
            except re.error:
                return None
    split, alias = raw.get("split"), raw.get("alias")
    if raw.get("mode") == "regex":
        if "split" in raw and not (isinstance(split, str) and 0 < len(split) <= 8):
            return None
        if "alias" in raw and not (isinstance(alias, list) and len(alias) == 3 and all(isinstance(x, str) and _IDENT.match(x) for x in alias)):
            return None
    else:
        split = alias = None
    return {"mode": raw.get("mode", "text"), "regex": list(rx) if raw.get("mode") == "regex" else [], "sort": raw.get("sort") is True,
            "split": split if isinstance(split, str) else "", "alias": list(alias) if isinstance(alias, list) else []}


def _merge(cfg: dict, raw: dict, errors: list[str], label: str) -> None:
    for sect, keys in _INT_KEYS.items():
        t = raw.get(sect)
        for k, (lo, hi) in keys.items():
            if isinstance(t, dict) and k in t:
                v = t[k]
                if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                    cfg[sect][k] = v
                else:
                    errors.append(f"{label}: [{sect}] {k} = {v!r} ignored (integer {lo}..{hi})")
        for k in _BOOL_KEYS[sect]:
            if isinstance(t, dict) and k in t:
                if isinstance(t[k], bool):
                    cfg[sect][k] = t[k]
                else:
                    errors.append(f"{label}: [{sect}] {k} ignored (true or false)")
    t = raw.get("inbox")
    if isinstance(t, dict) and "unsigned_sources" in t:
        us = t["unsigned_sources"]
        if isinstance(us, list) and all(x in ("email", "web") for x in us):
            cfg["inbox"]["unsigned_sources"] = list(us)
        else:
            errors.append(f"{label}: [inbox] unsigned_sources ignored")
    t = raw.get("ack")
    for k in _LIST_KEYS:
        if isinstance(t, dict) and k in t:
            v = t[k]
            if isinstance(v, list) and len(v) <= 200 and all(isinstance(x, str) and 0 < len(x) <= 80 for x in v):
                cfg["ack"][k] = list(v)
            else:
                errors.append(f"{label}: [ack] {k} ignored (a list of up to 200 task names)")
    if isinstance(t, dict) and "severities" in t:
        v = t["severities"]
        if isinstance(v, list) and v and all(x in RANK for x in v):
            cfg["ack"]["severities"] = sorted({x for x in v}, key=RANK.__getitem__)
        else:
            errors.append(f"{label}: [ack] severities ignored (a non-empty list of warn, crit)")
    keys = raw.get("key")
    for task, r in (keys.items() if isinstance(keys, dict) else []):
        rule = _rule(r)
        if rule is not None and isinstance(task, str) and 0 < len(task) <= 80:
            cfg["key"][task] = rule
        else:
            errors.append(f"{label}: [key.{task}] ignored (mode text|task|regex, 1..{MAX_PATTERNS} valid regexes)")
    cfg["ack"]["max_days"] = max(cfg["ack"]["max_days"], cfg["ack"]["min_days"])
    cfg["ack"]["days"] = min(max(cfg["ack"]["days"], cfg["ack"]["min_days"]), cfg["ack"]["max_days"])


def load_config(with_errors: bool = False) -> Any:
    """baseline (this file) overlaid by CONF_DIR/ack.toml. Never raises; a bad override is dropped as a whole (and listed in errors)."""
    cfg: dict = {"ack": {}, "inbox": {}, "key": {}}
    errors: list[str] = []
    base = _BASE
    for sect in ("ack", "inbox"):
        cfg[sect] = {k: (list(v) if isinstance(v, list) else v) for k, v in base.get(sect, {}).items()}
    for task, r in (base.get("key") or {}).items():
        cfg["key"][task] = _rule(r)
    try:
        text = (Path(core.CONF_DIR) / "ack.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError) as exc:
        text = None
        errors.append(f"ack.toml unreadable ({type(exc).__name__}): baseline used")
    if text is not None:
        try:
            over = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            over = {}
            errors.append(f"ack.toml is not valid TOML ({str(exc)[:80]}): baseline used")
        _merge(cfg, over, errors, "ack.toml")
    return (cfg, errors) if with_errors else cfg


def validate() -> list[str]:
    return load_config(True)[1]


# --------------------------------------------------------------------------- small helpers
class AckError(Exception):
    """A request that cannot be honoured. `code` is the machine reason (CLI exit text, inbox quarantine)."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.code = code


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WS = re.compile(r"\s+")


def _clean(s: Any, n: int = 200) -> str:
    """One printable line: control/format characters become spaces, whitespace collapses, cut at n."""
    t = "".join(" " if (unicodedata.category(c)[0] == "C" or _CTRL.match(c)) else c for c in str(s if s is not None else ""))
    return _WS.sub(" ", t).strip()[:n]


_R_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_R_PHONE = re.compile(r"(?<![\w.+-])(?:\+\d{10,15}(?!\d)|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-]))")
_R_URLQ = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s?#]*)[?#]\S*")
_R_BLOB = re.compile(r"[A-Za-z0-9_+=-]{32,}")             # tokens, hashes, HMACs, keys (no "/": a long path is not a secret)
_R_KV = re.compile(r"(?i)\b(pass(?:word|wd|phrase)?|secret|token|api[_-]?key|authorization|bearer|cookie)\b\s*[:=]\s*\S+")


def _redact(s: Any, n: int = 200) -> str:
    """Text for a public file or a log line: addresses, phone numbers, URL queries, key=value secrets and long opaque blobs removed.
    A local pass always runs; publish.clean (the project's redactor) is applied on top when it can be imported."""
    t = _clean(s, max(n * 2, 400))
    t = _R_KV.sub(lambda m: m.group(1) + "=[redacted]", t)
    t = _R_URLQ.sub(r"\1", t)
    t = _R_EMAIL.sub("[redacted]", t)
    t = _R_PHONE.sub("[redacted]", t)
    t = _R_BLOB.sub("[redacted]", t)
    try:
        from . import publish
        t = publish.clean(t, n)
    except Exception:  # noqa: BLE001 - the local pass above is already conservative
        pass
    return _clean(t, n)


def sev_of(x: Any) -> str:
    """warn | crit | "" : crit/error -> crit, warn -> warn, everything else (ok, info, skipped, junk) has no severity to acknowledge."""
    s = str(x or "").strip().lower()
    return "crit" if s in ("crit", "error") else "warn" if s == "warn" else ""


def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    f = float(v)
    return f if math.isfinite(f) and abs(f) < 1e13 else default


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _now(now: Any) -> float:
    n = _num(now)
    return time.time() if n is None else n


def token_hash(token: str) -> str:
    """The only form of a token that is ever stored, logged or exported."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _day(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(t)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


# --------------------------------------------------------------------------- fingerprint
class Fp(str):
    """The fingerprint: 16 lowercase hex characters (a str, so it works as a dict key and in notify's checks), carrying how it was made.
    An EMPTY Fp means "cannot be fingerprinted": nothing is ever acknowledged or suppressed for it. `ackable` is policy_ok's verdict."""
    task: str
    key: str
    severity: str
    mode: str
    ackable: bool

    def __new__(cls, fp: str = "", task: str = "", key: str = "", severity: str = "", mode: str = "", ackable: bool = False):
        o = super().__new__(cls, fp)
        o.task, o.key, o.severity, o.mode, o.ackable = task, key, severity, mode, bool(fp) and ackable
        return o

    @property
    def fp(self) -> str:
        return str(self)

    id = fp


_STATUS_PREFIX = re.compile(r"^(?:warn|crit|error|info|ok|skipped):\s*")
# "(+2 more)", "(+2)", "(2 more)", "; +2 more": the task cut its list. The COUNT is part of the error (a 5th failed unit behind the
# visible four is not "the same error"), so it is kept as a number, never stripped.
_TAIL_MORE = re.compile(r"\s*\((?:\+(\d+)(?: more)?|(\d+) more)\)|\s*;?\s*\+(\d+) more\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEXID = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b")                       # container/image ids: >= 8 hex with a digit (not the word "defaced")
# A number glued to a NAME is part of the name: immich-1, foo@1.service, /mnt/2tb, 10.0.0.5, nvme0, sda1 stay different. Only standalone numbers
# (preceded by a space, a bracket, a sign, "=") are volatile values.
_G = r"(?<![\w@/.:])(?<!\w-)"
_DATETIME = re.compile(_G + r"\d{4}-\d{2}-\d{2}(?:[ t]\d{2}:\d{2}(?::\d{2})?)?\b|" + _G + r"\d{1,2}:\d{2}(?::\d{2})?\b")
_SIZE = re.compile(_G + r"\d+(?:\.\d+)?\s?(?:[kmgtp]i?b|bytes?)\b")
_DUR_UNIT = r"(?:ms|s|sec|secs|m|min|mins|h|hr|hrs|d|w)"
_DUR = re.compile(_G + rf"\d+(?:\.\d+)?{_DUR_UNIT}(?:\d+(?:\.\d+)?{_DUR_UNIT})*\b")  # 6d10h, 28h, 13h31m, 5d
_PCT = re.compile(_G + r"\d+(?:\.\d+)?\s?%")
_FRAC = re.compile(_G + r"\d+(?:\.\d+)?/\d+(?:\.\d+)?(?![\w@/:])")              # 25/27
_NUM = re.compile(_G + r"\d+(?:\.\d+)?(?!\.\d)")                                 # not "1.2.3" or "10.0.0.5" either
# a number that NAMES something rather than measures it: "port 5432", "error 500", "exit code 137", "rc=1", "signal 9", "pid 41"
_CODE_CTX = re.compile(r"\b(?:ports?|errors?|errno|code|rc|exit|status|signal|http|pid)\W{0,3}\Z", re.I)


def _mask_num(m: re.Match) -> str:
    return m.group(0) if _CODE_CTX.search(m.string, 0, m.start()) else "#"


def _norm(text: Any, limit: int = 160) -> tuple[str, list[str]]:
    """(body, overflow counts): the normalised text without its "(+N more)" tails, and those N as strings (in order of appearance)."""
    t = _clean(text, 600).lower()
    t = _STATUS_PREFIX.sub("", t)
    more = [next(g for g in m if g) for m in _TAIL_MORE.findall(t)]
    t = _TAIL_MORE.sub("", t)
    for rx in (_UUID, _HEXID, _DATETIME, _SIZE, _DUR, _PCT, _FRAC):
        t = rx.sub("#", t)
    t = _NUM.sub(_mask_num, t)
    return _WS.sub(" ", t).strip()[:limit], more


def normalize(text: Any, limit: int = 160) -> str:
    """The text of an error with everything that merely MOVES taken out: lowercase; the status prefix, ids, dates, times, sizes, durations,
    percentages, fractions and every other STANDALONE number replaced by # (a number glued to a name, or naming a port / error / exit code,
    stays); whitespace collapsed; cut at `limit`. The "(+N more)" overflow counts follow the text as " +N": a longer hidden list is another error."""
    body, more = _norm(text, limit)
    return body + "".join(f" +{n}" for n in more)


@functools.lru_cache(maxsize=256)
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


_OVERFLOW = re.compile(r"\s*(?:\+(\d+)|\(\+(\d+)(?: more)?\))\s*$")


def _part(s: str) -> tuple[str, str]:
    """One extracted name: (lowercase name with edge punctuation removed, the "+N" overflow count it carried or "").
    "Radarr +1" -> ("radarr", "1"): the marker is not part of the name, but the count is part of the error."""
    t = _WS.sub(" ", s.lower()).strip()
    m = _OVERFLOW.search(t)
    n = (m.group(1) or m.group(2)) if m else ""
    if m:
        t = t[:m.start()]
    return t.strip(" .:;,")[:80], n


# magnitude groups: (?P<mag>...) in a rule's pattern. The captured quantity becomes a DECADE bucket (b1 = 1..9, b2 = 10..99, b3 = 100..999 ...),
# so "+2 reallocated sectors" and "+8000" are different errors (the status stays warn all the way: the severity ceiling cannot see it) while
# +2 and +7 are the same one. Durations are counted in hours and sizes in GiB first.
_Q_PLAIN = re.compile(r"[+-]?(\d+(?:\.\d+)?)\Z")
_Q_DUR = re.compile(rf"(?:\d+(?:\.\d+)?{_DUR_UNIT})+\Z", re.I)
_Q_DUR1 = re.compile(rf"(\d+(?:\.\d+)?)({_DUR_UNIT})", re.I)
_Q_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s?([kmgtp]?)(i?b|ytes?)\Z", re.I)
_HOURS = {"ms": 1 / 3.6e6, "s": 1 / 3600, "sec": 1 / 3600, "secs": 1 / 3600, "m": 1 / 60, "min": 1 / 60, "mins": 1 / 60, "h": 1, "hr": 1, "hrs": 1,
          "d": 24, "w": 168}
_GIB = {"": 1 / 1024 ** 3, "k": 1 / 1024 ** 2, "m": 1 / 1024, "g": 1, "t": 1024, "p": 1024 ** 2}


def _magnitude(text: str) -> float | None:
    t = text.strip()
    m = _Q_PLAIN.match(t)
    if m:
        return float(m.group(1))
    if _Q_DUR.match(t):
        return sum(float(n) * _HOURS[u.lower()] for n, u in _Q_DUR1.findall(t))
    m = _Q_SIZE.match(t)
    if m:
        return float(m.group(1)) * _GIB[m.group(2).lower()]
    return None


def _bucket(text: str) -> str:
    """"28h" -> b2, "30d" -> b3, "+8000" -> b4, "4.1 GiB" -> b1, "0.3" -> b0. A text that is not a quantity stays as it is (more specific)."""
    v = _magnitude(text)
    if v is None or not math.isfinite(v) or v > 1e15:
        return _WS.sub(" ", text.lower()).strip()[:40]
    return "b" + str(len(str(int(v))) if v >= 1 else 0)


def _regex_parts(rule: dict, s: str) -> set[str]:
    """Every part the rule's patterns find in `s`: a part per name of a list group, ONE joined part per match of a pattern with several groups
    (or a magnitude group), plus the "+N" overflow counts of the lists. Bounded by the summary (<= 1000 characters) and the 10 patterns."""
    parts: set[str] = set()
    for i, p in enumerate(rule["regex"]):
        try:
            rx = _rx(p)
            mags = {j for n, j in rx.groupindex.items() if n.startswith("mag")}
            for m in rx.finditer(s):
                groups = m.groups() if rx.groups else (m.group(0),)
                if len(groups) == 1 and not mags:
                    for raw in str(groups[0] if groups[0] is not None else "").split(","):
                        name, more = _part(raw)
                        if name:
                            parts.add(f"{i}:{name}")
                        if more:
                            parts.add(f"{i}:+{more}")
                else:
                    cells = [(_bucket(g) if (j + 1) in mags else _part(g)[0]) if g is not None else "" for j, g in enumerate(groups)]
                    if any(cells):
                        parts.add(f"{i}:" + "/".join(cells))
        except re.error:
            continue
    return parts


def _entity(name: str, rule: dict, metrics: Any) -> str:
    """The identity of a segment's lead word. With rule.alias [list, from, to] and status metrics at hand, sda -> "WDC WD40EFRX 1A2B"
    (model + serial tail): a kernel name can swap between boots and must not hand an acknowledgement to another disk."""
    al = rule.get("alias") or []
    if len(al) == 3 and isinstance(metrics, dict) and isinstance(metrics.get(al[0]), list):
        for row in metrics[al[0]][:200]:
            if isinstance(row, dict) and isinstance(row.get(al[1]), str) and row[al[1]].lower() == name:
                to = _part(row.get(al[2]) if isinstance(row.get(al[2]), str) else "")[0]
                return to or name
    return name


_LABEL = re.compile(r"^[A-Za-z][A-Za-z ]{0,24}: ")


def _segment_parts(rule: dict, s: str, metrics: Any) -> set[str]:
    """rule.split: "SMART: sda realloc +2, 72C; nvme0 70C" -> a segment per disk; each part is tagged with the (aliased) disk."""
    parts: set[str] = set()
    for seg in [x.strip() for x in _LABEL.sub("", s).split(rule["split"]) if x.strip()][:40]:
        lead, _, rest = seg.lower().partition(" ")
        who = _entity(lead, rule, metrics)
        found = _regex_parts(rule, rest) or {"~" + normalize(rest, 60)}      # a grammar nobody wrote a pattern for: its own text, more specific
        parts |= {f"{who}|{x}" for x in found}
    return parts


def _text_key(summary: str, rule: dict | None) -> str:
    body, more = _norm(summary, 600)
    tail = "".join(f" +{n}" for n in more)
    if rule and rule.get("sort"):
        head, sep, rest = body.partition(": ")
        if sep and re.fullmatch(r"[a-z][a-z ]{0,24}", head):          # "smart: sda ...; nvme0 ..." -> sort the segments after the label
            return (head + ": " + "; ".join(sorted(x.strip() for x in rest.split(";") if x.strip())))[:160] + tail
        return "; ".join(sorted(x.strip() for x in body.split(";") if x.strip()))[:160] + tail
    return body[:160] + tail


def _subject(subject: Any) -> tuple[str, Any, Any, Any]:
    """(summary, explicit issue_key, status, metrics) of a Result, a status.json entry or a bare summary string."""
    if isinstance(subject, str):
        return subject, None, "", None
    if isinstance(subject, dict):
        return subject.get("summary"), subject.get("issue_key"), subject.get("status"), subject.get("metrics")
    return getattr(subject, "summary", None), getattr(subject, "issue_key", None), getattr(subject, "status", None), getattr(subject, "metrics", None)


def issue_key(task: str, subject: Any, cfg: dict | None = None) -> tuple[str, str]:
    """(key, mode actually used). `subject` is a Result, a status.json task entry, or a summary string. Result.issue_key wins.
    A status of "error" (the CHECK ITSELF failed: it cannot read /proc/pressure, timed out, crashed) is never the same error as a condition
    the check reports: its key ends in "|error", so acknowledging a high-pressure episode cannot silence a blind monitor."""
    cfg = cfg or load_config()
    summary, explicit, status, metrics = _subject(subject)
    summary = _clean(summary, 1000)
    suffix = "|error" if str(status or "").strip().lower() == "error" or re.match(r"\s*error:", summary, re.I) else ""
    if isinstance(explicit, str) and explicit.strip():
        k = _clean(explicit[:4000], 4000)
        if len(explicit) > 4000:                                         # the WHOLE key is hashed in, never cut: two long keys differing at the tail differ
            k += "#" + hashlib.sha256(explicit.encode("utf-8", "replace")).hexdigest()[:32]
        return k + suffix, "explicit"
    rule = cfg["key"].get(task)
    if rule and rule["mode"] == "task":
        return "*" + suffix, "task"
    if rule and rule["mode"] == "regex":
        s = _STATUS_PREFIX.sub("", summary)
        more = [next(g for g in t if g) for t in _TAIL_MORE.findall(s)]
        s = _TAIL_MORE.sub("", s)
        parts = _segment_parts(rule, s, metrics) if rule.get("split") else _regex_parts(rule, s)
        parts |= {f"more:{n}" for n in more}
        if parts:
            key = "|".join(sorted(parts))
            if len(key) > 600:                                           # never cut: the rest is hashed in, so a long list differs at its tail too
                key = key[:560] + "#" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            return key + suffix, "regex"
        return _text_key(summary, None) + suffix, "text"                 # no match: MORE specific, so it can only alert more, never less
    return _text_key(summary, rule) + suffix, "text"


def _denied(task: str, cfg: dict) -> bool:
    a = cfg["ack"]
    return task in a["deny_tasks"] or any(task.startswith(p) for p in a["deny_prefixes"])


def policy_ok(task: Any, mode: Any = "", cfg: dict | None = None) -> bool:
    """THE policy: may an alert of this task be acknowledged (offered a button, accepted by the inbox/CLI, held by notify, marked in
    status.json, listed publicly)? Only when its fingerprint names THE ERROR and not just its wording: a Result.issue_key (mode "explicit"), a
    per-task rule in etc/ack.toml (mode "task"/"regex", or "text" with a [key.<task>] table), or an allow_tasks entry. The number-blind text of
    a summary keeps the same fingerprint while a failure count grows tenfold, so every other task is refused (require_rule = false lifts it).
    deny_tasks / deny_prefixes (smart_event, job:*) beat everything. Any doubt (no task, odd value, a rules file that cannot be read) is no."""
    try:
        cfg, t = cfg or load_config(), str(task or "").strip()
        if not t or t != task or _denied(t, cfg):
            return False
        a = cfg["ack"]
        if not a["require_rule"] or t in a["allow_tasks"]:
            return True
        m = str(getattr(mode, "mode", mode) or "")
        return m in ("explicit", "task", "regex") or t in cfg["key"]
    except Exception:  # noqa: BLE001
        return False


def severity_allowed(sev: Any, cfg: dict | None = None) -> bool:
    """May an issue at this severity be acknowledged AT ALL? The shipped policy is ["warn"]: a critical issue must keep alerting, so it is
    never offered a button, never given an id and never accepted by the inbox or the CLI. `sev` is normalised with sev_of, so "error" (the
    check itself failed) counts as crit and is refused too. Any doubt (no severity, nothing configured, a broken config) is False: no ack."""
    try:
        s = sev_of(sev)
        return bool(s) and s in (cfg or load_config())["ack"]["severities"]
    except Exception:  # noqa: BLE001
        return False


def ackable(task: Any, fp_or_mode: Any = "", cfg: dict | None = None) -> bool:
    """policy_ok for notify and the web side, AND severity_allowed: `fp_or_mode` is an Fp (its mode and severity are used) or a bare mode
    word (no severity to judge, so only policy_ok). An Fp at a severity the policy may not acknowledge is not ackable."""
    try:
        if not policy_ok(task, fp_or_mode, cfg):
            return False
        sev = getattr(fp_or_mode, "severity", "")
        return severity_allowed(sev, cfg) if sev else True
    except Exception:  # noqa: BLE001
        return False


def fingerprint(task: str, subject: Any, severity: Any = None, *, cfg: dict | None = None) -> Fp:
    """fp = sha1(task + "|" + issue_key)[:16]. Never raises: any problem returns the empty Fp (nothing can be acknowledged).
    `ackable` is policy_ok AND severity_allowed: a task whose alerts may be acknowledged, at a severity the policy allows (warn only by
    default), so a crit issue carries no id and no button anywhere."""
    try:
        t = _clean(task, 80) if isinstance(task, str) else ""
        if not t or t != task:
            return Fp()
        cfg = cfg or load_config()
        key, mode = issue_key(t, subject, cfg)
        sev = sev_of(severity)
        # The severity gate only NARROWS when a severity is known; a caller that has none (e.g. `ack explain --summary`, whose text is
        # judged by the task policy alone) keeps the task verdict.
        return Fp(hashlib.sha1(f"{t}|{key}".encode("utf-8")).hexdigest()[:16], t, key, sev, mode,
                  policy_ok(t, mode, cfg) and (not sev or severity_allowed(sev, cfg)))
    except Exception:  # noqa: BLE001
        return Fp()


# --------------------------------------------------------------------------- store
def _p(*names: str) -> Path:
    return Path(core.STATE_DIR).joinpath(*names)


def _empty() -> dict:
    return {"v": V, "acks": {}, "tokens": {}, "meta": {"accepted": [], "seen": {}, "suppress_log": {}, "notice_pending": {}, "rejected": []}}


def _str(v: Any, n: int) -> str:
    return _clean(v, n) if isinstance(v, str) else ""


def _mode(v: Any) -> str:
    """How the fingerprint of a stored ack/token was made (policy_ok needs it for explicit-key tasks): a known word or "" (unknown)."""
    return v if v in ("explicit", "task", "regex", "text") else ""


def _sanitize(raw: Any) -> dict:
    """The store as the code may trust it: every record re-validated, anything malformed DROPPED (an unreadable ack is no ack)."""
    out = _empty()
    if not isinstance(raw, dict):
        return out
    for fp, a in (raw.get("acks") if isinstance(raw.get("acks"), dict) else {}).items():
        if not (isinstance(fp, str) and FP_RE.match(fp) and isinstance(a, dict)):
            continue
        sev, acked, until = sev_of(a.get("severity")), _num(a.get("acked_at")), _num(a.get("until"))
        if sev not in RANK or acked is None or until is None or until <= acked:
            continue
        rec = {"task": _str(a.get("task"), 80), "title": _str(a.get("title"), 100), "issue_key": _str(a.get("issue_key"), 200),
               "summary": _str(a.get("summary"), 200), "severity": sev, "acked_at": acked, "until": until,
               "by": a.get("by") if a.get("by") in BY else "cli", "note": _str(a.get("note"), 200), "mode": _mode(a.get("mode")),
               "count_suppressed": max(_int(a.get("count_suppressed")) or 0, 0), "last_seen": _num(a.get("last_seen")),
               "first_ack": _num(a.get("first_ack"), acked)}
        if _num(a.get("expired_at")) is not None:
            rec["expired_at"] = _num(a.get("expired_at"))
        if rec["task"]:
            out["acks"][fp] = rec
    for h, t in (raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}).items():
        if not (isinstance(h, str) and HASH_RE.match(h) and isinstance(t, dict) and isinstance(t.get("fp"), str) and FP_RE.match(t["fp"])):
            continue
        sev, issued, exp = sev_of(t.get("severity")), _num(t.get("issued_at")), _num(t.get("exp"))
        if sev not in RANK or issued is None or exp is None:
            continue
        rec = {"fp": t["fp"], "task": _str(t.get("task"), 80), "title": _str(t.get("title"), 100), "summary": _str(t.get("summary"), 200),
               "severity": sev, "issued_at": issued, "exp": exp, "used": t.get("used") is True, "mode": _mode(t.get("mode"))}
        if _num(t.get("used_at")) is not None:
            rec["used_at"] = _num(t.get("used_at"))
        res = t.get("result")
        if isinstance(res, dict) and res.get("state") in ("applied", "rejected"):
            rec["result"] = {"state": res["state"], "at": _num(res.get("at"), 0.0), "until": _num(res.get("until")),
                             "reason": _str(res.get("reason"), 24)}
        out["tokens"][h] = rec
    m, om = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}, out["meta"]

    def lst(k: str) -> list:
        return m[k] if isinstance(m.get(k), list) else []

    def dct(k: str) -> dict:
        return m[k] if isinstance(m.get(k), dict) else {}
    om["accepted"] = [x for x in (_num(v) for v in lst("accepted")) if x is not None][-2000:]
    om["seen"] = {k: _num(v, 0.0) for k, v in dct("seen").items() if isinstance(k, str) and re.fullmatch(r"[0-9a-f]{32}", k) and _num(v) is not None}
    om["suppress_log"] = {k: _num(v, 0.0) for k, v in dct("suppress_log").items() if isinstance(k, str) and FP_RE.match(k) and _num(v) is not None}
    om["notice_pending"] = {k: max(_int(v) or 0, 0) for k, v in dct("notice_pending").items() if isinstance(k, str) and FP_RE.match(k)}
    om["rejected"] = [{"id": r["id"], "kind": r.get("kind") if r.get("kind") in ("ack", "unack") else "ack", "reason": _str(r.get("reason"), 24),
                       "at": _num(r.get("at"), 0.0)}
                      for r in lst("rejected") if isinstance(r, dict) and isinstance(r.get("id"), str) and FP_RE.match(r["id"])][-20:]
    return out


def _bad_const(c: str):
    raise ValueError(c)


def _read_store() -> tuple[dict, str]:
    """(store, state): ok | missing | corrupt | unreadable. Anything but ok yields an EMPTY store: nothing acknowledged (fail closed)."""
    try:
        with open(_p("acks.json"), "rb") as f:
            raw = f.read(MAX_STORE + 1)
    except FileNotFoundError:
        return _empty(), "missing"
    except OSError:
        return _empty(), "unreadable"
    if len(raw) > MAX_STORE:
        return _empty(), "corrupt"
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_bad_const)
    except (ValueError, RecursionError):
        return _empty(), "corrupt"
    if not isinstance(obj, dict) or _int(obj.get("v")) != V:
        return _empty(), "corrupt"
    try:
        return _sanitize(obj), "ok"
    except Exception:  # noqa: BLE001 - a store shaped so oddly that the validator tripped is a corrupt store
        return _empty(), "corrupt"


def _sweep(d: Path) -> None:
    """Remove temp files of a writer that was killed mid-write (older than TMP_MAX_AGE)."""
    try:
        for e in os.scandir(d):
            if e.name.startswith(".ack-") and e.name.endswith(".tmp") and time.time() - e.stat(follow_symlinks=False).st_mtime > TMP_MAX_AGE:
                with contextlib.suppress(OSError):
                    os.unlink(e.path)
    except OSError:
        pass


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Whole file or nothing: unique temp file in the same directory, fsync, rename. A kill at any point leaves the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ack-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _sweep(path.parent)


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _prune(store: dict, now: float, cfg: dict) -> None:
    """Bounded state: old expired acks, spent tokens, replay digests and counters are dropped; the caps drop the oldest first."""
    keep = cfg["ack"]["expired_keep_days"] * 86400
    for fp in [f for f, a in store["acks"].items() if a.get("expired_at") is not None and now - a["expired_at"] > keep]:
        del store["acks"][fp]
    for h in [h for h, t in store["tokens"].items() if max(t["exp"], t.get("used_at", 0)) + 2 * 86400 < now]:
        del store["tokens"][h]
    cap = cfg["ack"]["max_tokens"]
    if len(store["tokens"]) > cap:                       # spent/expired first (oldest), then the oldest outstanding
        order = sorted(store["tokens"], key=lambda h: (not (store["tokens"][h]["used"] or store["tokens"][h]["exp"] <= now),
                                                       store["tokens"][h]["issued_at"]))
        for h in order[:len(store["tokens"]) - cap]:
            del store["tokens"][h]
    m = store["meta"]
    m["accepted"] = [t for t in m.get("accepted", []) if now - 86400 < t <= now + 3600]
    m["seen"] = dict(sorted(((k, v) for k, v in m.get("seen", {}).items() if now - v < SEEN_KEEP_S), key=lambda kv: kv[1])[-5000:])
    m["suppress_log"] = {k: v for k, v in m.get("suppress_log", {}).items() if k in store["acks"] and now - v < 86400}
    m["notice_pending"] = {k: v for k, v in m.get("notice_pending", {}).items() if k in store["acks"] and v < cfg["ack"]["notice_attempts"]}
    m["rejected"] = [r for r in m.get("rejected", []) if now - r["at"] < PUBLIC_REJECT_S][-20:]


def _save(store: dict, now: float) -> None:
    _prune(store, now, load_config())
    _write_atomic(_p("acks.json"), _dumps(store), 0o600)


class _Lock:
    """flock on STATE_DIR/acks.lock, released explicitly or on close. Waits up to `wait` seconds (a processor holds it for a moment)."""

    def __init__(self, wait: float = 30.0):
        self.wait, self.f = wait, None

    def acquire(self) -> bool:
        p = _p("acks.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(p, "a+")
        end = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= end:
                    self.f.close()
                    self.f = None
                    return False
                time.sleep(0.01)

    def release(self) -> None:
        if self.f is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
            self.f = None

    def __enter__(self) -> "_Lock":
        if not self.acquire():
            raise AckError("busy", "acks.json is locked by another process")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _load_for_write(now: float) -> dict:
    """The store to modify (under the lock). A corrupt store is set aside as acks.json.corrupt and replaced by an empty one (alerts resume);
    a store that merely cannot be READ is never overwritten."""
    store, state = _read_store()
    if state == "unreadable":
        raise AckError("store_unreadable", "acks.json cannot be read")
    if state == "corrupt":
        with contextlib.suppress(OSError):
            os.replace(_p("acks.json"), _p("acks.json.corrupt"))
        _event("store-corrupt", now=now, outcome="set aside as acks.json.corrupt; every acknowledgement is void, alerts resume")
    return store


@contextlib.contextmanager
def _txn(now: float):
    """Lock, load, yield the store, save it (only when the body did not raise)."""
    with _Lock():
        store = _load_for_write(now)
        yield store
        _save(store, now)


# --------------------------------------------------------------------------- audit trail
def _event(ev: str, *, now: float | None = None, outcome: str = "", **fields: Any) -> None:
    """One line in acks.jsonl (0600; torn last lines are tolerated by every reader) and one in the project's audit log. Never raises.
    Only fingerprints, severities, counts and short redacted text: never a token, a hash of one beyond 8 characters, or a request body."""
    now = _now(now)
    rec: dict[str, Any] = {"t": round(now, 3), "ev": ev}
    for k, v in fields.items():
        rec[k] = _redact(v, 200) if isinstance(v, str) else v
    if outcome:
        rec["outcome"] = _redact(outcome, 160)
    try:
        p = _p("acks.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
        fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)           # RDWR: the torn-line check below reads the last byte
        try:
            if os.fstat(fd).st_size and os.pread(fd, 1, os.fstat(fd).st_size - 1) != b"\n":
                line = b"\n" + line                      # a crash tore the previous line: start a fresh one
            os.write(fd, line)
            big = os.fstat(fd).st_size > LOG_MAX
        finally:
            os.close(fd)
        if big:
            data = p.read_bytes()
            tail = data[len(data) // 2:]
            _write_atomic(p, tail[tail.find(b"\n") + 1:], 0o600)
    except OSError:
        pass
    try:
        core.audit("acks", ev, str(fields.get("fp", "")), 0, str(rec.get("outcome", "") or ev))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- queries (read-only, lock-free)
@dataclass(frozen=True)
class AckInfo:
    fp: str
    task: str
    title: str
    issue_key: str
    summary: str
    severity: str
    acked_at: float
    until: float
    by: str
    note: str
    count_suppressed: int = 0
    last_seen: float | None = None
    first_ack: float = 0.0
    mode: str = ""

    def days_left(self, now: float) -> int:
        return max(int(math.ceil((self.until - now) / 86400)), 0)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _info(fp: str, a: dict) -> AckInfo:
    return AckInfo(fp, a["task"], a["title"], a["issue_key"], a["summary"], a["severity"], a["acked_at"], a["until"], a["by"], a["note"],
                   a["count_suppressed"], a["last_seen"], a["first_ack"] if a["first_ack"] is not None else a["acked_at"], a.get("mode", ""))


def _covers(a: dict, sev: str, now: float, cfg: dict) -> bool:
    """Does this record silence `sev` right now? Active (not expired, not past `until`), the severity is at or below the acknowledged one, and the
    task may be acknowledged at all (policy_ok: a record of a task that is denied, or has no exact-error rule any more, silences nothing)."""
    if a.get("expired_at") is not None or not a["until"] > now:
        return False
    if sev not in RANK or not policy_ok(a["task"], a.get("mode", ""), cfg):
        return False
    return RANK[sev] <= RANK[a["severity"]] if cfg["ack"]["escalation_breaks"] else True


def is_acked(fp: Any, severity: Any, now: float | None = None) -> AckInfo | None:
    """The AckInfo that silences `fp` at `severity`, else None. An unknown/empty fp, an unknown severity, an unreadable store, an expired
    or removed ack, an escalation: None. Never raises."""
    try:
        f, sev, now = str(fp or "").strip().lower(), sev_of(severity), _now(now)
        if not FP_RE.match(f) or not sev:
            return None
        store, _state = _read_store()
        a = store["acks"].get(f)
        return _info(f, a) if a and _covers(a, sev, now, load_config()) else None
    except Exception:  # noqa: BLE001
        return None


def list_acks(now: float | None = None, *, include_expired: bool = False) -> list[AckInfo]:
    now = _now(now)
    store, _ = _read_store()
    rows = [_info(f, a) for f, a in store["acks"].items() if include_expired or (a.get("expired_at") is None and a["until"] > now)]
    return sorted(rows, key=lambda r: (r.until, r.fp))


def _entry_sev(entry: Any) -> str:
    return sev_of(entry.get("status")) if isinstance(entry, dict) else ""


def mark_entry(name: str, entry: dict, now: float | None = None, *, _store: dict | None = None, _cfg: dict | None = None) -> dict | None:
    """Set entry["fp"] (the issue id, for every warn/crit/error entry of a task that MAY be acknowledged: policy_ok) and entry["acked"] (when an ack
    covers it) on ONE status.json task entry; clear both when the task is healthy or not ackable (no id = no Acknowledge button on the site).
    Returns the acked dict or None. Never raises; any doubt = not acked."""
    try:
        now, sev = _now(now), _entry_sev(entry)
        entry.pop("acked", None)
        if not sev:
            entry.pop("fp", None)
            return None
        cfg = _cfg or load_config()
        fp = fingerprint(name, entry, sev, cfg=cfg)
        if not fp or not fp.ackable:
            entry.pop("fp", None)
            return None
        entry["fp"] = str(fp)
        store = _store if _store is not None else _read_store()[0]
        a = store["acks"].get(str(fp))
        if a and _covers(a, sev, now, cfg):
            entry["acked"] = {"fp": str(fp), "until": a["until"], "by": a["by"], "note": a["note"], "severity": a["severity"], "since": a["acked_at"]}
            return entry["acked"]
    except Exception:  # noqa: BLE001
        entry.pop("acked", None)
    return None


def entry_ackable(name: str, entry: Any) -> bool:
    """May the issue of this FAILING status.json entry be acknowledged (policy_ok)? publish asks before it exports an entry's `fp` (the
    website's Acknowledge button), so a stale `fp` left in status.json by an older release never offers a button the inbox would refuse."""
    try:
        sev = _entry_sev(entry)
        return bool(sev) and fingerprint(str(name), entry, sev).ackable
    except Exception:  # noqa: BLE001
        return False


def flag_live(acked: Any, now: float | None = None) -> bool:
    """Is this status.json `acked` flag one that still silences (a dict with a finite `until` in the future)? A malformed or ended flag is not:
    the true colour shows (fail closed). scheduler._overall / payloads / anything else that recomputes the overall must ask this."""
    u = _num(acked.get("until")) if isinstance(acked, dict) else None
    return u is not None and u > _now(now)


def overall(tasks: dict, now: float | None = None) -> str:
    """cli.overall without the acknowledged entries: ok | warn | crit. THE function every writer of status["overall"] should use
    (cli.cmd_run, scheduler.merge_status): an acknowledged task never turns the hero yellow or red, not even a minute after it was made green."""
    now, worst = _now(now), 0
    for t in tasks.values():
        if isinstance(t, dict) and t.get("alert", True) and not flag_live(t.get("acked"), now) and isinstance(t.get("status", "ok"), str):
            worst = max(worst, core.LEVELS.get(t.get("status", "ok"), 0))
    return {0: "ok", 1: "warn", 2: "crit"}[worst]


def apply_to_status(status: dict, now: float | None = None) -> dict:
    """Mark every task entry of a status.json dict (in place), set status["acked_n"] and status["overall"] (acked tasks excluded).
    Returns {"acked": n, "failing": m}. With an unreadable store nothing is marked: the true colour shows."""
    now = _now(now)
    tasks = status.get("tasks") if isinstance(status, dict) and isinstance(status.get("tasks"), dict) else {}
    store, _ = _read_store()
    cfg, n, failing = load_config(), 0, 0
    for name, e in tasks.items():
        if not isinstance(e, dict):
            continue
        failing += bool(_entry_sev(e))
        n += mark_entry(str(name), e, now, _store=store, _cfg=cfg) is not None
    if isinstance(status, dict):
        status["acked_n"] = n
        status["overall"] = overall(tasks, now)
    return {"acked": n, "failing": failing}


def _current_issues(status: Any = None, cfg: dict | None = None, ackable_only: bool = True) -> dict[str, dict]:
    """{fp: {task, title, summary, severity, issue_key, mode}} of everything failing right now, from status.json (or the dict given). Only what
    may be acknowledged (policy_ok) unless `ackable_only` is False (add() uses that to tell "not failing" from "not acknowledgeable")."""
    st = status if isinstance(status, dict) else core.read_json(_p("status.json"), {})
    cfg, out = cfg or load_config(), {}
    for name, e in ((st or {}).get("tasks") or {}).items() if isinstance((st or {}).get("tasks"), dict) else []:
        sev = _entry_sev(e)
        fp = fingerprint(str(name), e, sev, cfg=cfg) if sev else Fp()
        if fp and (fp.ackable or not ackable_only):
            out[str(fp)] = {"task": str(name), "title": _clean(e.get("title") or name, 100), "summary": _redact(e.get("summary"), 200),
                            "severity": sev, "issue_key": fp.key, "mode": fp.mode, "ackable": fp.ackable}
    return out


def record_suppressed(fp: Any, now: float | None = None) -> None:
    """The notifier held a message for `fp`: count it (and remember when the issue was last seen). One audit line per hour per fp."""
    try:
        f, now = str(fp or "").strip().lower(), _now(now)
        if not FP_RE.match(f) or f not in _read_store()[0]["acks"]:
            return
        cfg = load_config()
        with _txn(now) as store:
            a = store["acks"].get(f)
            if not a or a.get("expired_at") is not None:
                return
            a["count_suppressed"] += 1
            a["last_seen"] = now
            last = store["meta"].setdefault("suppress_log", {}).get(f, 0.0)
            say = now - last >= cfg["ack"]["suppress_log_s"]
            if say:
                store["meta"]["suppress_log"][f] = now
        if say:
            _event("suppressed", now=now, fp=f, task=a["task"], count=a["count_suppressed"], outcome=f"held back until {_day(a['until'])}")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- acknowledging
def _put_ack(store: dict, fp: str, issue: dict, days: int, note: str, by: str, now: float) -> dict:
    old = store["acks"].get(fp)
    live = bool(old) and old.get("expired_at") is None and old["until"] > now
    rec = {"task": issue["task"], "title": issue["title"], "issue_key": _str(issue.get("issue_key"), 200), "summary": issue["summary"],
           "severity": issue["severity"], "acked_at": now, "until": now + days * 86400, "by": by, "note": _clean(note, 200),
           "mode": _mode(issue.get("mode")),
           "count_suppressed": old["count_suppressed"] if live else 0, "last_seen": old["last_seen"] if old else None,
           "first_ack": (old.get("first_ack") or old["acked_at"]) if old else now}
    store["acks"][fp] = rec
    store["meta"].get("notice_pending", {}).pop(fp, None)
    return rec


def _check_days(days: Any, cfg: dict) -> int:
    d = cfg["ack"]["days"] if days is None else _int(days)
    if d is None or not cfg["ack"]["min_days"] <= d <= cfg["ack"]["max_days"]:
        raise AckError("days", f"days must be {cfg['ack']['min_days']}..{cfg['ack']['max_days']}")
    return d


def _active_count(store: dict, now: float) -> int:
    return sum(1 for a in store["acks"].values() if a.get("expired_at") is None and a["until"] > now)


def _not_ackable(task: str) -> AckError:
    return AckError("not_ackable", f"task {task!r} cannot be acknowledged: its alerts have no exact-error fingerprint "
                                   "(no [key.<task>] rule, no Result.issue_key, not in [ack] allow_tasks, or denied)")


def _resolve_issue(store: dict, target: str, now: float, cfg: dict) -> tuple[str, dict]:
    """Which issue is `target` (a 16-hex fingerprint or a task name)? Only something the host KNOWS about: failing right now, already
    acknowledged, or named by an outstanding token; and only a task that policy_ok lets be acknowledged. A fingerprint nobody has seen cannot
    be pre-acknowledged."""
    target = str(target or "").strip()
    cur = _current_issues(None, cfg, ackable_only=False)
    if FP_RE.match(target.lower()):
        f = target.lower()
        if f in cur:
            if not cur[f]["ackable"]:
                if cur[f]["severity"] and not severity_allowed(cur[f]["severity"], cfg):
                    raise AckError("severity_not_ackable", f"a {cur[f]['severity']} issue cannot be acknowledged: critical issues always alert")
                raise _not_ackable(cur[f]["task"])
            return f, cur[f]
        a = store["acks"].get(f)
        if a:
            if not policy_ok(a["task"], a.get("mode", ""), cfg):
                raise _not_ackable(a["task"])
            return f, {"task": a["task"], "title": a["title"], "summary": a["summary"], "severity": a["severity"], "issue_key": a["issue_key"],
                       "mode": a.get("mode", "")}
        for t in store["tokens"].values():
            if t["fp"] == f:
                if not policy_ok(t["task"], t.get("mode", ""), cfg):
                    raise _not_ackable(t["task"])
                return f, {"task": t["task"], "title": t["title"], "summary": t["summary"], "severity": t["severity"], "issue_key": "",
                           "mode": t.get("mode", "")}
        raise AckError("unknown_issue", f"no failing issue or acknowledgement with id {f}")
    hits = [(f, i) for f, i in cur.items() if i["task"] == target]
    if not hits:
        raise AckError("not_failing", f"task {target!r} is not failing right now (nothing to acknowledge)")
    if not any(i["ackable"] for _f, i in hits):
        crit = next((i for _f, i in hits if i["severity"] and not severity_allowed(i["severity"], cfg)), None)
        if crit:
            raise AckError("severity_not_ackable", f"a {crit['severity']} issue cannot be acknowledged: critical issues always alert")
        raise _not_ackable(target)
    return next((f, i) for f, i in hits if i["ackable"])


def add(target: str, days: int | None = None, note: str = "", severity: str | None = None, by: str = "cli", now: float | None = None,
        *, strict_severity: bool = False) -> AckInfo:
    """Acknowledge an issue: `target` = fingerprint or task name. The severity ceiling is the issue's CURRENT severity unless a higher one
    is asked for (CLI only; strict_severity, used for web requests, demands the same one). Raises AckError."""
    now, cfg = _now(now), load_config()
    if by not in BY:
        raise AckError("by", "by must be email, web or cli")
    n = _check_days(days, cfg)
    with _txn(now) as store:
        fp, issue = _resolve_issue(store, target, now, cfg)
        cur = _current_issues(None, cfg).get(fp)
        want = sev_of(severity) if severity is not None else ""
        if severity is not None and not want:
            raise AckError("severity", "severity must be warn or crit")
        have = cur["severity"] if cur else issue["severity"]
        if want and (want != have if strict_severity else (cur is not None and RANK[want] < RANK[have])):
            raise AckError("severity_changed", f"the issue is {have} now; acknowledging {want} would not cover it")
        issue = {**issue, "severity": want or have}
        if fp not in store["acks"] or store["acks"][fp].get("expired_at") is not None or store["acks"][fp]["until"] <= now:
            if _active_count(store, now) >= cfg["ack"]["max_active"]:
                raise AckError("too_many", f"already {cfg['ack']['max_active']} acknowledgements")
        rec = _put_ack(store, fp, issue, n, note, by, now)
        info = _info(fp, rec)
    _event("ack", now=now, fp=fp, task=rec["task"], severity=rec["severity"], days=n, by=by, until=rec["until"], note=rec["note"])
    return info


def remove(fp: str, by: str = "cli", now: float | None = None) -> bool:
    """Un-acknowledge: normal alerting resumes at once. True when an acknowledgement (active or expired) was removed."""
    now, f = _now(now), str(fp or "").strip().lower()
    if not FP_RE.match(f):
        return False
    with _txn(now) as store:
        a = store["acks"].pop(f, None)
        store["meta"].get("notice_pending", {}).pop(f, None)
    if a:
        _event("unack", now=now, fp=f, task=a["task"], by=by, outcome="alerting resumes")
    return a is not None


# --------------------------------------------------------------------------- tokens (the e-mail button)
def issue_token(fp: Any, task: str, title: str, summary: str, severity: Any, now: float | None = None, ttl_days: int | None = None,
                mode: str | None = None) -> str:
    """One single-use token for an alert e-mail. Returns the PLAINTEXT (43 chars) exactly once; stored: sha256 of it plus the issue it is
    bound to (fingerprint, severity, expiry, how the fingerprint was made). Raises ValueError for anything that cannot be bound, and for a
    task that policy_ok does not let be acknowledged (no button for it). `mode` is the Fp's mode when the caller has it; otherwise the mode of
    the issue as it fails right now (status.json) is used, else none (then only a rule or allow_tasks entry makes the task ackable)."""
    now, cfg = _now(now), load_config()
    f, sev = str(fp or "").strip().lower(), sev_of(severity)
    if not FP_RE.match(f) or sev not in RANK or not _clean(task, 80):
        raise ValueError("a token needs a 16-hex fingerprint, a task and a warn/crit severity")
    if mode is None:
        mode = (_current_issues(None, cfg, ackable_only=False).get(f) or {}).get("mode", "")
    if not policy_ok(_clean(task, 80), mode, cfg):
        raise ValueError(f"task {task!r} cannot be acknowledged (no exact-error fingerprint): no token")
    if not severity_allowed(sev, cfg):
        raise ValueError(f"a {sev} issue cannot be acknowledged ({cfg['ack']['severities']} only): no token")
    ttl = min(max(_int(ttl_days) or cfg["ack"]["token_ttl_days"], 1), 90)
    token = secrets.token_urlsafe(32)
    h = token_hash(token)
    with _txn(now) as store:
        store["tokens"][h] = {"fp": f, "task": _clean(task, 80), "title": _redact(title, 100), "summary": _redact(summary, 200),
                              "severity": sev, "issued_at": now, "exp": now + ttl * 86400, "used": False, "mode": _mode(mode)}
    _event("token", now=now, fp=f, task=_clean(task, 80), severity=sev, th=h[:8], ttl_days=ttl)
    with contextlib.suppress(Exception):
        export_tokens(now)                                # the website can verify it from now on; a failure is the doctor's to report
    return token


def revoke_token(token: str, now: float | None = None) -> bool:
    now, h = _now(now), token_hash(token)
    with _txn(now) as store:
        gone = store["tokens"].pop(h, None) is not None
    if gone:
        with contextlib.suppress(Exception):
            export_tokens(now)
    return gone


def _find_token(store: dict, h: str) -> tuple[str, dict] | None:
    """Look a token HASH up with constant-time comparisons (the container supplies it; nothing about the stored ones may leak by timing)."""
    hit = None
    for k, v in store["tokens"].items():
        if hmac.compare_digest(k, h):
            hit = (k, v)
    return hit


def verify_token(token: str, now: float | None = None) -> dict | None:
    """What the website's verify-only code does with a plaintext token: its record when it exists, is unused and unexpired, else None."""
    now = _now(now)
    if not TOKEN_RE.match(str(token or "")):
        return None
    hit = _find_token(_read_store()[0], token_hash(token))
    return dict(hit[1]) if hit and not hit[1]["used"] and hit[1]["exp"] > now else None


# --------------------------------------------------------------------------- expiry
def expire(now: float | None = None) -> list[str]:
    """End the acknowledgements whose time is up. Returns their fingerprints ONCE (the record stays, flagged expired, for
    expired_keep_days: notify reads it to word the single notice). Also drops what is long spent. Idle minutes cost one lock-free read."""
    now, cfg = _now(now), load_config()
    peek, state = _read_store()
    due = [f for f, a in peek["acks"].items() if a.get("expired_at") is None and a["until"] <= now]
    old = any(a.get("expired_at") is not None and now - a["expired_at"] > cfg["ack"]["expired_keep_days"] * 86400 for a in peek["acks"].values())
    spent = any(max(t["exp"], t.get("used_at", 0)) + 2 * 86400 < now for t in peek["tokens"].values())
    if state not in ("ok", "missing") or not (due or old or spent):
        return []
    out: list[str] = []
    with _txn(now) as store:
        for f, a in sorted(store["acks"].items()):
            if a.get("expired_at") is None and a["until"] <= now:
                a["expired_at"] = a["until"]                        # when it ended, not when we noticed (a runner that was down for days)
                store["meta"].setdefault("notice_pending", {}).setdefault(f, 0)
                out.append(f)
        recs = {f: dict(store["acks"][f]) for f in out}
    for f in out:
        _event("expire", now=now, fp=f, task=recs[f]["task"], severity=recs[f]["severity"], suppressed=recs[f]["count_suppressed"],
               outcome="alerting resumes; one notice is due")
    with contextlib.suppress(Exception):                      # the public views follow what was just ended or purged
        export_public(now)
        export_tokens(now)
    return out


def pending_notices() -> list[str]:
    return sorted(_read_store()[0]["meta"].get("notice_pending", {}))


def notice_done(fps: list[str], ok: bool = True, now: float | None = None) -> None:
    """The expiry notice for these fingerprints was handed over (ok) or failed (counts an attempt; dropped after notice_attempts)."""
    now = _now(now)
    with _txn(now) as store:
        for f in fps:
            if ok:
                store["meta"]["notice_pending"].pop(f, None)
            elif f in store["meta"]["notice_pending"]:
                store["meta"]["notice_pending"][f] += 1


# --------------------------------------------------------------------------- exports (public: no tokens, no hashes)
def public_doc(now: float | None = None) -> dict:
    """public/acks.json: {"generated_at","acks":[...],"rejected":[...],"stats":{...}} - see the module docstring."""
    now = _now(now)
    store, _ = _read_store()
    cur = _current_issues()
    rows = []
    cfg = load_config()
    for f, a in store["acks"].items():
        if a.get("expired_at") is None and a["until"] > now and policy_ok(a["task"], a.get("mode", ""), cfg):     # one that silences nothing is not listed
            rows.append({"id": f, "task": a["task"], "title": _redact(a["title"], 100), "summary": _redact(a["summary"], 200),
                         "severity": a["severity"], "acked_at": a["acked_at"], "until": a["until"],
                         "days_left": max(int(math.ceil((a["until"] - now) / 86400)), 0), "by": a["by"], "note": _redact(a["note"], 200),
                         "suppressed": a["count_suppressed"], "active": f in cur and RANK[cur[f]["severity"]] <= RANK[a["severity"]]})
    rows.sort(key=lambda r: (r["until"], r["id"]))
    exp30 = sum(1 for a in store["acks"].values() if a.get("expired_at") is not None and now - a["expired_at"] <= 30 * 86400)
    rej = [{"id": r["id"], "kind": r["kind"], "reason": r["reason"], "at": r["at"]} for r in store["meta"]["rejected"]
           if now - r["at"] < PUBLIC_REJECT_S]
    return {"generated_at": now, "acks": rows, "rejected": rej, "stats": {"active": len(rows), "expired_30d": exp30}}


def export_public(now: float | None = None) -> Path:
    """Write STATE_DIR/public/acks.json (0644). The directory is only ever created, never removed (the container bind-mounts it)."""
    now = _now(now)
    pub = _p("public")
    pub.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(pub, 0o755)
    _write_atomic(pub / "acks.json", _dumps(public_doc(now)), 0o644)
    return pub / "acks.json"


def tokens_doc(now: float | None = None) -> dict:
    """ack/tokens.json: ONLY {sha256(token): {fp, exp, used, title, summary, severity, state, until, reason}}. A hash of a 256-bit random
    token cannot be inverted or forged; the website hashes what the visitor presents and looks it up here."""
    now = _now(now)
    store, _ = _read_store()
    out = {}
    for h, t in store["tokens"].items():
        res = t.get("result") or {}
        state = "applied" if t["used"] else "expired" if t["exp"] <= now else "rejected" if res.get("state") == "rejected" else "pending"
        out[h] = {"fp": t["fp"], "exp": t["exp"], "used": t["used"], "title": _redact(t["title"], 100), "summary": _redact(t["summary"], 200),
                  "severity": t["severity"], "state": state, "until": res.get("until") if state == "applied" else None,
                  "reason": res.get("reason") if state == "rejected" else None}
    return out


def export_tokens(now: float | None = None) -> Path:
    """Write STATE_DIR/ack/tokens.json (0644) into the directory the container mounts; the directory is created (0755) if absent, never removed."""
    now = _now(now)
    d = _p("ack")
    if not d.exists():
        d.mkdir(parents=True, mode=0o755, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o755)
    _write_atomic(d / "tokens.json", _dumps(tokens_doc(now)), 0o644)
    return d / "tokens.json"


# --------------------------------------------------------------------------- signing (shared with the web side)
def canonical(req: dict) -> bytes:
    """The bytes that are signed: the request WITHOUT "sig", keys sorted, no spaces, ASCII."""
    return json.dumps({k: v for k, v in req.items() if k != "sig"}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def sign(req: dict, key: bytes) -> str:
    return hmac.new(key, canonical(req), hashlib.sha256).hexdigest()


def _read_key() -> bytes | None:
    """STATE_DIR/ack/web.key: a regular file, owned by root (or this user), not writable by group/world and not readable by world, >= 32
    bytes of text. The HMAC key is that text, stripped. Anything else means NO key: every signed request is refused (fail closed)."""
    try:
        fd = os.open(_p("ack", "web.key"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o027 or st.st_size > 4096:
            return None
        key = os.read(fd, 4097).strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return key if len(key) >= 32 else None


# --------------------------------------------------------------------------- the inbox
class _Reject(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


PUBLIC_REASON = {"rate_limited": "rate_limited", "used": "used", "expired": "expired", "unknown_issue": "unknown_issue",
                 "severity_changed": "severity_changed", "too_many": "too_many"}      # everything else is simply "invalid": no oracle
ALLOWED_KEYS = {"ack": {"v", "kind", "source", "ts", "sig", "days", "note", "severity", "fp", "token_hash"},
                "unack": {"v", "kind", "source", "ts", "sig", "fp"}}
_HANDLERS: dict[str, tuple[Callable[[dict, float], tuple[bool, str]], frozenset]] = {}


def register_inbox_handler(kind: str, fn: Callable[[dict, float], tuple[bool, str]], allowed_keys: Any = ()) -> None:
    """SPEC6: other request kinds (rule_change, auth_setup ...) ride the same strict envelope. After the common checks (size, JSON, replay,
    +-skew, signature) `fn(request_without_sig, now) -> (ok, reason)` runs with the acks lock RELEASED. Extra keys must be declared."""
    if not re.fullmatch(r"[a-z_]{1,24}", kind) or kind in ALLOWED_KEYS:
        raise ValueError("bad handler kind")
    _HANDLERS[kind] = (fn, frozenset(allowed_keys) | {"v", "kind", "source", "ts", "sig"})


def _auth_handlers() -> None:
    """SPEC6 s7: the production runner answers the website's first-run setup (`auth_setup`) and recovery-code burn (`auth_change`) itself
    (acks_auth). Called by process_inbox before the first request is read, in the module that reads the inbox (under `python3 -m
    homelab_maint.acks` that is __main__, so the registration goes to ITS table). Handlers someone else registered for those kinds
    (a test, a plugin) stay; ours are re-made each time so they follow STATE_DIR. Any failure only leaves the two kinds unanswered."""
    try:
        from . import acks_auth
        kinds = tuple(k for k in ("auth_setup", "auth_change") if k not in _HANDLERS or getattr(_HANDLERS[k][0], "_acks_auth", False))
        if kinds:
            acks_auth.register(sys.modules[__name__], kinds=kinds)
    except Exception as exc:  # noqa: BLE001
        _event("auth-handlers", outcome=f"not registered: {type(exc).__name__}")


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)       # fingerprints acknowledged
    unacked: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)      # internal reason codes, one per refused request
    handled: list[str] = field(default_factory=list)       # kinds served by a registered handler
    duplicates: int = 0
    junk: int = 0                                          # entries that were not request files at all
    skipped: bool = False                                  # another processor held the lock for the whole wait
    note: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _strict_json(data: bytes) -> dict:
    def pairs(items):
        d: dict = {}
        for k, v in items:
            if k in d:
                raise ValueError("duplicate key")
            d[k] = v
        return d
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=_bad_const)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Reject("malformed") from None
    if not isinstance(obj, dict):
        raise _Reject("malformed")
    return obj


def _validate(req: dict, cfg: dict) -> dict:
    """Schema and types of an ack/unack request. Everything exact: unknown keys, wrong types, bools-as-ints, nulls for absent fields and
    out-of-range values are refused; a field that is present must be valid."""
    kind, source = req.get("kind"), req.get("source")
    if _int(req.get("v")) != V or not isinstance(kind, str) or kind not in ALLOWED_KEYS or source not in ("email", "web"):
        raise _Reject("schema")
    if set(req) - ALLOWED_KEYS[kind]:
        raise _Reject("schema")
    ts = _num(req.get("ts"))
    if ts is None:
        raise _Reject("schema")
    if "sig" in req and not (isinstance(req["sig"], str) and re.fullmatch(r"[0-9a-fA-F]{64}", req["sig"])):
        raise _Reject("schema")
    fp, th, sev, days = (req.get(k) for k in ("fp", "token_hash", "severity", "days"))
    if ("fp" in req and not (isinstance(fp, str) and FP_RE.match(fp))) or ("token_hash" in req and not (isinstance(th, str) and HASH_RE.match(th))):
        raise _Reject("schema")
    out = {"kind": kind, "source": source, "ts": ts, "fp": fp}
    if kind == "unack":
        if source != "web" or fp is None:
            raise _Reject("schema")
        return out
    note = req.get("note", "")
    if not isinstance(note, str) or len(note) > 200 or ("severity" in req and sev not in RANK):
        raise _Reject("schema")
    if "days" in req and (_int(days) is None or not cfg["ack"]["min_days"] <= days <= cfg["ack"]["max_days"]):
        raise _Reject("schema")
    if source == "email":
        if th is None:
            raise _Reject("schema")
    elif fp is None or sev is None or th is not None:                  # a web request names the issue and its severity, and carries no token
        raise _Reject("schema")
    out.update(token_hash=th, note=_clean(note, 200), severity=sev, days=days)
    return out


_B32_RUN = re.compile(rb"[A-Z2-7]{26,}")                  # an authenticator secret (RFC 4648 base32) is never kept


def _kept(data: bytes) -> bytes:
    """What a refused request keeps of itself: an auth_* request loses every secret-bearing value (acks_auth.scrub), a base32 run (the
    authenticator secret) and the middle of any 64-hex run (a signature or token hash is never kept whole)."""
    try:
        from . import acks_auth
        data = acks_auth.scrub(data)
    except Exception:  # noqa: BLE001
        pass
    data = _B32_RUN.sub(b"<redacted>", data)
    return re.sub(rb"[0-9a-fA-F]{64}", lambda m: m.group(0)[:8] + b"..", data)


def _quarantine(dfd: int, data: bytes | None, code: str, now: float, cfg: dict) -> None:
    """Keep a refused file for the owner to look at: inbox/rejected/ (0700, ours), reason code in the NAME (from a fixed list, never request
    data), the newest rejected_keep kept. Skipped silently when the directory is not ours."""
    keep = cfg["inbox"]["rejected_keep"]
    if data is None or keep <= 0 or not re.fullmatch(r"[a-z_]{1,24}", code):
        return
    try:
        try:
            os.mkdir("rejected", 0o700, dir_fd=dfd)
        except FileExistsError:
            pass
        rfd = os.open("rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return
    try:
        st = os.fstat(rfd)
        if st.st_uid not in (0, os.geteuid()):
            return
        name = f"{int(now * 1000)}-{secrets.token_hex(4)}.{code}"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=rfd)
        with os.fdopen(fd, "wb") as f:
            f.write(_kept(data))
        names = sorted(e.name for e in os.scandir(rfd))
        for old in names[:max(len(names) - keep, 0)]:
            with contextlib.suppress(OSError):
                os.unlink(old, dir_fd=rfd)
    except OSError:
        pass
    finally:
        os.close(rfd)


def _read_request(dfd: int, name: str, max_bytes: int) -> bytes:
    """The bytes of one inbox file, or _Reject. lstat first (a symlink, directory, FIFO or device is never opened for reading), then
    open with O_NOFOLLOW|O_NONBLOCK and re-check on the descriptor: the container can swap names under us, never the descriptor."""
    try:
        st = os.lstat(name, dir_fd=dfd)
    except OSError:
        raise _Reject("vanished") from None
    if not stat.S_ISREG(st.st_mode):
        raise _Reject("not_regular")
    if st.st_size > max_bytes:
        raise _Reject("too_large")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        raise _Reject("not_regular") from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise _Reject("not_regular")
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise _Reject("too_large")
    return data


def _web_issue(store: dict, v: dict, now: float, cfg: dict, cur: dict) -> tuple[str, dict]:
    """The issue a signed web request names, and its severity check (strict: the card the owner clicked is the issue as it is NOW)."""
    fp = v["fp"]
    if fp in cur:                                                      # (cur holds only what policy_ok lets be acknowledged)
        issue, have = cur[fp], cur[fp]["severity"]
    elif fp in store["acks"]:
        a = store["acks"][fp]
        if not policy_ok(a["task"], a.get("mode", ""), cfg):
            raise _Reject("not_ackable")
        issue = {"task": a["task"], "title": a["title"], "summary": a["summary"], "severity": a["severity"], "issue_key": a["issue_key"],
                 "mode": a.get("mode", "")}
        have = a["severity"]
    else:
        raise _Reject("unknown_issue")
    if v["severity"] != have:
        raise _Reject("severity_changed")
    return fp, {**issue, "severity": v["severity"]}


def _apply_request(store: dict, v: dict, now: float, cfg: dict, cur: dict) -> tuple[str, str, str]:
    """Apply one validated, authenticated request to the in-memory store. Returns (action, fp, token_hash|""). Raises _Reject."""
    if v["kind"] == "unack":
        a = store["acks"].pop(v["fp"], None)
        if a is None:
            raise _Reject("unknown_issue")
        store["meta"].get("notice_pending", {}).pop(v["fp"], None)
        return "unack", v["fp"], ""
    accepted = [t for t in store["meta"].get("accepted", []) if now - 86400 < t <= now + 3600]
    tok_key = ""
    if v["source"] == "email":
        hit = _find_token(store, v["token_hash"])
        if hit is None:
            raise _Reject("unknown_token")
        tok_key, tok = hit
        if tok["used"]:
            raise _Reject("used")
        if tok["exp"] <= now:
            raise _Reject("expired")
        if (v["fp"] and v["fp"] != tok["fp"]) or (v["severity"] and v["severity"] != tok["severity"]):
            raise _Reject("binding")                     # a stolen link cannot be re-pointed at another issue
        mode = (cur.get(tok["fp"]) or {}).get("mode") or tok.get("mode", "")
        if not policy_ok(tok["task"], mode, cfg):
            raise _Reject("not_ackable")                 # the task lost its exact-error rule (or is denied) since the e-mail went out
        if not severity_allowed(tok["severity"], cfg):
            raise _Reject("not_ackable")                 # a link minted before the policy was tightened (a crit e-mail): it must not ack now
        fp, issue = tok["fp"], {"task": tok["task"], "title": tok["title"], "summary": tok["summary"], "severity": tok["severity"],
                                "issue_key": (cur.get(tok["fp"]) or {}).get("issue_key", ""), "mode": mode}
    else:
        fp, issue = _web_issue(store, v, now, cfg, cur)
    if len(accepted) >= cfg["ack"]["max_acks_per_day"]:
        raise _Reject("rate_limited")                    # the token is NOT spent: the owner can use the link again tomorrow
    fresh = fp not in store["acks"] or store["acks"][fp].get("expired_at") is not None or store["acks"][fp]["until"] <= now
    if fresh and _active_count(store, now) >= cfg["ack"]["max_active"]:
        raise _Reject("too_many")
    days = v["days"] if v["days"] is not None else cfg["ack"]["days"]
    rec = _put_ack(store, fp, issue, days, v["note"], v["source"], now)
    store["meta"]["accepted"] = accepted + [now]
    if tok_key:
        store["tokens"][tok_key].update(used=True, used_at=now, result={"state": "applied", "at": now, "until": rec["until"], "reason": ""})
    return "ack", fp, tok_key


def _note_rejection(store: dict, v: dict | None, code: str, now: float) -> None:
    """An AUTHENTIC request that was refused: tell the website why, in public words only (the token stays usable for a later click)."""
    if not v:
        return
    reason = PUBLIC_REASON.get(code, "invalid")
    if v.get("token_hash"):
        hit = _find_token(store, v["token_hash"])
        if hit and not hit[1]["used"] and code not in ("expired", "used"):
            hit[1]["result"] = {"state": "rejected", "at": now, "until": None, "reason": reason}
    elif v.get("fp"):
        store["meta"].setdefault("rejected", []).append({"id": v["fp"], "kind": v["kind"], "reason": reason, "at": now})


def _remove(dfd: int, name: str) -> None:
    """Delete an inbox entry by name relative to the directory descriptor (a symlink is removed itself, never followed)."""
    try:
        os.unlink(name, dir_fd=dfd)
    except IsADirectoryError:
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=dfd)
    except OSError:
        pass


_PRE_AUTH = ("malformed", "schema", "bad_signature", "no_key", "not_regular", "too_large", "stale", "future")   # refused before authentication


def _verify(req: dict, v: dict, key: bytes | None, cfg: dict, now: float) -> None:
    """Freshness (+-skew) and the HMAC. A source listed in [inbox] unsigned_sources (or require_signature = false) skips only the HMAC."""
    if abs(v["ts"] - now) > cfg["inbox"]["skew_s"]:
        raise _Reject("stale" if v["ts"] < now else "future")
    if not cfg["inbox"]["require_signature"] or v["source"] in cfg["inbox"]["unsigned_sources"]:
        return
    if key is None:
        raise _Reject("no_key")
    sig = req.get("sig")
    if not isinstance(sig, str) or not hmac.compare_digest(sign(req, key), sig.lower()):
        raise _Reject("bad_signature")


def _parse(data: bytes, cfg: dict, key: bytes | None, now: float) -> tuple[dict, dict, Any]:
    """bytes -> (request, validated fields, handler | None), or _Reject. Whatever a hostile file does to the parsing code (an unhashable kind,
    a surprise type) is a refusal of THAT file, never an exception that stops the whole batch (and, un-deleted, every later minute's)."""
    try:
        req = _strict_json(data)
        handler = _HANDLERS.get(req.get("kind")) if isinstance(req.get("kind"), str) else None
        if handler is None:
            v = _validate(req, cfg)
        elif _int(req.get("v")) != V or req.get("source") not in ("email", "web") or set(req) - handler[1] or _num(req.get("ts")) is None:
            raise _Reject("schema")
        else:
            v = {"kind": req["kind"], "source": req["source"], "ts": _num(req["ts"]), "fp": None}
        _verify(req, v, key, cfg, now)
    except _Reject:
        raise
    except Exception:  # noqa: BLE001
        raise _Reject("schema") from None
    return req, v, handler


def _one(dfd: int, name: str, store: dict, key: bytes | None, cfg: dict, now: float, rep: Report, lock: _Lock, memo: dict) -> tuple[dict, bool]:
    """One request file -> (store, changed). The file is always removed at the end, whatever the outcome (a refused one is quarantined)."""
    data, v, changed = None, None, False
    try:
        data = _read_request(dfd, name, cfg["inbox"]["max_bytes"])
        digest = hashlib.sha256(data).hexdigest()[:32]
        if digest in store["meta"]["seen"]:                          # already applied: a crash between save and delete, or a replay
            rep.duplicates += 1
            _remove(dfd, name)
            return store, False
        req, v, handler = _parse(data, cfg, key, now)
        if handler is not None:
            store["meta"]["seen"][digest] = now
            _save(store, now)
            lock.release()                                           # handlers run WITHOUT our lock: they may call acks.* themselves
            try:
                ok, why = handler[0]({k: x for k, x in req.items() if k != "sig"}, now)
            except Exception:  # noqa: BLE001
                ok, why = False, "handler_error"
            if not lock.acquire():
                raise AckError("busy")
            store = _load_for_write(now)
            (rep.handled if ok else rep.rejected).append(v["kind"] if ok else (why or "handler_error"))
            _event("handled" if ok else "reject", now=now, kind=v["kind"], source=v["source"], outcome=why or ("ok" if ok else "refused"))
        else:
            if "cur" not in memo:
                memo["cur"] = _current_issues(None, cfg)
            action, fp, _tk = _apply_request(store, v, now, cfg, memo["cur"])
            store["meta"]["seen"][digest] = now
            _save(store, now)                                        # durable BEFORE the file goes away
            changed = True
            (rep.applied if action == "ack" else rep.unacked).append(fp)
            a = store["acks"].get(fp) or {}
            _event(action, now=now, fp=fp, task=a.get("task", ""), severity=a.get("severity", ""), by=v["source"],
                   days=v.get("days") or cfg["ack"]["days"], until=a.get("until"), note=v.get("note", ""))
    except _Reject as r:
        rep.rejected.append(r.code)
        if r.code != "vanished":
            if v is not None and r.code not in _PRE_AUTH:            # authenticated but refused: say why, in public words, on the site
                _note_rejection(store, v, r.code, now)
                _save(store, now)
                changed = True
            gone = r.code in ("not_regular", "too_large")
            _event("reject", now=now, reason=r.code, source=(v or {}).get("source", ""), kind=(v or {}).get("kind", ""),
                   outcome="removed" if gone else "quarantined")
            if not gone:
                _quarantine(dfd, data, r.code, now, cfg)
    _remove(dfd, name)
    return store, changed


def process_inbox(now: float | None = None, *, refresh: bool = False) -> Report:
    """Apply what the web container queued in STATE_DIR/ack/inbox/. Per file: size and type gate, strict JSON, replay guard, +-skew, HMAC,
    schema, then the rule of the kind (token binding/expiry/single use, known issue, severity, rate limit, caps); the store is saved BEFORE the
    file is deleted (a crash in between only repeats a request the replay guard recognises). Refused files go to inbox/rejected/ with a
    reason; the website learns the reason from tokens.json / acks.json in public words. `refresh=True` also updates status.json, the
    incident ledger and the public files (what `homelab-maint ack process` does). Writers must create files atomically (a temp name
    starting with "." and a rename): a dotfile younger than 10 minutes is left alone, any other stray entry is removed."""
    now, cfg, rep = _now(now), load_config(), Report()
    try:
        dfd = os.open(_p("ack", "inbox"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        rep.note = "no inbox"
        return rep
    changed, lock, memo = False, _Lock(), {}
    try:
        with os.scandir(dfd) as it:                              # a flooded directory costs a bounded listing, not its whole size
            names = sorted(e.name for e in itertools.islice((x for x in it if x.name != "rejected"), LIST_MAX))
        if not names:
            return rep                                           # the usual minute: nothing to do, no lock taken
        _auth_handlers()
        if not lock.acquire():
            rep.skipped = True
            return rep
        store, key, todo = _load_for_write(now), _read_key(), 0
        for name in names[:5000]:
            if not NAME_RE.match(name):                              # not a request at all
                try:
                    if name.startswith(".") and now - os.lstat(name, dir_fd=dfd).st_mtime < TMP_MAX_AGE:
                        continue                                     # a writer's temp file in flight
                except OSError:
                    continue
                _remove(dfd, name)
                rep.junk += 1
                continue
            if todo >= cfg["inbox"]["max_files_per_run"]:
                break                                                # the rest waits for the next minute
            todo += 1
            store, did = _one(dfd, name, store, key, cfg, now, rep, lock, memo)
            changed = changed or did
    except AckError as exc:
        rep.note = exc.code
    finally:
        lock.release()
        os.close(dfd)
    if changed:
        with contextlib.suppress(Exception):
            export_public(now)
            export_tokens(now)
        if refresh:
            _after_change(now)
    return rep


# --------------------------------------------------------------------------- after a change: status.json, incidents, public files
class _StateLock:
    """cli's state.lock (the one cmd_run holds while it merges status.json). Taken here only AFTER the acks lock is released."""

    def __enter__(self):
        Path(core.STATE_DIR).mkdir(parents=True, exist_ok=True)
        self.f = open(_p("state.lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(OSError):
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _after_change(now: float) -> None:
    """Make the new state visible inside the minute instead of at the next 15-minute run: status.json (acked flags, overall), the public
    acks/tokens files, the incident ledger (state `acknowledged`) and the other public files. Every step is isolated and best effort."""
    status = None
    try:
        with _StateLock():
            status = core.read_json(_p("status.json"), None)
            if isinstance(status, dict) and isinstance(status.get("tasks"), dict):
                apply_to_status(status, now)
                core.write_json_atomic(_p("status.json"), status)
    except Exception as exc:  # noqa: BLE001
        _event("refresh-failed", now=now, outcome=f"status.json: {type(exc).__name__}")
    for step in (lambda: export_public(now), lambda: export_tokens(now)):
        with contextlib.suppress(Exception):
            step()
    if isinstance(status, dict):
        try:
            from . import incidents
            incidents.update(status, None, now)
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import publish
            publish.publish(status, now)
        except Exception:  # noqa: BLE001
            pass


def _notify_expired(due: list[str], now: float) -> bool:
    """Hand the expiry notices to notify (one e-mail per acknowledgement, through its normal routing). True = handed over."""
    try:
        from . import notify
        notify.notify_expired(now, expired=due)
        return True
    except Exception as exc:  # noqa: BLE001
        _event("notice-failed", now=now, outcome=f"{type(exc).__name__}")
        return False


def run_once(now: float | None = None, *, refresh: bool = True) -> dict:
    """What the one-minute job does: process the inbox, end what is due, deliver the expiry notices. Returns a small summary dict."""
    now = _now(now)
    rep = process_inbox(now, refresh=False)
    gone = expire(now)
    if rep.applied or rep.unacked or gone:
        if refresh:
            _after_change(now)
        else:
            with contextlib.suppress(Exception):
                export_public(now)
    due = pending_notices()
    if due:
        notice_done(due, _notify_expired(due, now), now)
    return {"applied": len(rep.applied), "unacked": len(rep.unacked), "rejected": len(rep.rejected), "duplicates": rep.duplicates,
            "expired": len(gone), "notices": len(due), "skipped": rep.skipped}


# --------------------------------------------------------------------------- installation helper and command line
def init_dirs(group: Any = None) -> list[str]:
    """Create STATE_DIR/ack (0750), ack/inbox (1730, the container group may create files but not list them), inbox/rejected (0700) and
    ack/web.key (64 random hex chars, 0640) when they are absent; with root and a group, chown them (a 0755 ack/ from an older install is
    closed to 0750 once it belongs to the group). install.sh lays out the same modes. Never deletes or re-creates anything."""
    done: list[str] = []
    d, inbox = _p("ack"), _p("ack", "inbox")
    gid = None
    if group is not None and os.geteuid() == 0:
        import grp
        gid = int(group) if str(group).isdigit() else grp.getgrnam(str(group)).gr_gid

    def own(path: Path) -> bool:
        if gid is None:
            return False
        try:
            os.chown(path, 0, gid)
            return True
        except OSError as exc:                                       # e.g. a staging run in a user namespace: not fatal, doctor shows the rest
            done.append(f"cannot chown {path.name} to group {gid}: {exc.strerror}")
            return False
    for path, mode in ((d, 0o750), (inbox, 0o1730), (inbox / "rejected", 0o700)):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
            done.append(f"created {path.name}/")
        if path != inbox / "rejected" and own(path) and path == d and stat.S_IMODE(path.stat().st_mode) == 0o755:
            os.chmod(path, 0o750)
            done.append("ack/ mode 0755 -> 0750 (group only)")
    key = d / "web.key"
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32) + "\n")
        done.append("created web.key")
    own(key)
    export_tokens()
    return done


def auth_state(ack_dir: Any = None) -> tuple[str, str]:
    """The website login as the HOST sees ack/auth.json: ("absent", "") first run | ("ok", "") | ("unreadable", why): the website's group cannot
    read it, so setup would look done and nobody could log in | ("unusable", why): not a file the site accepts. Read-only, never raises."""
    try:
        from . import acks_auth
        d = Path(ack_dir) if ack_dir else _p("ack")
        p = d / "auth.json"
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return "absent", ""
        if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            return "unusable", "not a regular file of sane size"
        try:
            ok = acks_auth.validate_auth(json.loads(p.read_text()))
        except (OSError, ValueError, UnicodeDecodeError):
            return "unusable", "cannot be read as JSON"
        if not ok:
            return "unusable", "the website would refuse it (not what parse_auth accepts)"
        gid = acks_auth.web_gid(d)
        if not acks_auth.readable_by(st, gid):
            return "unreadable", f"group {st.st_gid} mode {stat.S_IMODE(st.st_mode):o} is unreadable for the website's gid {gid}"
        return "ok", ""
    except Exception as exc:  # noqa: BLE001
        return "unusable", f"{type(exc).__name__}"


def _is_root() -> bool:
    return os.geteuid() == 0


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def web_main(argv: list[str] | None = None) -> int:
    """homelab-maint web bootstrap    show the first-run login secret of the maintenance website, ONCE, on a root terminal.
    Refused when not root, when setup is already complete (ack/auth.json exists), when stdout is not a terminal (a pipe or a file would keep
    it) or when the secret file is not a plain root-owned file. The secret is never logged, audited or copied: the website asks for it on its
    setup screen and the runner checks the owner's proof against ack/bootstrap.secret."""
    a = list(sys.argv[1:] if argv is None else argv)
    if a != ["bootstrap"]:
        print(web_main.__doc__, file=sys.stderr)
        return 2
    if not _is_root():
        print("error: run as root (sudo homelab-maint web bootstrap): the secret is a root-only file", file=sys.stderr)
        return 1
    state, why = auth_state()
    if state != "absent":
        print("error: setup is already complete (ack/auth.json exists" + (f"; it is {state}: {why}" if state != "ok" else "") + "); nothing to show. "
              "To start over: sudo rm " + str(_p("ack", "auth.json")) + " and run this again", file=sys.stderr)
        return 1
    if not _stdout_is_tty():
        print("error: stdout is not a terminal; the secret is only shown on one (not into a pipe, a file or a log)", file=sys.stderr)
        return 1
    try:
        fd = os.open(_p("ack", "bootstrap.secret"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        print(f"error: {_p('ack', 'bootstrap.secret')} is missing: run install.sh again (it creates it when absent)", file=sys.stderr)
        return 1
    try:
        st = os.fstat(fd)
        secret = os.read(fd, 4097).decode("utf-8", "replace").strip() if stat.S_ISREG(st.st_mode) and st.st_uid in (0, os.getuid()) and st.st_size <= 4096 else ""
    finally:
        os.close(fd)
    if not secret:
        print("error: ack/bootstrap.secret is empty, not a plain file, or not owned by root: remove it and run install.sh again", file=sys.stderr)
        return 1
    sys.stdout.write("First-run login for the maintenance website. This secret is shown once and is not logged:\n\n"
                     f"    {secret}\n\n"
                     "Open the site, choose a passphrase (an authenticator app is optional) and paste the secret on the setup screen.\n"
                     "It stays valid until setup finishes; after that this command refuses.\n")
    sys.stdout.flush()
    _event("bootstrap-shown", outcome="root terminal")                  # that it was shown, never what
    return 0


def doctor(now: float | None = None) -> list[tuple[str, bool, str]]:
    """Read-only self-check for `homelab-maint doctor`: [(label, ok, hint)]. Sends nothing, creates nothing, never raises."""
    now, rows = _now(now), []

    def add_row(label: str, ok: bool, hint: str = "") -> None:
        rows.append((label, ok, hint))
    try:
        errs = validate()
        add_row("ack.toml valid", not errs, "; ".join(errs)[:200])
        _store, state = _read_store()
        add_row("acks.json readable", state in ("ok", "missing"), f"{state}: every acknowledgement is void until fixed ({_p('acks.json')})")
        inbox = _p("ack", "inbox")
        try:
            inbox_ok = stat.S_ISDIR(os.lstat(inbox).st_mode)
        except OSError:
            inbox_ok = False
        add_row("ack inbox present", inbox_ok, "run: homelab-maint ack init --group 10001 (the website's acknowledge buttons cannot work without it)")
        add_row("ack HMAC key usable", _read_key() is not None,
                "ack/web.key missing, shorter than 32 characters, or not a private root-owned file: every web request is refused")
        stale = 0
        try:
            stale = sum(1 for e in os.scandir(inbox) if NAME_RE.match(e.name) and now - e.stat(follow_symlinks=False).st_mtime > 600)
        except OSError:
            pass
        add_row("ack inbox is being processed", stale == 0, f"{stale} request(s) waited over 10 minutes: the one-minute tick applies the inbox (cmd_tick; an optional ack-process job in jobs.toml does the same): is homelab-maint-tick.timer running?")
        pend = pending_notices()
        add_row("no expiry notice is stuck", not pend, f"{len(pend)} acknowledgement-expired notice(s) could not be delivered yet")
    except Exception as exc:  # noqa: BLE001
        add_row("acknowledgements self-check", False, f"{type(exc).__name__}: {exc}")
    return rows


def _fmt_row(a: AckInfo, now: float) -> str:
    live = "" if a.until > now else " (expired)"
    return (f"{a.fp}  {a.severity:<4}  {a.task:<22} until {_day(a.until)} ({a.days_left(now)} d){live}  by {a.by:<5} "
            f"held back {a.count_suppressed}" + (f"  note: {a.note}" if a.note else ""))


def _explain(task: str, summary: str | None) -> int:
    """Print how a task's current error is fingerprinted: the rule, the key it yields, the id, and whether it is acknowledged."""
    cfg = load_config()
    entry = ((core.read_json(_p("status.json"), {}) or {}).get("tasks") or {}).get(task)
    if summary is None and not isinstance(entry, dict):
        print(f"no status entry for {task!r}; pass --summary TEXT", file=sys.stderr)
        return 1
    subject = summary if summary is not None else entry
    shown = summary if summary is not None else str(entry.get("summary") or "")
    if summary is None:
        sev = _entry_sev(entry)
    else:
        m = re.match(r"\s*(warn|crit|error)\b", shown, re.I)      # a summary given on the command line carries its own severity
        sev = sev_of(m.group(0)) if m else ""
    fp = fingerprint(task, subject, sev)
    rule = cfg["key"].get(task) or {"mode": "text", "regex": [], "sort": False}
    print(f"task:     {task}\nsummary:  {shown}\nrule:     mode={rule['mode']}" + (" sort" if rule.get("sort") else "")
          + (f" split={rule['split']!r}" if rule.get("split") else "") + (f" alias={rule['alias']}" if rule.get("alias") else "")
          + "".join(f"\n          /{p}/" for p in rule["regex"]))
    why = ""
    if fp and not fp.ackable:
        why = (f"\nackable:  no (a {sev} issue always alerts)" if sev and not severity_allowed(sev, cfg)
               else "\nackable:  no (no [key.<task>] rule, no Result.issue_key, not in [ack] allow_tasks, or denied)")
    print(f"key:      {fp.key} ({fp.mode})\nid:       {fp or '(none: cannot be fingerprinted)'}" + why)
    info = is_acked(fp, sev or "warn")
    if info:
        print(f"acked:    until {_day(info.until)} by {info.by}, severity ceiling {info.severity}")
    return 0 if fp else 1


def _arg(a: list[str], flag: str, default: Any = None) -> Any:
    """Pop `flag VALUE` out of the argument list."""
    if flag in a:
        i = a.index(flag)
        if i + 1 >= len(a):
            raise ValueError(f"{flag} needs a value")
        v = a[i + 1]
        del a[i:i + 2]
        return v
    return default


def _link(fp: str, token: str, days: int, sev: str) -> str:
    """The e-mail button URL for a token (notify's builder and notify.toml [ack] base_url), or "" when it cannot be built."""
    try:
        from . import notify, notify_templates as T
        nc = notify.load_config()
        base = (nc.get("ack") or {}).get("base_url") or (nc.get("site") or {}).get("url") or ""
        return T.ack_url(base, token, fp, days, sev)
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str] | None = None) -> int:
    """homelab-maint ack list [--all] | add FP|TASK [--days N] [--note T] [--severity warn|crit] | remove FP | process | issue-token FP|TASK
    [--ttl-days N] [--days N] | export | explain TASK [--summary T] | init [--group G] | validate | doctor"""
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a.pop(0) if a else "list"
    now = time.time()
    try:
        if cmd == "list":
            rows = list_acks(now, include_expired="--all" in a)
            print(f"{len(rows)} acknowledged issue(s)")
            for r in rows:
                print("  " + _fmt_row(r, now))
            return 0
        if cmd in ("add", "issue-token", "remove", "explain"):
            opts = {f: _arg(a, f) for f in ("--days", "--note", "--severity", "--ttl-days", "--summary")}
            if len(a) != 1:
                print(main.__doc__, file=sys.stderr)
                return 2
            if cmd == "add":
                info = add(a[0], int(opts["--days"]) if opts["--days"] is not None else None, opts["--note"] or "", opts["--severity"], "cli", now)
                with contextlib.suppress(Exception):
                    _after_change(now)
                print(f"acknowledged {info.fp} ({info.task}, {info.severity}) until {_day(info.until)}; alerts for this exact issue stop")
                return 0
            if cmd == "remove":
                ok = remove(a[0], "cli", now)
                if ok:
                    with contextlib.suppress(Exception):
                        _after_change(now)
                print("removed; normal alerting resumes" if ok else "no such acknowledgement")
                return 0 if ok else 1
            if cmd == "explain":
                return _explain(a[0], opts["--summary"])
            fp, issue = _resolve_issue(_read_store()[0], a[0], now, load_config())
            days = int(opts["--days"]) if opts["--days"] else load_config()["ack"]["days"]
            token = issue_token(fp, issue["task"], issue["title"], issue["summary"], issue["severity"], now,
                                int(opts["--ttl-days"]) if opts["--ttl-days"] else None)
            url = _link(fp, token, days, issue["severity"])
            if not url:
                revoke_token(token, now)
                print("cannot build the link (notify.toml [ack] base_url / [site] url); no token kept", file=sys.stderr)
                return 1
            print(url)                                              # the plaintext token appears here, once, and nowhere else
            return 0
        if cmd == "process":
            r = run_once(now)
            print(f"acks: {r['applied']} applied, {r['unacked']} removed, {r['rejected']} rejected, {r['expired']} expired")
            return 0
        if cmd == "export":
            print(export_public(now))
            print(export_tokens(now))
            return 0
        if cmd == "init":
            for line in init_dirs(_arg(a, "--group")) or ["nothing to do"]:
                print(line)
            return 0
        if cmd == "doctor":
            rows = doctor(now)
            for label, ok, hint in rows:
                print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f"  ({hint})" if not ok and hint else ""))
            return 0 if all(r[1] for r in rows) else 1
        if cmd == "validate":
            errs = validate()
            for e in errs:
                print("warning: " + e)
            print("ack.toml ok" if not errs else f"{len(errs)} problem(s): the baseline rules apply to what is ignored")
            return 0 if not errs else 1
    except AckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(main.__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
