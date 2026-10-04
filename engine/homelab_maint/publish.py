"""Public export for the maintenance web UI (SPEC2 section 2, SPEC3 section 3, SPEC4 S12).

`publish(status=None, now=None) -> list[str]` renders small JSON files into STATE_DIR/public/ (dir 0755, files
0644, each written tmp + os.replace). The web container mounts ONLY that directory read-only and the files go through
a Cloudflare tunnel, so everything here is built as an allow-list and then scrubbed:

  * only fields the UI needs are copied (never whole dicts); metrics are scalars only, items are rows of scalars;
  * every string passes `clean()`: single line (the first line of multi-line output), traceback text collapsed to its
    last line, URLs stripped of userinfo/query/fragment and of token-like path segments, key=value secrets, bearer
    tokens, well-known token shapes, long hex strings, high-entropy blobs, e-mail addresses and phone numbers replaced by
    "[redacted]" (the "-p<password>" flag of mysql/psql/sshpass/docker login too),
    /home/<user>/ paths cut after the 3rd component, length capped;
  * alert (notify) audit records are reduced to {"task":"notify","action":"alert","outcome":...}: no subject, no body;
  * the alert-path check (it quotes the notification bridge's stderr: provider replies, credential-file names) is
    published from its counters only, and in every check the text after an "rc=<n>" marker (the raw command output the
    runner appends there) is cut;
  * the large `sample` history records are never read into memory (skipped by prefix before parsing);
  * every input read is bounded (audit tail 6 MiB, history 48 MiB, lines > 64 KiB ignored) and malformed lines skipped;
  * each file is capped at ~195 KB by trimming oldest entries; a file that cannot be built or cannot fit is left as it was.

Files (SPEC2): overview, checks, actions, storage, metrics, schedule, health-history. Files that are PASSED THROUGH from
the module that owns the data (SPEC3 / SPEC4), after the same scrubbing as everything else (every string leaf through
`clean`, credential-named and raw-output string fields dropped, text after "rc=<n>" cut, the multi-line postmortem
Markdown scrubbed line by line with traceback blocks dropped):

  routine.json        routine.export()               windows, freeze, routine steps, change log, 14-day calendar
  incidents.json      incidents.export_incidents()   open / recent incidents, MTTD / MTTR
  slo.json            incidents.export_slo()         objectives, error budget, burn
  pressure.json       tasks.pressure.export()        level, 24 h history, spikes, ladder actions, service classes
  jobs.json           scheduler.export()             the unified schedule (jobs, adapters, OS and app timers)
  monitors.json       tasks.monitors.export()        the probe inventory
  notifications.json  notify.export()                what the owner was told, delivery ok / failed (no bodies)
  migration.json      legacy.export_public()         legacy items, replacement, retired or not
  reports/index.json  see below
  self.json           tasks.self_health.export()     health of the monitoring pipeline itself (SPEC6 S6): runner -> publish -> website, tick, live, registry
  rules.json          registry.write_public()        what the script does: every rule of the registry, last evaluated / triggered (SPEC6 S5; 700 KB cap of its own)
  rules-history.json  the `history` of rules.json    the registry's recent changes as a file of its own: {schema, generated_at, registry_hash, history: [newest first]}
  manifest.json       registry.write_public()        schema + generated_at of every public file, runner version, registry hash: written LAST
  acks.json           acks.public_doc()              the acknowledged issues (SPEC5): no tokens, no hashes. Only once the feature is installed
                                                     (STATE_DIR/acks.json or STATE_DIR/ack/ exists); the file names are then added to the result
  ack/tokens.json     acks.export_tokens()           NOT public/: written next to it for the web container's verify-only mount ({sha256(token): {fp,
                                                     exp, used, ...}}, 0644); the directory STATE_DIR/ack is created if absent and never removed

ACKNOWLEDGED issues (SPEC5): a task entry whose status.json record carries `acked` (acks.apply_to_status) is shown MUTED. Its status word
is "info" in checks.json / overview counts / the headline (never red, not in the problem count), its row carries `acked` (until, by, note,
severity, since, the true status) and every failing row carries `fp`, the issue id the website's Acknowledge buttons post. The headline says
"(N acknowledged)". health-history.json and slo.json keep the TRUE statuses (an acknowledgement is not an excuse for better availability).

jobs, monitors, notifications and migration come from the SPEC4 modules, which may be absent (not written yet, or an import
error): the file is then written as {"unavailable": true, "generated_at": t} so the UI shows "not available" instead of an
old file, and that is not an export error. A module that is importable but raises, or returns something that is not an
object, is an export error like any other builder (the old file is kept). A source that returns None (migration: unusable
inventory) is published as "unavailable" too. routine, incidents, slo and pressure have no such fallback: they exist in this
build, so a failure there is an export error.

Cost: the exports are cheap reads of the modules' own small files, with two exceptions that are handled here. slo.json is
computed by incidents.export_slo() over the task records of the history pass this module already makes (a second 40 MiB parse
costs ~0.7 s), and incidents.export_incidents() is given an empty audit trail unless an incident is open (the trail only feeds
the "related actions" of open incidents; reading it costs ~0.3 s at 12 MiB). Measured on this host with a 40 MiB history and a
12 MiB audit log: 0.6 s for the whole run (1.5 s before those two shortcuts), ~0.1 s on the real state.

routine.json is built from routine.export() with the timer list this module already read (one `systemctl list-timers`
call; routine.write_export() would run its own with a 20 s timeout and would skip the scrubbing and size cap).
live.json is written by the live daemon and never by this module; overview.json only carries its age as "live_age_s"
(null when the file is missing; the age at publish time, the Live tab itself polls /api/live).

reports/: written by the report tasks (reports.generate). This module only checks the directory: it exists (created
empty when missing, never recreated or deleted: the bind mount follows the inode), is 0755, report files and
index.json are world readable (the web container runs as another uid), temp files of a killed writer older than 10 min
are swept, and index.json is rebuilt from the report files (reports.load_index / trim_index, the files are the truth)
when it is missing, damaged or disagrees with them. The rebuild runs under the reports.lock flock WITHOUT waiting: a
report being written refreshes the index itself. No report file is ever deleted or rewritten here.

`publish` never raises and costs well under a second. It returns the NAMES of the files written. Each file is built
independently, so one failing source (no status yet, no systemctl, missing metrics ring) leaves only that file stale.

A file that fails to build is listed in overview.json "export_errors" (so the UI can warn instead of showing stale
data as fresh); overview.json is therefore built last.

`generated_at`: overview.json carries the time of the status it was built from (so "data is stale" means "the runner
stopped"), every other file carries the publish time.

Tiers: check, daily, weekly and monthly (the monthly window has no timer: its next run comes from routine.json).

CLI: `python3 -m homelab_maint.publish` publishes once from STATE_DIR/status.json and prints the file names.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Callable

from . import core
from .core import GIB, human, read_json, sh

SCHEMA = 1
MAX_INT = 2 ** 53                    # larger counts are nonsense and would lose precision in the browser
MAX_FILE_BYTES = 195_000            # spec: each file < 200 KB
AUDIT_WINDOW = 6 * 1024 * 1024      # newest audit bytes considered (~40k records)
HISTORY_WINDOW = 48 * 1024 * 1024   # history.jsonl is capped at 40 MiB by core.append_history
MAX_LINE = 64 * 1024
RECENT_MAX = 300
RECENT_NOISE_MAX = 100              # dry-run / refused-* records are noise next to what was really done
JOURNAL_MAX = 100
DAYS = 30
SLOT_S = 900                        # check-tier cadence: one health "run" = one 15-minute slot

# status words as shown in the UI; skipped is "nothing to report", and warn/crit of a task flagged alert=False is
# informational (same rule as cli.overall and the Homarr payloads: it never pages and never turns the page yellow)
STATUS_RANK = {"crit": 0, "warn": 1, "error": 2, "info": 3, "ok": 4}
HEALTH_LEVEL = {"ok": 0, "info": 0, "skipped": 0, "warn": 1, "crit": 2, "error": 2}
LEVEL_WORD = {0: "ok", 1: "warn", 2: "crit"}
TIERS = ("check", "daily", "weekly", "monthly")
LIVE_MAX = 512 * 1024               # live.json is ~35 KB; a file far beyond that is not read (its mtime gives the age)
REPORT_KEEP = 60                    # reports.KEEP: index entries (and files) retained by the report tasks
REPORT_ID = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{4}-W\d{2})$")        # report ids, same shapes as reports._ID_FILE
INDEX_ID = re.compile(r"^[0-9A-Za-z-]{1,16}$")                     # what the web backend accepts for /api/reports/<id>
TMP_MAX_AGE = 600                   # leftovers of a killed writer older than this are swept

TIMER_TITLES = {
    "homelab-maint-check.timer": "Health checks", "homelab-maint-daily.timer": "Daily cleanup",
    "homelab-maint-weekly.timer": "Weekly review", "homelab-maint-monthly.timer": "Monthly review",
    "homelab-maint-metrics.timer": "Temperature and load sampler",
    "backup-system.timer": "System backup", "backup-immich.timer": "Immich backup",
    "docker-prune.timer": "Docker cleanup", "fstrim.timer": "SSD trim", "logrotate.timer": "Log rotation",
    "apt-daily-upgrade.timer": "Apt upgrades", "sysstat-collect.timer": "System statistics",
}
NOTABLE_TIMERS = ["backup-system.timer", "backup-immich.timer", "docker-prune.timer", "fstrim.timer",
                  "logrotate.timer", "apt-daily-upgrade.timer", "sysstat-collect.timer"]


# =========================================================================== redaction
# userinfo runs up to the LAST '@' of the authority: a password may itself contain '@' ("http://u:p@ss@host/")
_URL = re.compile(r"""(?i)\b([a-z][a-z0-9+.-]{1,10}://)([^\s/?#"'<>]*@)?([^\s/?#"'<>]*)([^\s?#"'<>]*)([?#][^\s"'<>]*)?""")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
# value = one token (or a quoted string): the next word is NOT swallowed ("password=x user=bob" keeps "user=bob")
_KV = re.compile(r"""(?ix)
    (\b[\w.-]{0,24}?(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|auth(?:orization)?|credential|cookie|
               session[_-]?id|private[_-]?key|access[_-]?key)[\w.-]{0,24})
    (["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;&"']+)""")
# key names too short/common for _KV (bare "key" would turn "KeyError: k" into "KeyError: [redacted]"): whole words only
_KV_WORD = re.compile(r"""(?i)((?<![A-Za-z])(?:key|sig|signature|pw|psk|jwt|pin|otp|dsn)(?![A-Za-z]))(["']?\s*[:=]\s*)"""
                      r"""("[^"]*"|'[^']*'|[^\s,;&"']+)""")
# "password = my secret phrase": a passphrase with spaces; runs to the next delimiter or the next key=value pair
_PASS_MULTI = re.compile(r"""(?i)(\b[\w.-]{0,24}?(?:pass(?:word|wd|phrase)?|pwd)[\w.-]{0,24}\s*=\s*)(?!["'])"""
                         r"""[^\s,;&"')=]+(?:[ \t]+(?![^\s=]*=)[^\s,;&"')=]+)+""")
_FLAG = re.compile(r"(?i)(--?[\w-]{0,24}(?:pass|secret|token|key|auth|cred)[\w-]{0,24})\s+(?!-)(\S+)")
# "-p<password>" / "-p <password>" is a password only for these tools (docker run -p 80:80 is a port)
_P_TOOLS = re.compile(r"(?i)\b(?:mysql(?:dump|admin)?|mariadb|psql|sshpass|redis-cli|(?:docker|podman)\s+login)\b")
_P_FLAG = re.compile(r"(?<!\S)-p\s*(?!-)\S+")
# phone numbers: +E.164 (compact or with separators) and North American "416-555-1234" / "(416) 555-1234"
_PHONE = re.compile(r"(?<![\w.+-])(?:\+\d{10,15}(?!\d)|\+\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]\d{3}[ .-]?\d{3,4}(?![\w-])|"
                    r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-]))")
_RC = re.compile(r"(?i)\b(rc=\d+)\b.*")                       # "rc=1: <stderr>": the runner appends raw command output here
_PREFIXED = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*|\d{6,12}:[A-Za-z0-9_-]{30,})")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BARE = re.compile(r"[A-Za-z0-9_+=-]{24,}")
_SHA = re.compile(r"(?i)\b(sha256:[0-9a-f]{12})[0-9a-f]{20,}")
_HOME = re.compile(r"""(?<![\w.])(/(?:home/[^/\s"']+|root))((?:/[^/\s"']+)+)""")
_TRACE = re.compile(r'Traceback \(most recent call last\)|File "[^"]+", line \d+')
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_SENSITIVE_KEY = re.compile(r"(?i)pass|secret|token|cred|auth|cookie|private|api.?key")
_RAW_KEYS = {"traceback", "last_error", "stderr", "stdout", "output", "raw", "body", "error_text"}
SECRET = "[redacted]"


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in Counter(s).values())


def _secretish(tok: str, min_len: int = 24) -> bool:
    """Heuristic for "this blob is a credential": hex >= 32, or a digit+letter mix with random-looking entropy.
    Natural names measure 3.4-4.1 bits/char, real tokens 4.5+, so separators ('-', '_') raise the bar to 4.4."""
    if len(tok) < min_len:
        return False
    if len(tok) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", tok):
        return True
    if not (re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok)):
        return False
    return _entropy(tok) >= (4.4 if re.search(r"[-_]", tok) else 4.0)


_SEG = re.compile(r"([A-Za-z0-9_-]+)([^A-Za-z0-9_-]{0,3})")
_UUID = re.compile(r"(?i)[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_TOKEN_WORDS = {"push", "hook", "hooks", "webhook", "webhooks", "token", "tokens", "ping", "trigger", "secret", "key", "bot"}


def _secret_segment(seg: str, prev: str) -> bool:
    """A URL path segment that is (probably) a credential: ping/webhook ids and push tokens ride in the path.
    Low-entropy ones count too (UUIDs, hex, long digit+letter ids), and so does any 8+ char digit+letter id right after
    a path word like "push" or "hook"."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", seg):
        return False
    if _UUID.fullmatch(seg) or (len(seg) >= 16 and re.fullmatch(r"[0-9a-fA-F]+", seg)):
        return True
    mixed = bool(re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg))
    if mixed and (len(seg) >= 20 or (len(seg) >= 8 and prev.lower() in _TOKEN_WORDS)):
        return True
    return _secretish(seg, 16)


def _url_sub(m: re.Match) -> str:
    scheme, _userinfo, host, path = m.group(1), m.group(2), m.group(3), m.group(4)
    segs, prev = [], ""
    for seg in path.split("/"):
        sm = _SEG.fullmatch(seg)                   # "(https://h/hook/TOKEN),": the path group swallows trailing punctuation
        segs.append(SECRET + sm.group(2) if sm and _secret_segment(sm.group(1), prev) else seg)
        prev = sm.group(1) if sm else seg
    return scheme + host + "/".join(segs)          # userinfo, query and fragment are dropped entirely


def _cut_home(m: re.Match) -> str:
    parts = m.group(2).split("/")[1:]
    return m.group(1) + "".join("/" + p for p in parts[:3]) + ("/..." if len(parts) > 3 else "")


def _cap(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    if s.startswith("/") and n >= 24:              # paths: the file name matters more than the middle
        return s[: n // 2 - 1] + "..." + s[-(n // 2 - 2):]
    return s[: max(n - 3, 1)] + "..."


def clean(v: Any, n: int = 160) -> str:
    """Scrub one value into a short, single-line, secret-free string (see module docstring)."""
    s = "" if v is None else str(v)
    s = s.encode("utf-8", "replace").decode("utf-8")                   # lone surrogates from odd file names
    if len(s) > 800:
        s = s[:800]                                                    # bound regex work on huge blobs
    if _TRACE.search(s):                                               # never publish traceback text, only its last line
        lines = [ln for ln in s.splitlines() if ln.strip()]
        s = lines[-1].strip() if lines and "Traceback" not in lines[-1] and 'File "' not in lines[-1] else "error (details withheld)"
    s = _PEM.sub(SECRET, s)
    parts = [ln for ln in s.splitlines() if ln.strip()]
    s = parts[0] if parts else ""                                      # raw multi-line output: first line only
    s = _CTRL.sub(" ", s).strip()
    s = _URL.sub(_url_sub, s)
    s = _BEARER.sub(lambda m: m.group(1) + " " + SECRET, s)
    s = _PASS_MULTI.sub(lambda m: m.group(1) + SECRET, s)
    s = _KV.sub(lambda m: m.group(1) + m.group(2) + SECRET, s)
    s = _KV_WORD.sub(lambda m: m.group(1) + m.group(2) + SECRET, s)
    if _P_TOOLS.search(s):                                             # before _FLAG: "-pTopSecret99" looks like a flag name
        s = _P_FLAG.sub("-p " + SECRET, s)
    s = _FLAG.sub(lambda m: m.group(1) + " " + SECRET, s)
    s = _PREFIXED.sub(SECRET, s)
    s = _SHA.sub(lambda m: m.group(1), s)                              # image digests: keep 12 hex, they are not secrets
    s = _BARE.sub(lambda m: SECRET if _secretish(m.group(0)) else m.group(0), s)
    s = _EMAIL.sub(SECRET, s)
    s = _PHONE.sub(SECRET, s)
    s = _HOME.sub(_cut_home, s)
    return _cap(s, n)


def _cut_rc(s: str) -> str:
    """Drop everything after an "rc=<n>" marker: what follows is raw stderr of a command (provider replies, file names)."""
    return _RC.sub(r"\1", s)


def _name(v: Any, n: int = 48) -> str:
    return re.sub(r"[^A-Za-z0-9_.:@+-]", "_", str(v))[:n]


def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):                     # float(10**400) raises OverflowError
        return default
    return f if math.isfinite(f) else default


def _int(v: Any, default: int = 0) -> int:
    f = _num(v)
    return default if f is None else int(max(-MAX_INT, min(MAX_INT, f)))      # JS-safe integers only


def _scalar(v: Any, n: int = 120) -> Any:
    """bool/int/finite float/None pass; str is cleaned; anything else (lists, dicts) is dropped (returns ...)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if -MAX_INT <= v <= MAX_INT else None                 # not exactly representable in the browser
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        return clean(v, n)
    return ...


def _scalar_dict(d: Any, max_keys: int, n: int = 120, rc: bool = False) -> dict:
    """Scalars of a dict, credential-named / raw-output fields dropped; rc=True also cuts text after "rc=<n>"."""
    out: dict = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if len(out) >= max_keys:
            break
        key = _name(k, 40)
        val = _scalar(v, n)
        if val is ...:
            continue
        if isinstance(val, str) and (key.lower() in _RAW_KEYS or _SENSITIVE_KEY.search(key)):
            continue                                                   # raw output / credential-named string fields
        out[key] = _cut_rc(val) if rc and isinstance(val, str) else val
    return out


def _json_safe(o: Any, depth: int = 0) -> Any:
    """Make an arbitrary object strict-JSON clean (NaN/inf -> null, surrogates -> '?', unknown types -> str)."""
    if depth > 8:
        return None
    if isinstance(o, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, str):
        return o.encode("utf-8", "replace").decode("utf-8")
    if o is None or isinstance(o, (bool, int)):
        return o
    return str(o)


# =========================================================================== file output
def _dump(doc: Any) -> bytes:
    return json.dumps(_json_safe(doc), separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _encode(doc: Any, trim: Callable[[Any, float], bool] | None) -> bytes:
    """Serialise; while over the cap, let `trim(doc, fraction)` drop the oldest entries (False = nothing left to drop)."""
    for _ in range(60):
        data = _dump(doc)
        if len(data) <= MAX_FILE_BYTES:
            return data
        if trim is None or not trim(doc, max(0.05, min(0.5, 1 - MAX_FILE_BYTES / len(data)))):
            break
    raise ValueError("does not fit in the size cap")


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pub-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _public_dir() -> Path:
    d = core.STATE_DIR / "public"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o755)
        for old in d.glob(".pub-*.tmp"):                               # leftovers of a killed publisher
            if time.time() - old.stat().st_mtime > 600:
                old.unlink()
    except OSError:
        pass
    return d


def _drop_oldest(items: list, frac: float) -> bool:
    """Remove the last `frac` of a newest-first list (at least one). False when empty."""
    if not items:
        return False
    del items[len(items) - max(1, int(len(items) * frac)):]
    return True


# =========================================================================== bounded input readers
def _tail_lines(path: Path, window: int):
    """Yield the lines (bytes) of the last `window` bytes of a file, skipping the cut-off first line and huge lines."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - window)
            f.seek(start)
            first = start > 0
            for line in f:
                if first:
                    first = False
                    continue
                if len(line) <= MAX_LINE:
                    yield line
    except OSError:
        return


def _parse_ts(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return float(v) if math.isfinite(v) else None
        except OverflowError:                                          # huge ints: neither finite() nor float() accept them
            return None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.strip()).timestamp()      # "2026-10-01T21:45:19-0400" (py3.11+)
        except (ValueError, OverflowError, OSError):              # e.g. a naive year-0001 stamp cannot become an epoch
            return None
    return None


def _outcome(task: str, raw: Any) -> str:
    raw = "" if raw is None else str(raw).strip()
    low = raw.lower()
    if task == "notify":                                              # free text (subject, bridge stderr) never leaves
        return "sent" if low.startswith("sent") else "dropped" if low.startswith("dropped") else "failed"
    if low in ("done", "dry-run", "approved", "sent"):
        return low
    if low.startswith("refused"):
        return "refused-" + (re.sub(r"[^a-z0-9-]", "", low[len("refused"):].lstrip("-"))[:30] or "other")
    if low.startswith("failed"):
        reason = _cut_rc(clean(raw[len("failed"):].lstrip(": "), 80))
        return "failed: " + reason if reason else "failed"
    return clean(raw, 100) or "unknown"


def _read_audit(now: float) -> list[dict]:
    """Audit records, oldest first, with `action`/`target` still RAW (scrubbed only for the few rows that are published:
    cleaning tens of thousands of records would dominate the run time). Malformed lines, non-dicts and far-future
    timestamps are skipped."""
    out: list[dict] = []
    for line in _tail_lines(core.LOG_DIR / "audit.jsonl", AUDIT_WINDOW):
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):               # hostile nesting raises RecursionError, not ValueError
            continue
        if not isinstance(r, dict):
            continue
        try:                                           # one odd record must never take the whole file down
            ts = _parse_ts(r.get("ts"))
            if ts is None or ts > now + 3600:
                continue
            task = _name(r.get("task") or "?", 40)
            if task == "notify":                       # subject and bridge stderr are free text: never published
                out.append({"ts": ts, "task": "notify", "action": "alert", "target": "", "bytes": 0,
                            "outcome": _outcome("notify", r.get("outcome"))})
            else:
                out.append({"ts": ts, "task": task, "action": r.get("action"), "target": r.get("target"),
                            "bytes": max(_int(r.get("bytes")), 0), "outcome": _outcome(task, r.get("outcome"))})
        except Exception:                              # noqa: BLE001
            continue
    out.sort(key=lambda r: r["ts"])
    return out


def _public_record(r: dict) -> dict:
    return {"ts": r["ts"], "task": r["task"], "action": clean(r["action"], 40), "target": clean(r["target"], 160),
            "bytes": r["bytes"], "outcome": r["outcome"]}


def _read_journal() -> list[dict]:
    out = []
    for line in _tail_lines(core.STATE_DIR / "maintenance-journal.jsonl", 1024 * 1024):
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):               # hostile nesting raises RecursionError, not ValueError
            continue
        try:
            ts = _parse_ts(r.get("ts")) if isinstance(r, dict) else None
            if ts is None or not r.get("title"):
                continue
            out.append({"ts": ts, "title": clean(r["title"], 120), "detail": clean(r.get("detail") or "", 600)})
        except Exception:                              # noqa: BLE001
            continue
    out.sort(key=lambda r: -r["ts"])
    return out[:JOURNAL_MAX]


# history.jsonl lines are written compactly by core.append_history; match the common shapes without JSON parsing
# (the file holds ~100k small lines per month) and fall back to json.loads for anything unusual.
_TASK_RX = re.compile(rb'^\{"t":\s*([0-9.eE+-]+),\s*"kind":\s*"task",\s*"task":\s*"([^"\\]*)",\s*"status":\s*"([^"\\]*)"'
                      rb'(?:,\s*"alert":\s*(true|false)\b)?')
_DISK_RX = re.compile(rb'^\{"t":\s*([0-9.eE+-]+),\s*"kind":\s*"disk",\s*"mount":\s*"([^"\\]*)",\s*"free":\s*(-?[0-9.eE+-]+)')


class _History:
    """One pass over history.jsonl: hourly free-space means per mount, worst level per 15-min slot, runs per task.
    A garbled line (bad number, odd types, absurd or future timestamp) is skipped and never aborts the pass."""

    def __init__(self, now: float, alert_off: set[str]):
        cutoff, horizon = now - DAYS * 86400, now + 3600
        self.disk: dict[str, dict[int, list[float]]] = {}
        self.slots: dict[int, int] = {}
        self.runs: Counter = Counter()
        # (t, task, status, alert flag | None) of every task record in the window: incidents.export_slo() takes these instead of
        # parsing the same (up to 40 MiB) file a second time
        self.recs: list[tuple] = []
        for line in _tail_lines(core.STATE_DIR / "history.jsonl", HISTORY_WINDOW):
            task_ok, alert, status = True, None, None
            try:
                m = _TASK_RX.match(line)
                if m and m.group(4) is None and b'"alert"' in line:
                    m = None                                          # an alert flag in an unusual place or shape: parse the line
                if m:
                    t, kind, a, b = float(m.group(1)), "task", m.group(2).decode("utf-8", "replace"), m.group(3).decode("utf-8", "replace")
                    status = b
                    alert = None if m.group(4) is None else m.group(4) == b"true"       # the per-run flag decides what the SLO counts
                else:
                    m = _DISK_RX.match(line)
                    if m:
                        t, kind, a, b = float(m.group(1)), "disk", m.group(2).decode("utf-8", "replace"), float(m.group(3))
                    else:
                        head = line[:100]
                        if b'"sample"' in head or b'"size"' in head or (b'"task"' not in head and b'"disk"' not in head):
                            continue                                  # sample records are huge and never published
                        r = json.loads(line)
                        t, kind = _num(r.get("t")), r.get("kind")
                        if kind == "task":
                            a, b = str(r.get("task")), str(r.get("status"))
                            task_ok, status = isinstance(r.get("task"), str), r.get("status")
                            alert = r["alert"] if isinstance(r.get("alert"), bool) else None
                        elif kind == "disk":
                            a, b = str(r.get("mount")), _num(r.get("free"))
                        else:
                            continue
            except (ValueError, OverflowError, RecursionError, AttributeError, TypeError):
                continue
            if t is None or not cutoff <= t <= horizon:
                continue
            if kind == "task":
                self._task(t, a, b, alert_off)
                if task_ok:
                    self.recs.append((t, a, status, alert))
            elif a and b is not None and math.isfinite(b):
                bucket = self.disk.setdefault(a, {}).setdefault(int(t // 3600) * 3600, [0.0, 0])
                bucket[0] += b
                bucket[1] += 1

    def _task(self, t: float, task: str, status: str, alert_off: set[str]) -> None:
        self.runs[task] += 1
        slot = int(t // SLOT_S)
        if task in alert_off:                                         # alert=False findings never colour the calendar,
            self.slots.setdefault(slot, 0)                            # but the run still proves there is data that day
            return
        lvl = HEALTH_LEVEL.get(status, 0)
        if lvl > self.slots.get(slot, -1):
            self.slots[slot] = lvl


# =========================================================================== shared run state
def _list_timers() -> list[dict] | None:
    r = sh(["systemctl", "list-timers", "--all", "--output=json", "--no-pager"], timeout=2)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except (ValueError, RecursionError):
        return None
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else None


def _timer_specs(units: list[str]) -> dict[str, str]:
    """unit -> raw calendar/monotonic spec text via ONE `systemctl show` call ("" for units that have none)."""
    if not units:
        return {}
    r = sh(["systemctl", "show", "-p", "Id", "-p", "TimersCalendar", "-p", "TimersMonotonic", *units], timeout=2)
    out: dict[str, str] = {}
    if r.returncode != 0:
        return out
    for block in (r.stdout or "").split("\n\n"):
        uid, spec = "", ""
        for ln in block.splitlines():
            if ln.startswith("Id="):
                uid = ln[3:].strip()
            elif ln.startswith("TimersCalendar=") and "OnCalendar=" in ln:
                spec = ln.split("OnCalendar=", 1)[1].split(" ;", 1)[0].strip()
            elif ln.startswith("TimersMonotonic=") and not spec and "OnUnitActiveUSec=" in ln:
                spec = "every " + ln.split("OnUnitActiveUSec=", 1)[1].split(" ;", 1)[0].strip()
        if uid:
            out[uid] = spec
    return out


_DOW = {"Mon": "Mondays", "Tue": "Tuesdays", "Wed": "Wednesdays", "Thu": "Thursdays", "Fri": "Fridays",
        "Sat": "Saturdays", "Sun": "Sundays"}


def _friendly(spec: str) -> str:
    """Timer spec text -> plain words for the common shapes; anything else is shown as systemd wrote it."""
    spec = spec.strip()
    m = re.fullmatch(r"every (\d+)\s*(min|s|h|d)", spec)             # from OnUnitActiveSec
    if m:
        n, u = int(m.group(1)), m.group(2)
        if n == 1 and u != "s":
            return {"min": "every minute", "h": "every hour", "d": "every day"}[u]
        return f"every {n} {u}"
    m = re.fullmatch(r"\*-\*-\* \*:0?0?/(\d+):00", spec)              # *-*-* *:00/15:00
    if m:
        return f"every {int(m.group(1))} min"
    m = re.fullmatch(r"(?:(\w{3}) )?\*-\*-\* (\d\d):(\d\d):00", spec)  # [Wed ]*-*-* 07:45:00
    if m:
        dow, hh, mm = m.groups()
        return f"{_DOW.get(dow, 'daily')} at {hh}:{mm}"
    return clean(spec, 60)


class _Run:
    """Inputs shared by several output files, each read at most once and only if a builder asks for it."""

    def __init__(self, status: dict | None, now: float, pub: Path | None = None):
        self.status, self.now = status, now
        self.pub = pub                                 # the public dir (reports/ and live.json live inside it)
        self.errors: list[str] = []                    # files whose build/write failed in this run (shown by overview.json)
        self._memo: dict[str, tuple[bool, Any]] = {}

    def memo(self, key: str, fn: Callable[[], Any]) -> Any:
        """fn() once per run; a failure is remembered too (re-raised), so a broken source costs one attempt, not one per user."""
        if key not in self._memo:
            try:
                self._memo[key] = (True, fn())
            except Exception as exc:                   # noqa: BLE001
                self._memo[key] = (False, exc)
        ok, v = self._memo[key]
        if not ok:
            raise v
        return v

    def slo_history(self) -> list[dict]:
        """The task records of the last 30 days as incidents.export_slo() reads them from history.jsonl."""
        return [{"t": t, "kind": "task", "task": task, "status": st} | ({"alert": al} if al is not None else {})
                for t, task, st, al in self.history.recs]

    def routine_raw(self) -> dict:
        """routine.export() once per run: routine.json and the overview's monthly forecast both read it."""
        return self.memo("routine", lambda: _routine_export(self.now, self.timer_map))

    @cached_property
    def tasks(self) -> dict[str, dict]:
        t = (self.status or {}).get("tasks")
        return {str(k): v for k, v in t.items() if isinstance(v, dict)} if isinstance(t, dict) else {}

    @cached_property
    def raw_timers(self) -> list[dict] | None:
        """`systemctl list-timers` rows (epoch MICROseconds), read once; None when systemctl is unavailable."""
        return _list_timers()

    @cached_property
    def timer_map(self) -> dict[str, dict]:
        """{unit: {"next": epoch s | None, "last": epoch s | None}}: the shape routine.export() takes for its calendar."""
        return {str(t.get("unit")): {"next": _us(t.get("next")), "last": _us(t.get("last"))} for t in self.raw_timers or []}

    @cached_property
    def timers(self) -> list[dict]:
        raw = self.raw_timers
        if raw is None:
            return []
        by_unit = {str(t.get("unit")): t for t in raw}
        units = [u for u in by_unit if u.startswith("homelab-maint-") and u.endswith(".timer")]
        order = {"check": 0, "daily": 1, "weekly": 2, "monthly": 3}
        units.sort(key=lambda u: (order.get(u[len("homelab-maint-"):-len(".timer")], 9), u))
        units += [u for u in NOTABLE_TIMERS if u in by_unit]
        specs = _timer_specs(units)
        out = []
        for u in units:
            t = by_unit[u]
            last, nxt = _us(t.get("last")), _us(t.get("next"))
            out.append({"unit": u, "title": TIMER_TITLES.get(u) or _name(u[:-6]).replace("-", " ").capitalize(),
                        "last": last, "next": nxt, "schedule": _friendly(specs.get(u, "")) if specs.get(u) else ""})
        return out

    @cached_property
    def audit(self) -> list[dict]:
        return _read_audit(self.now)

    @cached_property
    def history(self) -> _History:
        off = {n for n, e in self.tasks.items() if e.get("alert") is False}
        return _History(self.now, off)

    @cached_property
    def acts(self) -> "_Acts":
        return _Acts(self)

    @cached_property
    def reclaimed(self) -> list[dict]:
        log = (self.status or {}).get("reclaimed_log")
        out = []
        for r in log if isinstance(log, list) else []:
            try:                                                             # one odd record must not fail the whole list
                t, b = (_num(r.get("t")), _num(r.get("bytes"), 0)) if isinstance(r, dict) else (None, 0)
                if t is not None and t <= self.now + 3600 and b and b > 0:   # future stamps would break the day buckets
                    out.append({"t": t, "task": _name(r.get("task") or "?", 40), "bytes": _int(b)})
            except Exception:                                                # noqa: BLE001
                continue
        return out


def _us(v: Any) -> float | None:
    f = _num(v)
    return f / 1e6 if f and f > 0 else None


def _acked(e: dict, now: float | None = None) -> dict | None:
    """The live acknowledgement of a FAILING task entry (acks.apply_to_status wrote `acked`), validated, else None."""
    a = e.get("acked")
    if not isinstance(a, dict) or e.get("status") not in ("warn", "crit", "error"):
        return None
    until = _num(a.get("until"))
    return a if until is not None and until > (time.time() if now is None else now) else None


def _ackable(name: str, e: dict) -> bool:
    """acks.entry_ackable (the one policy), False when acks cannot be imported: no module means nothing can be acknowledged, so no button."""
    try:
        from . import acks
        return acks.entry_ackable(name, e)
    except Exception:  # noqa: BLE001
        return False


def _eff(e: dict, now: float | None = None) -> str:
    """Status word shown in the UI for one task entry."""
    s = e.get("status")
    s = s if s in ("ok", "info", "warn", "crit", "error", "skipped") else "info"
    if s == "skipped" or (s in ("warn", "crit") and e.get("alert") is False) or _acked(e, now):
        return "info"                                    # an acknowledged problem is shown muted, never red
    return s


def _title(name: str, e: dict) -> str:
    return clean(e.get("title") or name.replace("_", " ").capitalize(), 40)


# =========================================================================== overview.json
def _tier_mode(run: _Run, tier: str) -> str:
    if tier == "check":
        return "check"
    cl = [e for e in run.tasks.values() if e.get("tier") == tier and e.get("klass") in ("C1", "C2")]
    return "apply" if any(e.get("mode") == "apply" for e in cl) else "report"


def _headline(run: _Run) -> str:
    cnt: dict[str, list[str]] = {"crit": [], "error": [], "warn": []}
    for name, e in sorted(run.tasks.items(), key=lambda kv: (STATUS_RANK[_eff(kv[1], run.now)], kv[0])):
        if _eff(e, run.now) in cnt:
            cnt[_eff(e, run.now)].append(_title(name, e))
    if not run.tasks:
        return "No data yet"
    n_ack = sum(1 for e in run.tasks.values() if _acked(e, run.now))
    tail = f" ({n_ack} acknowledged)" if n_ack else ""
    if not any(cnt.values()):
        return (f"All {len(run.tasks)} checks healthy" if len(run.tasks) > 1 else "All checks healthy") + tail
    parts = []
    if cnt["crit"]:
        parts.append(f"{len(cnt['crit'])} critical")
    if cnt["error"]:
        parts.append(f"{len(cnt['error'])} failed")
    if cnt["warn"]:
        parts.append(f"{len(cnt['warn'])} warning" + ("s" if len(cnt["warn"]) != 1 else ""))
    titles = cnt["crit"] + cnt["error"] + cnt["warn"]
    shown = ", ".join(titles[:3]) + (f" +{len(titles) - 3}" if len(titles) > 3 else "")
    return _cap(", ".join(parts) + ": " + shown + tail, 80)


def _uptime() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _routine_next(run: _Run, tier: str) -> float | None:
    """Next run of a tier that has no systemd timer (monthly) or whose timer is unknown: the routine's own forecast for the
    cadence of the same name. A routine export that failed is the routine file's problem, not the overview's."""
    try:
        doc = run.routine_raw()
    except Exception:                                  # noqa: BLE001
        return None
    nxt = [_num(r.get("next_run")) for r in (doc.get("routine") if isinstance(doc, dict) else None) or []
           if isinstance(r, dict) and r.get("cadence") == tier]
    nxt = [t for t in nxt if t is not None]
    return min(nxt) if nxt else None


def _live_age(run: _Run) -> float | None:
    """Seconds since the live daemon last wrote live.json (its own generated_at, else the file's mtime); None when absent.
    A stamp from the future is clock trouble, not freshness: the mtime is used then."""
    if run.pub is None:
        return None
    path = run.pub / "live.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    gen = None
    try:
        with open(path, "rb") as f:
            raw = f.read(LIVE_MAX + 1)
        doc = json.loads(raw) if len(raw) <= LIVE_MAX else None
        gen = _num(doc.get("generated_at")) if isinstance(doc, dict) else None
    except (OSError, ValueError, RecursionError):
        pass
    if gen is None or gen > run.now + 3600:
        gen = mtime
    return round(max(run.now - gen, 0.0), 1)


def build_overview(run: _Run):
    st = run.status
    gen = _num((st or {}).get("generated_at"))
    if gen is None:                                    # no/garbled status: keep the old file, it will visibly age
        return None, None
    counts = {"ok": 0, "warn": 0, "crit": 0, "info": 0, "error": 0}
    for e in run.tasks.values():
        counts[_eff(e, run.now)] += 1
    overall = st.get("overall")
    if overall not in ("ok", "warn", "crit") or any(_acked(e, run.now) for e in run.tasks.values()):
        # Derived from the muted words (_eff) whenever the file says nothing usable OR an acknowledged task exists: status["overall"] is not
        # trusted then, because scheduler.merge_status recomputes it every tick from the raw statuses (an acknowledged warn would turn the
        # hero yellow again a minute after `ack process` made it green).
        overall = LEVEL_WORD[max([HEALTH_LEVEL[_eff(e, run.now)] for e in run.tasks.values()] or [0])]
    runs = st.get("tier_runs") if isinstance(st.get("tier_runs"), dict) else {}
    nxt = {t["unit"]: t["next"] for t in run.timers}
    tiers = {}
    for tier in TIERS:
        last = _num((runs.get(tier) or {}).get("last_run")) if isinstance(runs.get(tier), dict) else None
        if last is None:
            last = max([_num(e.get("last_run"), 0) or 0 for e in run.tasks.values() if e.get("tier") == tier] or [0]) or None
        nxt_run = nxt.get(f"homelab-maint-{tier}.timer")
        if nxt_run is None and tier != "check":
            nxt_run = _routine_next(run, tier)
        tiers[tier] = {"last_run": last, "next_run": nxt_run, "mode": _tier_mode(run, tier)}
    cleaners = [e for e in run.tasks.values() if e.get("klass") in ("C1", "C2")]
    applying = sum(1 for e in cleaners if e.get("mode") == "apply")
    cleanup = "report" if applying == 0 else "apply" if applying == len(cleaners) else "mixed"
    host = clean(st.get("host") or os.uname().nodename, 64)
    n_ack = sum(1 for e in run.tasks.values() if _acked(e, run.now))
    return {"schema": SCHEMA, "generated_at": gen, "host": host, "overall": overall,
            "paused": bool(st.get("paused")), "counts": counts, "headline": _headline(run), "tiers": tiers,
            "cleanup_mode": cleanup, "uptime_s": _uptime(), "kernel": clean(os.uname().release, 60),
            "live_age_s": _live_age(run), "export_errors": sorted(run.errors), **({"acknowledged": n_ack} if n_ack else {})}, None


# =========================================================================== checks.json
def _mode_word(run: _Run, e: dict) -> str:
    if e.get("mode") == "apply":
        return "apply"
    if e.get("klass") == "C0" or e.get("mode") == "check":
        return "check"
    tr, tier = (run.status or {}).get("tier_runs"), e.get("tier")
    t = tr.get(tier) if isinstance(tr, dict) and isinstance(tier, str) else None
    # a real --apply run whose task is configured mode=report reports; a global --dry-run run is a dry run
    return "report" if isinstance(t, dict) and t.get("dry_run") is False else "dry-run"


def _trim_checks(doc: dict, frac: float) -> bool:
    """Shed detail first (item rows, then metrics, a fraction per pass), then whole checks, lowest severity first."""
    cs = doc["checks"]
    for key in ("items", "metrics"):
        if any(c[key] for c in cs):
            for c in cs:
                n = len(c[key])
                keep = n - math.ceil(n * frac)
                if key == "items":
                    del c["items"][keep:]
                else:
                    for k in list(c["metrics"])[keep:]:
                        del c["metrics"][k]
            return True
    return _drop_oldest(cs, frac)


# Checks whose summary/items quote ANOTHER tool's error text. alert_path_health puts the notification bridge's stderr
# (mail/SMS provider replies, phone numbers, the credential file it could not load) after "rc=", so it is published
# from its counters instead: a fixed vocabulary, never free text.
OPAQUE_CHECKS = {"alert_path_health"}


def _opaque_summary(e: dict) -> str:
    """Fixed-vocabulary summary of the alert-path check, from booleans and counts only."""
    st = e.get("status")
    if st not in ("warn", "crit", "error"):
        return clean(e.get("summary") or "", 160)       # the task's own all-clear text carries no stderr
    if st == "error":                                   # "error: <ExceptionName>: <message>" may quote anything
        return "alert path: check failed"
    m = e.get("metrics") if isinstance(e.get("metrics"), dict) else {}
    problems = []
    if m.get("bridge_ok") is False:
        problems.append("bridge missing")
    if m.get("hook_ok") is False:
        problems.append("smartd hook broken")
    if m.get("smart_broken") is True:
        problems.append(f"{max(_int(m.get('smart_fail_24h')), 1)} SMART alert sends failed in 24h")
    if m.get("notify_broken") is True:
        problems.append(f"{max(_int(m.get('notify_fail_24h')), 1)} notifier sends failed in 24h")
    if not problems:                                    # e.g. "cannot read log": count the flagged items instead
        items = e.get("items") if isinstance(e.get("items"), list) else []
        n = sum(1 for i in items if isinstance(i, dict) and i.get("level") in ("warn", "crit"))
        return f"alert path: {max(n, 1)} problem(s)"
    return "alert path: " + "; ".join(problems)


def _opaque_items(items: list[dict]) -> list[dict]:
    """what/level only; the free-text detail stays only when it holds no raw output ("rc=") and no path."""
    out = []
    for i in items:
        row = {k: i[k] for k in ("what", "level") if k in i}
        d = i.get("detail")
        if isinstance(d, str) and "rc=" not in d.lower() and "/" not in d:
            row["detail"] = d
        out.append(row)
    return out


def build_checks(run: _Run):
    if not run.tasks:
        return None, None
    rows = []
    for name, e in run.tasks.items():
        items = [_scalar_dict(r, 12, 120, rc=True) for r in (e.get("items") if isinstance(e.get("items"), list) else [])[:8]
                 if isinstance(r, dict)]
        if name in OPAQUE_CHECKS:
            items = _opaque_items(items)
            summary = _opaque_summary(e)
        else:
            summary = clean(e.get("summary") or "", 160)
        summary = _cut_rc(re.sub(r"^(ok|info|warn|crit|error|skipped):\s*", "", summary))
        extra: dict = {}
        if (isinstance(e.get("fp"), str) and re.fullmatch(r"[0-9a-f]{16}", e["fp"]) and e.get("status") in ("warn", "crit", "error")
                and _ackable(name, e)):
            extra["fp"] = e["fp"]                          # the issue id the Acknowledge buttons post (only for a task that may be acknowledged)
        ak = _acked(e, run.now)
        if ak:
            extra["acked"] = {"until": _num(ak.get("until")), "by": clean(ak.get("by") or "", 8), "note": clean(ak.get("note") or "", 200),
                              "severity": "crit" if ak.get("severity") == "crit" else "warn", "since": _num(ak.get("since")),
                              "true_status": e.get("status")}
        rows.append({**extra, "name": _name(name), "title": _title(name, e),
                     "klass": e.get("klass") if e.get("klass") in ("C0", "C1", "C2") else "C0",
                     "tier": e.get("tier") if e.get("tier") in TIERS else "check", "status": _eff(e, run.now),
                     "summary": summary, "last_run": _num(e.get("last_run")),
                     "duration_s": round(_num(e.get("duration_s"), 0.0) or 0.0, 2), "mode": _mode_word(run, e),
                     "metrics": _scalar_dict(e.get("metrics"), 20, rc=True), "items": [i for i in items if i]})
    rows.sort(key=lambda c: (STATUS_RANK[c["status"]], c["title"].lower(), c["name"]))
    return {"generated_at": run.now, "checks": rows}, _trim_checks


# =========================================================================== actions.json
_NOT_MAINTENANCE = {"notify", "gate", "acks"}          # (acks: the owner's acknowledgements are not maintenance actions)
FREED_WINDOWS = (("freed_24h", 86400), ("freed_7d", 7 * 86400), ("freed_30d", 30 * 86400), ("freed_total", None))


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _last_days(now: float) -> list[str]:
    today = datetime.fromtimestamp(now).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]


class _Acts:
    """What maintenance freed, per task, window and day. The audit log (action sizes of "done" records) and
    status.reclaimed_log (per-run totals) describe the same bytes, so each figure takes the larger of the two."""

    def __init__(self, run: _Run):
        now = run.now
        self.freed: dict[str, dict[str, int]] = {}
        self.last_action: dict[str, float] = {}
        self.last_out: dict[str, str] = {}
        self.days: dict[str, int] = {}
        self.actions_24h = self.actions_7d = self.actions_30d = 0     # "done" audit records (what was really done), not bytes
        from_log: dict[str, dict[str, int]] = {}
        log_days: dict[str, int] = {}
        for r in run.audit:
            if r["task"] in _NOT_MAINTENANCE:
                continue
            self.last_out[r["task"]] = r["outcome"]
            if r["outcome"] == "done":
                self._add(self.freed, self.days, r["task"], r["ts"], r["bytes"], now)
                age = now - r["ts"]
                self.actions_24h += age <= 86400
                self.actions_7d += age <= 7 * 86400
                self.actions_30d += age <= DAYS * 86400
        for r in run.reclaimed:
            self._add(from_log, log_days, r["task"], r["t"], r["bytes"], now)
        for task, f in from_log.items():
            cur = self.freed.setdefault(task, dict.fromkeys((w for w, _ in FREED_WINDOWS), 0))
            for w in cur:
                cur[w] = max(cur[w], f[w])
        for d, b in log_days.items():
            self.days[d] = max(self.days.get(d, 0), b)

    def _add(self, per_task: dict, per_day: dict, task: str, ts: float, nbytes: int, now: float) -> None:
        self.last_action[task] = max(self.last_action.get(task, 0.0), ts)
        f = per_task.setdefault(task, dict.fromkeys((w for w, _ in FREED_WINDOWS), 0))
        for w, lim in FREED_WINDOWS:
            if lim is None or now - ts <= lim:
                f[w] += nbytes
        if now - ts <= DAYS * 86400:
            per_day[_day(ts)] = per_day.get(_day(ts), 0) + nbytes


def _trim_actions(doc: dict, frac: float) -> bool:
    return _drop_oldest(doc["recent"], frac) or _drop_oldest(doc["journal"], frac)


def build_actions(run: _Run):
    audit, acts = [r for r in run.audit if r["task"] != "acks"], run.acts       # (the owner's acknowledgements have their own panel and file)
    quiet = lambda r: r["outcome"] == "dry-run" or r["outcome"].startswith("refused")      # noqa: E731
    # Report mode audits every candidate as "dry-run"/"refused-*" on each run; those would bury what was really done.
    recent = [r for r in reversed(audit) if not quiet(r)][:RECENT_MAX]
    recent += [r for r in reversed(audit) if quiet(r)][:max(0, min(RECENT_NOISE_MAX, RECENT_MAX - len(recent)))]
    recent.sort(key=lambda r: -r["ts"])

    names = {n for n, e in run.tasks.items() if e.get("klass") in ("C1", "C2")} | set(acts.freed) | set(acts.last_out)
    by_task, totals = {}, dict.fromkeys(("freed_24h", "freed_7d", "freed_30d"), 0)
    for n in sorted(names - _NOT_MAINTENANCE):
        f = acts.freed.get(n) or dict.fromkeys((w for w, _ in FREED_WINDOWS), 0)
        for w in totals:
            totals[w] += f[w]
        by_task[_name(n, 40)] = {"last_run": _num((run.tasks.get(n) or {}).get("last_run")),
                                 "last_action": acts.last_action.get(n), "last_outcome": acts.last_out.get(n, "none"),
                                 **f, "runs_30d": run.history.runs.get(n, 0)}
    totals.update(actions_24h=int(acts.actions_24h), actions_7d=int(acts.actions_7d), actions_30d=int(acts.actions_30d))
    return {"generated_at": run.now, "recent": [_public_record(r) for r in recent], "by_task": by_task,
            "totals": totals, "journal": _read_journal()}, _trim_actions


# =========================================================================== storage.json
def _statvfs_sizes(mounts: list[str], budget: float = 0.5) -> dict[str, int]:
    """Filesystem sizes (used + available, the way the task's used_pct counts them). One helper thread so a hung
    network or USB mount can only cost `budget` seconds in total."""
    out: dict[str, int] = {}

    def work() -> None:
        for m in mounts:
            try:
                s = os.statvfs(m)
                out[m] = (s.f_blocks - s.f_bfree + s.f_bavail) * s.f_frsize
            except OSError:
                pass

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(budget)
    return dict(out)


def _mount_size(row: dict, free: int, pct: float, vfs: int) -> int:
    """Total size: a size field of the task row, else statvfs (if plausible), else derived from used_pct (0 = unknown)."""
    for k in ("size", "size_b", "total", "capacity"):
        v = _num(row.get(k))
        if v and v > 0:
            return int(v)
    if vfs and vfs >= free:
        return vfs
    return int(free / (1 - pct / 100)) if free > 0 and 0 <= pct < 99.5 else 0


def _trim_storage(doc: dict, frac: float) -> bool:
    cut = False
    for s in doc["series"].values():
        k = max(1, int(len(s["t"]) * frac)) if len(s["t"]) > 24 else 0
        if k:
            del s["t"][:k], s["free_gib"][:k]
            cut = True
    return cut or bool(doc["freed_by_day"] and doc["freed_by_day"].pop(0))


def build_storage(run: _Run):
    m = (run.tasks.get("disk_forecast") or {}).get("metrics")
    rows = [r for r in (m.get("mounts") if isinstance(m, dict) else None) or [] if isinstance(r, dict) and r.get("mount")]
    if not rows:                                       # no disk_forecast result yet: keep the previous file
        return None, None
    vfs = _statvfs_sizes([str(r["mount"]) for r in rows])
    mounts = []
    for r in rows:
        free, pct = max(_int(r.get("free")), 0), _num(r.get("used_pct"), 0.0) or 0.0
        lvl = r.get("level") if r.get("level") in ("ok", "info", "warn", "crit") else "ok"
        days = _num(r.get("days"))
        mounts.append({"mount": clean(r["mount"], 80), "free_b": free, "size_b": _mount_size(r, free, pct, vfs.get(str(r["mount"]), 0)),
                       "used_pct": round(pct, 1), "free_h": clean(r.get("free_h"), 14) or human(free),
                       "days": int(round(days)) if days is not None and days >= 0 else None, "level": lvl,
                       "info": bool(r.get("info") or r.get("info_only")) or lvl == "info"})
    mounts.sort(key=lambda x: (x["info"], STATUS_RANK[x["level"]], -x["used_pct"], x["mount"]))
    watch = {x["mount"] for x in mounts if not x["info"]}
    series = {}
    for mount, hours in sorted(run.history.disk.items()):
        label = clean(mount, 80)
        if label in watch and len(series) < 12:        # only watch mounts have a free-space history worth charting
            ts = sorted(hours)[-DAYS * 24:]
            series[label] = {"t": ts, "free_gib": [round(hours[t][0] / hours[t][1] / GIB, 2) for t in ts]}
    return {"generated_at": run.now, "mounts": mounts, "series": series,
            "freed_by_day": [{"day": d, "bytes": run.acts.days.get(d, 0)} for d in _last_days(run.now)]}, _trim_storage


# =========================================================================== metrics / schedule / health-history
def _metrics_export(now: float) -> dict | None:
    """The 7-day ring export (SPEC2 section 3). Module-level so tests can substitute it."""
    from . import metrics_ring
    return metrics_ring.export(now)


def build_metrics(run: _Run):
    try:
        doc = _metrics_export(run.now)
    except Exception:                                  # noqa: BLE001 - no ring module/file yet: keep the previous file
        return None, None
    if not isinstance(doc, dict):
        return None, None
    doc = dict(doc)
    doc.setdefault("generated_at", run.now)
    return doc, None


def build_schedule(run: _Run):
    return {"generated_at": run.now, "timers": run.timers}, None


def build_health(run: _Run):
    by_day: dict[str, dict[str, int]] = {}
    for slot, lvl in run.history.slots.items():
        d = by_day.setdefault(_day(slot * SLOT_S), {"warn": 0, "crit": 0, "worst": 0})
        d["worst"] = max(d["worst"], lvl)
        if lvl == 1:
            d["warn"] += 15
        elif lvl == 2:
            d["crit"] += 15
    days = []
    for d in _last_days(run.now):
        v = by_day.get(d)
        days.append({"day": d, "worst": LEVEL_WORD[v["worst"]] if v else "unknown",
                     "warn_minutes": v["warn"] if v else 0, "crit_minutes": v["crit"] if v else 0})
    return {"generated_at": run.now, "days": days}, None


# =========================================================================== SPEC3 / SPEC4 documents (passed through, scrubbed)
TEXT_KEYS = frozenset({"postmortem_md"})            # multi-line Markdown: scrubbed line by line, newlines kept
STR_MAX = 600                                       # longest ordinary string (the owning modules cap theirs lower)
TEXT_MAX = 3400                                     # longest multi-line text (the postmortem is cut at 3000 upstream)
_TB_FILE = re.compile(r'^\s*File "[^"]*", line \d+')


def _clean_text(s: str, n: int = TEXT_MAX, n_line: int = 300) -> str:
    """Multi-line `clean`: private-key blocks removed, traceback blocks dropped (their final exception line stays), every
    other line scrubbed on its own so lists and headings survive. Blank lines are kept (Markdown paragraphs)."""
    s = s.encode("utf-8", "replace").decode("utf-8")[: n * 2]
    s = _PEM.sub(SECRET, s)
    out: list[str] = []
    in_tb = False
    for ln in s.splitlines():
        if "Traceback (most recent call last)" in ln:
            in_tb = True
            continue
        if _TB_FILE.match(ln) or (in_tb and (not ln.strip() or ln[:1] in " \t")):
            continue
        in_tb = False
        out.append(_cut_rc(clean(ln, n_line)) if ln.strip() else "")
    text = "\n".join(out).strip("\n")
    return text if len(text) <= n else text[: n - 3] + "..."


class _Scrub:
    """Walks a module's export and applies the file-level redaction rules to every leaf (see the module docstring).
    Strings repeat a lot (class words, modes), so each distinct string is cleaned once per run."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], str] = {}

    def line(self, s: str, n: int) -> str:
        k = (s, n)
        v = self._cache.get(k)
        if v is None:
            v = self._cache[k] = _cut_rc(clean(s, n))
        return v

    def walk(self, o: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 9:
            return None
        if isinstance(o, dict):
            out: dict = {}
            for k, v in o.items():
                ks = self.line(str(k), 60) or "_"
                if ks.lower() in _RAW_KEYS or _SENSITIVE_KEY.search(ks):
                    if isinstance(v, str) or (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
                        continue                                       # credential-named / raw-output string fields
                out[ks] = self.walk(v, ks, depth + 1)
            return out
        if isinstance(o, (list, tuple)):
            return [self.walk(v, key, depth + 1) for v in o]
        if isinstance(o, str):
            return _clean_text(o) if key in TEXT_KEYS else self.line(o, STR_MAX)
        if o is None or isinstance(o, (bool, float)):
            return o                                                   # NaN/inf become null in _json_safe
        if isinstance(o, int):
            return o if -MAX_INT <= o <= MAX_INT else None             # the browser cannot hold bigger integers exactly
        return self.line(str(o), STR_MAX)                              # Path, bytes, sets ...: scrubbed as text, never passed raw


def _public_doc(raw: Any, now: float) -> dict:
    """A module's export -> the file content: scrubbed, an object, with a numeric generated_at."""
    if not isinstance(raw, dict):
        raise TypeError(f"export returned {type(raw).__name__}, not an object")
    doc = _Scrub().walk(raw)
    if _num(doc.get("generated_at")) is None:
        doc["generated_at"] = now
    return doc


def _trim_lists(*paths: tuple[str, ...], front: tuple[tuple[str, ...], ...] = ()) -> Callable[[Any, float], bool]:
    """Trim function for `_encode`: drop the tail of the first non-empty list named by `paths` (lists ordered newest or most
    important FIRST), or the head of those in `front` (oldest first); a path is the keys leading to the list."""
    def get(doc: Any, path: tuple[str, ...]):
        for k in path:
            doc = doc.get(k) if isinstance(doc, dict) else None
        return doc if isinstance(doc, list) else None

    def trim(doc: Any, frac: float) -> bool:
        for path in paths:
            lst = get(doc, path)
            if lst:
                return _drop_oldest(lst, frac)
        for path in front:
            lst = get(doc, path)
            if lst:
                del lst[: max(1, int(len(lst) * frac))]
                return True
        return False
    return trim


# The five documents the runner's own modules always provide. A failure is an export error and keeps the previous file.
def _routine_export(now: float, timers: dict) -> dict:
    """routine.export() with the timers this run already read (module-level so tests can substitute it)."""
    from . import routine
    return routine.export(now, timers=timers)


def _incidents_export(now: float) -> dict:
    """incidents.export_incidents(). Without an open incident the audit trail is not needed (it only feeds the "related actions"
    of OPEN incidents; resolved ones carry their own), and reading it is the expensive part: an empty list is passed then."""
    from . import incidents
    doc = incidents.export_incidents(now, audit_rows=[])
    if isinstance(doc, dict) and doc.get("open"):
        doc = incidents.export_incidents(now)
    return doc


def _slo_export(now: float, history: Callable[[], list[dict]]) -> dict:
    """incidents.export_slo() over the task records publish has already read, when the objectives' window fits in what is kept
    here (30 days); a longer window makes the module read the file itself."""
    from . import incidents
    cfg = incidents.load_config()
    window = _int((cfg.get("slo_defaults") or {}).get("window_days"), 30)
    return incidents.export_slo(history() if window <= DAYS else None, now, cfg)


def _pressure_export(now: float) -> dict:
    from .tasks import pressure
    return pressure.export(now)


def build_routine(run: _Run):
    return _public_doc(run.routine_raw(), run.now), _trim_lists(("changes",), ("calendar",))


def build_incidents(run: _Run):
    return _public_doc(_incidents_export(run.now), run.now), _trim_lists(("recent",))


def build_slo(run: _Run):
    return _public_doc(_slo_export(run.now, run.slo_history), run.now), None


def build_pressure(run: _Run):
    return _public_doc(_pressure_export(run.now), run.now), _trim_lists(("spikes",), ("actions",), front=(("history",),))


# The SPEC4 documents. Their modules are written by another stream and may be missing: then the file says so.
# file -> candidate (module under homelab_maint, function) pairs; the first that imports and exists is the source.
OPTIONAL_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "jobs.json": (("scheduler", "export"),),
    "monitors.json": (("tasks.monitors", "export"), ("probes", "export")),
    "notifications.json": (("notify", "export"),),
    "migration.json": (("legacy", "export_public"),),
    "self.json": (("tasks.self_health", "export"),),         # SPEC6 S6: the pipeline's own health; the website judges its age itself (ttl)
}


def _find_source(name: str) -> Callable[[float], Any] | None:
    """The export function behind an optional file, or None when no candidate module imports (module-level for tests)."""
    for mod, attr in OPTIONAL_SOURCES.get(name, ()):
        try:
            fn = getattr(importlib.import_module(f"{__package__ or 'homelab_maint'}.{mod}"), attr, None)
        except Exception:                              # noqa: BLE001 - not written yet, or broken at import: the same to the UI
            continue
        if callable(fn):
            return fn
    return None


def _unavailable(run: _Run) -> dict:
    return {"unavailable": True, "generated_at": run.now}


def _optional_builder(name: str, trim: Callable[[Any, float], bool] | None):
    def build(run: _Run):
        fn = _find_source(name)
        if fn is None:
            return _unavailable(run), None
        raw = fn(run.now)                              # an exception here is an export error: the old file stays, flagged
        if raw is None:                                # the source says "nothing usable" (legacy: unreadable inventory)
            return _unavailable(run), None
        return _public_doc(raw, run.now), trim
    build.__name__ = "build_" + name.split(".")[0]
    return build


build_jobs = _optional_builder("jobs.json", _trim_lists(("jobs",)))
build_monitors = _optional_builder("monitors.json", _trim_lists(("probes",)))
build_notifications = _optional_builder("notifications.json", _trim_lists(("recent",)))
build_migration = _optional_builder("migration.json", _trim_lists(("items",)))
build_self = _optional_builder("self.json", _trim_lists(("checks",)))


# =========================================================================== reports/ (written by the report tasks)
WROTE = object()          # a builder that wrote its own file (under a lock) returns (WROTE, None)


def _read_index(path: Path) -> list[dict] | None:
    """reports/index.json as a list of entries; None when it is missing, too big, not strict JSON or not a list of
    {"id": <valid id>, ...} objects with unique ids."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None
        doc = json.loads(raw.decode("utf-8"), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, list) or not all(isinstance(e, dict) and isinstance(e.get("id"), str) and INDEX_ID.match(e["id"])
                                            for e in doc):
        return None
    return doc if len({e["id"] for e in doc}) == len(doc) else None


@contextlib.contextmanager
def _reports_lock():
    """The report writer's flock (STATE_DIR/reports.lock), taken WITHOUT waiting: yields False when it is held or cannot be
    opened. A held lock means a report is being written right now, and that writer refreshes the index itself."""
    f = None
    got = False
    try:
        f = open(core.STATE_DIR / "reports.lock", "a")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = True
    except OSError:
        pass
    try:
        yield got
    finally:
        if f is not None:
            f.close()                                  # closing the descriptor drops the lock


def build_reports(run: _Run):
    """Check reports/ and refresh index.json when it is missing, damaged or out of step with the report files (see the module
    docstring). Nothing is ever deleted except temp files of a killed writer; (None, None) means "nothing to write"."""
    rdir = run.pub / "reports"
    rdir.mkdir(exist_ok=True)                          # the parent exists: never parents=True, never recreated
    try:
        os.chmod(rdir, 0o755)
    except OSError:
        pass
    ids: set[str] = set()
    now = time.time()
    for entry in os.scandir(rdir):
        name = entry.name
        try:
            if name.startswith(".") and name.endswith(".tmp"):
                if now - entry.stat().st_mtime > TMP_MAX_AGE:
                    os.unlink(entry.path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            stem, ext = os.path.splitext(name)
            if ext != ".json" or not (REPORT_ID.match(stem) or stem == "index"):
                continue
            if stem != "index":
                ids.add(stem)
            mode = entry.stat().st_mode
            if not mode & 0o004:                       # the web container runs as another uid: it must be able to read
                os.chmod(entry.path, mode & 0o7777 | 0o644)
        except OSError:
            continue
    index = _read_index(rdir / "index.json")
    if index is None and not ids:
        return None, None                              # no reports yet: nothing to index
    listed = [e["id"] for e in index] if index is not None else []
    if index is not None and set(listed) <= ids and (ids <= set(listed) or len(listed) >= REPORT_KEEP):
        return None, None                              # consistent (a retention overflow is evicted by the next report)
    with _reports_lock() as got:
        if not got:
            return None, None
        from . import reports
        kept, _dropped = reports.trim_index(reports.load_index(rdir))   # the files are the truth; nothing is deleted here
        data = _dump(_Scrub().walk(kept))                  # the headlines are copied out of report files: scrubbed like every string
        try:
            if (rdir / "index.json").read_bytes() == data:      # e.g. a report file nobody can parse: the rebuild is what is there
                return None, None
        except OSError:
            pass
        _write_atomic(rdir / "index.json", data)
    return WROTE, None


BUILDERS: list[tuple[str, Callable]] = [
    ("overview.json", build_overview), ("checks.json", build_checks), ("actions.json", build_actions),
    ("storage.json", build_storage), ("metrics.json", build_metrics), ("schedule.json", build_schedule),
    ("health-history.json", build_health),
    ("routine.json", build_routine), ("incidents.json", build_incidents), ("slo.json", build_slo),
    ("pressure.json", build_pressure), ("jobs.json", build_jobs), ("monitors.json", build_monitors),
    ("notifications.json", build_notifications), ("migration.json", build_migration), ("self.json", build_self),
    ("reports/index.json", build_reports),
]


# =========================================================================== acknowledged issues (SPEC5)
def _publish_acks(pub: Path, now: float) -> list[str]:
    """public/acks.json and ack/tokens.json, once the acknowledgement feature is installed (acks.json or the ack/ directory exists), so an
    install without it publishes exactly what it did before. Isolated like every builder: a failure leaves the old files and is printed."""
    if not ((core.STATE_DIR / "acks.json").exists() or (core.STATE_DIR / "ack").is_dir()):
        return []
    out: list[str] = []
    try:
        from . import acks
        _write_atomic(pub / "acks.json", _encode(_public_doc(acks.public_doc(now), now), _trim_lists(("acks",))))
        out.append("acks.json")
        acks.export_tokens(now)                        # NOT scrubbed: its keys are hashes, which the redactor would (rightly) treat as secrets
        out.append("ack/tokens.json")
    except Exception as exc:                           # noqa: BLE001
        print(f"homelab-maint publish: acks: {type(exc).__name__}: {clean(exc, 100)}", file=sys.stderr)
    return out


# =========================================================================== the rules registry (SPEC6)
def _write_rules_history(pub: Path, now: float) -> bool:
    """public/rules-history.json from the `history` of the rules.json just written (the registry's own redaction already ran on it, so no
    second scrub: the cleaner would take a 64-hex registry hash for a secret). Capped like every public file. True = written."""
    try:
        with open(pub / "rules.json", "rb") as f:
            raw = f.read(2 * MAX_FILE_BYTES + 400_000)
        rj = json.loads(raw)
        hist = rj.get("history") if isinstance(rj, dict) else None
        if not isinstance(hist, list):
            return False
        doc = {"schema": SCHEMA, "generated_at": now, "registry_hash": str(rj.get("registry_hash") or ""), "history": hist}
        _write_atomic(pub / "rules-history.json", _encode(doc, _trim_lists(("history",))))
        return True
    except (OSError, ValueError, RecursionError, TypeError):
        return False


def _publish_registry(pub: Path, status: Any, now: float) -> list[str]:
    """rules.json, rules-history.json and manifest.json, LAST: the manifest describes the files that are already there. registry.write_public
    never raises, rebuilds rules.json at most every 5 minutes unless the registry changed, and has its own size cap (700 KB), which is why
    these files are exempt from MAX_FILE_BYTES. Its first manifest is replaced at once by one that also lists rules-history.json and gives a
    file without a `schema` key (most SPEC2/3 documents) this module's SCHEMA, so the website never sees a null version."""
    try:
        from . import registry
        out = list(registry.write_public(pub, status=status if isinstance(status, dict) else None, now=now))
        if ("rules.json" in out or not (pub / "rules-history.json").exists()) and _write_rules_history(pub, now):
            out.insert(out.index("manifest.json") if "manifest.json" in out else len(out), "rules-history.json")
        mf = registry.build_manifest(pub, now=now)
        for v in mf["files"].values():
            if isinstance(v, dict) and v.get("schema") is None:
                v["schema"] = SCHEMA
        registry.atomic_write(pub / "manifest.json", json.dumps(mf, separators=(",", ":"), default=str).encode(), 0o644, ".pub-")
        return out if "manifest.json" in out else out + ["manifest.json"]
    except Exception as exc:                           # noqa: BLE001
        print(f"homelab-maint publish: rules export: {type(exc).__name__}: {clean(exc, 100)}", file=sys.stderr)
        return []


# =========================================================================== entry points
def publish(status: dict | None = None, now: float | None = None) -> list[str]:
    """Write the public files; return the names written. Never raises; a file that cannot be built keeps its old content."""
    written: list[str] = []
    try:
        now = time.time() if now is None else float(now)
        pub = _public_dir()
        if not isinstance(status, dict):
            status = read_json(core.STATE_DIR / "status.json", None)
        run = _Run(status if isinstance(status, dict) else None, now, pub)
        done: set[str] = set()
        # overview.json goes last: it announces (export_errors) the files whose build failed in this very run
        for name, build in sorted(BUILDERS, key=lambda b: b[0] == "overview.json"):
            try:
                doc, trim = build(run)
                if doc is None:
                    continue
                if doc is not WROTE:
                    _write_atomic(pub / name, _encode(doc, trim))
                done.add(name)
            except Exception as exc:                   # noqa: BLE001 - isolate files from each other
                run.errors.append(name)
                print(f"homelab-maint publish: {name}: {type(exc).__name__}: {clean(exc, 100)}", file=sys.stderr)
        written = [n for n, _ in BUILDERS if n in done]
        written += _publish_acks(pub, now)
        written += _publish_registry(pub, status, now)                 # manifest.json last of all
    except Exception as exc:                           # noqa: BLE001
        print(f"homelab-maint publish: {type(exc).__name__}: {clean(exc, 100)}", file=sys.stderr)
    return written


def main() -> int:
    names = publish()
    print("published: " + (", ".join(names) or "nothing"), file=sys.stderr if not names else sys.stdout)
    return 0 if names else 1


if __name__ == "__main__":
    raise SystemExit(main())
