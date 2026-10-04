"""incidents: incident ledger, playbooks, SLOs and postmortem stubs (SPEC3 S3).

One call per runner tick, after status.json and alerts.json are written, read-only on every task and on the host:

    incidents.update(status, history, now)      # glue in cli.cmd_run; never raises, returns a small dict. history=None reads
                                                # STATE_DIR/history.jsonl itself (what the glue should do). Notify ONLY for the LIVE
                                                # lists (see "update() result"); `backfilled` is history, not news
    incidents.export_incidents()                # public incidents.json shape (read-only)
    incidents.export_slo()                      # public slo.json shape (read-only)
    incidents.write_public()                    # both of the above into STATE_DIR/public/ (what the website reads)
    incidents.playbook_for(task)                # operator guidance for a task (playbooks.toml)
    python3 -m homelab_maint.incidents list|show ID|playbook TASK|slo|export|update

Files (all under STATE_DIR, the tool's own state):
  incidents.jsonl        append-only events, the SOURCE OF TRUTH. A torn last line is skipped on read and the next append
                         starts on a fresh line. Events: open, escalate, improve, ack, mitigated, group, resolve, note, acked, unacked.
  incidents-state.json   per-task debounce counters, a read cursor per task and a "live" overlay (current summary/entities of
                         open incidents). Losing it is harmless (see "Crash safety").
  incidents.json         public snapshot (export_incidents shape), rewritten every update. Compact JSON, < 190 KB.
  slo.json               public snapshot (export_slo shape), rewritten every update.
  incidents.lock         flock so two tiers finishing together cannot interleave.
Config: the baseline homelab_maint/data/playbooks.toml (inside the package, replaced on every install) overlaid per playbook and per
key by the optional, overrides-only /etc/homelab-maint/playbooks.toml (the etc/playbooks.toml of the source tree is its template).

Lifecycle (the SAME debounce as core.Notifier, replayed from history so a missed tick loses nothing; tests/test_incidents.py runs the
real Notifier and this engine on random sequences and asserts every page coincides with an incident event):
  * Every history task record is an observation (t, status). Level: ok/info = 0, warn = 1, crit/error = 2, `skipped` is
    neutral (no information, never resolves anything); a task that ran with alert=false counts as 0 (the per-run flag if the
    record has one, else the `informational` task list in playbooks.toml).
  * A different level must repeat `confirm_runs` times in a row before it is "confirmed": per task
    `[tasks.X].alert_confirm_runs` in maint.toml, else `[incidents].confirm_runs`, else `[global].alert_confirm_runs` (default 2).
    0 -> warn/crit opens an incident, warn -> crit escalates, crit -> warn improves, -> 0 after `resolve_runs` consecutive healthy
    observations resolves. One-run blips and flapping never open anything.
  * Severity: sev3 = confirmed warn, sev2 = confirmed crit (or error), sev1 = crit with at least `sev1_related` OTHER open incidents
    in the same group. Severity only rises while an incident is open (it records the peak).
  * Correlation: incidents whose failures STARTED within `group_window_s` (600 s) of each other share one flat group; the earliest
    is the parent (probable root cause), the others carry `parent`; chains merge. Checks are sampled every 15 minutes, so the window
    is compared with one sampling interval of slack (two faults 3 minutes apart are seen 15 minutes apart whenever a run falls
    between them).
  * The ledger is authoritative for "is task X open": the confirmed level is derived from the open incident, never stored.

Acknowledged issues (SPEC5, homelab_maint/acks.py). A failing status entry that carries `acked: {fp, until, by, note, severity, since}`
(acks.apply_to_status, set by the glue before this module runs) keeps its incident OPEN but ACKNOWLEDGED: ledger event `acked`, public
`status: "acknowledged"` (still in `open`, still listed) plus `ack: {...}` and the issue id `fp`; the owner's acknowledgement is the
incident's `acknowledged_at` when nothing paged it first (MTTA). An acknowledged incident does not count towards another one's sev1 rule,
and its opened/escalated transitions are NOT in the lists glue pages from (they are listed under `held`, newly acknowledged ones under
`acked`). When the entry stays failing but loses `acked` (removed, expired, or worse than the acknowledged severity) the event `unacked`
puts it back. A recovery that is still being confirmed keeps the state; the incident resolves as always. The tracker (debounce, levels)
never looks at acknowledgements: the true status keeps driving it. Without `acked` keys nothing here changes.

update() result  {"ok", "opened", "escalated", "improved", "resolved", "backfilled": {the same four}, "events", "open", "future",
                  "clamped", "acked", "held"}. Each list holds incident ids. opened/escalated/improved/resolved are the LIVE transitions: the event is at
                  most LIVE_S (two sampling intervals) older than `now`. A first install replays up to 31 days of history, and a runner
                  that missed ticks catches up in one call: those transitions are listed under `backfilled`, and glue that wired the
                  lists to incident_open / incident_resolved notifications would otherwise page about yesterday (5 pages on this host's
                  real history, against a daily budget of 8). Glue must NOT notify for `backfilled`. (A later transition of an incident whose
                  OPENING was backfilled in the same call is backfilled too: nobody was told it was open.)
                  `future`: {task: n} observations ignored for being stamped > FUTURE_S ahead of the clock; `clamped`: tasks whose cursor
                  was found in the future and moved behind the clock (see "Clock steps").

Clock steps. Observations are consumed by comparing wall-clock times with a per-task cursor, so a cursor stamped in the future (RTC ahead,
then an NTP step back) would make every later observation look old: the check could be genuinely failing for hours with no incident, and
an open incident would never see its healthy samples (the Notifier has no such cursor and would keep paging). Therefore an observation
stamped more than FUTURE_S (15 min) ahead of the clock is ignored (counted in `future`, noted once on the open incident's timeline and in
`incidents list`), a cursor / watermark / floor found ahead of the clock is moved CLAMP_BACK_S behind it, and the tracker remembers its last
8 consumed observation times so nothing is ever counted twice after such a step. SLO slots ignore such samples too.

Crash safety (the ledger is fsynced BEFORE the state file is written; update() is idempotent for the same inputs):
  * state older than the ledger (killed between the two writes): observations at or before the task's newest ledger event are
    skipped (a per-task watermark), so a multi-episode backlog replayed after a crash does not open the same episode twice.
  * state ahead of the ledger (a ledger line was lost or torn): a tracker with no streak pending knows the confirmed level, so a
    lost open / escalate / improve / resolve line is re-emitted before the new observations are consumed.
  * group and sev1 are reconciled at the end of every tick, ack and mitigation are re-derived from audit.jsonl; so those lines can
    be lost without consequence. tests/test_incidents.py tears a six-event batch at every byte boundary and compares.

Definitions (all seconds, all derived from observation times, never from the wall clock at processing time):
  started_at       first failing observation of the episode (a lower bound: the check only runs every 15 minutes)
  detected_at      the confirming observation (= the open event timestamp)
  acknowledged_at  first "sent" WARN/CRIT page for the task in audit.jsonl (fallback: alerts.json last_sent); a failed send, a
                   dropped one (budget) and the OK recovery page are not acknowledgements
  mitigated_at     first audit action with outcome "done" by the task itself, a task in the playbook's related_tasks, or
                   on a target named in the failing items. Dry-run ("would have") lines are listed but never count.
  resolved_at      FIRST healthy observation of the confirming recovery streak (service restored); closed_at = the confirming one
  mttd_s = detected_at - started_at     mtta_s = acknowledged_at - started_at     mttr_s = resolved_at - started_at

Public shapes (also written to the snapshot files):
  incidents.json  {"generated_at","open":[{id,task,title,severity,level,status,since,duration_s,detected_at,acknowledged_at,
                   mitigated_at,last_checked,summary,cause_hint,parent,children,related,service_class,playbook,
                   timeline:[<=20 {t,kind,text}],related_actions:[<=10 {t,last_t,task,action,outcome,count,bytes,target}]}],
                   "recent":[<=50 resolved: the same keys WITHOUT playbook (it is ~3 KB each and only useful while open) plus
                   resolved_at, mttd_s, mttr_s, resolution, postmortem_md (<=3 KB)],
                   "stats":{mttd_s_30d,mttr_s_30d,mtta_s_30d,incidents_30d,resolved_30d,open_count,by_severity_30d}}
  playbook        {task,title,class,meaning,impact,checks[],fixes[],avoid[],ask}; a check line starting "$ " is a command.
  slo.json        {"generated_at","window_days","objectives":[{name,target_pct,class,checks[],availability_pct,
                   budget_remaining_pct,burn_rate_1d,status,samples,observed_h,bad_minutes,budget_minutes,note}]}; with no samples
                   yet the numbers are null and status is "ok" with note "collecting data". Availability is over the hours
                   observed (`observed_h`), the budget over the whole 30-day window, so a young install can already be breached.
Everything dynamic is redacted (see redact(): publish.clean() plus local rules, so ENV_STYLE_NAMES=, postgres://u:pw@h, -pPASS,
Cookie: and sk-ant-/xoxb- tokens too): no secrets, tokens, e-mail addresses, URL query strings, notify error text or tracebacks ever
reach the snapshots or the exports, and the ledger file itself is redacted at INGEST (_scrub_event), not only when it is folded.
This module never runs a command and never writes outside STATE_DIR.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from . import core

SEV_RANK = {"sev3": 1, "sev2": 2, "sev1": 3}
LEVEL_WORD = {1: "warn", 2: "crit"}
SLOT_S = 900                          # one history sample per 15 minutes = one SLO slot
FUTURE_S = 900                        # an observation stamped further ahead of the clock than this is not trusted (RTC ahead, then an NTP step back)
CLAMP_BACK_S = 8 * SLOT_S             # a cursor found in the future is moved this far BEHIND the clock: the tracker remembers its last 8 consumed
                                      # observations (never counted twice), and anything older was consumed before the clock went wrong
LIVE_S = 2 * SLOT_S                   # a transition older than this when it is first folded is history, not news (first install, missed ticks)
LEDGER_COMPACT_BYTES = 2 << 20
PM_MAX = 3000                         # postmortem_md in the public export
CAUSE_LAG_S = 1800                    # default [[correlation]] lag_s: a cause that ended > 30 min before the effect began is not offered
TL_PUBLIC, ACT_PUBLIC = 20, 10
PUBLIC_MAX_BYTES = 190_000            # each public file must stay < 200 KB (SPEC2)
_PKG_PLAYBOOKS = Path(__file__).resolve().parent / "data" / "playbooks.toml"      # the baseline INSIDE the package: replaced on every install

_DEFAULTS = {"confirm_runs": None, "resolve_runs": None, "group_window_s": 600, "sev1_related": 2,
             "orphan_close_h": 6, "keep_days": 90, "recent_max": 50}
_SLO_DEFAULTS = {"window_days": 30, "at_risk_budget_pct": 25, "at_risk_burn": 3.0,
                 "informational": ["docker_df", "config_drift", "qos_classes", "bulkhead_check",
                                "docker_prune_parity", "c2_candidates", "report_daily", "report_weekly"]}
_BUILTIN_PB = {"title": "", "class": "P2", "ask": "The owner (ohmz).",
               "meaning": "A homelab-maint task reported a problem that persisted for the confirmation window.",
               "impact": "Not known for this task."}


# =========================================================================== small helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def _int(v: Any, default: int, lo: int = 1) -> int:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= lo else default


def _flt(v: Any, default: float) -> float:
    n = _num(v)
    return default if n is None else n


def _sev(v: Any, default: str = "sev3") -> str:
    return v if isinstance(v, str) and v in SEV_RANK else default


def _r(t: float | None) -> float | None:
    return None if t is None else round(float(t), 1)


def _dur(s: float | None) -> str:
    if s is None:
        return "n/a"
    s = int(max(s, 0))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}m" if h else (f"{m}m" if m else f"{s}s")


def _when(t: float | None) -> str:
    return "n/a" if t is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


# =========================================================================== redaction (nothing sensitive leaves this module)
# Two layers, because this is the last gate before an internet-facing file:
#   1. publish.clean(), THE redactor of the public export (SPEC2 section 2): key=value and ENV_STYLE=NAMES, URL userinfo / query /
#      token path segments, -pPASS, Cookie:, prefixed tokens (sk-ant-, xoxb-, ghp_ ...), entropy blobs, e-mail, phone, /home paths.
#      Delegating means a hole closed there is closed here. It scans 800 chars of ONE line, so redact() feeds it line by line.
#   2. The rules below, applied before (the multi-line ones) and after it. They are the whole defence when publish cannot be
#      imported, and cover what layer 1 leaves: "password: a pass phrase", multi-pair Cookie headers, tracebacks.
# Trusted text (shipped playbooks, `paths=False`) skips layer 1: it is long, holds commands, and the lint tests keep it clean.
_PRE = r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"    # start of a name PART: DB_PASSWORD, x.api_key, dbPassword ("bypass", "passed" do not match)
_AFFIX = r"(?:[_.-][\w.-]{0,24})?"               # PASSWORD_FILE, SECRET_KEY, AWS_SECRET_ACCESS_KEY (the part after the key word)
_PW = _PRE + r"(?:password|passwd|passphrase|pwd)" + _AFFIX
_SECRET_KEY = (_PRE + r"(?:pass|token|secret|api[_-]?key|apikey|authorization|credentials?|session[_-]?id|(?:private|access)[_-]?key)s?"
               + _AFFIX)
_RX_PRE = [
    (re.compile(r"Traceback \(most recent call last\):.*", re.S), "[traceback removed]"),
    (re.compile(r'(?m)^[ \t]*File "[^"]*", line \d+.*$'), ""),
    # a password may hold spaces ("password: a pass phrase") and a Cookie header several pairs: redact to the end of the line
    (re.compile(rf"(?i)({_PW}|{_PRE}(?:set-)?cookie)([\"']?[ \t]*[=:][ \t]*).*"), r"\1\2[redacted]"),
]
_RX_POST = [
    (re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+"), "[redacted]"),          # e-mail ({1,64}: the unbounded form is quadratic)
    (re.compile(rf"""(?i)({_SECRET_KEY})(["']?[ \t]*[=:][ \t]*)(?:(?:bearer|basic)[ \t]+)?(?:"[^"]*"|'[^']*'|[^\s,;&"']+)"""),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)(--?[\w-]{0,24}(?:pass|secret|token|api[_-]?key|auth|cred)[\w-]{0,24})[ \t]+(?!-)\S+"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(?:bearer|basic)[ \t]+[\w.~+/=-]{8,}"), "bearer [redacted]"),
    (re.compile(r"""(?i)\b([a-z][a-z0-9+.-]{1,10}://)[^\s/?#"'<>]*@"""), r"\1[redacted]@"),   # userinfo of ANY scheme: postgres://u:pw@h
    (re.compile(r"""(?i)\b([a-z][a-z0-9+.-]{1,10}://[^\s?#"'<>]+)[?#]\S*"""), r"\1"),             # query string / fragment dropped
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{16,}|sk-[\w-]{16,}|xox[abprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}|"
                r"AIza[\w-]{30,}|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*)"), "[redacted]"),                     # well-known token shapes (hyphens included)
    (re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "[redacted]"),
    (re.compile(r"[A-Za-z0-9_+=]{32,}"), "[redacted]"),                       # long tokens, ids, hashes
]
_P_TOOLS = re.compile(r"(?i)\b(?:mysql(?:dump|admin)?|mariadb|psql|sshpass|redis-cli|(?:docker|podman)[ \t]+login)\b")
_P_FLAG = re.compile(r"(?<!\S)-p[ \t]*(?!-)\S+")                               # `mysql -pSecret`; `docker run -p 80:80` is a port, so tool-gated
_RX_PATH = (re.compile(r"(/home/[^/\s]+(?:/[^/\s]+){3})/\S+"), r"\1/...")           # SPEC2: truncate deep paths under /home/<user>
_LINE_MAX, _TEXT_MAX = 800, 16000                  # scanned chars per line / per text: bounds regex work on a hostile blob
_clean_fn: Any = None


def _publish_clean() -> Any:
    """publish.clean, imported on first use (not at module level: publish is a bigger module that may later import this one)."""
    global _clean_fn
    if _clean_fn is None:
        try:
            from . import publish
            _clean_fn = publish.clean
        except Exception:  # noqa: BLE001 - layer 2 alone still redacts
            _clean_fn = False
    return _clean_fn or None


def _local(s: str, paths: bool, pre: bool) -> str:
    for rx, rep in (_RX_PRE if pre else _RX_POST):
        s = rx.sub(rep, s)
    if not pre:
        if _P_TOOLS.search(s):
            s = _P_FLAG.sub("-p [redacted]", s)
        if paths:
            s = _RX_PATH[0].sub(_RX_PATH[1], s)
    return s


def redact(s: Any, paths: bool = True) -> str:
    """Strip secrets, e-mail addresses, phone numbers, URL userinfo and query strings and tracebacks. Line structure is kept (the
    postmortem is Markdown). `paths` = untrusted text: layer 1 as well, and deep paths under /home/<user> are truncated (SPEC2:
    audit targets). `paths=False` = trusted playbook text: layer 2 only, so commands and paths survive."""
    s = "" if s is None else str(s)
    s = s.encode("utf-8", "replace").decode("utf-8")        # lone surrogates from odd file names break json/utf-8 later
    if paths:
        s = s[:_TEXT_MAX]
    clean = _publish_clean() if paths else None
    out = []
    for ln in _local(s, paths, True).split("\n"):
        if paths:
            ln = ln[:_LINE_MAX]
            if clean is not None:
                try:
                    ln = clean(ln, 10 * _LINE_MAX)
                except Exception:  # noqa: BLE001
                    pass
        out.append(_local(ln, paths, False))
    return "\n".join(out)


def _line(s: Any, n: int = 200) -> str:
    """Redacted, single-line, control-free, bounded text."""
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", redact(s))).strip()
    return s if len(s) <= n else s[: max(n - 2, 1)] + ".."


# =========================================================================== config and playbooks
_TOML_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}


def _toml(path: Path, cache: bool = False) -> dict:
    """Parsed TOML, {} when the file is missing or broken (a broken file must not stop incident tracking). `cache` keeps the
    parse of the big packaged playbooks file for as long as its (mtime, size) are unchanged."""
    try:
        key = None
        if cache:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
            hit = _TOML_CACHE.get(str(path))
            if hit and hit[0] == key:
                return hit[1]
        with open(path, "rb") as f:
            doc = tomllib.load(f)
        if key is not None:
            _TOML_CACHE[str(path)] = (key, doc)
        return doc
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge_tables(base: Any, over: Any, key) -> list[dict]:
    """Two TOML array-of-tables merged by `key(table)`: an override with the same key is merged per key into the shipped one, a new
    key is added, `enabled = false` drops one. (Replacing the whole list would let one stale or partial override hide every
    shipped objective.)"""
    out: dict[Any, dict] = {}
    for src in (base, over):
        for t in src if isinstance(src, list) else []:
            if isinstance(t, dict) and key(t) is not None:
                out[key(t)] = {**out.get(key(t), {}), **t}
    return [t for t in out.values() if t.get("enabled", True) is not False]


def _slo_key(t: dict) -> Any:
    return t["name"] if isinstance(t.get("name"), str) else None


def _corr_key(t: dict) -> Any:
    return (t["cause"], t["effect"]) if isinstance(t.get("cause"), str) and isinstance(t.get("effect"), str) else None


def load_config() -> dict:
    """The shipped baseline (homelab_maint/data/playbooks.toml, replaced on every install) overlaid by the optional overrides-only
    /etc/homelab-maint/playbooks.toml: per playbook and per key; [[slo]] by name; [[correlation]] by cause + effect."""
    pkg, over = _toml(_PKG_PLAYBOOKS, cache=True), _toml(core.CONF_DIR / "playbooks.toml")
    pbs: dict[str, dict] = {}
    for n in set(pkg.get("playbook", {})) | set(over.get("playbook", {})):
        pbs[n] = {**pkg.get("playbook", {}).get(n, {}), **over.get("playbook", {}).get(n, {})}
    return {"incidents": {**_DEFAULTS, **pkg.get("incidents", {}), **over.get("incidents", {})},
            "slo_defaults": {**_SLO_DEFAULTS, **pkg.get("slo_defaults", {}), **over.get("slo_defaults", {})},
            "slo": _merge_tables(pkg.get("slo"), over.get("slo"), _slo_key),
            "correlation": _merge_tables(pkg.get("correlation"), over.get("correlation"), _corr_key),
            "playbook": pbs}


def override_notes() -> list[str]:
    """Warnings about /etc/homelab-maint/playbooks.toml for the operator. An override value that is IDENTICAL to the shipped one
    changes nothing today and shadows every later fix of it: that is what a pasted copy of the baseline looks like."""
    pkg, over = _toml(_PKG_PLAYBOOKS, cache=True), _toml(core.CONF_DIR / "playbooks.toml")
    same: list[str] = []

    def cmp(label: str, base: Any, tbl: Any) -> None:
        if isinstance(base, dict) and isinstance(tbl, dict):
            same.extend(f"{label}.{k}" for k, v in tbl.items() if k in base and base[k] == v and k not in ("name", "cause", "effect"))

    for name, tbl in (over.get("playbook") or {}).items():
        cmp(name, (pkg.get("playbook") or {}).get(name), tbl)
    for sect in ("incidents", "slo_defaults"):
        cmp(sect, pkg.get(sect), over.get(sect))
    for sect, key in (("slo", _slo_key), ("correlation", _corr_key)):
        shipped = {key(t): t for t in pkg.get(sect) or [] if isinstance(t, dict)}
        for t in over.get(sect) if isinstance(over.get(sect), list) else []:
            if isinstance(t, dict) and key(t) in shipped:
                cmp(f"{sect}[{key(t)[0] if isinstance(key(t), tuple) else key(t)}]", shipped[key(t)], t)
    if not same:
        return []
    return [f"{core.CONF_DIR / 'playbooks.toml'} repeats {len(same)} shipped value(s) (e.g. {', '.join(same[:3])}); they change nothing today "
            f"and hide every later fix of that text. Keep only what you changed on purpose."]


def _runs(cfg: dict, maint: dict | None = None, task: str | None = None) -> tuple[int, int]:
    """(confirm_runs, resolve_runs): the pager's debounce, so an incident opens when the page goes out. Precedence: a per-task
    `[tasks.X].alert_confirm_runs` in maint.toml, then `[incidents].confirm_runs` in playbooks.toml, then the global
    `[global].alert_confirm_runs` (default 2). resolve_runs defaults to the same number."""
    maint = maint if maint is not None else _toml(core.CONF_DIR / "maint.toml")
    c = (maint.get("tasks", {}).get(task, {}) or {}).get("alert_confirm_runs") if task else None
    if not (isinstance(c, int) and not isinstance(c, bool) and c >= 1):
        c = cfg["incidents"].get("confirm_runs")
    if not (isinstance(c, int) and not isinstance(c, bool) and c >= 1):
        c = _int((maint.get("global") or {}).get("alert_confirm_runs"), 2)
    r = cfg["incidents"].get("resolve_runs")
    return c, (r if isinstance(r, int) and not isinstance(r, bool) and r >= 1 else c)


def playbook_for(task: str, cfg: dict | None = None) -> dict:
    """Normalised playbook for `task`; keys missing from it fall back to [playbook._default]."""
    cfg = cfg or load_config()
    pbs = cfg["playbook"]
    raw = {**_BUILTIN_PB, **pbs.get("_default", {}), **pbs.get(task, {})}
    generic = task not in pbs

    def txt(v: Any) -> str:
        return str(v).replace("TASK_NAME", task) if isinstance(v, str) else ""

    def lst(k: str) -> list[str]:
        return [txt(x) for x in raw.get(k, []) if isinstance(x, str)][:20]

    return {"task": task, "title": txt(raw.get("title")) or task, "class": txt(raw.get("class")) or "P2",
            "meaning": txt(raw.get("meaning")), "impact": txt(raw.get("impact")), "checks": lst("checks"),
            "fixes": lst("fixes"), "avoid": lst("avoid"), "ask": txt(raw.get("ask")), "followups": lst("followups"),
            "related_tasks": [x for x in raw.get("related_tasks", []) if isinstance(x, str)],
            "hint": [] if generic else [h for h in raw.get("hint", []) if isinstance(h, dict)
                                        and isinstance(h.get("match"), str) and isinstance(h.get("text"), str)]}


def _public_pb(pb: dict) -> dict:
    """The playbook as exported. Redacted like everything else: the shipped text is lint-tested clean, this is the safety net for
    an owner's override file."""
    out: dict[str, Any] = {}
    for k in ("task", "title", "class", "meaning", "impact", "checks", "fixes", "avoid", "ask"):
        v = pb[k]
        out[k] = [redact(x, paths=False) for x in v] if isinstance(v, list) else redact(v, paths=False)
    return out


def format_playbook(task: str, cfg: dict | None = None) -> str:
    """Plain-text runbook for the terminal (python3 -m homelab_maint.incidents playbook TASK)."""
    pb = playbook_for(task, cfg)
    out = [f"{pb['title']} ({task}), service class {pb['class']}", "", "What it means:", f"  {pb['meaning']}", "",
           "Impact:", f"  {pb['impact']}", "", "First checks:"]
    out += [f"  {c}" if c.startswith("$ ") else f"- {c}" for c in pb["checks"]]
    out += ["", "Safe fixes:"] + [f"  - {x}" for x in pb["fixes"]]
    out += ["", "Do NOT:"] + [f"  - {x}" for x in pb["avoid"]]
    out += ["", f"Who or what to ask: {pb['ask']}"]
    return "\n".join(out)


# =========================================================================== file I/O
def _p(name: str) -> Path:
    return core.STATE_DIR / name


def _write_public_file(path: Path, obj: Any) -> None:
    """Public snapshot: compact JSON (the browser downloads it every poll), atomic, mode 0644. Sized by the same encoding the
    export builders cap on, so the 190 KB budget really is the file size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class _Lock:
    def __enter__(self):
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(_p("incidents.lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _read_events() -> list[dict]:
    """Ledger lines that parse. A truncated/garbled line (crash mid-write) is skipped, never fatal."""
    out: list[dict] = []
    try:
        with open(_p("incidents.jsonl"), "rb") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def _append_events(events: list[dict]) -> None:
    """Append and fsync. If the file does not end in a newline (a torn write) start on a fresh line first, so the new
    event is not glued onto the garbage."""
    if not events:
        return
    p = _p("incidents.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(e, separators=(",", ":"), sort_keys=True, default=str) + "\n" for e in events).encode()
    with open(p, "ab+") as f:
        end = f.seek(0, os.SEEK_END)
        if end:
            f.seek(end - 1)
            if f.read(1) != b"\n":
                data = b"\n" + data
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _compact(L: "_Ledger", keep_days: int, now: float) -> None:
    """Drop whole incidents resolved more than keep_days ago once the ledger is big (atomic rewrite)."""
    p = _p("incidents.jsonl")
    try:
        if p.stat().st_size <= LEDGER_COMPACT_BYTES:
            return
    except OSError:
        return
    old = {i for i, r in L.incs.items() if r["state"] == "resolved" and (r["closed_at"] or 0) < now - keep_days * 86400}
    if not old:
        return
    keep = [e for e in _read_events() if e.get("id") not in old]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, separators=(",", ":"), sort_keys=True, default=str) + "\n" for e in keep))
    os.replace(tmp, p)


def _load_state() -> dict:
    s = core.read_json(_p("incidents-state.json"), None)
    if isinstance(s, dict) and isinstance(s.get("tasks"), dict):
        s["tasks"] = {k: _tracker_from(v) for k, v in s["tasks"].items()
                      if isinstance(v, dict) and _num(v.get("cursor")) is not None}      # hand-edited junk is dropped
        s["live"] = {k: v for k, v in (s.get("live") or {}).items() if isinstance(v, dict)}
        return s
    return {"v": 1, "tasks": {}, "live": {}, "fresh": True}


def _tracker_from(v: dict) -> dict:
    """A tracker read from the state file: defaults for missing keys, and `seen` / `fnote` forced to their types."""
    tr = {**_new_tracker(0.0), **v}
    tr["seen"] = [float(x) for x in tr["seen"] if _num(x) is not None][-8:] if isinstance(tr["seen"], list) else []
    tr["fnote"] = tr["fnote"] is True
    return tr


def _parse_ts(v: Any) -> float | None:
    if _num(v) is not None:
        return float(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v, fmt).timestamp()
            except ValueError:
                continue
    return None


def _read_audit(since: float, max_bytes: int = 16 << 20) -> list[dict]:
    """audit.jsonl rows at or after `since`, newest tail only. Rows: {t, task, action, target, bytes, outcome, cls}."""
    rows: list[dict] = []
    try:
        with open(core.LOG_DIR / "audit.jsonl", "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(size - max_bytes, 0))
            if size > max_bytes:
                f.readline()                                  # the first line is probably cut
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                t = _parse_ts(r.get("ts")) if isinstance(r, dict) else None
                if t is None or t < since:
                    continue
                o = str(r.get("outcome", ""))
                rows.append({"t": t, "task": str(r.get("task", "")), "action": str(r.get("action", "")),
                             "target": str(r.get("target", "")), "bytes": int(_num(r.get("bytes")) or 0),
                             "outcome": o, "cls": _outcome_cls(o)})
    except OSError:
        pass
    rows.sort(key=lambda r: r["t"])
    return rows


def _outcome_cls(o: str) -> str:
    if o in ("done", "sent"):
        return "done" if o == "done" else "sent"
    if o == "dry-run":
        return "would"
    for pre in ("refused", "failed", "approved", "dropped"):
        if o.startswith(pre):
            return pre
    return "other"


def _read_task_history(since: float) -> list[dict]:
    out: list[dict] = []
    try:
        with open(_p("history.jsonl"), "rb") as f:
            for ln in f:
                if b"task" not in ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("kind") == "task" and (_num(r.get("t")) or 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


# =========================================================================== the ledger (fold of events)
_TEXT_FIELDS = {"text": 220, "title": 80, "summary": 140, "resolution": 140, "note": 200}      # the limits _Ledger._apply folds them with


def _scrub_event(e: dict) -> dict:
    """The event as it is WRITTEN: free text is redacted at ingest, so incidents.jsonl itself never holds a secret. The fold
    redacts again (that also covers lines written by older code or edited by hand); redaction is idempotent."""
    out = dict(e)
    for k, n in _TEXT_FIELDS.items():
        if isinstance(out.get(k), str):
            out[k] = _line(out[k], n)
    if isinstance(out.get("entities"), list):
        out["entities"] = [_line(x, 80) for x in out["entities"][:8] if isinstance(x, str)]
    if isinstance(out.get("word"), str):
        out["word"] = _line(out["word"], 12)
    if isinstance(out.get("postmortem_md"), str):
        out["postmortem_md"] = redact(out["postmortem_md"])[:8000]
    return out


class _Ledger:
    """incidents.jsonl folded into incident dicts. `emit` applies an event AND queues it for appending."""

    def __init__(self, events: list[dict]):
        self.incs: dict[str, dict] = {}
        self.open_idx: dict[str, str] = {}          # task -> id of its open incident
        self.new: list[dict] = []
        for e in events:
            self._apply(e)

    def emit(self, e: dict) -> None:
        e = _scrub_event(e)
        self._apply(e)
        self.new.append(e)

    def open_for(self, task: str) -> dict | None:
        i = self.open_idx.get(task)
        return self.incs.get(i) if i else None

    def open_list(self) -> list[dict]:
        return [r for r in self.incs.values() if r["state"] == "open"]

    @staticmethod
    def _tl(r: dict, t: float, kind: str, text: str) -> None:
        if len(r["tl"]) < 80:
            r["tl"].append({"t": t, "kind": kind, "text": text})

    def _apply(self, e: dict) -> None:
        i, ev, ts = e.get("id"), e.get("ev"), _num(e.get("ts"))
        if not isinstance(i, str) or ts is None:
            return
        text = _line(e.get("text", ""), 220)
        if ev == "open":
            task = str(e.get("task", ""))[:64]
            if i in self.incs or task in self.open_idx:       # duplicate or crash-replayed open line: first one wins
                return
            lvl = _int(e.get("level"), 1)
            r = {"id": i, "task": task, "title": _line(e.get("title") or task, 80), "level": lvl, "peak_level": lvl,
                 "severity": _sev(e.get("severity")),
                 "word": str(e.get("word", "")), "started_at": _num(e.get("started_at")) or ts, "detected_at": ts,
                 "acknowledged_at": None, "mitigated_at": None, "resolved_at": None, "closed_at": None,
                 "summary": _line(e.get("summary", ""), 140),
                 "entities": [_line(x, 80) for x in (e.get("entities") or [])[:8] if isinstance(x, str)],
                 "parent": None, "state": "open", "tl": [], "actions": [], "postmortem_md": "", "resolution": "", "ack": None}
            self.incs[i] = r
            self.open_idx[task] = i
            if r["started_at"] < ts - 1:
                self._tl(r, r["started_at"], "first_seen", "First failing sample: " + (r["summary"] or r["title"]))
            self._tl(r, ts, "open", text or f"Confirmed {LEVEL_WORD.get(lvl, 'warn')}")
            return
        r = self.incs.get(i)
        if r is None:
            return                                            # its open line was lost: ignore the orphan
        if ev in ("escalate", "improve"):
            r["level"] = _int(e.get("level"), r["level"], 0)
            r["peak_level"] = max(r["peak_level"], r["level"])
            if SEV_RANK[_sev(e.get("severity"), r["severity"])] > SEV_RANK[r["severity"]]:
                r["severity"] = _sev(e.get("severity"))
            if e.get("summary"):
                r["summary"] = _line(e["summary"], 140)
            self._tl(r, ts, ev, text)
        elif ev == "ack" and r["acknowledged_at"] is None:
            r["acknowledged_at"] = ts
            self._tl(r, ts, "ack", text or "Alert sent")
        elif ev == "acked" and r["state"] == "open":
            fp, until = e.get("fp"), _num(e.get("until"))
            if isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{16}", fp) and until is not None:
                r["ack"] = {"fp": fp, "until": until, "at": ts, "by": _line(e.get("by"), 12), "note": _line(e.get("note", ""), 200),
                            "severity": "crit" if e.get("severity") == "crit" else "warn"}
                if r["acknowledged_at"] is None:
                    r["acknowledged_at"] = ts                      # nobody was paged first: the owner's acknowledgement is the MTTA
                self._tl(r, ts, "acked", text or "Acknowledged by the owner")
        elif ev == "unacked" and r["ack"] is not None:
            r["ack"] = None
            self._tl(r, ts, "unacked", text or "Acknowledgement ended")
        elif ev == "mitigated" and r["mitigated_at"] is None:
            r["mitigated_at"] = ts
            self._tl(r, ts, "mitigated", text or "Mitigating action recorded")
        elif ev == "group":
            r["parent"] = e.get("parent") if isinstance(e.get("parent"), str) and e.get("parent") != i else None
            self._tl(r, ts, "group", text)
        elif ev == "note":
            self._tl(r, ts, "note", text)
        elif ev == "resolve" and r["state"] == "open":
            r.update(state="resolved", closed_at=ts, resolved_at=_num(e.get("resolved_at")) or ts, level=0, ack=None,
                     resolution=_line(e.get("resolution", ""), 140),
                     actions=[a for a in (e.get("actions") or []) if isinstance(a, dict)][:ACT_PUBLIC],
                     postmortem_md=str(e.get("postmortem_md") or "")[:8000])
            self._tl(r, r["resolved_at"], "resolve", text or "Recovered")
            if self.open_idx.get(r["task"]) == i:
                del self.open_idx[r["task"]]


# =========================================================================== audit: ack, mitigation, related actions
def _touches(inc: dict, row: dict, pb: dict) -> bool:
    """Did this audit row act on what the incident is about?"""
    if row["task"] in ("notify", "gate", "acks", ""):
        return False
    if row["task"] == inc["task"] or row["task"] in pb["related_tasks"]:
        return True
    tgt = row["target"].lower()
    return any(len(e) >= 4 and e.lower() in tgt for e in inc["entities"])


def _aggregate(rows: list[dict]) -> list[dict]:
    """Collapse hundreds of per-item rows into one line per (task, action, outcome class)."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        g = groups.setdefault((r["task"], r["action"], r["cls"]),
                              {"t": r["t"], "last_t": r["t"], "task": r["task"], "action": r["action"],
                               "outcome": r["cls"], "count": 0, "bytes": 0, "_tg": []})
        g["count"] += 1
        g["bytes"] += max(r["bytes"], 0)
        g["last_t"] = max(g["last_t"], r["t"])
        if r["target"] not in g["_tg"] and len(g["_tg"]) < 3:
            g["_tg"].append(r["target"])
    out = []
    for g in groups.values():
        tg = g.pop("_tg")
        g["target"] = _line(tg[0] if g["count"] == 1 or len(tg) == 1 else f"{g['count']} items, e.g. {tg[0]}", 90)
        g["task"], g["action"] = _line(g["task"], 40), _line(g["action"], 60)
        g["t"], g["last_t"] = _r(g["t"]), _r(g["last_t"])
        out.append(g)
    rank = {"done": 0, "failed": 1, "refused": 2, "would": 3}
    out.sort(key=lambda a: (rank.get(a["outcome"], 4), a["t"]))
    return sorted(out[:ACT_PUBLIC], key=lambda a: a["t"])


def _is_page(row: dict, inc: dict) -> bool:
    """Is this audit row a delivered WARN/CRIT page about `inc`? Two target spellings exist: core.Notifier writes
    "<task>: WARN|CRIT <title>", notify.send writes "alert: <title>" (or "incident_open: <title>"). Recoveries, digests, dropped
    (budget) and failed sends are never an acknowledgement."""
    if row["task"] != "notify" or row["action"] != "send" or row["cls"] != "sent":
        return False
    head, _, rest = row["target"].partition(": ")
    if head == inc["task"]:
        return rest.split(" ", 1)[0] in ("WARN", "CRIT")
    return head in ("alert", "incident_open") and bool(inc["title"]) and inc["title"].lower() in rest.lower()


def _sync_audit(L: _Ledger, inc: dict, rows: list[dict], pb: dict, alerts: dict, until: float) -> list[dict]:
    """Emit any missing ack/mitigated event for `inc` (idempotent) and return its aggregated related actions."""
    lo, hi = inc["started_at"] - 60, until + 60
    mine = [r for r in rows if lo <= r["t"] <= hi]
    if inc["acknowledged_at"] is None:
        ack = next((r["t"] for r in mine if _is_page(r, inc)), None)
        if ack is None:
            a = (alerts.get("tasks") or {}).get(inc["task"]) or {}
            sent = _num(a.get("last_sent"))
            if a.get("alerted") and sent is not None and lo <= sent <= hi:
                ack = sent
        if ack is not None:
            L.emit({"ts": ack, "id": inc["id"], "ev": "ack", "text": "Alert sent to the owner"})
    acts = [r for r in mine if _touches(inc, r, pb)]
    if inc["mitigated_at"] is None:
        done = [r for r in acts if r["cls"] == "done"]
        if done:
            r = done[0]
            L.emit({"ts": r["t"], "id": inc["id"], "ev": "mitigated",
                    "text": _line(f"{r['task']}: {r['action']} {r['target']} done", 160)})
    return _aggregate(acts)


# =========================================================================== views, timeline, cause hints
_ACT_VERB = {"done": "did", "would": "would have run", "refused": "was refused for", "failed": "failed on",
             "approved": "approved", "other": "ran"}


def _timeline(inc: dict, actions: list[dict], n: int = TL_PUBLIC) -> list[dict]:
    tl = [{"t": _r(x["t"]), "kind": x["kind"], "text": x["text"]} for x in inc["tl"]]
    for a in actions:
        more = f" x{a['count']}" if a["count"] > 1 else ""
        freed = f", {core.human(a['bytes'])}" if a["outcome"] == "done" and a["bytes"] > 0 else ""
        tl.append({"t": a["t"], "kind": "would" if a["outcome"] == "would" else "action",
                   "text": _line(f"{a['task']} {_ACT_VERB.get(a['outcome'], 'ran')} {a['action']}{more}{freed}", 160)})
    tl.sort(key=lambda x: x["t"])
    if len(tl) > n:
        cut = len(tl) - (n - 3)
        tl = tl[:2] + [{"t": tl[2]["t"], "kind": "note", "text": f"... {cut - 2} entries omitted"}] + tl[cut:]
    return tl


def _cause_hint(inc: dict, L: _Ledger, cfg: dict, summary: str) -> str:
    pb = playbook_for(inc["task"], cfg)
    hints: list[str] = []
    for h in pb["hint"]:
        try:
            if re.search(h["match"], summary) and h["text"] not in hints:
                hints.append(h["text"])
        except re.error:
            continue
    for c in cfg["correlation"]:
        if not isinstance(c, dict) or c.get("effect") != inc["task"]:
            continue
        lag = _flt(c.get("lag_s"), CAUSE_LAG_S)           # how long before the effect began the cause may already have ended
        for o in L.incs.values():
            if (o["task"] == c.get("cause") and o["id"] != inc["id"] and o["started_at"] <= inc["started_at"] + 60
                    and (o["state"] == "open" or (o["resolved_at"] or 0) >= inc["started_at"] - lag)):
                hints.append(f"{c.get('text', '')} (see {o['id']})")
                break
    prior = sum(1 for o in L.incs.values() if o["task"] == inc["task"] and o["id"] != inc["id"]
                and inc["started_at"] - 7 * 86400 <= o["started_at"] < inc["started_at"])
    if prior:
        hints.append(f"Recurring: {prior} earlier incident(s) for this check in 7 days; look for a root cause, not another cleanup.")
    if inc["word"] == "error":
        hints.append("The check itself errored; the host may be fine. Run it by hand to see why.")
    return _line(" ".join(hints[:3]), 420)


def _members(L: _Ledger, inc: dict) -> list[dict]:
    root = inc["parent"] or inc["id"]
    return [o for o in L.incs.values() if (o["parent"] or o["id"]) == root and o["id"] != inc["id"]]


def _view(inc: dict, L: _Ledger, cfg: dict, st: dict, now: float, actions: list[dict], last_seen: float | None) -> dict:
    pb = playbook_for(inc["task"], cfg)
    live = st["live"].get(inc["id"]) if inc["state"] == "open" else None
    summary = _line((live or {}).get("summary") or inc["summary"], 140)      # the live overlay comes from the state file: redacted again
    open_ = inc["state"] == "open"
    end = now if open_ else (inc["resolved_at"] or now)
    rel = _members(L, inc)
    v = {"id": inc["id"], "task": inc["task"], "title": inc["title"], "severity": inc["severity"],
         "level": inc["level"], "status": "open" if open_ else "resolved", "since": _r(inc["started_at"]),
         "duration_s": int(max(end - inc["started_at"], 0)), "detected_at": _r(inc["detected_at"]),
         "acknowledged_at": _r(inc["acknowledged_at"]), "mitigated_at": _r(inc["mitigated_at"]),
         "last_checked": _r(last_seen), "summary": summary, "cause_hint": _cause_hint(inc, L, cfg, summary),
         "parent": inc["parent"], "children": sorted(o["id"] for o in L.incs.values() if o["parent"] == inc["id"]),
         "related": sorted({o["task"] for o in rel if o["state"] == "open"}), "service_class": pb["class"],
         "timeline": _timeline(inc, actions), "related_actions": actions[:ACT_PUBLIC],
         "mttd_s": int(max(inc["detected_at"] - inc["started_at"], 0))}
    if isinstance((live or {}).get("fp"), str) and re.fullmatch(r"[0-9a-f]{16}", live["fp"]):
        v["fp"] = live["fp"]                      # the issue id (acks.py): what the Acknowledge buttons post
    elif open_ and inc.get("ack"):
        v["fp"] = inc["ack"]["fp"]                # recovered but still acknowledged: the id stays for the Un-acknowledge button only
    if open_ and inc.get("ack"):
        v["status"] = "acknowledged"              # still open and listed, but the owner knows: never red, never paged
        v["ack"] = {k: inc["ack"][k] for k in ("fp", "until", "at", "by", "note", "severity")}
    if open_:
        v["playbook"] = _public_pb(pb)            # operator guidance is for what is open; ~3 KB each, so not repeated in `recent`
    if not open_:
        v.update(resolved_at=_r(inc["resolved_at"]), mttr_s=int(max(inc["resolved_at"] - inc["started_at"], 0)),
                 resolution=inc["resolution"],
                 postmortem_md=inc["postmortem_md"][:PM_MAX] + ("\n... (truncated)" if len(inc["postmortem_md"]) > PM_MAX else ""))
    return v


def _mean(xs: list[float]) -> int | None:
    return int(sum(xs) / len(xs)) if xs else None


def _stats(L: _Ledger, now: float) -> dict:
    lo = now - 30 * 86400
    month = [r for r in L.incs.values() if r["started_at"] >= lo]
    res = [r for r in L.incs.values() if r["state"] == "resolved" and (r["resolved_at"] or 0) >= lo]
    by = {s: sum(1 for r in month if r["severity"] == s) for s in ("sev1", "sev2", "sev3")}
    out = {"mttd_s_30d": _mean([r["detected_at"] - r["started_at"] for r in month]),
           "mttr_s_30d": _mean([r["resolved_at"] - r["started_at"] for r in res]),
           "mtta_s_30d": _mean([r["acknowledged_at"] - r["started_at"] for r in month if r["acknowledged_at"]]),
           "incidents_30d": len(month), "resolved_30d": len(res), "open_count": len(L.open_list()), "by_severity_30d": by}
    n_ack = sum(1 for r in L.open_list() if r["ack"] is not None)
    if n_ack:
        out["acknowledged_count"] = n_ack                  # only when there is one: the shape is unchanged for an install without acknowledgements
    return out


def _build_export(L: _Ledger, st: dict, cfg: dict, rows: list[dict], alerts: dict, now: float) -> dict:
    """Public incidents.json shape. Read-only on the ledger (ack/mitigation are only DISPLAYED here when found late)."""
    last_seen = {t: s.get("cursor") for t, s in st["tasks"].items()}
    opens, recent = [], []
    for inc in sorted(L.open_list(), key=lambda r: (-SEV_RANK[r["severity"]], r["started_at"], r["id"])):
        pb = playbook_for(inc["task"], cfg)
        lo, hi = inc["started_at"] - 60, now + 60
        acts = _aggregate([r for r in rows if lo <= r["t"] <= hi and _touches(_with_live(inc, st), r, pb)])
        opens.append(_view(_with_live(inc, st), L, cfg, st, now, acts, last_seen.get(inc["task"])))
    res = sorted((r for r in L.incs.values() if r["state"] == "resolved"), key=lambda r: -(r["closed_at"] or 0))
    for inc in res[:_int(cfg["incidents"].get("recent_max"), 50)]:
        recent.append(_view(inc, L, cfg, st, now, inc["actions"], last_seen.get(inc["task"])))
    out = {"generated_at": _r(now), "open": opens, "recent": recent, "stats": _stats(L, now)}
    i = len(recent) - 1                                                  # keep the file < 200 KB: shed oldest detail first
    while len(json.dumps(out, separators=(",", ":"))) > PUBLIC_MAX_BYTES and i >= 0:
        recent[i]["postmortem_md"], recent[i]["timeline"] = recent[i].get("postmortem_md", "")[:400], recent[i]["timeline"][:6]
        i -= 1
    while len(json.dumps(out, separators=(",", ":"))) > PUBLIC_MAX_BYTES and out["recent"]:
        out["recent"].pop()
    return out


def _with_live(inc: dict, st: dict) -> dict:
    """The open incident with the entities from the live overlay merged in (used for audit matching)."""
    live = st["live"].get(inc["id"]) or {}
    ents = list(dict.fromkeys(inc["entities"] + [e for e in live.get("entities", []) if isinstance(e, str)]))[:8]
    return {**inc, "entities": ents}


# =========================================================================== postmortem stub
def postmortem_md(inc: dict, actions: list[dict], cfg: dict | None = None, peers: list[dict] | None = None) -> str:
    """Markdown stub for a resolved sev1/sev2 incident, from recorded facts only. Redacted, ASCII-ish, no HTML."""
    pb = playbook_for(inc["task"], cfg)
    mttd = inc["detected_at"] - inc["started_at"]
    ttr = (inc["resolved_at"] or inc["closed_at"] or inc["detected_at"]) - inc["started_at"]
    ack = inc["acknowledged_at"]
    done = [a for a in actions if a["outcome"] == "done"]
    would = [a for a in actions if a["outcome"] == "would"]
    L = [f"# Postmortem: {inc['title']} ({inc['id']})", "",
         f"- Severity: {inc['severity']} (peak level {LEVEL_WORD.get(inc['peak_level'], 'warn')})",
         f"- Service class: {pb['class']}  |  Check: `{inc['task']}`",
         f"- Window: {_when(inc['started_at'])} to {_when(inc['resolved_at'])} ({_dur(ttr)})",
         f"- MTTD {_dur(mttd)}  |  time to page {_dur(ack - inc['started_at']) if ack else 'no page recorded'}  |  MTTR {_dur(ttr)}",
         "", "## Summary", "",
         (f"{inc['title']} was confirmed {LEVEL_WORD.get(inc['peak_level'], 'warn')} at {_when(inc['detected_at'])}. "
          f"What the check said: {inc['summary'] or 'n/a'}. "
          + (f"Recovery: {inc['resolution']}." if inc["resolution"] else "")).strip(),
         "", "## Impact", "", pb["impact"] + " (Fill in what users actually noticed.)"]
    if peers:
        L.append("Related incidents in the same group: " + ", ".join(f"{p['id']} ({p['task']})" for p in peers) + ".")
    L += ["", "## Timeline", ""]
    tl = _timeline(inc, actions, 13)
    if not any(x["kind"] == "resolve" for x in tl):
        tl.append({"t": inc["resolved_at"], "kind": "resolve", "text": "Recovered: " + (inc["resolution"] or "healthy again")})
    for x in tl:
        L.append(f"- {time.strftime('%H:%M', time.localtime(x['t']))} [{x['kind']}] {x['text']}")
    L += ["", "## Detection gap", "",
          f"- First failing sample to confirmation: {_dur(mttd)} (the check runs every 15 minutes and is confirmed over "
          "consecutive runs; the real onset may be up to one interval earlier).",
          (f"- First failing sample to page: {_dur(ack - inc['started_at'])}." if ack else
           "- No page was recorded for this incident: check the alert budget, the alert path and whether the task runs with alert = false."),
          "", "## What went well", ""]
    well = []
    if mttd <= 1800:
        well.append(f"Detected within {_dur(mttd)} by the routine check.")
    if ack:
        well.append("The owner was paged by the existing alert path.")
    if done:
        well.append("An automatic or recorded action took part in the fix: " + "; ".join(f"{a['task']} {a['action']}" for a in done[:3]) + ".")
    if not done and not inc["mitigated_at"]:
        well.append("It recovered without any recorded intervention.")
    L += [f"- {w}" for w in well or ["Nothing notable recorded."]]
    L += ["", "## What went badly", ""]
    bad = []
    if not ack:
        bad.append("No page was recorded.")
    if ttr > 4 * 3600:
        bad.append(f"It took {_dur(ttr)} to recover.")
    if inc["severity"] == "sev1":
        bad.append("It escalated to sev1: several related checks failed together.")
    if would and not done:
        bad.append("Cleanup tasks only reported what they would have done (report mode); the fix was manual or none.")
    if not done and not would and ttr > 3600:
        bad.append("No mitigating action is on record: write the manual fix into the maintenance journal.")
    L += [f"- {b}" for b in bad or ["Nothing notable recorded."]]
    L += ["", "## Follow-ups", ""]
    fu = list(pb["followups"])
    if not ack:
        fu.append("Find out why no page was sent and fix it.")
    fu.append(f"Review the `{inc['task']}` playbook: were the first checks and fixes right? Update etc/playbooks.toml.")
    L += [f"- [ ] {f}" for f in fu[:6]]
    return redact("\n".join(L))[:8000]


# =========================================================================== observation tracker (Notifier-equivalent debounce)
def _new_tracker(cursor: float) -> dict:
    return {"cursor": cursor, "pending": 0, "streak": 0, "since": None, "bad_since": None, "ok_since": None,
            "seen": [], "fnote": False}                # seen: the last consumed observation times (never counted twice, even after a clock step back)


def _advance(tr: dict, level: int, lvl: int | None, t: float, confirm: int, resolve_n: int) -> tuple[int, float] | None:
    """One observation. `level` = confirmed level (that of the open incident, else 0). Returns (new_level, first_t) when a
    different level has now been seen enough times in a row; None otherwise. lvl None = neutral (skipped probe)."""
    if lvl is None:
        return None
    if lvl > 0:
        tr["ok_since"] = None
        tr["bad_since"] = t if tr["bad_since"] is None else tr["bad_since"]
    else:
        tr["bad_since"] = None
        tr["ok_since"] = t if tr["ok_since"] is None else tr["ok_since"]
    if lvl == level:
        tr["pending"], tr["streak"], tr["since"] = lvl, 0, None
        return None
    if tr["pending"] == lvl and tr["streak"] > 0:
        tr["streak"] += 1
    else:
        tr["pending"], tr["streak"], tr["since"] = lvl, 1, t
    if tr["streak"] >= (confirm if lvl > 0 else resolve_n):
        first = tr["since"] if tr["since"] is not None else t
        tr["pending"], tr["streak"], tr["since"] = lvl, 0, None
        return lvl, first
    return None


def _alertable(rec: dict, name: str, info: set[str]) -> bool:
    """False for a run with alert=false. An explicit flag wins; otherwise the `informational` task list decides."""
    return rec["alert"] if isinstance(rec.get("alert"), bool) else name not in info


def _obs_level(status: Any, alert: bool) -> int | None:
    if status == "skipped":
        return None
    return core.LEVELS.get(status, 0) if alert else 0


_HEALTHY_WORDS = {"ok", "info", "up", "running", "waiting", "absent", "skipped", "paused", "idle"}


def _entities(entry: dict) -> list[str]:
    """Names of the FAILING things in a status entry's items (mounts, units, containers, devices, paths). Rows describe their
    health in different keys (level for most checks, state/sev for probes and os_jobs); a healthy row is never an entity."""
    out: list[str] = []
    for row in (entry.get("items") or [])[:12]:
        if not isinstance(row, dict):
            continue
        if any(str(row.get(k, "")).lower() in _HEALTHY_WORDS for k in ("level", "state", "sev")):
            continue
        for k in ("name", "mount", "path", "dev"):
            v = row.get(k)
            if isinstance(v, str) and v and _line(v, 80) not in out:
                out.append(_line(v, 80))
    return out[:8]


def _ack_of(entry: dict, now: float) -> dict | None:
    """The acknowledgement a status entry carries, validated (acks.apply_to_status wrote it), else None. Only a FAILING entry can be
    acknowledged; an ack that is already past its `until` is none."""
    a = entry.get("acked")
    if not isinstance(a, dict) or entry.get("status") not in ("warn", "crit", "error"):
        return None
    fp, until = a.get("fp"), _num(a.get("until"))
    if not (isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{16}", fp)) or until is None or until <= now:
        return None
    return {"fp": fp, "until": until, "by": _line(a.get("by"), 12), "note": _line(a.get("note", ""), 200),
            "severity": "crit" if a.get("severity") == "crit" else "warn", "since": _num(a.get("since"))}


def _title(task: str, entry: dict | None, cfg: dict | None = None) -> str:
    """Status entry title, else the registry title, else the playbook title, else the task name."""
    if entry and isinstance(entry.get("title"), str) and entry["title"]:
        return entry["title"]
    t = core.REGISTRY.get(task)
    if t and t.title:
        return t.title
    if cfg and task in cfg["playbook"] and isinstance(cfg["playbook"][task].get("title"), str):
        return cfg["playbook"][task]["title"] or task
    return task


# =========================================================================== the engine
def _regroup(L: _Ledger, cfg: dict, ts: float) -> None:
    """Correlation, then the sev1 rule. Open incidents whose failures STARTED within `group_window_s` of each other (chained) share
    ONE flat group; the earliest is the parent. Checks are only sampled every SLOT_S (15 min), so a first failing sample can be
    up to one interval later than the real onset: two faults 3 minutes apart in reality are seen 15 minutes apart whenever a run
    falls between them. The window therefore compares sampled times with `group_window_s + SLOT_S`.
    Idempotent: it emits only what is missing, so it runs after every open and again at the end of each tick, which re-derives a
    group line that a crash lost."""
    gap = _flt(cfg["incidents"].get("group_window_s"), 600.0)
    win = max(gap, 0.0) + SLOT_S
    for inc in sorted(L.open_list(), key=lambda r: (r["started_at"], r["id"])):
        cands = [o for o in L.open_list() if o["id"] != inc["id"] and abs(o["started_at"] - inc["started_at"]) <= win]
        if not cands:
            continue
        roots = {(o["parent"] or o["id"]) for o in cands} | {inc["parent"] or inc["id"]}
        root = min((L.incs[r] for r in roots if r in L.incs), key=lambda r: (r["started_at"], r["id"]))
        for o in L.open_list():
            if (o["parent"] or o["id"]) in roots and o["id"] != root["id"] and o["parent"] != root["id"]:
                L.emit({"ts": ts, "id": o["id"], "ev": "group", "parent": root["id"],
                        "text": f"Grouped under {root['task']} ({root['id']}): started failing within {int(gap // 60)} min "
                                f"(plus one 15-minute check interval) of it"})
    _sev1(L, cfg, ts)


def _sev1(L: _Ledger, cfg: dict, ts: float) -> None:
    need = _int(cfg["incidents"].get("sev1_related"), 2)
    for o in L.open_list():
        if o["level"] >= 2 and o["severity"] != "sev1" and o["ack"] is None:        # an acknowledged incident is not escalated ...
            rel = [m for m in _members(L, o) if m["state"] == "open" and m["ack"] is None]     # ... and does not escalate its neighbours
            if len(rel) >= need:
                L.emit({"ts": ts, "id": o["id"], "ev": "escalate", "level": o["level"], "severity": "sev1",
                        "text": f"Escalated to sev1: crit with {len(rel)} related open incidents ({', '.join(sorted(m['task'] for m in rel)[:4])})"})


def _resolve(L: _Ledger, inc: dict, cfg: dict, rows: list[dict], alerts: dict, resolved_at: float, ts: float,
             why: str) -> None:
    pb = playbook_for(inc["task"], cfg)
    actions = _sync_audit(L, inc, rows, pb, alerts, ts + 900)        # grace: a cleanup can finish after the first healthy run
    inc = L.incs[inc["id"]]                                          # ack/mitigated events may have updated it
    ev = {"ts": ts, "id": inc["id"], "ev": "resolve", "resolved_at": resolved_at, "resolution": why, "actions": actions,
          "text": f"Recovered: {why}"}
    if inc["severity"] in ("sev1", "sev2"):
        prev = {**inc, "state": "resolved", "resolved_at": resolved_at, "closed_at": ts, "resolution": _line(why, 140),
                "actions": actions}
        ev["postmortem_md"] = postmortem_md(prev, actions, cfg, [m for m in _members(L, inc)])
    L.emit(ev)


def update(status: dict | None, history: list[dict] | None = None, now: float | None = None,
           audit_rows: list[dict] | None = None) -> dict:
    """One tick. Idempotent for the same inputs; never raises (a failure returns {"ok": False, "error": ...}). The result's
    opened / escalated / improved / resolved hold only LIVE transitions; replayed ones are under `backfilled` (module docstring)."""
    now = time.time() if now is None else float(now)
    try:
        with _Lock():
            return _update(status, history, now, audit_rows)
    except Exception as exc:  # noqa: BLE001 - incident tracking must never break the runner
        return {"ok": False, "error": _line(f"{type(exc).__name__}: {exc}", 120)}


def _emit_open(L: _Ledger, cfg: dict, task: str, ent: dict | None, ts: float, level: int, started: float, word: str,
               summ: str, why: str) -> str:
    """Append an `open` event (+ grouping and the sev1 rule) and return the new incident id."""
    iid = _new_id(L, ts)
    L.emit({"ts": ts, "id": iid, "ev": "open", "task": task, "title": _title(task, ent, cfg), "level": level,
            "severity": "sev2" if level >= 2 else "sev3", "word": word, "started_at": min(started, ts), "summary": summ,
            "entities": _entities(ent) if ent else [], "text": f"Confirmed {LEVEL_WORD[level]} {why}: {summ}"})
    _regroup(L, cfg, ts)
    return iid


def _watermarks(events: list[dict], L: _Ledger) -> dict[str, float]:
    """task -> time of the newest ledger event that was emitted FROM an observation (open/escalate/improve/resolve). Everything
    at or before it is already reflected in the ledger, so a replay after a crash (ledger written, state not) must skip it or the
    same episode would be opened twice."""
    wm: dict[str, float] = {}
    for e in events:
        r = L.incs.get(e.get("id")) if e.get("ev") in ("open", "escalate", "improve", "resolve") else None
        ts = _num(e.get("ts"))
        if r is not None and ts is not None:
            wm[r["task"]] = max(wm.get(r["task"], 0.0), ts)
    return wm


def _update(status: dict | None, history: list[dict] | None, now: float, audit_rows: list[dict] | None) -> dict:
    cfg = load_config()
    maint = _toml(core.CONF_DIR / "maint.toml")
    events = _read_events()
    L = _Ledger(events)
    st = _load_state()
    # Wall-clock cursors must never sit in the future: after an RTC error and an NTP step back they would silently drop every
    # later observation until the clock caught up (and an open incident would never see its healthy samples). So a cursor, a
    # watermark or a floor ahead of the clock is moved behind it (CLAMP_BACK_S), and an observation stamped more than FUTURE_S
    # ahead of the clock is ignored. Moving it BEHIND `now`, not onto it, keeps the current run's own observation (stamped just
    # before `now`); `seen` keeps anything already consumed from being counted again.
    limit = now + FUTURE_S

    def unfuture(t: float) -> float:
        return t if t <= limit else now - CLAMP_BACK_S

    clamped: list[str] = []
    for name, tr in st["tasks"].items():
        if tr["cursor"] > limit:
            tr["cursor"] = unfuture(tr["cursor"])
            for k in ("since", "bad_since", "ok_since"):
                if _num(tr.get(k)) is not None and tr[k] > now:
                    tr[k] = now
            clamped.append(name)
    future: set[tuple[str, float]] = set()                  # (task, t) of observations ignored for being stamped in the future
    # After the state file was lost, only observations newer than the ledger's newest event are replayed, so history that was
    # already turned into incidents is not turned into them again.
    floor = unfuture(max((_num(e.get("ts")) or 0.0 for e in events), default=0.0)) if st.pop("fresh", False) else 0.0
    wm = {k: unfuture(v) for k, v in _watermarks(events, L).items()}
    for name, w in wm.items():                       # state older than the ledger (crash between the two writes): catch it up
        tr = st["tasks"].get(name)
        if tr is not None and tr["cursor"] < w:
            st["tasks"][name] = _new_tracker(w)
    entries = {k: v for k, v in (status.get("tasks") or {}).items() if isinstance(v, dict)} \
        if isinstance(status, dict) and isinstance(status.get("tasks"), dict) else {}
    info = set(cfg["slo_defaults"].get("informational", []))
    alerts = core.read_json(_p("alerts.json"), {}) or {}
    if history is None:
        history = _read_task_history(now - _int(cfg["slo_defaults"].get("window_days"), 30) * 86400 - 86400)

    # ---- observations newer than each task's cursor, in time order across all tasks. A history record carries no summary
    # (and, until cli adds it, maybe no alert flag); the live status entry for the same run does, so it wins on a tie.
    def cursor(name: str) -> float:
        tr = st["tasks"].get(name)
        return tr["cursor"] if tr else wm.get(name, floor)

    def fresh_obs(name: str, t: float | None) -> bool:
        """Is (task, t) an observation to consume now: newer than the cursor, not stamped in the future, not consumed before."""
        if t is None:
            return False
        if t > limit:
            future.add((name, t))
            return False
        tr = st["tasks"].get(name)
        return t > cursor(name) and not (tr is not None and t in tr["seen"])

    obs: dict[tuple[float, str], tuple[str, bool, str | None]] = {}
    for r in history:
        if not isinstance(r, dict) or r.get("kind") != "task" or not isinstance(r.get("task"), str):
            continue
        t, name = _num(r.get("t")), r["task"]
        if fresh_obs(name, t):
            obs[(t, name)] = (str(r.get("status", "ok")), _alertable(r, name, info), None)
    for name, ent in entries.items():
        t = _num(ent.get("last_run"))
        if fresh_obs(name, t):
            obs[(t, name)] = (str(ent.get("status", "ok")), _alertable(ent, name, info), _line(ent.get("summary", ""), 140))

    lo = min([t for t, _ in obs] + [o["started_at"] for o in L.open_list()] + [now - 2 * 86400]) - 3600
    rows = audit_rows if audit_rows is not None else _read_audit(lo)
    out: dict[str, Any] = {"ok": True, "opened": [], "escalated": [], "improved": [], "resolved": [], "acked": [], "held": [],
                           "backfilled": {"opened": [], "escalated": [], "improved": [], "resolved": []}}

    def moved(kind: str, iid: str, ts: float) -> None:
        """Record a transition. opened/escalated/improved/resolved hold only LIVE ones (the event is at most LIVE_S old); the glue
        may notify for those. Older ones (first install replays a month of history, ticks were missed) are listed under
        `backfilled`: they are history, and notifying for them would page about yesterday. So is every later transition of an
        incident whose OPENING was backfilled in this same call: nobody was told it was open, a "resolved" would be a page about
        something the owner never heard of."""
        live = ts >= now - LIVE_S and not (kind != "opened" and iid in out["backfilled"]["opened"])
        (out if live else out["backfilled"])[kind].append(iid)

    # ---- reconcile ledger and trackers FIRST (before this tick's observations are consumed). The state file is written AFTER the
    # (fsynced) ledger, so a tracker can only be ahead of the ledger when a ledger line was lost or torn. Replay cannot repair
    # that (the observation is already consumed); the tracker can: with no streak pending, its `pending` level IS the confirmed one.
    for name, tr in list(st["tasks"].items()):
        if tr["streak"] != 0:
            continue                                                       # still counting toward a confirmation
        cur, ent = L.open_for(name), entries.get(name)
        match = ent is not None and _obs_level(ent.get("status"), _alertable(ent, name, info)) == tr["pending"]
        if cur is not None and tr["pending"] == 0 and tr["ok_since"] is not None:
            _resolve(L, cur, cfg, rows, alerts, tr["ok_since"], tr["cursor"], "healthy again (recovered from an interrupted write)")
            moved("resolved", cur["id"], tr["cursor"])
        elif cur is not None and tr["pending"] > 0 and tr["pending"] != cur["level"] and tr["bad_since"] is not None:
            new = tr["pending"]
            summ = _line((ent if match else {}).get("summary") or f"{cur['title']} reported {LEVEL_WORD[new]}", 140)
            ev = {"ts": tr["cursor"], "id": cur["id"], "ev": "escalate" if new > cur["level"] else "improve", "level": new,
                  "summary": summ, "text": f"{'Escalated to' if new > cur['level'] else 'Improved to'} {LEVEL_WORD[new]} "
                                           f"(recovered from an interrupted write): {summ}"}
            if new > cur["level"]:
                ev["severity"] = "sev2"                                    # an improvement never raises the severity
            L.emit(ev)
            _regroup(L, cfg, tr["cursor"])
            moved("escalated" if new > cur["level"] else "improved", cur["id"], tr["cursor"])
        elif cur is None and tr["pending"] > 0 and tr["bad_since"] is not None and ent is not None:
            summ = _line(ent.get("summary", ""), 140) if match else f"{_title(name, ent, cfg)} reported {LEVEL_WORD[tr['pending']]}"
            moved("opened", _emit_open(L, cfg, name, ent if match else None, tr["cursor"], tr["pending"], tr["bad_since"],
                                       str(ent.get("status")) if match else LEVEL_WORD[tr["pending"]], summ,
                                       "(recovered from an interrupted write)"), tr["cursor"])

    for (t, task), (word, alert, summ) in sorted(obs.items()):
        tr = st["tasks"].setdefault(task, _new_tracker(wm.get(task, floor)))
        cur = L.open_for(task)
        level = cur["level"] if cur else 0
        lvl = _obs_level(word, alert)
        confirm, resolve_n = _runs(cfg, maint, task)
        tr["cursor"] = t
        tr["seen"] = (tr["seen"] + [t])[-8:]
        res = _advance(tr, level, lvl, t, confirm, resolve_n)
        if res is None:
            continue
        new, first = res
        ent = entries.get(task) if summ is not None else None
        title = _title(task, entries.get(task), cfg)
        summ = summ or f"{title} reported {word}"
        if level == 0 and new > 0:
            moved("opened", _emit_open(L, cfg, task, ent, t, new, first, word, summ, f"after {confirm} consecutive runs"), t)
        elif level > 0 and new == 0:
            _resolve(L, cur, cfg, rows, alerts, first, t, "healthy again for " + str(resolve_n) + " consecutive runs")
            moved("resolved", cur["id"], t)
        elif new > level:
            L.emit({"ts": t, "id": cur["id"], "ev": "escalate", "level": new, "severity": "sev2", "summary": summ,
                    "text": f"Escalated to {LEVEL_WORD[new]} (sev2): {summ}"})
            _regroup(L, cfg, t)
            moved("escalated", cur["id"], t)
        else:
            L.emit({"ts": t, "id": cur["id"], "ev": "improve", "level": new, "summary": summ,
                    "text": f"Improved to {LEVEL_WORD[new]}: {summ}"})
            moved("improved", cur["id"], t)

    orphan_s = _flt(cfg["incidents"].get("orphan_close_h"), 6) * 3600
    for inc in L.open_list():                                              # a check that vanished must not keep an incident open
        tr = st["tasks"].get(inc["task"])
        if entries and inc["task"] not in entries and now - (tr["cursor"] if tr else 0) > orphan_s:
            _resolve(L, inc, cfg, rows, alerts, now, now, "check no longer reported (removed or disabled)")
            moved("resolved", inc["id"], now)
            st["tasks"][inc["task"]] = _new_tracker(tr["cursor"] if tr else now)      # nothing left to reconcile for it
    for name, a in (alerts.get("tasks") or {}).items():                    # the pager is the authority on "confirmed" (it saw every run)
        ent, tr = entries.get(name), st["tasks"].get(name)
        if not (isinstance(a, dict) and _int(a.get("level"), 0, 0) > 0 and ent and L.open_for(name) is None):
            continue
        lv = _obs_level(ent.get("status"), _alertable(ent, name, info))
        if not lv:
            continue
        t0 = min(_num(ent.get("last_run")) or now, now)                    # never stamp an event in the future
        # Normally we confirmed on the same run and this loop finds the incident open. It only fires when we are behind the
        # pager (history trimmed before install, a run we never saw): do not make the owner wait for our own debounce too.
        started = tr["since"] if tr is not None and tr["since"] is not None and tr["pending"] == lv else t0
        moved("opened", _emit_open(L, cfg, name, ent, t0, lv, started, str(ent.get("status")), _line(ent.get("summary", ""), 140),
                                   "by the pager"), t0)
        tr = st["tasks"].setdefault(name, _new_tracker(t0))                # keep the tracker in step with the incident
        tr.update(cursor=max(tr["cursor"], t0), pending=lv, streak=0, since=None, bad_since=min(started, t0), ok_since=None)
    _regroup(L, cfg, now)                                                # reconcile: also re-derives a group line a crash lost

    # ---- clock trouble is said out loud: on the open incident's timeline (once per episode), in `incidents list`, in the result
    skewed: dict[str, int] = {}
    for name, _t in future:
        skewed[name] = skewed.get(name, 0) + 1
    for name, tr in st["tasks"].items():
        cur = L.open_for(name)
        if name not in skewed:
            tr["fnote"] = False
        elif not tr["fnote"] and cur is not None:
            L.emit({"ts": now, "id": cur["id"], "ev": "note",
                    "text": f"Ignored {skewed[name]} sample(s) stamped more than {FUTURE_S // 60} min ahead of the clock (clock jump?); "
                            "their timestamps are not trusted until the clock agrees"})
            tr["fnote"] = True
    if skewed or clamped:
        st["clock"] = {"t": now, "future": skewed, "clamped": sorted(clamped)}
    out.update(future=skewed, clamped=sorted(clamped))

    # ---- the owner's acknowledgements: the incident stays open, its state changes (see "Acknowledged issues" in the docstring)
    for inc in L.open_list():
        ent = entries.get(inc["task"])
        if ent is None:
            continue                                                       # no news about this check: leave the state as it is
        a, was = _ack_of(ent, now), inc["ack"]
        if a and (was is None or (was["fp"], was["until"]) != (a["fp"], a["until"])):
            ts = max(min(a["since"] if a["since"] is not None else now, now), inc["detected_at"])
            L.emit({"ts": ts, "id": inc["id"], "ev": "acked", "fp": a["fp"], "until": a["until"], "by": a["by"], "note": a["note"],
                    "severity": a["severity"], "text": f"Acknowledged by the owner ({a['by'] or 'unknown'}) until {_when(a['until'])}: "
                                                       "alerts for this exact issue stop; it stays open and listed"})
            out["acked"].append(inc["id"])
        elif a is None and was is not None and ent.get("status") in ("warn", "crit", "error"):
            worse = core.LEVELS.get(ent.get("status"), 0) > (2 if was["severity"] == "crit" else 1)
            L.emit({"ts": now, "id": inc["id"], "ev": "unacked",
                    "text": "Acknowledgement ended: it got worse than what was acknowledged, so it alerts again" if worse
                    else "Acknowledgement ended (removed, expired, or the error changed): it alerts again"})
    held_ids = {i["id"] for i in L.open_list() if i["ack"] is not None}
    for kind in ("opened", "escalated"):                                   # glue pages from these lists: never for an acknowledged incident
        out["held"] += [i for i in out[kind] if i in held_ids]
        out[kind] = [i for i in out[kind] if i not in held_ids]

    # ---- live overlay, acknowledgement and mitigation for what is open now
    live: dict[str, dict] = {}
    for inc in L.open_list():
        ent = entries.get(inc["task"])
        prev = st["live"].get(inc["id"], {})
        if ent and _obs_level(ent.get("status"), _alertable(ent, inc["task"], info)) not in (None, 0):
            live[inc["id"]] = {"summary": _line(ent.get("summary", ""), 140), "entities": _entities(ent)}
            if isinstance(ent.get("fp"), str) and re.fullmatch(r"[0-9a-f]{16}", ent["fp"]):
                live[inc["id"]]["fp"] = ent["fp"]
        elif prev:
            # Keep the last summary/entities while the task recovers, but not its `fp`: that id names an issue the host no longer has
            # (acks._current_issues reads the same status.json), so a web Acknowledge posting it would only be refused as unknown_issue.
            live[inc["id"]] = {k: v for k, v in prev.items() if k != "fp"}
        _sync_audit(L, L.incs[inc["id"]], rows, playbook_for(inc["task"], cfg), alerts, now)
    st["live"] = live

    # ---- persist: ledger first (fsync), then state, then the public snapshots
    _append_events(L.new)
    st["v"], st["updated"] = 1, now
    core.write_json_atomic(_p("incidents-state.json"), st, 0o644)
    pub = _build_export(L, st, cfg, rows, alerts, now)
    _write_public_file(_p("incidents.json"), pub)
    _write_public_file(_p("slo.json"), export_slo(history, now, cfg))
    _compact(L, _int(cfg["incidents"].get("keep_days"), 90), now)
    out.update(open=len(pub["open"]), events=len(L.new))
    return out


def _new_id(L: _Ledger, ts: float) -> str:
    pre = "INC-" + time.strftime("%Y%m%d", time.localtime(ts)) + "-"
    n = max([int(i[len(pre):]) for i in L.incs if i.startswith(pre) and i[len(pre):].isdigit()] or [0]) + 1
    return f"{pre}{n:03d}"


# =========================================================================== public exports
def export_incidents(now: float | None = None, audit_rows: list[dict] | None = None) -> dict:
    """Public incidents.json content, read-only (does not touch any file)."""
    now = time.time() if now is None else float(now)
    cfg = load_config()
    L = _Ledger(_read_events())
    st = _load_state()
    rows = audit_rows if audit_rows is not None else _read_audit(min([o["started_at"] for o in L.open_list()] + [now]) - 3600)
    return _build_export(L, st, cfg, rows, core.read_json(_p("alerts.json"), {}) or {}, now)


def _slots(history: list[dict], since: float, info: set[str], until: float) -> dict[str, dict[int, bool]]:
    """task -> {15-minute slot: any bad sample in it}. A sample stamped after `until` (the clock plus FUTURE_S) is not trusted."""
    by: dict[str, dict[int, bool]] = {}
    for r in history:
        t = _num(r.get("t")) if isinstance(r, dict) else None
        if t is None or t < since or t > until or r.get("kind") != "task" or not isinstance(r.get("task"), str):
            continue
        name = r["task"]
        if r.get("status") == "skipped":
            continue                                     # the check did not run: no information, not a healthy sample
        bad = r.get("status") in ("warn", "crit", "error")
        if bad and (r.get("alert") is False or (not isinstance(r.get("alert"), bool) and name in info)):
            bad = False                                  # an informational task can never count against an SLO
        d = by.setdefault(name, {})
        d[int(t // SLOT_S)] = d.get(int(t // SLOT_S), False) or bad
    return by


def export_slo(history: list[dict] | None = None, now: float | None = None, cfg: dict | None = None) -> dict:
    """Public slo.json content: availability, error budget and 1-day burn per objective (see module docstring)."""
    now = time.time() if now is None else float(now)
    cfg = cfg or load_config()
    sd = cfg["slo_defaults"]
    wd = _int(sd.get("window_days"), 30)
    since = now - wd * 86400
    if history is None:
        history = _read_task_history(since)
    by = _slots(history, since, set(sd.get("informational", [])), now + FUTURE_S)
    cut24 = int((now - 86400) // SLOT_S)
    objs = []
    for o in cfg["slo"]:
        if not isinstance(o, dict) or not isinstance(o.get("name"), str):
            continue
        checks = [c for c in o.get("checks", []) if isinstance(c, str)]
        target = min(max(_flt(o.get("target_pct"), 99.0), 1.0), 99.999)
        slots: dict[int, bool] = {}
        for c in checks:
            for s, bad in by.get(c, {}).items():
                slots[s] = slots.get(s, False) or bad
        bad_n, total = sum(slots.values()), len(slots)
        row = {"name": o["name"], "target_pct": target, "class": str(o.get("class", "P2")), "checks": checks,
               "samples": total, "observed_h": round(total * SLOT_S / 3600, 1), "note": str(o.get("note", ""))[:160]}
        if total == 0:
            row.update(availability_pct=None, budget_remaining_pct=None, burn_rate_1d=None, status="ok",
                       bad_minutes=0, budget_minutes=None, note="collecting data")
        else:
            allowed = wd * 86400 / SLOT_S * (1 - target / 100)           # bad slots the whole window may contain
            remaining = max(min(100.0 * (1 - bad_n / allowed), 100.0), -100.0)
            s24 = [b for s, b in slots.items() if s >= cut24]
            burn = (sum(s24) / len(s24)) / (1 - target / 100) if s24 else None
            if remaining <= 0:
                status = "breached"
            elif remaining < _flt(sd.get("at_risk_budget_pct"), 25.0) or (burn is not None and burn >= _flt(sd.get("at_risk_burn"), 3.0)):
                status = "at_risk"
            else:
                status = "ok"
            row.update(availability_pct=round(100.0 * (total - bad_n) / total, 3), budget_remaining_pct=round(remaining, 1),
                       burn_rate_1d=None if burn is None else round(burn, 2), status=status,
                       bad_minutes=bad_n * SLOT_S // 60, budget_minutes=round(allowed * SLOT_S / 60))
        objs.append(row)
    return {"generated_at": _r(now), "window_days": wd, "objectives": objs}


def write_public(now: float | None = None) -> list[str]:
    """Write incidents.json and slo.json into STATE_DIR/public/ (what the website reads). Glue for publish/cli: one call.
    Read-only on the ledger; never raises; returns the file names written."""
    done: list[str] = []
    try:
        now = time.time() if now is None else float(now)
        pub = core.STATE_DIR / "public"
        pub.mkdir(parents=True, exist_ok=True)
        os.chmod(pub, 0o755)
        for name, build in (("incidents.json", lambda: export_incidents(now)), ("slo.json", lambda: export_slo(None, now))):
            try:
                _write_public_file(pub / name, build())
                done.append(name)
            except Exception:  # noqa: BLE001 - one file failing must not stop the other
                continue
    except Exception:  # noqa: BLE001
        pass
    return done


# =========================================================================== command line (read-only except `update`)
def _show(v: dict) -> str:
    out = [f"{v['id']}  {v['severity']}  {v['status']}  {v['title']}  since {_when(v['since'])}  ({_dur(v['duration_s'])})",
           f"  {v['summary']}"]
    if v.get("cause_hint"):
        out.append(f"  Hint: {v['cause_hint']}")
    out += [f"  {_when(x['t'])}  {x['kind']:<10} {x['text']}" for x in v["timeline"]]
    if v.get("postmortem_md"):
        out += ["", v["postmortem_md"]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "list"
    if cmd == "playbook" and len(a) > 1:
        print(format_playbook(a[1]))
    elif cmd == "slo":
        for o in export_slo()["objectives"]:
            print(f"{o['status']:<9} {o['name']:<18} target {o['target_pct']}%  now {o['availability_pct']}%  budget left {o['budget_remaining_pct']}%  burn {o['burn_rate_1d']}")
    elif cmd == "export":
        print(json.dumps(export_incidents(), indent=1))
    elif cmd == "show" and len(a) > 1:
        pub = export_incidents()
        v = next((x for x in pub["open"] + pub["recent"] if x["id"] == a[1]), None)
        print(_show(v) if v else f"no incident {a[1]}")
        return 0 if v else 1
    elif cmd == "update":
        print(json.dumps(update(core.read_json(_p("status.json"), {}) or {})))
    elif cmd == "list":
        pub = export_incidents()
        print(f"{len(pub['open'])} open, {pub['stats']['incidents_30d']} in 30 days, MTTR {_dur(pub['stats']['mttr_s_30d'])}")
        for v in pub["open"] + pub["recent"][:10]:
            print(f"  {v['id']}  {v['severity']}  {v['status']:<8} {v['title']:<24} {_dur(v['duration_s']):>8}  {v['summary'][:70]}")
        clk = _load_state().get("clock")
        if isinstance(clk, dict) and time.time() - (_num(clk.get("t")) or 0) < 86400:
            print(f"NOTE: clock trouble (last seen {_when(_num(clk.get('t')))}): samples stamped more than {FUTURE_S // 60} min in the future are "
                  f"ignored for {sorted(clk.get('future') or {})}; cursors moved back for {clk.get('clamped')}")
        for note in override_notes():
            print("NOTE: " + note)
    else:
        print(__doc__.split("\n\n")[0] + "\nusage: list | show ID | playbook TASK | slo | export | update", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
