"""registry: the ONE rules registry on the host (SPEC6 v2). Everything the maintenance script does is a rule in rules.d; the legacy
config files the runner modules read are GENERATED from it.

  /etc/homelab-maint/rules.d/NN-<category>.toml   THE registry (edit here)          [meta] + [[rule]] tables, see load_registry
        |  sync = validate -> compile -> invariants -> snapshot -> atomic replace -> record the change -> notice
        v
  /etc/homelab-maint/{maint,routine,jobs,probes,classes,notify,ack,protected}.toml   GENERATED (header says so; "# rule: ID" above
                                                                                     every emitted table names the source rule)
  STATE_DIR/rules/{current.json,history.jsonl,baseline.json,snapshots/<hash>/}      change tracking, rollback, the last effective baseline
  STATE_DIR/public/{rules.json,manifest.json}                                       the read-only mirror the website shows

Design rules (SPEC.md / SPEC6.md):
  * Fail closed. A registry that does not validate, whose compile does not re-parse to the data it compiled, that breaks an
    invariant, or that the runner's OWN loaders (jobs, scheduler, probes, routine, notify, ack, classes) would reject with anything the files
    in force do not already have, NEVER replaces the last good generated files; the failure is recorded once and the runner keeps its config.
  * Deterministic: rules compile in (depth, registry file, order, id) order; the output depends on the rules alone (no timestamps,
    no registry hash inside the generated files), so an edit to why/does/title rewrites nothing and one edited param rewrites one table.
  * The compiler's output is parsed back with tomllib and compared strictly (types included) with the data it was built from before
    anything is written: the emitter cannot silently corrupt a config file.
  * sync() is the per-minute tick's call: a changed hash is the only thing that costs time (one stat+read+sha256 of rules.d and
    the generated files otherwise); one flock serialises writers; every file is replaced with os.replace after all temp files exist, an
    intent marker in current.json tells a half-finished replace from a hand edit, and a failure part-way puts the replaced files back.
  * A generated file that was edited by hand (header stripped, an old copy put back) is drift: rewritten, the edited copy kept in
    STATE_DIR/rules/orig, announced as significant. Files the registry never adopted are never clobbered, but their safety is watched.
  * Python 3.12 stdlib only. Nothing here sends, restarts or deletes anything outside its own state; the one outbound call is the
    guarded `notify` hook (a "rules changed" maintenance notice), replaceable by tests.

Rule fields (all but the first five optional; unknown keys are ERRORS, a typo like `destuctive` must not silently drop a flag):
  id title kind why does | applies_to file target merge params mode enabled severity destructive proof principle owner_notes since order
  target grammar:  ""  root of the file | a.b.c  table path (created) | a.b[]  array of tables (merge=append adds ONE element: params)
                   | @RULE_ID:sub.path  relative to the element created by another rule (so a probe is a rule of its own, inside a group)
  merge=set    deep-merge params into the table; two rules writing the same scalar/list key = ERROR (no silent override)
  merge=append every list-valued param is EXTENDED (exact duplicates dropped); with a `[]` target, params is one new element
  mode         report|apply -> target.mode (a rule that enables apply must be flagged destructive: checked by the invariants)
  enabled      false => compiled out (a task table becomes `enabled = false`; an item or table is omitted)
  order        int (default 0): position inside lists built by several rules (routine steps, retention rules, probes, ...)
Only 00-baseline-invariants.toml may carry the [baseline] table; only 99-owner-overrides.toml may carry [meta] allow_baseline_removal
(protected patterns) and [meta] allow_unprotect (extra `unprotect` regexes).

Safety invariants are judged on WHAT THE RULES COMPILE TO (the generated documents) plus the task catalog, never on what a rule says about
itself (kind, destructive): a delete-type task (class C1, or a task nobody declared) is confined whichever rule writes it, an `apply` value
anywhere under [tasks.*] needs a destructive rule that owns it, and every `unprotect` regex must be on the baseline allow-list.
The baseline is pinned twice: the release floor lives in this module (PROTECTED_FLOOR, NEVER_TOUCH, LIMITS, UNPROTECT_ALLOW, APPLY_KEYS;
the installed package is root-owned) and rules.d/00-baseline-invariants.toml is only a mirror: the effective baseline is the strictest of
the two, a mirror weaker than the floor blocks the sync, and any change of the effective baseline is flagged in history and the notice.

Public API (the glue lists the exact call sites):
  sync(conf, state, wait=, hooks=, adopt=) / tick()     validate -> compile -> invariants -> record; tick() is the per-minute, never-raising call
  load_registry / analyze / compile_registry / check_invariants   what `rules check` runs;  status()  cheap health for self_health and the check task
  rollback(hash) / history(n)                           change tracking (STATE_DIR/rules: current.json, history.jsonl, snapshots/<hash>/)
  build_rules_json / build_manifest / write_public      the read-only export for the website (rules.json, manifest.json)
  migrate(src, out) / prove / write_baseline            bootstrap rules.d from the legacy files and PROVE compile(rules.d) == the files
  task_catalog()                                        task names, classes and option keys derived from the source (never imported)
  check_task / register_tasks()                         the C0 `rules_registry` task (valid, applied, in sync, files on disk safe)
  doc_invariants / rule_invariants / effective_baseline / package_floor   the safety checks, on documents, on rules, and the pinned floor
  consumer_problems / new_consumer_problems             what the runner's own loaders say about a set of config texts
  dumps / same / diff_docs                              the TOML emitter and the strict (type-aware) comparison it is proven with
  main(argv)                                            the CLI: homelab-maint rules list|show|check|diff|sync|history|rollback|export|migrate|explain|where|orphans
"""
from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import math
import os
import posixpath
import re
import stat as _stat
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import core


def _lazy(name: str):
    """Heavy stdlib modules are imported on first use: the per-minute tick imports this module and must stay cheap."""
    import importlib
    return importlib.import_module(name)


SCHEMA = 1
HEADER = ("# GENERATED from rules.d by homelab-maint: edit the registry (rules.d), not this file.\n"
          "# `homelab-maint rules sync` overwrites it; the \"# rule: ID\" comments name the rule that produced each table.\n")
GENERATED_MARK = "# GENERATED from rules.d"
MANAGED_FILES = ("maint.toml", "routine.toml", "jobs.toml", "probes.toml", "classes.toml", "notify.toml", "ack.toml", "protected.toml")
CATEGORIES = {"checks": "Health checks", "spike": "Spike and load response", "cleanup": "Cleanup and retention",
              "protection": "Protections", "alerts": "Alerts and notifications", "schedule": "Schedule and routine",
              "monitoring": "Monitoring probes", "jobs": "Jobs", "safety": "Safety limits", "ack": "Acknowledgements"}
KINDS = ("check", "cleanup", "protection", "alert", "schedule", "probe", "job", "spike", "policy", "safety")
SEVERITIES = ("warn", "crit", "none")
MERGES = ("set", "append")
MODES = ("report", "apply")
RULE_FIELDS = ("id", "title", "kind", "why", "does", "applies_to", "file", "target", "merge", "params", "mode", "enabled", "severity",
               "destructive", "proof", "principle", "owner_notes", "since", "order")
REQUIRED = ("id", "title", "kind", "why", "does")
DOC_ONLY_FORBIDDEN = ("target", "merge", "params", "mode")      # a rule without `file` documents a policy; it configures nothing
ID_RX = re.compile(r"[a-z][a-z0-9_.-]{2,80}\Z")
REG_FILE_RX = re.compile(r"(\d{2})-([a-z0-9][a-z0-9_-]*)\.toml\Z")
BASELINE_FILE = "00-baseline-invariants.toml"
OVERRIDES_FILE = "99-owner-overrides.toml"
TODO = "TODO-CONTENT"
# Options every task honours although its code never calls ctx.opt for them (core.Ctx, routine, scheduler, incidents read them).
COMMON_TASK_KEYS = frozenset({"mode", "enabled", "unprotect", "max_gib_per_run", "max_items_per_run", "disruptive", "schedule",
                              "alert_confirm_runs"})
MAX_FILE_BYTES = 1 << 20
MAX_RULES = 5000
MAX_PARAMS_BYTES = 64 * 1024
MAX_DEPTH = 10
KEEP_SNAPSHOTS = 20
KEEP_UNAPPLIED = 5
MAX_HISTORY_BYTES = 20 * 1024 * 1024      # what a reader parses at most
HISTORY_FILE_MAX = 4 << 20                # history.jsonl is trimmed (oldest records first) when it passes this
HISTORY_MIN_KEEP = 20                     # ... but the newest records always stay
HISTORY_MODIFIED_MAX = 40                 # modified rules stored per history record (the rest only as modified_count)
HISTORY_IDS_MAX = 200                     # added/removed ids stored per record (the rest only as a count)
RULES_JSON_MAX = 700_000                  # SPEC6 S5 said < 400 KB for placeholder text (332 KB today); ~430 rules with real why/does/proof prose need ~650 KB. Website MAX_JSON is 1 MB
INLINE_MAX = 100                          # a table/array that fits on one line this wide stays inline in generated files
TICK_REFRESH_S = 300                      # the idle tick refreshes current.json["last_tick"] at most this often
ERROR_RENOTICE_S = 6 * 3600               # the same sync error is announced again after this long


# =========================================================================== TOML emitter (the subset the configs use)
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_CTRL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _qstr(s: str) -> str:
    """Basic string, every control character escaped (a config file must stay one-key-per-line and reviewable)."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch in _ESC:
            out.append(_ESC[ch])
        elif o < 0x20 or o == 0x7F:
            out.append("\\u%04x" % o)
        elif 0xD800 <= o <= 0xDFFF:
            raise ValueError("lone surrogate in string")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _str(s: str) -> str:
    """Literal string ('...') when it holds backslashes or quotes and nothing needing an escape (regexes stay readable)."""
    if ("\\" in s or '"' in s) and "'" not in s and not _CTRL.search(s) and not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return "'" + s + "'"
    return _qstr(s)


def _key(k: Any) -> str:
    if not isinstance(k, str):
        raise ValueError(f"TOML key must be a string, got {type(k).__name__}")
    return k if _BARE_KEY.match(k) else _qstr(k)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "nan"
        if v in (math.inf, -math.inf):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    raise ValueError(f"value of type {type(v).__name__} cannot be written as TOML")


def _inline(v: Any) -> str:
    if isinstance(v, dict):
        return "{}" if not v else "{ " + ", ".join(f"{_key(k)} = {_inline(x)}" for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_inline(x) for x in v) + "]"
    return _scalar(v)


class _Emit:
    """Layout: root keys, then sub-tables / arrays of tables in insertion order. `tprov` maps id(table) -> [rule ids] and `lprov`
    id(list) -> [(first index, rule id)], so a comment `# rule: ID` stands above every table and every run of list items."""

    def __init__(self, tprov: dict | None, lprov: dict | None, aot_keys: frozenset[str]):
        self.tprov, self.lprov, self.aot_keys = tprov or {}, lprov or {}, aot_keys

    def doc(self, d: dict) -> str:
        out: list[str] = []
        self._table(out, (), d, "root")
        return "\n".join(out).rstrip("\n") + "\n"

    def _marked(self, v: Any) -> bool:
        """Does this table, or any table below it, carry a rule comment? Then it cannot be flattened into an inline table."""
        if id(v) in self.tprov:
            return True
        kids = v.values() if isinstance(v, dict) else v if isinstance(v, list) else ()
        return any(isinstance(x, (dict, list)) and self._marked(x) for x in kids)

    def _is_sub(self, k: str, v: Any) -> bool:
        if isinstance(v, dict):
            return self._marked(v) or len(_inline(v)) > INLINE_MAX
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return (k in self.aot_keys or id(v) in self.lprov or any(id(x) in self.tprov for x in v)
                    or any(len(_inline(x)) > INLINE_MAX for x in v))
        return False

    @staticmethod
    def _tag(ids: list[str]) -> str:
        return "# rule: " + ", ".join(ids)

    def _item_rule(self, lst: list, i: int) -> str | None:
        runs = self.lprov.get(id(lst)) or []
        cur = None
        for start, rid in runs:
            if start <= i:
                cur = rid
        return cur

    def _table(self, out: list[str], path: tuple[str, ...], tbl: dict, kind: str, tag: str | None = None) -> None:
        scal, subs = [], []
        for k, v in tbl.items():
            (subs if self._is_sub(k, v) else scal).append((k, v))
        prov = self.tprov.get(id(tbl))
        if kind == "root":
            if scal and prov:
                out.append(self._tag(prov))
        elif scal or not subs or prov or kind == "aot" or tag:
            if out and out[-1] != "":
                out.append("")
            if tag or prov:
                out.append(self._tag(prov) if prov and not tag else f"# rule: {tag}")
            name = ".".join(_key(p) for p in path)
            out.append(f"[[{name}]]" if kind == "aot" else f"[{name}]")
        for k, v in scal:
            self._kv(out, k, v)
        for k, v in subs:
            if isinstance(v, dict):
                self._table(out, path + (k,), v, "table")
            else:
                for i, el in enumerate(v):
                    self._table(out, path + (k,), el, "aot", None if id(el) in self.tprov else self._item_rule(v, i))

    def _kv(self, out: list[str], k: str, v: Any) -> None:
        line = f"{_key(k)} = {_inline(v)}"
        runs = self.lprov.get(id(v)) if isinstance(v, list) else None
        if not isinstance(v, list) or (len(line) <= INLINE_MAX + 20 and not runs):
            out.append(line)
            return
        out.append(f"{_key(k)} = [")
        marks = {s: r for s, r in (runs or [])}
        row: list[str] = []

        def flush() -> None:
            if row:
                out.append("  " + ", ".join(row) + ",")
                row.clear()
        for i, x in enumerate(v):
            if i in marks:
                flush()
                out.append("  # rule: " + marks[i])
            s = _inline(x)
            if isinstance(x, dict) or len(s) > 60:
                flush()
                out.append("  " + s + ",")
            else:
                if sum(len(r) + 2 for r in row) + len(s) > 92:
                    flush()
                row.append(s)
        flush()
        out.append("]")


def dumps(doc: dict, *, tprov: dict | None = None, lprov: dict | None = None, aot_keys: Iterable[str] = ()) -> str:
    """TOML text for a dict of str/int/float/bool/date/list/dict. Raises ValueError for anything TOML cannot say."""
    if not isinstance(doc, dict):
        raise ValueError("the document must be a table")
    return _Emit(tprov, lprov, frozenset(aot_keys)).doc(doc)


def same(a: Any, b: Any) -> bool:
    """Strict deep equality: key order and comments do not matter, list order and TOML types do (1 != 1.0 != True, date != datetime)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def diff_docs(a: Any, b: Any, path: tuple = ()) -> list[tuple[str, Any, Any]]:
    """[(dotted path, a-value, b-value)] for every leaf that differs; `_MISSING` marks an absent side."""
    out: list[tuple[str, Any, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append((_dotted(path + (k,)), _MISSING, b[k]))
            elif k not in b:
                out.append((_dotted(path + (k,)), a[k], _MISSING))
            else:
                out += diff_docs(a[k], b[k], path + (k,))
    elif isinstance(a, list) and isinstance(b, list) and a and b and all(isinstance(x, dict) for x in a + b):
        for i in range(max(len(a), len(b))):
            if i >= len(a):
                out.append((_dotted(path + (i,)), _MISSING, b[i]))
            elif i >= len(b):
                out.append((_dotted(path + (i,)), a[i], _MISSING))
            else:
                out += diff_docs(a[i], b[i], path + (i,))
    elif not same(a, b):
        out.append((_dotted(path), a, b))
    return out


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _dotted(path: Iterable) -> str:
    return ".".join(f"[{p}]" if isinstance(p, int) else str(p) for p in path).replace(".[", "[")


# =========================================================================== small utilities
def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def _conf(conf_dir: Path | str | None) -> Path:
    return Path(conf_dir) if conf_dir is not None else core.CONF_DIR


def _state(state_dir: Path | str | None) -> Path:
    return Path(state_dir) if state_dir is not None else core.STATE_DIR


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short(h: str | None) -> str:
    return (h or "")[:12]


def _trusted(p: Path) -> bool:
    """Config can name commands run as root: only a file owned by root-or-us that nobody else can write, in a directory nobody
    else can write, is believed (same test as probes._trusted)."""
    try:
        st, dst = p.lstat(), p.parent.lstat()
    except OSError:
        return False
    return st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022 and not dst.st_mode & 0o022


def _read_regular(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Bytes of a regular, non-symlink file <= limit. Never blocks on a FIFO, never follows a planted link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if st.st_size > limit:
            raise OSError(f"larger than {limit} bytes")
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read(limit + 1)
    finally:
        os.close(fd)


_tmp_seq = 0


def atomic_write(path: Path, data: bytes, mode: int = 0o644, prefix: str = ".hm-rules-") -> None:
    """Unique temp file in the same directory, fsync, chmod, os.replace: a reader sees the old or the new file, never a mix."""
    global _tmp_seq
    _tmp_seq += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{prefix}{os.getpid()}-{_tmp_seq}-{path.name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sweep_tmp(d: Path) -> None:
    """Leftovers of a killed writer (we hold the sync lock, so none can be live)."""
    with contextlib.suppress(OSError):
        for p in d.glob(".hm-rules-*.tmp"):
            with contextlib.suppress(OSError):
                p.unlink()


def _json_line(rec: dict) -> bytes:
    return (json.dumps(rec, separators=(",", ":"), sort_keys=True, default=str) + "\n").encode()


def _append_line(path: Path, rec: dict) -> None:
    """One O_APPEND write: concurrent writers never interleave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, _json_line(rec))
    finally:
        os.close(fd)


def _trim_jsonl(path: Path, max_bytes: int = HISTORY_FILE_MAX, keep: int = HISTORY_MIN_KEEP) -> None:
    """BYTES bound the file, not lines: once it passes max_bytes the OLDEST records go until it is under half of max_bytes (so the next
    appends do not rewrite it again), whatever one record weighs; the newest `keep` records always stay (the caller holds the sync lock)."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        kept: list[bytes] = []
        total = 0
        for ln in reversed(path.read_bytes().splitlines()):
            if len(kept) >= keep and total + len(ln) + 1 > max_bytes // 2:
                break
            kept.append(ln)
            total += len(ln) + 1
        kept.reverse()
        atomic_write(path, b"\n".join(kept) + b"\n")
    except OSError:
        pass


def _read_jsonl(path: Path, tail_bytes: int = MAX_HISTORY_BYTES) -> list[dict]:
    """Records of a JSON-lines file (newest `tail_bytes` only); damaged lines are skipped."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - tail_bytes))
            raw = f.read()
    except OSError:
        return []
    out = []
    for ln in raw.splitlines()[1 if size > tail_bytes else 0:]:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _slug(s: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", str(s).lower()).strip("-.")
    s = re.sub(r"-{2,}", "-", s) or "x"
    if not s[0].isalpha():
        s = "r" + s
    if len(s) > limit:
        s = s[: limit - 7].rstrip("-.") + "-" + hashlib.sha1(s.encode()).hexdigest()[:6]
    return s


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _validate_value(v: Any, where: str, errs: list[str], depth: int = 0) -> None:
    """Params must be plain TOML data: no None, tuples, objects, NaN/inf (they cannot reach JSON exports), lone surrogates."""
    if depth > MAX_DEPTH:
        errs.append(f"{where}: nested deeper than {MAX_DEPTH}")
    elif isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str) or not k or len(k) > 120:
                errs.append(f"{where}: bad key {k!r}")
            else:
                _validate_value(x, f"{where}.{k}", errs, depth + 1)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _validate_value(x, f"{where}[{i}]", errs, depth + 1)
    elif isinstance(v, float):
        if not math.isfinite(v):
            errs.append(f"{where}: non-finite number")
    elif isinstance(v, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            errs.append(f"{where}: invalid text")
        elif len(v) > 20000:
            errs.append(f"{where}: string longer than 20000 characters")
    elif not isinstance(v, (bool, int, _dt.datetime, _dt.date, _dt.time)):
        errs.append(f"{where}: unsupported value type {type(v).__name__}")


# =========================================================================== rule model and loader
@dataclass
class Rule:
    id: str
    title: str
    kind: str
    why: str
    does: str
    applies_to: list[str] = field(default_factory=list)
    file: str | None = None
    target: str = ""
    merge: str = "set"
    params: dict = field(default_factory=dict)
    mode: str | None = None
    enabled: bool = True
    severity: str = "none"
    destructive: bool = False
    proof: str = ""
    principle: str = ""
    owner_notes: str = ""
    since: str = ""
    order: int = 0
    category: str = ""                  # from [meta] of the registry file (or its name)
    source: str = ""                    # registry file name
    seq: int = 0                        # position in that file

    def as_dict(self) -> dict:
        d = {k: copy.deepcopy(getattr(self, k)) for k in RULE_FIELDS}
        d["category"], d["source_file"] = self.category, self.source
        return d


@dataclass
class Registry:
    dir: Path
    present: bool = False
    rules: list[Rule] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)          # {"name","sha","size"} of EVERY *.toml in rules.d (the hash input)
    meta: dict[str, dict] = field(default_factory=dict)      # category -> {"title","blurb"}
    baseline: dict | None = None
    allow_removal: list[str] = field(default_factory=list)
    allow_unprotect: list[dict] = field(default_factory=list)       # [{"file","path","regex"}] the owner accepted in 99-owner-overrides.toml
    hash: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def by_id(self) -> dict[str, Rule]:
        return {r.id: r for r in self.rules}


def registry_hash(files: list[dict]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["name"]):
        h.update(f["name"].encode() + b"\0" + bytes.fromhex(f["sha"]))
    return h.hexdigest()


def rules_dir(conf_dir: Path | str | None = None) -> Path:
    return _conf(conf_dir) / "rules.d"


def scan_registry(conf_dir: Path | str | None = None, *, rdir: Path | None = None) -> tuple[list[dict], dict[str, bytes], list[str]]:
    """(files, contents, errors): every *.toml in rules.d with its sha. Cheap and never raises: this is the fast path of sync()."""
    d = rdir or rules_dir(conf_dir)
    files: list[dict] = []
    data: dict[str, bytes] = {}
    errs: list[str] = []
    try:
        names = sorted(e.name for e in os.scandir(d) if e.name.endswith(".toml") and not e.name.startswith("."))
    except OSError:
        return files, data, errs
    for n in names:
        try:
            raw = _read_regular(d / n)
        except OSError as exc:
            errs.append(f"{n}: cannot be read ({exc})")
            files.append({"name": n, "sha": sha_bytes(b"unreadable:" + str(exc).encode()), "size": 0})
            continue
        files.append({"name": n, "sha": sha_bytes(raw), "size": len(raw)})
        data[n] = raw
    return files, data, errs


# --------------------------------------------------------------------------- target grammar
_SEG_RX = re.compile(r"[A-Za-z0-9_-]+")


def parse_target(s: Any) -> tuple[str | None, list[tuple[str, bool]]]:
    """(ref rule id | None, [(key, is_array_of_tables)]); ValueError when the grammar is violated. No `..`, no slashes, no escapes."""
    if not isinstance(s, str) or len(s) > 300 or "\0" in s:
        raise ValueError("target must be a short string")
    ref = None
    if s.startswith("@"):
        ref, sep, s = s[1:].partition(":")
        if not sep or not ID_RX.fullmatch(ref):
            raise ValueError("a relative target is @RULE_ID:path")
    segs: list[tuple[str, bool]] = []
    i = 0
    while s[i:]:
        if s[i] == '"':
            j, buf = i + 1, []
            while j < len(s) and s[j] != '"':
                if s[j] == "\\" and j + 1 < len(s) and s[j + 1] in '"\\':
                    j += 1
                buf.append(s[j])
                j += 1
            if j >= len(s) or not buf:
                raise ValueError("bad quoted key")
            key, i = "".join(buf), j + 1
        else:
            m = _SEG_RX.match(s, i)
            if not m:
                raise ValueError(f"bad path segment at {s[i:i + 8]!r}")
            key, i = m.group(0), m.end()
        lst = s.startswith("[]", i)
        i += 2 if lst else 0
        segs.append((key, lst))
        if i < len(s):
            if s[i] != "." or i + 1 >= len(s):
                raise ValueError("segments are separated by single dots")
            i += 1
    if len(segs) > 8:
        raise ValueError("target deeper than 8 levels")
    if any(lst for _, lst in segs[:-1]):
        raise ValueError("`[]` is only allowed on the last segment (address an element with @RULE_ID:)")
    return ref, segs


def fmt_target(path: Iterable[str], *, array: bool = False, ref: str | None = None) -> str:
    """The inverse of parse_target for a path of table keys (keys that are not bare words are quoted)."""
    segs = [p if _SEG_RX.fullmatch(p) else '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"' for p in path]
    return (f"@{ref}:" if ref else "") + ".".join(segs) + ("[]" if array else "")


def _close(word: str, options: Iterable[str]) -> str:
    m = _lazy("difflib").get_close_matches(word, list(options), n=1, cutoff=0.7)
    return f" (did you mean {m[0]!r}?)" if m else ""


def _str_field(d: dict, k: str, errs: list[str], ctx: str, *, required: bool = False, maxlen: int = 4000, default: str = "") -> str:
    v = d.get(k, default)
    if k not in d:
        return default
    if isinstance(v, (_dt.date, _dt.datetime)) and k == "since":
        return v.isoformat()
    if not isinstance(v, str):
        errs.append(f"{ctx}: {k} must be a string")
        return default
    if (required and not v.strip()) or len(v) > maxlen or any(0xD800 <= ord(c) <= 0xDFFF for c in v):
        errs.append(f"{ctx}: {k} must be {'non-empty and ' if required else ''}at most {maxlen} characters")
        return default
    return v


def _parse_rule(d: Any, fname: str, idx: int, category: str, errs: list[str]) -> Rule | None:
    if not isinstance(d, dict):
        errs.append(f"{fname}: rule #{idx} is not a table")
        return None
    rid = d.get("id")
    ctx = f"{fname}: rule #{idx}" + (f" ({rid})" if isinstance(rid, str) and len(rid) < 90 else "")
    n0 = len(errs)
    for k in d:
        if k not in RULE_FIELDS:
            errs.append(f"{ctx}: unknown field {str(k)[:40]!r}{_close(str(k), RULE_FIELDS)}")
    for k in REQUIRED:
        if k not in d:
            errs.append(f"{ctx}: missing required field {k!r}")
    if "id" in d and not (isinstance(rid, str) and ID_RX.fullmatch(rid)):
        errs.append(f"{ctx}: id must match {ID_RX.pattern.replace(chr(92) + 'Z', '$')} (lowercase letters, digits, . _ -; 3-81 characters)")
    kind = d.get("kind")
    if "kind" in d and kind not in KINDS:
        errs.append(f"{ctx}: kind must be one of {', '.join(KINDS)}")
    title = _str_field(d, "title", errs, ctx, required=True, maxlen=120)
    why, does = _str_field(d, "why", errs, ctx, required=True), _str_field(d, "does", errs, ctx, required=True)
    proof, principle = _str_field(d, "proof", errs, ctx), _str_field(d, "principle", errs, ctx, maxlen=120)
    notes, since = _str_field(d, "owner_notes", errs, ctx), _str_field(d, "since", errs, ctx, maxlen=40)
    applies = d.get("applies_to", [])
    if not (isinstance(applies, list) and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", x) for x in applies)):
        errs.append(f"{ctx}: applies_to must be a list of task/job/probe names")
        applies = []
    file = d.get("file")
    if file is not None and (not isinstance(file, str) or file not in MANAGED_FILES):
        bad = "must be a plain file name" if isinstance(file, str) and re.search(r"[/\\\0]|^\.|\.\.", file) else "is not a managed file"
        errs.append(f"{ctx}: file {bad} (one of {', '.join(MANAGED_FILES)})")
        file = None
    merge = d.get("merge", "set")
    if merge not in MERGES:
        errs.append(f"{ctx}: merge must be one of {', '.join(MERGES)}")
        merge = "set"
    mode = d.get("mode")
    if mode is not None and mode not in MODES:
        errs.append(f"{ctx}: mode must be report or apply")
    sev = d.get("severity", "none")
    if sev not in SEVERITIES:
        errs.append(f"{ctx}: severity must be one of {', '.join(SEVERITIES)}")
        sev = "none"
    for k in ("enabled", "destructive"):
        if k in d and not isinstance(d[k], bool):
            errs.append(f"{ctx}: {k} must be true or false")
    order = d.get("order", 0)
    if isinstance(order, bool) or not isinstance(order, int) or abs(order) > 10 ** 9:
        errs.append(f"{ctx}: order must be an integer")
        order = 0
    params = d.get("params", {})
    if not isinstance(params, dict):
        errs.append(f"{ctx}: params must be a table")
        params = {}
    else:
        _validate_value(params, f"{ctx}: params", errs)
        if len(json.dumps(params, default=str)) > MAX_PARAMS_BYTES:
            errs.append(f"{ctx}: params larger than {MAX_PARAMS_BYTES} bytes")
    target = d.get("target", "")
    if file is None and "file" not in d:
        bad = [k for k in DOC_ONLY_FORBIDDEN if d.get(k) not in (None, {}, "", "set")]
        if bad:
            errs.append(f"{ctx}: a rule without `file` only documents a policy; remove {', '.join(bad)} or add file")
    elif file is not None:
        try:
            ref, segs = parse_target(target)
            lst = bool(segs) and segs[-1][1]
            if lst and merge != "append":
                errs.append(f"{ctx}: a `[]` target needs merge = \"append\"")
            if lst and mode is not None:
                errs.append(f"{ctx}: mode cannot be set on an array element (put it in params)")
            if merge == "append" and not lst and not all(isinstance(v, list) for v in params.values()):
                errs.append(f"{ctx}: merge = \"append\" needs list-valued params (or a `[]` target for one new element)")
            if ref == d.get("id"):
                errs.append(f"{ctx}: a rule cannot be relative to itself")
        except ValueError as exc:
            errs.append(f"{ctx}: target: {exc}")
    if len(errs) > n0 or not isinstance(rid, str):
        return None
    return Rule(rid, title, kind, why, does, list(applies), file, target if isinstance(target, str) else "", merge, params, mode,
                bool(d.get("enabled", True)), sev, bool(d.get("destructive", False)), proof, principle, notes, since, order,
                category, fname, idx)


def _category_of(fname: str, meta: dict, errs: list[str]) -> str:
    cat = meta.get("category")
    if cat is None:
        m = REG_FILE_RX.fullmatch(fname)
        cat = "safety" if fname in (BASELINE_FILE, OVERRIDES_FILE) else m.group(2) if m else ""
    if cat not in CATEGORIES:
        errs.append(f"{fname}: category must be one of {', '.join(CATEGORIES)} (set [meta] category)")
        return "checks"
    return cat


def _parse_meta(meta: Any, fname: str, errs: list[str]) -> dict:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        errs.append(f"{fname}: [meta] must be a table")
        return {}
    for k in meta:
        if k not in ("category", "title", "blurb", "allow_baseline_removal", "allow_unprotect"):
            errs.append(f"{fname}: [meta] unknown key {str(k)[:40]!r}{_close(str(k), ('category', 'title', 'blurb'))}")
    for k in ("title", "blurb", "category"):
        if k in meta and not isinstance(meta[k], str):
            errs.append(f"{fname}: [meta] {k} must be a string")
    ab = meta.get("allow_baseline_removal")
    if ab is not None:
        if fname != OVERRIDES_FILE:
            errs.append(f"{fname}: allow_baseline_removal is only honoured in {OVERRIDES_FILE}")
        elif not (isinstance(ab, list) and all(isinstance(x, str) for x in ab)):
            errs.append(f"{fname}: allow_baseline_removal must be a list of patterns")
    au = meta.get("allow_unprotect")
    if au is not None:
        if fname != OVERRIDES_FILE:
            errs.append(f"{fname}: allow_unprotect is only honoured in {OVERRIDES_FILE}")
        elif not (isinstance(au, list) and all(isinstance(x, dict) and set(x) == {"file", "path", "regex"} and all(isinstance(v, str) for v in x.values())
                                                for x in au)):
            errs.append(f"{fname}: allow_unprotect must be a list of {{ file = \"maint.toml\", path = \"tasks.X.unprotect\", regex = \"^name$\" }}")
    return meta


def parse_baseline(b: Any, fname: str, errs: list[str]) -> dict | None:
    """The [baseline] table: protected_patterns[], never_touch[] (regexes), min_root_depth, limit[] = {file, path, max}."""
    if fname != BASELINE_FILE:
        errs.append(f"{fname}: the [baseline] table is only valid in {BASELINE_FILE}")
        return None
    if not isinstance(b, dict):
        errs.append(f"{fname}: [baseline] must be a table")
        return None
    out = {"version": b.get("version", 1), "protected_patterns": [], "never_touch": [], "min_root_depth": 2, "limit": [], "unprotect": [],
           "apply_keys": [], "derived_from": str(b.get("derived_from", ""))}
    for k in b:
        if k not in ("version", "derived_from", "protected_patterns", "never_touch", "min_root_depth", "limit", "unprotect", "apply_keys"):
            errs.append(f"{fname}: [baseline] unknown key {str(k)[:40]!r}")
    for k in ("protected_patterns", "never_touch"):
        v = b.get(k, [])
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            errs.append(f"{fname}: baseline {k} must be a list of strings")
            continue
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                errs.append(f"{fname}: baseline {k}: bad regex {p[:40]!r}: {exc}")
        out[k] = list(v)
    d = b.get("min_root_depth", 2)
    if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 6:
        out["min_root_depth"] = d
    else:
        errs.append(f"{fname}: baseline min_root_depth must be 1..6")
    for i, lim in enumerate(b.get("limit", [])):
        ok = (isinstance(lim, dict) and lim.get("file") in MANAGED_FILES and isinstance(lim.get("path"), str)
              and isinstance(lim.get("max"), (int, float)) and not isinstance(lim.get("max"), bool))
        if not ok:
            errs.append(f"{fname}: baseline limit #{i + 1} needs file, path, max")
        else:
            out["limit"].append({"file": lim["file"], "path": lim["path"], "max": lim["max"], "why": str(lim.get("why", ""))})
    for i, u in enumerate(b.get("unprotect", [])):
        ok = (isinstance(u, dict) and u.get("file") in MANAGED_FILES and isinstance(u.get("path"), str) and isinstance(u.get("allow"), list)
              and all(isinstance(x, str) for x in u["allow"]))
        if not ok:
            errs.append(f"{fname}: baseline unprotect #{i + 1} needs file, path, allow (a list of exact regexes)")
        else:
            out["unprotect"].append({"file": u["file"], "path": u["path"], "allow": list(u["allow"]), "why": str(u.get("why", ""))})
    for i, a in enumerate(b.get("apply_keys", [])):
        if not (isinstance(a, dict) and isinstance(a.get("task"), str) and isinstance(a.get("key"), str)):
            errs.append(f"{fname}: baseline apply_keys #{i + 1} needs task and key")
        else:
            out["apply_keys"].append({"task": a["task"], "key": a["key"], "why": str(a.get("why", ""))})
    return out


def load_registry(conf_dir: Path | str | None = None, *, rdir: Path | None = None, trust: bool = True, scanned: tuple | None = None) -> Registry:
    """Read and schema-check rules.d. NEVER raises: every problem is an entry in .errors (blocking) or .warnings.
    `scanned` = the result of scan_registry() the caller already holds, so hash, snapshot and validation see the same bytes."""
    d = rdir or rules_dir(conf_dir)
    reg = Registry(dir=d)
    if not d.is_dir():
        return reg
    reg.present = True
    errs, warns = reg.errors, reg.warnings
    if trust and (d.is_symlink() or not _trusted(d / ".")):
        errs.append(f"{d}: the registry directory must not be a symlink and must be owned by root (or this user) and not be "
                    "group/world-writable")
    files, data, scan_errs = scanned if scanned is not None else scan_registry(rdir=d)
    reg.files = files
    reg.hash = registry_hash(files)
    errs += scan_errs
    if len(files) > 200:
        errs.append(f"{d}: more than 200 registry files")
    ids: dict[str, str] = {}
    for f in files:
        fname = f["name"]
        raw = data.get(fname)
        if raw is None:
            continue
        if not REG_FILE_RX.fullmatch(fname):
            errs.append(f"{fname}: registry files are named NN-category.toml (two digits, a dash, lowercase letters)")
            continue
        if trust and not _trusted(d / fname):
            errs.append(f"{fname}: not trusted (it must be a regular file owned by root or this user, not group/world-writable)")
            continue
        try:
            doc = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError, ValueError, OverflowError) as exc:
            errs.append(f"{fname}: not valid TOML: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        for k in doc:
            if k not in ("meta", "rule", "baseline"):
                errs.append(f"{fname}: unknown top-level table {str(k)[:40]!r} (only [meta], [[rule]]" +
                            (", [baseline]" if fname == BASELINE_FILE else "") + ")")
        meta = _parse_meta(doc.get("meta"), fname, errs)
        cat = _category_of(fname, meta, errs)
        reg.meta.setdefault(cat, {"title": meta.get("title") or CATEGORIES[cat], "blurb": meta.get("blurb", "")})
        if meta.get("title") or meta.get("blurb"):
            reg.meta[cat] = {"title": meta.get("title") or reg.meta[cat]["title"], "blurb": meta.get("blurb", reg.meta[cat]["blurb"])}
        if fname == OVERRIDES_FILE and isinstance(meta.get("allow_baseline_removal"), list):
            reg.allow_removal = [x for x in meta["allow_baseline_removal"] if isinstance(x, str)]
        if fname == OVERRIDES_FILE and isinstance(meta.get("allow_unprotect"), list):
            reg.allow_unprotect = [dict(x) for x in meta["allow_unprotect"] if isinstance(x, dict)
                                   and set(x) == {"file", "path", "regex"} and all(isinstance(v, str) for v in x.values())]
        if "baseline" in doc:
            reg.baseline = parse_baseline(doc["baseline"], fname, errs)
        rules = doc.get("rule", [])
        if not isinstance(rules, list):
            errs.append(f"{fname}: rule must be an array of tables ([[rule]])")
            continue
        for i, rd in enumerate(rules, 1):
            if len(reg.rules) >= MAX_RULES:
                errs.append(f"more than {MAX_RULES} rules: the rest of {fname} is ignored")
                break
            r = _parse_rule(rd, fname, i, cat, errs)
            if r is None:
                continue
            if r.id in ids:
                errs.append(f"{fname}: rule {r.id}: duplicate id (also in {ids[r.id]})")
                continue
            ids[r.id] = fname
            reg.rules.append(r)
    todo = sum(1 for r in reg.rules if TODO in (r.why + r.does + r.proof))
    if todo:
        warns.append(f"{_plural(todo, 'rule')} still carry {TODO} placeholder text (run `rules check --todo` to list them)")
    return reg


# =========================================================================== task catalog (derived from the code, never imported)
@dataclass
class TaskInfo:
    name: str
    klass: str = "C0"
    tier: str = "check"
    title: str = ""
    module: str = ""
    keys: set = field(default_factory=set)       # option names the code reads (ctx.opt("k"), ctx.tcfg.get("k"))
    open: bool = False                           # some read has a computed name: the key set cannot be known, never warn


@dataclass
class _Fn:
    node: Any
    reads: set = field(default_factory=set)
    dyn: bool = False
    refs: set = field(default_factory=set)


def _lit(node: Any) -> str | None:
    ast = _lazy("ast")
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _scan_fn(node: ast.AST) -> _Fn:
    """Which option names a function reads. `<x>.opt("k")`, `<x>.tcfg.get("k")` / `["k"]` count; a read with a computed name, or
    a use of the whole tcfg table, makes the function dynamic (its task's key set is then open). One pass over the subtree."""
    ast = _lazy("ast")
    fn = _Fn(node)
    parent: dict = {}
    nodes: list = []
    stack = [node]
    while stack:
        n = stack.pop()
        nodes.append(n)
        for c in ast.iter_child_nodes(n):
            parent[c] = n
            stack.append(c)
    alias = {"tcfg"}
    for n in nodes:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Attribute) and n.value.attr == "tcfg":
            alias |= {t.id for t in n.targets if isinstance(t, ast.Name)}

    def is_tcfg(x: ast.AST) -> bool:
        return (isinstance(x, ast.Attribute) and x.attr == "tcfg") or (isinstance(x, ast.Name) and x.id in alias)
    for n in nodes:
        if isinstance(n, ast.Name):
            fn.refs.add(n.id)
        elif isinstance(n, ast.Attribute):
            fn.refs.add(n.attr)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "opt" or (n.func.attr == "get" and is_tcfg(n.func.value)):
                k = _lit(n.args[0]) if n.args else None
                if k is not None:
                    fn.reads.add(k)
                elif n.args or n.func.attr == "opt":
                    fn.dyn = True
        elif isinstance(n, ast.Subscript) and is_tcfg(n.value):
            k = _lit(n.slice)
            if k is not None:
                fn.reads.add(k)
            else:
                fn.dyn = True
        if is_tcfg(n):
            p = parent.get(n)
            ok = ((isinstance(p, ast.Attribute) and p.attr == "get" and isinstance(parent.get(p), ast.Call))
                  or isinstance(p, ast.Subscript) or (isinstance(p, ast.Assign) and n is p.value))
            if not ok and not (isinstance(n, ast.Name) and isinstance(parent.get(n), ast.Assign) and n in parent[n].targets):
                if not (isinstance(n, ast.Name) and isinstance(p, (ast.Attribute, ast.Subscript))):
                    fn.dyn = True
    return fn


def _defs(body: list) -> Iterable[Any]:
    """Functions and methods at module level (also inside module-level if/try blocks and class bodies), not the ones nested in
    another function: those are part of their parent's subtree already."""
    ast = _lazy("ast")
    for n in body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n
        elif isinstance(n, ast.ClassDef):
            yield from _defs(n.body)
        elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for blk in ("body", "orelse", "finalbody"):
                yield from _defs(getattr(n, blk, []))
            for h in getattr(n, "handlers", []):
                yield from _defs(h.body)


def _task_deco(dec: Any) -> dict | None:
    ast = _lazy("ast")
    if not isinstance(dec, ast.Call):
        return None
    f = dec.func
    nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    name = _lit(dec.args[0]) if dec.args else None
    if nm not in ("task", "register_task") or not name:
        return None
    info = {"name": name, "klass": "C0", "tier": "check", "title": name}
    for pos, key in ((1, "klass"), (2, "tier"), (3, "title")):
        if len(dec.args) > pos and _lit(dec.args[pos]):
            info[key] = _lit(dec.args[pos])
    for kw in dec.keywords:
        if kw.arg in ("klass", "tier", "title") and _lit(kw.value):
            info[kw.arg] = _lit(kw.value)
    return info


# Tasks this module registers AT RUNTIME (register_tasks): the static catalog parses source for @task decorators and cannot see them, so
# known_names (applies_to) and check_references (a [tasks.X] table) take them from here (a test keeps this equal to what register_tasks
# registers). They stay OUT of task_catalog on purpose: that is "every task the code declares", and a task with no knob has no option keys.
RUNTIME_TASKS = frozenset({"rules_registry"})

_CATALOG_CACHE: dict = {"sig": None, "val": {}}


def catalog_files(conf_dir: Path | str | None = None) -> list[Path]:
    pkg = Path(__file__).resolve().parent
    files = [*sorted((pkg / "tasks").glob("*.py")), pkg / "reports.py", pkg / "routine.py"]
    with contextlib.suppress(OSError):
        files += [p for p in sorted((_conf(conf_dir) / "plugins.d").glob("*.py")) if p.stat().st_size <= 256 * 1024]
    return [p for p in files if p.is_file()]


def task_catalog(conf_dir: Path | str | None = None) -> dict[str, TaskInfo]:
    """name -> TaskInfo for every task declared in the package or plugins.d, found by parsing source (nothing is executed)."""
    ast = _lazy("ast")
    files = catalog_files(conf_dir)
    sig = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _CATALOG_CACHE["sig"] == sig:
        return _CATALOG_CACHE["val"]
    mods: list[tuple[str, dict, dict, list]] = []              # (module, funcs, classes, [(info, fn)])
    for p in files:
        try:
            tree = ast.parse(p.read_text(errors="replace"), filename=str(p))
        except (SyntaxError, ValueError, RecursionError, OSError):
            continue
        funcs: dict[str, list[_Fn]] = {}
        classes: dict[str, list[_Fn]] = {}
        decl: list = []
        for n in _defs(tree.body):
            fn = _scan_fn(n)
            funcs.setdefault(n.name, []).append(fn)
            for dec in n.decorator_list:
                info = _task_deco(dec)
                if info:
                    decl.append((info, fn))
        for c in (x for x in ast.walk(tree) if isinstance(x, ast.ClassDef)):
            classes[c.name] = [f for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                               for f in funcs.get(m.name, []) if f.node is m]
        mods.append((p.stem, funcs, classes, decl))
    uniq: dict[str, list[_Fn]] = {}
    for _m, funcs, _c, _d in mods:
        for name, fl in funcs.items():
            uniq.setdefault(name, []).extend(fl)
    out: dict[str, TaskInfo] = {}
    for modname, funcs, classes, decl in mods:
        for info, root in decl:
            seen, stack = {id(root)}, [root]
            keys: set[str] = set()
            dyn = False
            while stack:
                f = stack.pop()
                keys |= f.reads
                dyn = dyn or f.dyn
                for ref in f.refs:
                    nxt = list(funcs.get(ref, [])) + list(classes.get(ref, []))
                    if not nxt and len(uniq.get(ref, [])) == 1 and ref.startswith("_"):
                        nxt = uniq[ref]
                    for g in nxt:
                        if id(g) not in seen:
                            seen.add(id(g))
                            stack.append(g)
            out[info["name"]] = TaskInfo(info["name"], info["klass"], info["tier"], info["title"], modname, keys, dyn)
    _CATALOG_CACHE["sig"], _CATALOG_CACHE["val"] = sig, out
    return out


# =========================================================================== the compiler
@dataclass
class Compiled:
    docs: dict[str, dict] = field(default_factory=dict)        # legacy file -> data
    texts: dict[str, str] = field(default_factory=dict)        # legacy file -> generated TOML text
    shas: dict[str, str] = field(default_factory=dict)         # legacy file -> sha256 of the text
    writers: dict = field(default_factory=dict)                # (file, path tuple) -> rule id that wrote that scalar/list/table key
    lists: dict = field(default_factory=dict)                  # (file, path tuple) -> [(first index, rule id)] list contributions
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tprov: dict = field(default_factory=dict, repr=False)      # id(table) -> [rule ids]   (provenance comments; the objects live in docs)
    lprov: dict = field(default_factory=dict, repr=False)      # id(list) -> [(first index, rule id)]


class _CompileError(Exception):
    pass


def _tag(c: Compiled, tbl: dict, rid: str) -> None:
    ids = c.tprov.setdefault(id(tbl), [])
    if rid not in ids:
        ids.append(rid)


def _depths(rules: list[Rule], errs: list[str]) -> dict[str, int]:
    by = {r.id: r for r in rules}
    memo: dict[str, int] = {}

    def depth(r: Rule, stack: tuple) -> int:
        if r.id in memo:
            return memo[r.id]
        ref, _ = parse_target(r.target)
        d = 0
        if ref:
            p = by.get(ref)
            if p is None:
                errs.append(f"rule {r.id}: target refers to unknown rule {ref!r}")
            elif ref in stack or r.id in stack:
                errs.append(f"rule {r.id}: relative targets form a cycle ({' -> '.join(stack + (r.id,))})")
            elif not parse_target(p.target)[1] or not parse_target(p.target)[1][-1][1]:
                errs.append(f"rule {r.id}: {ref!r} does not create an array element (its target must end in `[]`)")
            elif p.file != r.file:
                errs.append(f"rule {r.id}: {ref!r} writes {p.file}, this rule writes {r.file}")
            else:
                d = 1 + depth(p, stack + (r.id,))
        memo[r.id] = d
        return d
    for r in rules:
        depth(r, ())
    return memo


def compile_registry(reg: Registry) -> Compiled:
    """Deterministic compile of every enabled rule into the generated legacy files. Errors never raise: they fill .errors and leave
    .texts empty, so the caller keeps the last good files."""
    c = Compiled()
    rules = [r for r in reg.rules if r.file]
    depth = _depths(rules, c.errors)
    if c.errors:
        return c
    rules.sort(key=lambda r: (depth[r.id], r.source, r.order, r.id))
    elems: dict[str, tuple[dict, tuple]] = {}
    off: set[str] = set()                      # element-creating rules that are disabled: their children vanish with them
    for r in rules:
        c.docs.setdefault(r.file, {})
    for r in rules:
        ref, segs = parse_target(r.target)
        creates = bool(segs) and segs[-1][1]
        try:
            if ref and ref in off:                     # the parent element is compiled out: so is everything inside it
                if creates:
                    off.add(r.id)
                continue
            if not r.enabled:
                if creates:
                    off.add(r.id)
                elif r.file == "maint.toml" and not ref and r.merge == "set" and len(segs) == 2 and segs[0][0] == "tasks":
                    tbl, path = _walk(c, c.docs[r.file], (), segs, r)[1:]      # a disabled task keeps only `enabled = false`
                    _put(c, r, tbl, path, "enabled", False)
                    _tag(c, tbl, r.id)
                continue
            if ref:
                if ref not in elems:
                    raise _CompileError(f"its parent {ref!r} could not be created (see that rule's own error)")
                base, bpath = elems[ref]
            else:
                base, bpath = c.docs[r.file], ()
            kind, tbl, path = _walk(c, base, bpath, segs, r)
            if kind == "elem":
                _append_elem(c, r, tbl, path, elems)
            else:
                if r.merge == "set" or r.mode:               # an append rule is named per run of list items, not above the table
                    _tag(c, tbl, r.id)
                if r.mode:
                    _put(c, r, tbl, path, "mode", r.mode)
                (_merge_append if r.merge == "append" else _merge_set)(c, r, tbl, path, r.params)
        except _CompileError as exc:
            c.errors.append(f"rule {r.id}: {exc}")
    if c.errors:
        c.docs.clear()
        return c
    for f, doc in c.docs.items():
        try:
            body = dumps(doc, tprov=c.tprov, lprov=c.lprov)
            text = HEADER + ("\n" + body if body.strip() else "")
            back = tomllib.loads(text)
        except (ValueError, tomllib.TOMLDecodeError, RecursionError) as exc:
            c.errors.append(f"{f}: the generated text is not valid TOML ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        if not same(back, doc):
            c.errors.append(f"{f}: internal error: the generated text does not re-parse to the compiled data "
                            f"({'; '.join(p for p, _a, _b in diff_docs(doc, back)[:3])})")
            continue
        c.texts[f], c.shas[f] = text, sha_bytes(text.encode())
    if c.errors:
        c.texts.clear()
        c.shas.clear()
        c.docs.clear()
    return c


def _owner(c: Compiled, r: Rule, path: tuple) -> str:
    w = c.writers.get((r.file, path))
    return f" (written by {w})" if w else ""


def _walk(c: Compiled, base: dict, bpath: tuple, segs: list, r: Rule) -> tuple[str, Any, tuple]:
    """Resolve the target below `base`: ("table", dict, path) or ("elem", (parent table, key), path of the parent)."""
    cur, path = base, bpath
    for i, (key, lst) in enumerate(segs):
        if lst:
            arr = cur.get(key)
            if arr is None:
                arr = cur[key] = []
            elif not (isinstance(arr, list) and all(isinstance(x, dict) for x in arr)):
                raise _CompileError(f"{_dotted(path + (key,))} is not an array of tables{_owner(c, r, path + (key,))}")
            return "elem", (cur, key), path
        nxt = cur.get(key)
        if nxt is None:
            nxt = cur[key] = {}
        elif not isinstance(nxt, dict):
            raise _CompileError(f"{_dotted(path + (key,))} is not a table{_owner(c, r, path + (key,))}")
        cur, path = nxt, path + (key,)
    return "table", cur, path


def _put(c: Compiled, r: Rule, tbl: dict, path: tuple, key: str, val: Any) -> None:
    if key in tbl:
        raise _CompileError(f"{r.file} {_dotted(path + (key,))} is already set{_owner(c, r, path + (key,))}: two rules cannot write the "
                            "same key")
    tbl[key] = copy.deepcopy(val)
    c.writers[(r.file, path + (key,))] = r.id


def _merge_set(c: Compiled, r: Rule, tbl: dict, path: tuple, params: dict) -> None:
    for k, v in params.items():
        if isinstance(v, dict):
            cur = tbl.get(k)
            if cur is None:
                cur = tbl[k] = {}
                c.writers[(r.file, path + (k,))] = r.id
            elif not isinstance(cur, dict):
                raise _CompileError(f"{r.file} {_dotted(path + (k,))} is already set to a value{_owner(c, r, path + (k,))}")
            _merge_set(c, r, cur, path + (k,), v)
        else:
            _put(c, r, tbl, path, k, v)


def _merge_append(c: Compiled, r: Rule, tbl: dict, path: tuple, params: dict) -> None:
    for k, v in params.items():
        cur = tbl.get(k)
        if cur is None:
            cur = tbl[k] = []
        elif not isinstance(cur, list):
            raise _CompileError(f"{r.file} {_dotted(path + (k,))} is not a list{_owner(c, r, path + (k,))}")
        start = len(cur)
        for item in v:
            if not any(same(item, x) for x in cur):
                cur.append(copy.deepcopy(item))
        if len(cur) > start or not v:
            c.lprov.setdefault(id(cur), []).append((start, r.id))
            c.lists.setdefault((r.file, path + (k,)), []).append((start, r.id))


def _append_elem(c: Compiled, r: Rule, loc: tuple, path: tuple, elems: dict) -> None:
    parent, key = loc
    arr = parent[key]
    new = copy.deepcopy(r.params)
    idx = len(arr)
    arr.append(new)
    elems[r.id] = (new, path + (key, idx))
    _tag(c, new, r.id)
    c.lists.setdefault((r.file, path + (key,)), []).append((idx, r.id))
    for k in new:
        c.writers[(r.file, path + (key, idx, k))] = r.id


# =========================================================================== references and invariants
def known_names(comp: Compiled, catalog: dict[str, TaskInfo]) -> set[str]:
    """Every task / job / probe / routine / service name the registry or the code defines (what applies_to may name)."""
    names = set(catalog) | set(RUNTIME_TASKS) | set((comp.docs.get("maint.toml", {}).get("tasks") or {}))
    d = comp.docs
    for e in (d.get("jobs.toml", {}).get("job") or []) + (d.get("jobs.toml", {}).get("external") or []):
        names.add(e.get("name"))
    pr = d.get("probes.toml", {})
    for e in pr.get("probe") or []:
        names.add(e.get("name"))
    for g in pr.get("group") or []:
        names.add(g.get("group"))
        names |= {p.get("name") for p in g.get("probes") or [] if isinstance(p, dict)}
    for e in (d.get("routine.toml", {}).get("routine") or []) + (d.get("routine.toml", {}).get("system") or []):
        names.add(e.get("name"))
    names |= {s.get("name") for s in (d.get("maint.toml", {}).get("live", {}).get("services") or []) if isinstance(s, dict)}
    return {n for n in names if isinstance(n, str)}


def check_references(reg: Registry, comp: Compiled, catalog: dict[str, TaskInfo]) -> tuple[list[str], list[str]]:
    errs, warns = [], []
    names = known_names(comp, catalog)
    for r in reg.rules:
        for n in r.applies_to:
            if n not in names:
                errs.append(f"{r.source}: rule {r.id}: applies_to {n!r} is not a task, job or probe{_close(n, names)}")
    tasks = comp.docs.get("maint.toml", {}).get("tasks") or {}
    for t, tbl in sorted(tasks.items()):
        if catalog and t not in catalog and t not in RUNTIME_TASKS and not t.startswith("routine_verify_"):
            who = comp.writers.get(("maint.toml", ("tasks", t)))
            warns.append(f"{('rule ' + who + ': ') if who else ''}[tasks.{t}] configures a task that does not exist{_close(t, catalog)}")
            continue
        info = catalog.get(t)
        if not info or info.open or not isinstance(tbl, dict):
            continue
        for k in tbl:
            if k not in info.keys and k not in COMMON_TASK_KEYS:
                who = comp.writers.get(("maint.toml", ("tasks", t, k))) or (comp.lists.get(("maint.toml", ("tasks", t, k))) or [(0, "?")])[0][1]
                warns.append(f"rule {who}: unknown option {k!r} for task {t}{_close(k, info.keys)}")
    return errs, warns


def _regex_bad(pats: Any) -> list[str]:
    out = []
    for p in pats if isinstance(pats, list) else []:
        if isinstance(p, str):
            try:
                re.compile(p)
            except re.error as exc:
                out.append(f"{p[:50]!r}: {exc}")
    return out


def _limit_values(doc: Any, parts: list[str], path: tuple = ()) -> Iterable[tuple[str, Any]]:
    if not parts:
        yield _dotted(path), doc
    elif isinstance(doc, dict):
        if parts[0] == "*":
            for k in sorted(doc):
                yield from _limit_values(doc[k], parts[1:], path + (k,))
        elif parts[0] in doc:
            yield from _limit_values(doc[parts[0]], parts[1:], path + (parts[0],))


def _paths_in(v: Any, key: str = "") -> list[str]:
    """Every filesystem path a rule's params name (path/dir/root, paths/dirs/roots lists, anywhere below)."""
    out: list[str] = []
    if isinstance(v, dict):
        for k, x in v.items():
            if k in ("path", "dir", "root") and isinstance(x, str):
                out.append(x)
            else:
                out += _paths_in(x, k)
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, str) and key in ("paths", "dirs", "roots"):
                out.append(x)
            else:
                out += _paths_in(x, key)
    return out


# --------------------------------------------------------------------------- the release floor (pinned in the code, not in /etc)
PROTECTED_FLOOR = (
    "surreal", "postgres", "mariadb", "mysql", "qdrant", "meilisearch", "redis", "grimmory-db", "tday_db", "immich_postgres",
    "comfyui", "ollama", "plexmediaserver", "plex media", "plex transcoder", "immich", "open-notebook", "open_notebook",
    "infinity", "omnivoice", "speaches", "kokoro", "ebook2audiobook", "tunarr", "kometa", "sabnzbd", "radarr", "sonarr",
    "prowlarr", "bazarr", "buildkitd", "docker build", "buildctl", "dockerd", "containerd", "systemd", "sshd", "cloudflared",
    "tailscaled", "xorg", "xwayland", "gnome-shell", "networkmanager", "homarr", "uptime-kuma", "glances", "fail2ban", "smartd",
    "libvirt", "qemu", "claude", "/var/snap/plexmediaserver", "/media/SandiskSSD/plex", "/usr/share/ollama",
    "/volume1/docker/comfyui/models", "/var/lib/docker/volumes", "surreal_data", "notebook_data", "/mnt/backup", "/media/Immich",
    "pgdata")
# Every `unprotect` regex the release knows: (file, dotted path of the list, regexes, why). Anything else needs the owner's override.
UNPROTECT_ALLOW = (
    ("maint.toml", "tasks.app_cache_trim.unprotect", ("^/home/ohmz/StudioProjects/tunarr/\\.docker-data/tunarr/cache/subtitles(/|$)",),
     "a disposable subtitle cache inside the protected tunarr app dir"),
    ("maint.toml", "tasks.caps.unprotect", ("^tunarr-host-net$", "^kavita$", "^sabnzbd$", "^omnivoice-studio-gpu$", "^comfyui$"),
     "a memory ceiling is not a kill"),
    ("maint.toml", "tasks.openwebui_media_prune.unprotect", ("^/volume1/docker/comfyui/output/owui_",), "allow-listed generated media"),
    ("maint.toml", "tasks.comfyui_idle_reclaim.unprotect", ("^comfyui$",), "the port of comfyui-idle-vram.sh restarts ComfyUI"),
    ("maint.toml", "tasks.immich_recycle.unprotect", ("^immich_server$",), "the port of immich-server-recycle.service restarts the server"),
    ("maint.toml", "tasks.c2_candidates.unprotect", ("StudioProjects/kometa/backups/config-backup-",), "one stale tarball inside the protected kometa dir"),
    ("classes.toml", "ladder.throttle_unprotect",
     ("^(comfyui|tunarr-host-net|Sabnzbd|Radarr|Sonarr|sonarr-status|prowlarr|bazarr|kometa|immich_machine_learning)$",
      "^(omnivoice-studio-gpu|ebook2audiobook-ebook2audiobook-gpu-1|kokoro|infinity-rerank|open-notebook-open_notebook-1|open-notebook-speaches-1)$"),
     "L3 only lowers cpu-shares of batch containers"),
    ("classes.toml", "ladder.qos_unprotect",
     ("^(immich_server|homarr|uptime-kuma|immich-public-proxy)$",
      "^(immich_postgres|immich_redis|nextcloud_postgres|nextcloud_redis|tday_db|tday_ollama|owui-public-ollama)$"),
     "qos_classes only raises P1 weights"))
# [tasks.X] keys that may hold the word "apply" besides `mode` (set through params; the rule must be destructive): the ladder's rung switches.
APPLY_KEYS = (("pressure_response", "reclaim"), ("pressure_response", "throttle"), ("pressure_response", "restart"), ("pressure_response", "emergency"))
APPLY_FLAGS = frozenset({"enforce", "apply_generic", "allow_manual_check_items"})       # boolean switches that make a task act / skip a human check
FLOOR_ENFORCED = True            # tests switch it off to exercise a custom mirror in isolation; production never does
EMPTY_FLOOR: dict = {"protected_patterns": [], "never_touch": [], "min_root_depth": 1, "limit": [], "unprotect": None, "apply_keys": None}


def package_floor() -> dict:
    """The release baseline pinned in this module (a root-owned install): the mirror in rules.d can only add to it."""
    if not FLOOR_ENFORCED:
        return copy.deepcopy(EMPTY_FLOOR)
    return {"protected_patterns": list(PROTECTED_FLOOR), "never_touch": list(NEVER_TOUCH), "min_root_depth": MIN_ROOT_DEPTH,
            "limit": [{"file": f, "path": p, "max": m, "why": w} for f, p, m, w in LIMITS],
            "unprotect": [{"file": f, "path": p, "allow": list(a), "why": w} for f, p, a, w in UNPROTECT_ALLOW],
            "apply_keys": [{"task": t, "key": k, "why": "the ladder rung switches"} for t, k in APPLY_KEYS]}


def _unp_map(entries: Iterable[dict]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for u in entries:
        out.setdefault((u["file"], u["path"]), [])
        out[(u["file"], u["path"])] += [x for x in u["allow"] if x not in out[(u["file"], u["path"])]]
    return out


def baseline_sha(eff: dict) -> str:
    keys = ("protected_patterns", "never_touch", "min_root_depth", "limit", "unprotect", "apply_keys")
    return hashlib.sha256(json.dumps({k: eff.get(k) for k in keys}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def effective_baseline(mirror: dict, pkg: dict, allowed: Iterable[str] = ()) -> tuple[dict, list[str]]:
    """(effective baseline, ways the mirror is WEAKER than the release floor). Effective = strictest of both: union of protected patterns and
    never-touch regexes, lowest of every limit, highest min_root_depth, intersection of the allow-lists. `allowed` (owner override) excuses
    protected patterns only."""
    allowed = set(allowed)
    weak: list[str] = []

    def union(a: Iterable[str], b: Iterable[str]) -> list[str]:
        return list(dict.fromkeys([*a, *b]))
    for p in pkg["protected_patterns"]:
        if p not in mirror["protected_patterns"] and p not in allowed:
            weak.append(f"protected pattern {p!r} is missing")
    for p in pkg["never_touch"]:
        if p not in mirror["never_touch"]:
            weak.append(f"never-touch regex {p[:50]!r} is missing")
    if mirror["min_root_depth"] < pkg["min_root_depth"]:
        weak.append(f"min_root_depth {mirror['min_root_depth']} is below {pkg['min_root_depth']}")
    pk = {(x["file"], x["path"]): x for x in pkg["limit"]}
    mk = {(x["file"], x["path"]): x for x in mirror["limit"]}
    limits = []
    for key in dict.fromkeys([*pk, *mk]):
        a, b = pk.get(key), mk.get(key)
        if a and not b:
            weak.append(f"limit {key[0]} {key[1]} is missing")
        elif a and b and b["max"] > a["max"]:
            weak.append(f"limit {key[0]} {key[1]} max {b['max']} is above {a['max']}")
        src = min((x for x in (a, b) if x), key=lambda x: x["max"])
        limits.append({"file": key[0], "path": key[1], "max": src["max"], "why": src.get("why", "")})
    if pkg["unprotect"] is None:
        unp = [dict(u) for u in mirror["unprotect"]]
    else:
        pm, mm = _unp_map(pkg["unprotect"]), _unp_map(mirror["unprotect"])
        for loc, rxs in mm.items():
            extra = [x for x in rxs if x not in pm.get(loc, [])]
            if extra:
                weak.append(f"unprotect allow-list {loc[0]} {loc[1]} lists {extra[0][:50]!r}, which the release does not")
        unp = [{"file": loc[0], "path": loc[1], "allow": [x for x in rxs if x in mm.get(loc, [])], "why": ""} for loc, rxs in pm.items() if loc in mm]
    if pkg["apply_keys"] is None:
        apk = [dict(a) for a in mirror["apply_keys"]]
    else:
        pa = {(a["task"], a["key"]) for a in pkg["apply_keys"]}
        for a in mirror["apply_keys"]:
            if (a["task"], a["key"]) not in pa:
                weak.append(f"apply key {a['task']}.{a['key']} is not in the release baseline")
        apk = [dict(a) for a in pkg["apply_keys"] if any((m["task"], m["key"]) == (a["task"], a["key"]) for m in mirror["apply_keys"])]
    eff = {"version": mirror.get("version", 1), "derived_from": mirror.get("derived_from", ""),
           "protected_patterns": union(pkg["protected_patterns"], mirror["protected_patterns"]),
           "never_touch": union(pkg["never_touch"], mirror["never_touch"]),
           "min_root_depth": max(pkg["min_root_depth"], mirror["min_root_depth"]), "limit": limits, "unprotect": unp, "apply_keys": apk}
    eff["sha"] = baseline_sha(eff)
    return eff, weak


def baseline_diff(old: dict | None, new: dict | None) -> dict:
    """What changed in the EFFECTIVE baseline between two applies: {"weakened": [...], "strengthened": [...]} (short phrases)."""
    out: dict[str, list[str]] = {"weakened": [], "strengthened": []}
    if not old or not new:
        return out

    def sets(key: str) -> tuple[set, set]:
        return set(old.get(key) or []), set(new.get(key) or [])
    for key, word in (("protected_patterns", "protected pattern"), ("never_touch", "never-touch regex")):
        a, b = sets(key)
        out["weakened"] += [f"{word} {x[:60]!r} removed" for x in sorted(a - b)]
        out["strengthened"] += [f"{word} {x[:60]!r} added" for x in sorted(b - a)]
    if new.get("min_root_depth", 0) < old.get("min_root_depth", 0):
        out["weakened"].append(f"min_root_depth {old['min_root_depth']} -> {new['min_root_depth']}")
    elif new.get("min_root_depth", 0) > old.get("min_root_depth", 0):
        out["strengthened"].append(f"min_root_depth {old['min_root_depth']} -> {new['min_root_depth']}")
    ol = {(x["file"], x["path"]): x["max"] for x in old.get("limit") or []}
    nl = {(x["file"], x["path"]): x["max"] for x in new.get("limit") or []}
    for k in sorted(set(ol) | set(nl)):
        if k not in nl or (k in ol and nl[k] > ol[k]):
            out["weakened"].append(f"limit {k[1]} {ol.get(k)} -> {nl.get(k, 'none')}")
        elif k not in ol or nl[k] < ol[k]:
            out["strengthened"].append(f"limit {k[1]} {ol.get(k, 'none')} -> {nl[k]}")
    om, nm = _unp_map(old.get("unprotect") or []), _unp_map(new.get("unprotect") or [])
    for loc in sorted(set(om) | set(nm)):
        a, b = set(om.get(loc, [])), set(nm.get(loc, []))
        out["weakened"] += [f"unprotect {loc[1]} may now hold {x[:50]!r}" for x in sorted(b - a)]
        out["strengthened"] += [f"unprotect {loc[1]} may no longer hold {x[:50]!r}" for x in sorted(a - b)]
    oa, na = {(a["task"], a["key"]) for a in old.get("apply_keys") or []}, {(a["task"], a["key"]) for a in new.get("apply_keys") or []}
    out["weakened"] += [f"apply key {t}.{k} added" for t, k in sorted(na - oa)]
    out["strengthened"] += [f"apply key {t}.{k} removed" for t, k in sorted(oa - na)]
    return out


# --------------------------------------------------------------------------- invariants on the COMPILED documents
TARGET_STR = frozenset({"path", "dir", "root"})
TARGET_LIST = frozenset({"paths", "dirs", "roots"})
_PATHY = re.compile(r"(^|_)(dirs?|paths?|roots?|home|folders?|director(y|ies)|stores|installations)$")      # cache_dir, projects_root, pnpm_stores, ...
_SKIP_KEY = re.compile(r"^(exclude|ref_|archive|never_touch|link_)|unprotect")      # lists that EXCLUDE, reference or receive: not deletion targets
_WILD = re.compile(r"[*?\[{]")


def _nt_head(rx: str) -> tuple[list[str], list[str] | None] | None:
    """Where an ANCHORED never-touch regex starts: ([components, "*" for a [^/]+ part], the literal alternatives of the group that follows
    when the regex lists them, e.g. (docker|libvirt) -> None otherwise). None for a regex that is not anchored at an absolute path."""
    if not rx.startswith("^/"):
        return None
    i, comps, cur = 1, [], ""
    while i < len(rx):
        c = rx[i]
        if c == "/":
            if cur:
                comps.append(cur)
            cur, i = "", i + 1
        elif rx.startswith("[^/]+", i):
            cur, i = cur + "*", i + 5
        elif c == "\\" and i + 1 < len(rx) and rx[i + 1] in ".-_":
            cur, i = cur + rx[i + 1], i + 2
        elif c in "()[]{}.*+?|$^\\":
            break
        else:
            cur, i = cur + c, i + 1
    alts = None
    if cur:
        comps.append(cur)
    elif rx[i:i + 1] == "(":
        body = rx[i + 1:rx.find(")", i)] if ")" in rx[i:] else ""
        if body and re.fullmatch(r"[A-Za-z0-9_.\\|-]+", body):
            alts = [re.sub(r"\\(.)", r"\1", a) for a in body.split("|")]
    return (comps, alts) if comps else None


def _reaches(head: tuple[list[str], list[str] | None], reach: list[str], wild: str) -> bool:
    """Can a glob whose first wildcard segment is `wild`, applied below the literal directory `reach`, select something inside the never-touch
    tree that starts at `head`? Conservative where the regex does not say which names follow (any wildcard may match them)."""
    comps, alts = head
    n, m = len(reach), len(comps)
    if n > m or not all((fnmatch_case(reach[i], comps[i]) if "*" in comps[i] else comps[i] == reach[i]) for i in range(n)):
        return False
    if wild.startswith("**"):
        return True
    if n < m:
        return comps[n] == "*" or fnmatch_case(comps[n], wild)
    return alts is None or any(fnmatch_case(a, wild) for a in alts)


@dataclass
class _Tgt:
    value: Any
    glob: str | None
    strict: bool                    # path/dir/root(s): must lie inside allowed_roots
    path: tuple


def _scan_task(node: Any, path: tuple, out: list[_Tgt], globs: list[tuple[tuple, Any]]) -> None:
    """Every filesystem path (and glob) below a [tasks.X] table, at any depth, with its position in the document. A path is what a
    path-like KEY holds (path, dir, roots, cache_dir, projects_root ...) or any other string that starts with "/": a cleaner's option
    does not become unchecked by having an unusual name."""
    if isinstance(node, dict):
        glob = node.get("glob") if isinstance(node.get("glob"), str) else None
        for k, v in node.items():
            p = path + (k,)
            if not isinstance(k, str) or k == "allowed_roots" or _SKIP_KEY.search(k):
                continue
            strict = k in TARGET_STR or k in TARGET_LIST
            if k == "glob":
                globs.append((p, v))
            elif strict or _PATHY.search(k):
                if isinstance(v, list):
                    out += [_Tgt(x, None, strict, p + (i,)) for i, x in enumerate(v)]
                else:
                    out.append(_Tgt(v, glob if k == "path" else None, strict, p))
            elif isinstance(v, str):
                if v.startswith("/"):
                    out.append(_Tgt(v, None, False, p))
            elif isinstance(v, (dict, list)):
                _scan_task(v, p, out, globs)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            if isinstance(x, str) and x.startswith("/"):
                out.append(_Tgt(x, None, False, path + (i,)))
            else:
                _scan_task(x, path + (i,), out, globs)


def _writer_of(comp: Compiled, file: str, path: tuple) -> str | None:
    """The rule that wrote the key at `path` (the longest recorded prefix; list items resolve through the list's contributions)."""
    for i in range(len(path), 0, -1):
        p = path[:i]
        w = comp.writers.get((file, p))
        if w:
            return w
        if isinstance(p[-1], int):
            cand = [rid for start, rid in comp.lists.get((file, p[:-1]), []) if start <= p[-1]]
            if cand:
                return cand[-1]
    return None


def _confine(task: str, tbl: dict, base: dict, nt: list[tuple[re.Pattern, str]], heads: list,
             who: Callable[[tuple], str], own_roots: bool = False) -> list[str]:
    """Delete confinement of ONE [tasks.X] table: every path it names is absolute, normalised, off the never-touch list and (for the
    path/dir/root keys) inside allowed_roots; globs stay relative. Judged on the compiled table, whatever rule or flag wrote it."""
    bad: list[str] = []
    here = ("tasks", task)
    roots = tbl.get("allowed_roots")
    if "allowed_roots" in tbl:
        for i, rt in enumerate(roots if isinstance(roots, list) else [None]):
            w = who(here + ("allowed_roots", i))
            if not (isinstance(rt, str) and posixpath.isabs(rt) and posixpath.normpath(rt) == rt.rstrip("/") or rt == "/"):
                bad.append(f"{w}allowed_roots entry {str(rt)[:60]!r} must be an absolute, normalised path")
            elif len([x for x in rt.split("/") if x]) < base["min_root_depth"]:
                bad.append(f"{w}allowed_roots entry {rt!r} is too broad (at least {base['min_root_depth']} path components)")
    roots = [x for x in roots if isinstance(x, str)] if isinstance(roots, list) else []
    tg: list[_Tgt] = []
    globs: list[tuple[tuple, Any]] = []
    _scan_task(tbl, here, tg, globs)
    if own_roots:                          # a cleaner without an allowed_roots option (log_compress.roots, ...): its roots ARE its scope
        for t in tg:
            t.strict = False
            if isinstance(t.value, str) and posixpath.isabs(t.value) and len([x for x in t.value.split("/") if x]) < base["min_root_depth"]:
                bad.append(f"{who(t.path)}path {t.value!r} is too broad (at least {base['min_root_depth']} path components)")
    for gp, g in globs:
        if not (isinstance(g, str) and not g.startswith("/") and ".." not in g.split("/") and "\0" not in g):
            bad.append(f"{who(gp)}glob {str(g)[:60]!r} must be relative and stay inside its path (no .., no leading /)")
    if any(t.strict for t in tg) and not roots:
        w = who(next(t.path for t in tg if t.strict))
        bad.append(f"{w}names paths but [tasks.{task}] has no allowed_roots to confine them")
    for t in tg:
        v, w = t.value, who(t.path)
        if not (isinstance(v, str) and posixpath.isabs(v) and "\0" not in v and posixpath.normpath(v) == v and not v.startswith("//")):
            bad.append(f"{w}path {str(v)[:80]!r} must be absolute and normalised (no .., //, trailing /)")
            continue
        g = t.glob if isinstance(t.glob, str) and not t.glob.startswith("/") and ".." not in t.glob.split("/") else None
        segs = [s for s in (g or "").split("/") if s]
        cands = [v] + [v + "/" + "/".join(segs[:i]) for i in range(1, len(segs) + 1)]
        if any(rx.search(c) for rx, _s in nt for c in cands):
            bad.append(f"{w}path {v!r}" + (f" with glob {g!r}" if g else "") + " is on the never-touch list")
            continue
        lit = next((i for i, s in enumerate(segs) if _WILD.search(s)), len(segs))
        if lit < len(segs):                                         # a wildcard that could walk into a never-touch tree
            reach = [x for x in (v + ("/" + "/".join(segs[:lit]) if lit else "")).split("/") if x]
            hit = next((h for h in heads if _reaches(h, reach, segs[lit])), None)
            if hit:
                bad.append(f"{w}glob {g!r} below {v!r} can reach the never-touch tree /{'/'.join(hit[0])}")
        if t.strict and roots and not any(v == rt.rstrip("/") or v.startswith(rt.rstrip("/") + "/") for rt in roots):
            bad.append(f"{w}path {v!r} escapes allowed_roots {roots}")
    return bad


def fnmatch_case(name: str, pattern: str) -> bool:
    return _lazy("fnmatch").fnmatchcase(name, pattern)


def _unprotect_lists(doc: Any, path: tuple = ()) -> Iterable[tuple[tuple, Any]]:
    """Every `unprotect` / `*_unprotect` value of a document with its position."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(k, str) and (k == "unprotect" or k.endswith("_unprotect")):
                yield path + (k,), v
            elif isinstance(v, dict):
                yield from _unprotect_lists(v, path + (k,))


def _apply_leaves(node: Any, path: tuple = (), key: str = "") -> Iterable[tuple[tuple, str, Any]]:
    """(path, key, value) of every "apply" string and every true APPLY_FLAGS switch below a node."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _apply_leaves(v, path + (k,), str(k))
    elif isinstance(node, list):
        for i, x in enumerate(node):
            yield from _apply_leaves(x, path + (i,), key)
    elif node == "apply" and isinstance(node, str):
        yield path, key, node
    elif node is True and key in APPLY_FLAGS:
        yield path, key, node


def doc_invariants(docs: dict[str, dict], base: dict, catalog: dict[str, TaskInfo], *, allowed: Iterable[str] = (),
                   owner_unprotect: Iterable[tuple[str, str, str]] = (), who: Callable[[str, tuple], str] | None = None,
                   need_protected: bool = True) -> tuple[list[str], list[str], list[str]]:
    """(violations, warnings, allowed baseline removals) judged on documents alone: protected superset and type, regex validity, numeric
    ceilings, the unprotect allow-list and delete confinement. Used on the compiled output AND on the files on disk."""
    bad: list[str] = []
    warns: list[str] = []
    removed: list[str] = []
    allowed = set(allowed)
    owner = set(owner_unprotect)
    wf = who or (lambda _f, _p: "")

    def w(f: str, p: tuple) -> str:
        r = wf(f, p)
        return f"rule {r}: " if r else f"{f} {_dotted(p)}: "
    # 1. protected patterns are a superset of the baseline, and a list of strings (a non-string entry makes every ctx.act() raise)
    if "protected.toml" in docs or need_protected:
        raw = (docs.get("protected.toml") or {}).get("patterns", [])
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
            bad.append("protected.toml: patterns must be a list of strings (a non-string entry would make every protection check fail)")
        have = {p for p in raw if isinstance(p, str)} if isinstance(raw, list) else set()
        for p in base["protected_patterns"]:
            if p not in have:
                if p in allowed:
                    removed.append(p)
                    warns.append(f"LOUD: baseline protection {p!r} removed by {OVERRIDES_FILE} allow_baseline_removal")
                else:
                    bad.append(f"protected.toml lost the baseline pattern {p!r}; it cannot shrink silently (the owner may list it in "
                               f"[meta] allow_baseline_removal of {OVERRIDES_FILE})")
        for p in sorted(allowed - set(base["protected_patterns"])):
            warns.append(f"allow_baseline_removal lists {p!r}, which is not a baseline pattern (no effect)")
        for p in _regex_bad(raw):
            bad.append(f"protected.toml: invalid regex {p}")
    # 2. unprotect lists: valid regexes, strings only, and ONLY the ones on the baseline allow-list (or the owner's explicit override)
    allow = {k: set(v) for k, v in _unp_map(base["unprotect"]).items()}
    for f, doc in sorted(docs.items()):
        for p, lst in _unprotect_lists(doc):
            where = _dotted(p)
            if not isinstance(lst, list):
                bad.append(f"{w(f, p)}unprotect must be a list of regex strings (a bare string would be read character by character)")
                continue
            for i, rx in enumerate(lst):
                if not isinstance(rx, str):
                    bad.append(f"{w(f, p + (i,))}unprotect entry {str(rx)[:40]!r} is not a string")
                    continue
                try:
                    re.compile(rx)
                except re.error as exc:
                    label = f"[tasks.{p[1]}] {p[2]}" if f == "maint.toml" and len(p) == 3 and p[0] == "tasks" else where
                    bad.append(f"{f} {label}: invalid regex {rx[:50]!r}: {exc}")
                    continue
                if rx in allow.get((f, where), ()):
                    continue
                if (f, where, rx) in owner:
                    warns.append(f"LOUD: unprotect {rx!r} in {f} {where} accepted by {OVERRIDES_FILE} allow_unprotect")
                    continue
                exposed = [pt for pt in base["protected_patterns"] if re.search(rx, pt, re.I)][:4]
                bad.append(f"{w(f, p + (i,))}unprotect {rx!r} in {f} {where} is not on the baseline allow-list; it would exempt from protected.toml"
                           + (f" ({', '.join(exposed)} ...)" if exposed else "") + f" (the owner may accept it in {OVERRIDES_FILE} allow_unprotect)")
    tasks = (docs.get("maint.toml") or {}).get("tasks") or {}
    for t, tbl in sorted(tasks.items()) if isinstance(tasks, dict) else ():
        if isinstance(tbl, dict) and "never_touch" in tbl:
            lst = tbl["never_touch"]
            for p in _regex_bad(lst):
                bad.append(f"maint.toml [tasks.{t}] never_touch: invalid regex {p}")
    # 3. numeric ceilings (per-run caps <= hard limits, ...)
    for lim in base["limit"]:
        doc = docs.get(lim["file"])
        for where, v in _limit_values(doc, lim["path"].split(".")) if doc else ():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                bad.append(f"{lim['file']} {where} = {v!r} must be a number (hard limit {lim['max']})")
            elif v > lim["max"]:
                bad.append(f"{lim['file']} {where} = {v} exceeds the hard limit {lim['max']}" + (f" ({lim['why']})" if lim["why"] else ""))
    # 4. delete confinement of every delete-type task (class C1, or a task nobody declared), whatever wrote it
    nt: list[tuple[re.Pattern, str]] = []
    for p in base["never_touch"]:
        try:
            nt.append((re.compile(p), p))
        except re.error:
            bad.append(f"baseline never_touch: invalid regex {p[:50]!r}")
    heads = [h for h in (_nt_head(p) for _rx, p in nt) if h]
    for t, tbl in sorted(tasks.items()) if isinstance(tasks, dict) else ():
        info = catalog.get(t)
        if not isinstance(tbl, dict) or tbl.get("enabled") is False or (info and info.klass in ("C0", "C2")):
            continue                              # C0 never mutates; C2 only plans (a human approves the exact plan hash)
        own = bool(info) and "allowed_roots" not in info.keys and "allowed_roots" not in tbl
        bad += _confine(t, tbl, base, nt, heads, lambda p, f="maint.toml": w(f, p), own)
    return list(dict.fromkeys(bad)), warns, removed


_SENS = TARGET_STR | TARGET_LIST | {"allowed_roots", "mode", "unprotect"}


def _sens_hits(node: Any, extra: frozenset | set, out: set[str], key: str = "") -> None:
    """Names of the keys below a fragment that carry paths, exemptions or apply switches (a string starting with "/" counts as a path)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and (k in _SENS or k in extra or k in APPLY_FLAGS or k.endswith("_unprotect")
                                       or (_PATHY.search(k) and not _SKIP_KEY.search(k))):
                out.add(k)
            _sens_hits(v, extra, out, str(k))
    elif isinstance(node, list):
        for x in node:
            _sens_hits(x, extra, out, key)
    elif isinstance(node, str) and node.startswith("/") and key and not _SKIP_KEY.search(key):
        out.add(key)


def _rule_tasks(r: Rule, by: dict[str, Rule]) -> list[tuple[str, Any]]:
    """[(task name, the fragment of its table this rule writes)] for a maint.toml rule, followed through relative targets."""
    if r.file != "maint.toml":
        return []
    root, hops = r, 0
    while root is not None and parse_target(root.target)[0] and hops < 20:
        root, hops = by.get(parse_target(root.target)[0]), hops + 1
    segs = [k for k, _l in parse_target(root.target)[1]] if root is not None else []
    if segs[:1] == ["tasks"] and len(segs) >= 2:
        return [(segs[1], r.params)]
    if segs == ["tasks"]:
        return [(t, v) for t, v in r.params.items() if isinstance(v, dict)]
    if not segs and isinstance(r.params.get("tasks"), dict):
        return [(t, v) for t, v in r.params["tasks"].items() if isinstance(v, dict)]
    return []


def rule_invariants(reg: Registry, comp: Compiled, base: dict, catalog: dict[str, TaskInfo]) -> tuple[list[str], list[str]]:
    """(violations, warnings) that need the rules: who owns an `apply`, and whether the rule that touches a cleaner says so."""
    bad: list[str] = []
    warns: list[str] = []
    by = reg.by_id()
    reported: set[str] = set()
    # 5a. a rule's own mode field
    for r in reg.rules:
        if not r.enabled:
            continue
        if r.mode == "apply" and not r.destructive:
            bad.append(f"rule {r.id}: mode = \"apply\" is only allowed on a rule flagged destructive = true (so the registry shows it)")
            reported.add(r.id)
        if isinstance(r.params.get("mode"), str) and r.params["mode"] == "apply":
            bad.append(f"rule {r.id}: do not put mode = \"apply\" in params; use the rule's own mode field (explicit and audited)")
            reported.add(r.id)
        if r.kind == "cleanup" and r.file == "maint.toml" and not r.destructive and r.target.startswith("tasks.") and r.mode != "report":
            t = r.target.split(".", 1)[1]
            if catalog.get(t) and catalog[t].klass in ("C1", "C2") and not r.params.get("mode"):
                warns.append(f"rule {r.id}: cleaner {t} is not flagged destructive")
    # 5b. every "apply" (and every true switch) anywhere under [tasks.*] of the COMPILED maint.toml has an owner that says so
    rung = {(a["task"], a["key"]) for a in base["apply_keys"]}
    tasks = (comp.docs.get("maint.toml") or {}).get("tasks") or {}
    for path, key, val in _apply_leaves(tasks, ("tasks",)) if isinstance(tasks, dict) else ():
        wid = _writer_of(comp, "maint.toml", path)
        wr = by.get(wid) if wid else None
        if wid in reported:
            continue
        who, where = (f"rule {wid}: " if wid else "maint.toml "), _dotted(path)
        if val is True:
            if wr is None or not wr.destructive:
                bad.append(f"{who}{where} = true switches a task to act on its own but the rule is not flagged destructive = true")
        elif len(path) == 3 and key == "mode":
            if wr is None or wr.mode != "apply":
                bad.append(f"{who}{where} = \"apply\" must come from the rule's own mode field (explicit and audited), not from params")
            elif not wr.destructive:
                bad.append(f"{who}{where} = \"apply\" is only allowed on a rule flagged destructive = true")
        elif len(path) == 3 and (path[1], key) in rung:
            if wr is None or not wr.destructive:
                bad.append(f"{who}{where} = \"apply\" switches a ladder rung on but the rule is not flagged destructive = true")
        else:
            bad.append(f"{who}{where} = \"apply\" is hidden below the task table: give the task its own rule and set apply with that rule's mode "
                       "field (the ladder rung keys of the baseline are the only exception)")
    # 5c. a rule that writes paths, unprotect or apply settings of a cleaner must be flagged destructive (the notice and the website read the flag)
    for r in reg.rules:
        if not r.enabled:
            continue
        for t, frag in _rule_tasks(r, by):
            info = catalog.get(t)
            if info and info.klass == "C0":
                continue
            hits: set[str] = set()
            _sens_hits(frag, {k for tt, k in rung if tt == t}, hits)
            if r.mode:
                hits.add("mode")
            if hits and not r.destructive:
                bad.append(f"rule {r.id}: writes {', '.join(sorted(hits))} of cleaner {t} but is not flagged destructive = true "
                           "(paths, protections and apply switches of a cleaner are always destructive settings)")
    return list(dict.fromkeys(bad)), warns


def check_invariants(reg: Registry, comp: Compiled, catalog: dict[str, TaskInfo] | None = None, *, eff: dict | None = None,
                     weak: list[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    """(violations, warnings, baseline removals the owner allowed). A violation blocks the sync and raises an alert."""
    catalog = catalog or {}
    if reg.baseline is None:
        return [f"the baseline {BASELINE_FILE} is missing: the safety invariants cannot be verified (install it with the registry)"], [], []
    if eff is None:
        eff, weak = effective_baseline(reg.baseline, package_floor(), reg.allow_removal)
    bad = [f"{BASELINE_FILE} is weaker than the release baseline ({w}); restore the shipped file (install.sh), a weaker floor is never applied"
           for w in (weak or [])[:6]]
    if len(weak or []) > 6:
        bad.append(f"{BASELINE_FILE}: {len(weak) - 6} more weaker values")
    docs = dict(comp.docs)
    docs.setdefault("protected.toml", {})
    owner = [(u["file"], u["path"], u["regex"]) for u in reg.allow_unprotect]
    b1, warns, removed = doc_invariants(docs, eff, catalog, allowed=reg.allow_removal, owner_unprotect=owner,
                                        who=lambda f, p: _writer_of(comp, f, p) or "")
    b2, w2 = rule_invariants(reg, comp, eff, catalog)
    return list(dict.fromkeys(bad + b1 + b2)), warns + w2, removed


# =========================================================================== what the consumers think of the compiled files
def _core_shape(docs: dict[str, dict]) -> list[str]:
    """What core.load_config / Ctx rely on: numeric caps, tables that are tables, protected patterns that are strings."""
    out: list[str] = []
    m = docs.get("maint.toml") or {}
    for k in ("global", "caps", "tasks"):
        if k in m and not isinstance(m[k], dict):
            out.append(f"maint.toml: [{k}] must be a table")
    for k, v in (m.get("caps") or {}).items() if isinstance(m.get("caps"), dict) else ():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            out.append(f"maint.toml: caps.{k} must be a number")
    for t, tbl in (m.get("tasks") or {}).items() if isinstance(m.get("tasks"), dict) else ():
        if not isinstance(tbl, dict):
            out.append(f"maint.toml: [tasks.{t}] must be a table")
            continue
        for k in ("max_gib_per_run", "max_items_per_run"):
            if k in tbl and (isinstance(tbl[k], bool) or not isinstance(tbl[k], (int, float))):
                out.append(f"maint.toml: tasks.{t}.{k} must be a number")
        if "mode" in tbl and tbl["mode"] not in ("report", "apply", "off"):
            out.append(f"maint.toml: tasks.{t}.mode must be report or apply")
    p = (docs.get("protected.toml") or {}).get("patterns", [])
    if not isinstance(p, list) or not all(isinstance(x, str) for x in p):
        out.append("protected.toml: patterns must be a list of strings")
    return out


@contextlib.contextmanager
def _conf_as(d: Path):
    """The consumers read core.CONF_DIR at call time: point it at the private copy while they run (the sync holds the lock, one thread)."""
    old = core.CONF_DIR
    core.CONF_DIR = d
    try:
        yield
    finally:
        core.CONF_DIR = old


def consumer_problems(files: dict[str, str]) -> list[str]:
    """Run the runner's OWN loaders over a set of config texts (file name -> text) in a private temp dir and return what they complain about,
    prefixed with the loader. Never raises: a loader that crashes is itself reported (so a change that crashes it is refused)."""
    out: list[str] = []
    try:
        td = Path(_lazy("tempfile").mkdtemp(prefix="hm-validate-"))
    except OSError as exc:                                      # no scratch space: the same finding for old and new files, so it blocks nothing
        return [f"validation sandbox unavailable ({type(exc).__name__})"]
    docs: dict[str, dict] = {}
    try:
        for name, text in files.items():
            fd = os.open(td / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                docs[name] = tomllib.loads(text)
            except (tomllib.TOMLDecodeError, ValueError) as exc:
                out.append(f"{name}: not valid TOML ({str(exc)[:60]})")
        out += _core_shape(docs)

        def run(label: str, fn: Callable[[], list[str]]) -> None:
            try:
                out.extend(f"{label}: {m}" for m in fn())
            except Exception as exc:  # noqa: BLE001 - a crashing loader is a finding, not a crash of the sync
                out.append(f"{label}: the loader crashed ({type(exc).__name__}: {str(exc)[:80]})")

        def jobs_check() -> list[str]:
            from . import jobs, scheduler
            cfg = jobs.load(td / "jobs.toml", {"tasks": (docs.get("maint.toml") or {}).get("tasks") or {}}, apply_modes=False, enforce_trust=False)
            return list(scheduler.validate(cfg, check_paths=False))

        def probes_check() -> list[str]:
            from . import probes
            if "probes.toml" not in docs:
                return []
            with _conf_as(td):
                return list(probes.load_probes()[2])

        def routine_check() -> list[str]:
            from . import routine
            if "routine.toml" not in docs:
                return []
            rc = routine.load_config(td / "routine.toml")
            return list(rc.errors) + ([] if rc.valid else ["the routine config is invalid: every apply would be refused"])

        def notify_check() -> list[str]:
            from . import notify
            err = notify.load_config({"notify": docs.get("notify.toml") or {}}).get("_config_error")
            return [str(err)] if err else []

        def acks_check() -> list[str]:
            from . import acks
            if "ack.toml" not in docs:
                return []
            with _conf_as(td):
                return list(acks.validate())

        def classes_check() -> list[str]:
            if "classes.toml" not in docs:
                return []
            from .tasks import pressure
            return [] if pressure.ClassMap(docs["classes.toml"]).ok else ["the class map is unusable (the ladder's throttle/restart/stop rungs would refuse to act)"]
        for label, fn in (("jobs", jobs_check), ("probes", probes_check), ("routine", routine_check), ("notify", notify_check),
                          ("ack", acks_check), ("classes", classes_check)):
            run(label, fn)
    finally:
        _lazy("shutil").rmtree(td, ignore_errors=True)
    return [m.replace(str(td), "<conf>") for m in out]


def _current_texts(conf: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in MANAGED_FILES:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            out[name] = _read_regular(conf / name, 8 << 20).decode("utf-8")
    return out


def new_consumer_problems(comp: Compiled, conf: Path) -> list[str]:
    """Problems the consumers have with the compiled files that they do NOT already have with the files now in force."""
    cur = _current_texts(conf)
    before = set(consumer_problems(cur))
    return [p for p in consumer_problems({**cur, **comp.texts}) if p not in before]


def fill_orphans(comp: Compiled, conf: Path, gen: dict) -> list[str]:
    """A managed file this registry wrote earlier (listed in current.json "generated", or still carrying the GENERATED header) that no rule
    writes any more becomes an EMPTY generated document: removing the last rule of a file really removes its effect."""
    filled = []
    for name in MANAGED_FILES:
        if name in comp.docs:
            continue
        ours = name in gen
        if not ours:
            with contextlib.suppress(OSError):
                ours = _is_generated(_read_regular(conf / name, 8 << 20))
        if ours:
            comp.docs[name], comp.texts[name] = {}, HEADER
            comp.shas[name] = sha_bytes(HEADER.encode())
            filled.append(name)
    return filled


# =========================================================================== analysis = load + compile + references + invariants
@dataclass
class Analysis:
    reg: Registry
    comp: Compiled | None
    errors: list[str]
    warnings: list[str]
    removals: list[str]
    baseline: dict | None = None                 # the EFFECTIVE baseline (release floor + mirror)

    @property
    def ok(self) -> bool:
        return self.reg.present and not self.errors and self.comp is not None and bool(self.comp.texts or not self.comp.docs)


def analyze(conf_dir: Path | str | None = None, *, rdir: Path | None = None, trust: bool = True,
            catalog: dict[str, TaskInfo] | None = None, scanned: tuple | None = None, consumers: bool = False) -> Analysis:
    """Everything `rules check` reports. A registry with ANY load error is not compiled (it would be an incomplete rule set).
    `consumers` also runs the runner's own loaders (jobs, probes, routine, notify, ack, classes) over the compiled files."""
    reg = load_registry(conf_dir, rdir=rdir, trust=trust, scanned=scanned)
    errors, warnings = list(reg.errors), list(reg.warnings)
    if not reg.present or errors:
        return Analysis(reg, None, errors, warnings, [])
    comp = compile_registry(reg)
    errors += comp.errors
    if comp.errors:
        return Analysis(reg, None, errors, warnings, [])
    cat = catalog if catalog is not None else task_catalog(conf_dir)
    e, w = check_references(reg, comp, cat)
    errors += e
    warnings += w
    eff, weak = effective_baseline(reg.baseline, package_floor(), reg.allow_removal) if reg.baseline is not None else (None, [])
    bad, w2, removed = check_invariants(reg, comp, cat, eff=eff, weak=weak)
    errors += bad
    warnings += w2
    if consumers and not errors:
        errors += [f"{p} (the runner would not accept the compiled file)" for p in new_consumer_problems(comp, _conf(conf_dir))]
    return Analysis(reg, comp, errors, warnings, removed, eff)


# =========================================================================== change tracking (STATE_DIR/rules)
def rules_state(state_dir: Path | str | None = None) -> Path:
    return _state(state_dir) / "rules"


def read_current(state_dir: Path | str | None = None) -> dict | None:
    try:
        d = json.loads((rules_state(state_dir) / "current.json").read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _cap_val(v: Any, n: int = 300) -> Any:
    try:
        s = json.dumps(v, default=str)
    except (TypeError, ValueError):
        return "<unprintable>"
    return v if len(s) <= n else {"_truncated": len(s), "sha": hashlib.sha1(s.encode()).hexdigest()[:12]}


def snapshot_rules(reg: Registry) -> dict[str, dict]:
    return {r.id: r.as_dict() for r in reg.rules}


def diff_rules(old: dict[str, dict], new: dict[str, dict]) -> dict:
    """{"added":[ids],"removed":[ids],"modified":[{"id","fields":[...],"before":{..},"after":{..}}]}; params are compared per key."""
    out = {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()), "modified": []}
    for rid in sorted(old.keys() & new.keys()):
        o, n = old[rid], new[rid]
        fields: list[str] = []
        before: dict = {}
        after: dict = {}
        for f in sorted(set(o) | set(n)):
            if f == "params":
                op, np_ = o.get("params") or {}, n.get("params") or {}
                for k in sorted(set(op) | set(np_)):
                    if not same(op.get(k, _MISSING), np_.get(k, _MISSING)):
                        fields.append(f"params.{k}")
                        before[f"params.{k}"] = _cap_val(redact(op.get(k), k))      # history and notices are not the place for a secret
                        after[f"params.{k}"] = _cap_val(redact(np_.get(k), k))
            elif not same(o.get(f), n.get(f)):
                fields.append(f)
                before[f], after[f] = _cap_val(o.get(f)), _cap_val(n.get(f))
        if fields:
            out["modified"].append({"id": rid, "fields": fields, "before": before, "after": after})
    return out


@dataclass
class SyncResult:
    status: str                        # unchanged | applied | invalid | blocked | locked | no_registry | error
    hash: str = ""
    previous: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)       # generated files replaced
    drift: list[str] = field(default_factory=list)         # generated files someone had edited (reverted by this sync)
    notified: bool = False
    fresh: bool = False                # something new was recorded by THIS call (a change, a first refusal); False for "already known"
    ms: float = 0.0
    baseline: dict = field(default_factory=dict)           # {"weakened": [...], "strengthened": [...]} of the EFFECTIVE baseline, this apply
    emptied: list[str] = field(default_factory=list)       # files no rule writes any more, written as an empty generated document

    @property
    def changed(self) -> bool:
        return self.status == "applied" and bool(self.added or self.removed or self.modified or self.written)

    def line(self) -> str:
        if self.status == "applied":
            return (f"applied {short(self.hash)}: +{len(self.added)} -{len(self.removed)} ~{len(self.modified)} rules, "
                    f"{len(self.written)} file(s) written" + (f", {len(self.drift)} drifted file(s) repaired" if self.drift else "")
                    + (f", {len(self.emptied)} file(s) emptied (no rule writes them any more)" if self.emptied else ""))
        if self.status in ("invalid", "blocked"):
            return f"{self.status}: the last good config stays in force: {'; '.join(self.errors[:3])}"[:300]
        if self.status in ("error", "locked-too-long"):
            return f"{self.status}: {'; '.join(self.errors[:2])}"[:300]
        return self.status + (f" {short(self.hash)}" if self.hash else "")


@dataclass
class Hooks:
    notify: Callable[[dict], Any] | None = None
    journal: Callable[[dict], Any] | None = None


NO_HOOKS = Hooks()


LOCK_STALE_S = 600.0                      # a sync that holds the lock longer than this is stuck: the tick says so


@contextlib.contextmanager
def _flock(st: Path, wait: bool | float):
    """The sync lock. The holder writes "pid epoch" into the lock file while it holds the lock (a waiter can tell a stuck holder)."""
    d = st / "rules"
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "sync.lock", "a")
    got = False
    try:
        deadline = time.monotonic() + (30.0 if wait is True else float(wait or 0))
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        if got:
            with contextlib.suppress(OSError):
                os.ftruncate(f.fileno(), 0)
                f.write(f"{os.getpid()} {time.time():.0f}\n")
                f.flush()
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                os.ftruncate(f.fileno(), 0)
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _lock_held_for(st: Path, now: float) -> float:
    """Seconds the current holder has had the lock (0 when unknown)."""
    try:
        return max(0.0, now - float((st / "rules" / "sync.lock").read_text().split()[1]))
    except (OSError, ValueError, IndexError):
        return 0.0


def _trusted_all(d: Path, files: list[dict]) -> bool:
    """The directory and every registry file still pass the ownership/permission test (a chmod changes no hash, so the fast path asks)."""
    return _trusted(d / ".") and not d.is_symlink() and all(_trusted(d / f["name"]) for f in files)


def _generated_ok(conf: Path, gen: dict) -> bool:
    """Every generated file exists and has the sha recorded at the last sync (a hand edit or a half-finished replace says no)."""
    for name, sha in gen.items():
        try:
            if sha_bytes(_read_regular(conf / name, 8 << 20)) != sha:
                return False
        except OSError:
            return False
    return True


def _is_generated(raw: bytes) -> bool:
    return raw.lstrip().startswith(GENERATED_MARK.encode())


def _write_snapshot(st: Path, h: str, data: dict[str, bytes], texts: dict[str, str], rules: dict, ts: float, extra: dict) -> None:
    shutil = _lazy("shutil")
    root = st / "rules" / "snapshots"
    d = root / h
    if d.exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{h[:16]}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "rules.d").mkdir(parents=True)
    (tmp / "generated").mkdir()
    for n, b in data.items():
        (tmp / "rules.d" / n).write_bytes(b)
    for n, t in texts.items():
        (tmp / "generated" / n).write_text(t)
    (tmp / "rules.json").write_text(json.dumps(rules, sort_keys=True, default=str))
    (tmp / "meta.json").write_text(json.dumps({"hash": h, "ts": ts, "rules_count": len(rules), **extra}, sort_keys=True))
    try:
        os.rename(tmp, d)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)


def _prune_snapshots(st: Path, keep_hash: str) -> None:
    shutil = _lazy("shutil")
    root = st / "rules" / "snapshots"
    with contextlib.suppress(OSError):
        dirs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        for prefix, keep in (("unapplied-", KEEP_UNAPPLIED), ("", KEEP_SNAPSHOTS)):
            grp = [p for p in dirs if p.name.startswith("unapplied-") == bool(prefix)]
            grp.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for p in grp[keep:]:
                if p.name != keep_hash:
                    shutil.rmtree(p, ignore_errors=True)
        for p in root.glob(".*.tmp-*"):
            shutil.rmtree(p, ignore_errors=True)


def load_snapshot(st: Path, h: str) -> tuple[dict[str, dict], dict] | None:
    """(normalised rules, meta) of an applied snapshot, or None."""
    d = st / "rules" / "snapshots" / h
    try:
        return json.loads((d / "rules.json").read_text()), json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _retired(st: Path) -> dict:
    try:
        d = json.loads((st / "rules" / "retired.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


FLAG_FIELDS = ("destructive", "kind", "mode", "enabled")      # what a rule says about itself: a change of these is always shown with before/after


def _significant(change: dict, new_rules: dict[str, dict], old_rules: dict[str, dict], removals: list[str]) -> bool:
    """SMS-worthy: a baseline removal, a protection/safety/destructive rule touched (judged on the OLD and the NEW flags: flipping
    `destructive` off must not hide the change), or a flag/mode of any rule flipped."""
    if removals:
        return True
    for rid in change["added"] + change["removed"] + [m["id"] for m in change["modified"]]:
        for r in (new_rules.get(rid) or {}, old_rules.get(rid) or {}):
            if r.get("kind") in ("protection", "safety") or r.get("destructive"):
                return True
    return any(f in ("destructive", "kind", "mode") for m in change["modified"] for f in m["fields"])


def _what(x: dict) -> str:
    """One modified rule in words: flags first (with before -> after), then the params, at most five fields."""
    fl = [f for f in x["fields"] if f in FLAG_FIELDS]
    order = fl + [f for f in x["fields"] if f not in FLAG_FIELDS]
    return ", ".join(f"{f.split('.', 1)[-1]} {_brief(x['before'].get(f))} -> {_brief(x['after'].get(f))}"
                     if f.startswith("params.") or f in FLAG_FIELDS else f for f in order[:5])


def notice_payload(rec: dict, new_rules: dict, old_rules: dict, removals: list[str], host: str = "") -> dict:
    """Plain-words text of a registry event for the maintenance notice and the journal."""
    a, r, m = rec["added"], rec["removed"], rec["modified"]
    na, nr, nm = rec.get("added_count", len(a)), rec.get("removed_count", len(r)), rec.get("modified_count", len(m))
    bc = rec.get("baseline_change") or {}
    weak, strong = list(bc.get("weakened") or []), list(bc.get("strengthened") or [])
    title = lambda rid: (new_rules.get(rid) or old_rules.get(rid) or {}).get("title", "")  # noqa: E731
    lines: list[str] = []
    kind = rec.get("kind")
    if kind == "invalid":
        head = "The rules registry is not valid: the last good configuration stays in force."
        lines = [f"Problem: {e}" for e in rec["errors"][:8]]
        sev, sig = "warn", False
        if any("is weaker than the release baseline" in e for e in rec["errors"]):          # somebody edited the safety floor: that is never routine
            head, sig = "The safety baseline in rules.d is WEAKER than the release's: nothing was applied; the last good configuration stays in force.", True
    elif kind == "blocked":
        head = "The rules registry was NOT applied: a generated config file has changes the registry does not know."
        lines = [f"Problem: {e}" for e in rec["errors"][:8]]
        sev, sig = "warn", False
    elif kind == "disk_unsafe" and rec.get("unadopted"):
        head = "The config files from before the registry break a safety rule of this release; nothing was changed. Adopt: homelab-maint rules diff, then sudo homelab-maint rules sync --adopt."
        lines = [f"UNSAFE: {e}" for e in rec["errors"][:8]]
        sev, sig = "warn", False
    elif kind == "disk_unsafe":
        head = "A config file the runner reads breaks a safety rule: the registry did not write it and cannot repair it."
        lines = [f"UNSAFE: {e}" for e in rec["errors"][:8]]
        sev, sig = "warn", True
    elif kind == "error":
        head = "The rules sync failed: the generated config files may be a mix of old and new; the next minute retries."
        lines = [f"Error: {e}" for e in rec["errors"][:3]]
        sev, sig = "warn", False
    elif rec.get("drift") and not (a or r or m or weak or strong):
        head = "Someone edited a generated config file by hand; the registry put it back (the edited copy is kept in the rules state dir)."
        lines = [f"Reverted: {n}" for n in rec["drift"]]
        sev, sig = "warn", True
    elif not rec.get("from"):
        head = f"The rules registry is now in charge of the config files ({_plural(rec.get('rules_count', 0), 'rule')})."
        sev, sig = "ok", True
    elif not (a or r or m) and (weak or strong or rec.get("owner_unprotect_new")):
        head = "The safety baseline changed." if (weak or strong) else "The owner accepted an extra protection exemption."
        sev, sig = "ok", False
    else:
        head = f"Rules changed: {na} added, {nr} removed, {nm} modified."
        for rid in a[:6]:
            lines.append(f"Added {rid}: {title(rid)}")
        for rid in r[:6]:
            lines.append(f"Removed {rid}: {title(rid)}")
        for x in m[:8]:
            lines.append(f"Changed {x['id']}: {_what(x)}")
        more = na + nr + nm - len(lines)
        if more > 0:
            lines.append(f"... and {more} more (homelab-maint rules history)")
        sev, sig = "ok", _significant(rec, new_rules, old_rules, removals)
        if rec.get("drift"):
            lines.append("Also reverted a hand edit of: " + ", ".join(rec["drift"]))
            sev, sig = "warn", True
    if weak:
        head, sev, sig = "BASELINE WEAKENED. " + head, "warn", True
        lines = [f"BASELINE WEAKENED: {w}" for w in weak[:8]] + lines
    lines += [f"Baseline strengthened: {x}" for x in strong[:4]]
    for p in removals:
        lines.append(f"LOUD: the owner removed the baseline protection {p!r}")
    for f, path, rx in rec.get("owner_unprotect_new") or []:
        lines.append(f"LOUD: the owner accepted {rx!r} in {f} {path}: it now exempts those names from protected.toml")
        sig = True
    summary = f"{head} Registry {short(rec.get('to'))}." + (f" ({host})" if host else "")
    return {"summary": summary[:300], "lines": lines, "significant": sig, "severity": sev, "record": rec}


def _brief(v: Any) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= 40 else s[:37] + "..."


def _notice_via_notify(p: dict) -> bool:
    """The real hook: a maintenance event through notify.py (guarded import). Raises nothing."""
    try:
        from . import notify
        ev = notify.maintenance_event("rules_registry", "Rules registry", p["summary"], done=p["lines"] or None,
                                      significant=p["significant"], severity=p["severity"])
        rec = p.get("record") or {}
        ev.dedupe_key = rec.get("dedupe") or f"rules-{short(rec.get('to'))}-{rec.get('kind') or 'applied'}"      # one notice per change: the 1 h dedupe must not swallow the next one
        d = notify.send(ev)
        return bool(getattr(d, "ok", False) or getattr(d, "handled", False))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] rules notice not sent: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
        return False


def _journal_via_routine(p: dict) -> bool:
    try:
        from . import routine
        rec = p["record"]
        routine.record_change("rules_registry", "config", p["summary"][:200], short(rec.get("from")) or None, short(rec.get("to")) or None,
                              outcome="applied" if rec.get("applied") else "refused", verified=getattr(routine, "NA", "n/a"))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] rules journal entry not written: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
        return False


def default_hooks() -> Hooks:
    return Hooks(_notice_via_notify, _journal_via_routine)


def _deliver(hooks: Hooks, payload: dict) -> bool:
    ok = False
    for fn in (hooks.journal, hooks.notify):
        if fn is not None:
            try:
                ok = bool(fn(payload)) or ok
            except Exception as exc:  # noqa: BLE001 - a broken hook must never fail a sync
                print(f"[warn] rules hook failed: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
    return ok


# --------------------------------------------------------------------------- sync
def sync(conf_dir: Path | str | None = None, state_dir: Path | str | None = None, *, wait: bool | float = False,
         hooks: Hooks | None = None, now: float | None = None, adopt: bool = False, trust: bool = True,
         rdir: Path | None = None, rollback_to: str | None = None) -> SyncResult:
    """validate -> compile -> invariants -> snapshot -> atomic replace -> record -> notice. Idempotent and tick-safe: never raises,
    returns at once when rules.d is unchanged and the generated files still match, and (wait=False) when another sync holds the lock."""
    t0 = time.perf_counter()
    conf, st = _conf(conf_dir), _state(state_dir)
    if not (rdir or rules_dir(conf)).is_dir():                  # before `rules migrate`: nothing to do, and nothing is created
        return SyncResult("no_registry")
    hooks = hooks if hooks is not None else default_hooks()
    t = _now(now)
    try:
        with _flock(st, wait) as got:
            if got:
                res = _sync_locked(conf, st, rdir or rules_dir(conf), hooks, t, adopt, trust, rollback_to)
            elif _lock_held_for(st, time.time()) > LOCK_STALE_S:
                res = SyncResult("locked-too-long", errors=[f"another rules sync has held the lock for {int(_lock_held_for(st, time.time()))} s"])
            else:
                res = SyncResult("locked")
    except Exception as exc:  # noqa: BLE001
        res = SyncResult("error", errors=[f"{type(exc).__name__}: {str(exc)[:200]}"])
        _record_error(st, hooks, t, res.errors[0])
    res.ms = round((time.perf_counter() - t0) * 1000, 2)
    return res


def tick(conf_dir: Path | str | None = None, state_dir: Path | str | None = None) -> str:
    """The per-minute scheduler hook (glue: call it first in cmd_tick). Never raises, never waits for a lock, costs about a millisecond when
    rules.d is unchanged. Returns one line to log, or "" when there was nothing to do. A failing sync always returns its line (journald)."""
    try:
        r = sync(conf_dir, state_dir, wait=False)
    except Exception as exc:  # noqa: BLE001
        return f"rules sync crashed: {type(exc).__name__}: {str(exc)[:100]}"
    return f"rules sync: {r.line()}" if r.fresh or r.status in ("error", "locked-too-long") else ""


def _write_current(st: Path, cur: dict) -> None:
    atomic_write(rules_state(st) / "current.json", json.dumps(cur, sort_keys=True, indent=1).encode())


def _touch_current(st: Path, cur: dict, now: float) -> None:
    """The idle tick: clear a problem that went away and refresh `last_tick` (a liveness stamp) at most every TICK_REFRESH_S."""
    stale = now - float(cur.get("last_tick") or cur.get("synced_at") or 0) > TICK_REFRESH_S
    dirty = bool(cur.get("invalid") or cur.get("last_error") or cur.get("disk_unsafe") or cur.get("intent"))
    if dirty or stale:
        cur["invalid"] = None
        for k in ("last_error", "disk_unsafe", "intent"):
            cur.pop(k, None)
        cur["last_tick"] = round(now, 3)
        _write_current(st, cur)


def _read_baseline(st: Path) -> dict | None:
    try:
        d = json.loads((rules_state(st) / "baseline.json").read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _compact(rec: dict) -> dict:
    """The record as stored in history.jsonl: ids and modified rules capped (the counts keep the truth), so one mass edit stays small."""
    out = dict(rec)
    for k in ("added", "removed"):
        out[f"{k}_count"] = len(rec.get(k) or [])
        out[k] = list(rec.get(k) or [])[:HISTORY_IDS_MAX]
    mod = rec.get("modified") or []
    out["modified_count"] = len(mod)
    out["modified"] = [{**x, "fields": x["fields"][:4], "before": {f: x["before"].get(f) for f in x["fields"][:4]},
                        "after": {f: x["after"].get(f) for f in x["fields"][:4]}} for x in mod[:HISTORY_MODIFIED_MAX]]
    return out


def _append_history(st: Path, rec: dict) -> None:
    hp = rules_state(st) / "history.jsonl"
    _append_line(hp, _compact(rec))
    _trim_jsonl(hp, HISTORY_FILE_MAX, HISTORY_MIN_KEEP)


def _intent_done(conf: Path, name: str, intent_files: dict) -> bool:
    """A file an interrupted sync had already replaced (its sha is the one the intent marker promised) is not a hand edit."""
    try:
        return name in intent_files and sha_bytes(_read_regular(conf / name, 8 << 20)) == intent_files[name]
    except OSError:
        return False


def _protect_first(old_raw: bytes | None, new_doc: dict) -> bool:
    """Write protected.toml FIRST when this change only adds patterns, LAST when it removes some: a crash between two replaces then leaves
    the stricter protection in force with the older rules, never the weaker."""
    try:
        old = set(tomllib.loads((old_raw or b"").decode("utf-8")).get("patterns") or [])
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return False
    return old <= {p for p in new_doc.get("patterns") or [] if isinstance(p, str)}


def _sync_locked(conf: Path, st: Path, rdir: Path, hooks: Hooks, now: float, adopt: bool, trust: bool, rollback_to: str | None) -> SyncResult:
    _sweep_tmp(conf)
    files, data, scan_errs = scan_registry(rdir=rdir)
    if not rdir.is_dir():
        return SyncResult("no_registry")
    h = registry_hash(files)
    cur = read_current(st)
    gen = (cur or {}).get("generated") or {}
    intent = (cur or {}).get("intent") or {}
    if (cur and cur.get("hash") == h and not scan_errs and rollback_to is None and (not trust or _trusted_all(rdir, files))
            and _generated_ok(conf, gen)):
        _touch_current(st, cur, now)
        return SyncResult("unchanged", hash=h)
    inv = (cur or {}).get("invalid") or {}
    if inv.get("hash") == h and inv.get("kind") == "invalid" and not adopt and inv.get("code") == _code_sig(conf):
        _disk_watch(conf, st, hooks, now)                  # the files the runner reads are the old ones: are they still safe?
        return SyncResult("invalid", hash=h, previous=cur.get("hash", ""), errors=list(inv.get("errors", [])))     # already recorded, say nothing new
    # a generated file that no longer matches what the last sync wrote (hand edit, deleted, header stripped, half-finished replace) is drift
    done = intent.get("files") or {}
    drift = sorted(n for n in gen if not _generated_ok(conf, {n: gen[n]}) and not _intent_done(conf, n, done))
    an = analyze(conf, rdir=rdir, trust=trust, scanned=(files, data, scan_errs))
    prev = (cur or {}).get("hash", "")
    old_rules = (load_snapshot(st, prev) or ({}, {}))[0] if prev else {}
    if not an.ok:
        return _record_refusal(st, hooks, now, cur, h, prev, "invalid", an.errors, an.warnings, old_rules, conf)
    comp = an.comp
    assert comp is not None
    emptied = fill_orphans(comp, conf, gen)
    cprob = new_consumer_problems(comp, conf)              # the runner's own loaders must not complain about anything new
    if cprob:
        return _record_refusal(st, hooks, now, cur, h, prev, "invalid", [f"{p} (the runner would not accept the compiled file)" for p in cprob],
                               an.warnings, old_rules, conf)
    # Never clobber a hand-maintained legacy file this registry has NOT adopted yet (no GENERATED header, never written by a sync) whose data
    # the registry does not reproduce. A file adopted before is ours whatever its header says: a stripped header or a copied-over old file is drift.
    conflicts: list[str] = []
    displaced: dict[str, bytes] = {}
    for name, doc in comp.docs.items():
        try:
            old = _read_regular(conf / name, 8 << 20)
        except FileNotFoundError:
            continue
        except OSError as exc:
            conflicts.append(f"{name}: cannot be read ({exc})")
            continue
        if _is_generated(old) or name in gen:
            if name in drift or not _is_generated(old):
                displaced[name] = old                      # the edited copy is kept for the audit
            continue
        try:
            same_data = same(tomllib.loads(old.decode("utf-8")), doc)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            same_data = False
        displaced[name] = old
        if not same_data and not adopt:
            conflicts.append(f"{name} is not generated and differs from what the registry compiles (merge the edit into rules.d, or run "
                             "`homelab-maint rules sync --adopt`; the original is kept in the rules state dir)")
    if conflicts:
        return _record_refusal(st, hooks, now, cur, h, prev, "blocked", conflicts, an.warnings, old_rules, conf)
    new_rules = snapshot_rules(an.reg)
    ch = diff_rules(old_rules, new_rules)
    bch = baseline_diff((_read_baseline(st) or {}).get("effective"), an.baseline)
    owner_now = [[u["file"], u["path"], u["regex"]] for u in an.reg.allow_unprotect]
    owner_new = [e for e in owner_now if e not in ((_read_baseline(st) or {}).get("owner_unprotect") or [])]
    # 1. snapshot (immutable, by hash), 2. originals that are about to be replaced, 3. temp files, 4. replace
    _write_snapshot(st, h, data, comp.texts, new_rules, now, {"warnings": len(an.warnings), "categories": an.reg.meta})
    for name, raw in displaced.items():
        atomic_write(rules_state(st) / "orig" / f"{name}.{sha_bytes(raw)[:8]}", raw, 0o600)
    todo: list[tuple[str, bytes, int, bytes | None]] = []
    for name, text in sorted(comp.texts.items()):
        raw = text.encode()
        prior = None
        try:
            old_st = os.stat(conf / name)
            prior = _read_regular(conf / name, 8 << 20)
            if prior == raw:
                continue
            mode = _stat.S_IMODE(old_st.st_mode)
        except OSError:
            mode = 0o644
        todo.append((name, raw, mode, prior))
    first = _protect_first((next((x[3] for x in todo if x[0] == "protected.toml"), None)), comp.docs.get("protected.toml") or {})
    todo.sort(key=lambda x: (0 if x[0] == "protected.toml" and first else 2 if x[0] == "protected.toml" else 1, x[0]))
    conf.mkdir(parents=True, exist_ok=True)
    written = _replace_all(conf, st, cur, h, now, todo)
    rec = {"ts": round(now, 3), "from": prev or None, "to": h, "added": ch["added"], "removed": ch["removed"], "modified": ch["modified"],
           "valid": True, "errors": [], "applied": True, "rules_count": len(new_rules), "files": sorted(comp.texts), "written": written,
           "drift": drift, "baseline_removals": an.removals, "rollback_to": rollback_to, "warnings": len(an.warnings), "emptied": emptied}
    if bch["weakened"] or bch["strengthened"]:
        rec["baseline_change"] = bch
    if owner_new:
        rec["owner_unprotect_new"] = owner_new
    hp = rules_state(st) / "history.jsonl"
    last = (_read_jsonl(hp, 1 << 20) or [{}])[-1]
    if not (last.get("to") == h and last.get("applied") and not drift and not written):
        _append_history(st, rec)
    retired = _retired(st)
    for rid in ch["removed"]:
        retired[rid] = round(now, 3)
    reused = [i for i in ch["added"] if i in retired]
    if ch["removed"]:
        atomic_write(rules_state(st) / "retired.json", json.dumps(retired, sort_keys=True).encode())
    if an.baseline is not None:
        atomic_write(rules_state(st) / "baseline.json", json.dumps({"effective": an.baseline, "removals": an.removals, "owner_unprotect": owner_now},
                                                                   sort_keys=True).encode())
    _write_current(st, {
        "hash": h, "synced_at": round(now, 3), "last_tick": round(now, 3), "rules_count": len(new_rules),
        "files": [{"name": f["name"], "sha": f["sha"]} for f in files], "generated": dict(comp.shas), "invalid": None,
        "baseline_removals": an.removals, "baseline_sha": (an.baseline or {}).get("sha", ""), "schema": SCHEMA})
    _prune_snapshots(st, h)
    res = SyncResult("applied", h, prev, ch["added"], ch["removed"], ch["modified"], [], an.warnings + [f"id {i} was retired earlier: reuse" for i in reused],
                     written, drift, fresh=True, baseline=bch, emptied=emptied)
    if (ch["added"] or ch["removed"] or ch["modified"] or drift or not prev or an.removals or bch["weakened"] or bch["strengthened"] or owner_new):
        res.notified = _deliver(hooks, notice_payload(rec, new_rules, old_rules, an.removals, _host()))
    return res


def _replace_all(conf: Path, st: Path, cur: dict | None, h: str, now: float, todo: list[tuple[str, bytes, int, bytes | None]]) -> list[str]:
    """Stage every new file next to its target, then os.replace them one by one. An intent marker ({file: sha} the replaces promise) goes
    into current.json first and is cleared by the final write: after a crash the next sync tells a half-finished replace from a hand edit.
    A failure part-way puts the files already replaced back (best effort) and re-raises."""
    if not todo:
        return []
    base = dict(cur or {})
    base.setdefault("hash", "")
    base["intent"] = {"to": h, "ts": round(now, 3), "files": {n: sha_bytes(raw) for n, raw, _m, _p in todo}}
    _write_current(st, base)
    staged: list[tuple[Path, Path, int, bytes | None]] = []
    written: list[str] = []
    undo: list[tuple[Path, int, bytes | None]] = []
    try:
        for name, raw, mode, prior in todo:
            tmp = conf / f".hm-rules-{os.getpid()}-{name}.tmp"
            staged.append((tmp, conf / name, mode, prior))               # tracked BEFORE it exists: any failure below removes it
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
        for tmp, dst, mode, prior in staged:
            os.replace(tmp, dst)
            written.append(dst.name)
            undo.append((dst, mode, prior))
    except BaseException:
        for tmp, _dst, _m, _p in staged:
            with contextlib.suppress(OSError):
                tmp.unlink()
        restored = True
        for dst, mode, prior in reversed(undo):
            try:
                if prior is None:
                    dst.unlink()
                else:
                    atomic_write(dst, prior, mode)
            except OSError:
                restored = False
        if restored:                                                    # nothing half-done any more: the marker has nothing to say
            with contextlib.suppress(OSError):
                base.pop("intent", None)
                _write_current(st, base)
        raise
    return written


def _host() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def _code_sig(conf: Path) -> str:
    """Changes when the code the validation reads changes (the task sources and the loaders): a refused registry is re-examined after an upgrade."""
    pkg = Path(__file__).resolve().parent
    extra = [pkg / n for n in ("jobs.py", "scheduler.py", "probes.py", "notify.py", "acks.py")]
    fl = [p for p in [*catalog_files(conf), *extra] if p.is_file()]
    return hashlib.sha1(repr([(str(p), p.stat().st_mtime_ns) for p in fl]).encode()).hexdigest()[:12]


def _record_refusal(st: Path, hooks: Hooks, now: float, cur: dict | None, h: str, prev: str, kind: str, errors: list[str],
                    warnings: list[str], old_rules: dict, conf: Path | None = None) -> SyncResult:
    """The registry cannot be applied: the generated files stay as they are. One history record, one notice, per distinct registry. The files
    the runner keeps reading are checked against the safety invariants every time (they may be hand-maintained or hand-edited): a violation
    there is announced on its own, once per distinct problem set."""
    errs = [e[:300] for e in errors[:20]]
    last = (cur or {}).get("invalid") or {}
    if last.get("hash") == h and last.get("kind") == kind and last.get("errors") == errs:
        if conf is not None:
            _disk_watch(conf, st, hooks, now)
        return SyncResult(kind, h, prev, errors=errors, warnings=warnings)       # the same problem as the last tick: nothing new to say
    rec = {"ts": round(now, 3), "from": prev or None, "to": h, "added": [], "removed": [], "modified": [], "valid": kind != "invalid",
           "errors": errs, "applied": False, "kind": kind, "warnings": len(warnings)}
    _append_history(st, rec)
    base = dict(read_current(st) or cur or {})
    base["invalid"] = {"hash": h, "ts": round(now, 3), "kind": kind, "errors": errs, "code": _code_sig(conf) if conf else ""}
    base.setdefault("hash", "")
    _write_current(st, base)
    res = SyncResult(kind, h, prev, errors=errors, warnings=warnings, fresh=True)
    res.notified = _deliver(hooks, notice_payload(rec, {}, old_rules, [], _host()))
    if conf is not None:
        _disk_watch(conf, st, hooks, now)
    return res


# --------------------------------------------------------------------------- the files the runner actually reads
DISK_FILES = ("maint.toml", "protected.toml", "classes.toml", "notify.toml", "ack.toml")


def _disk_baseline(st: Path) -> tuple[dict, set[str], set[tuple[str, str, str]]] | None:
    """(effective baseline, allowed baseline removals, owner-accepted unprotect entries) the disk files are judged by: the release floor merged
    with the baseline of the last applied registry. None when nothing is known (no floor, nothing applied yet)."""
    pkg = package_floor()
    saved = _read_baseline(st) or {}
    eff = saved.get("effective") if isinstance(saved.get("effective"), dict) else None
    if eff is None and not FLOOR_ENFORCED:
        return None
    mirror = eff or {"protected_patterns": list(pkg["protected_patterns"]), "never_touch": list(pkg["never_touch"]),
                     "min_root_depth": pkg["min_root_depth"], "limit": list(pkg["limit"]), "unprotect": list(pkg["unprotect"] or []),
                     "apply_keys": list(pkg["apply_keys"] or [])}
    try:
        removals = {x for x in saved.get("removals", []) if isinstance(x, str)}
        owner = {tuple(x) for x in saved.get("owner_unprotect", []) if isinstance(x, list) and len(x) == 3}
        base, _weak = effective_baseline(mirror, pkg, removals)
        return base, removals, owner  # type: ignore[return-value]
    except (KeyError, TypeError, ValueError):
        return None


def disk_problems(conf: Path, st: Path) -> list[str]:
    """Safety-invariant violations in the config files on disk (what the root runner reads): protected superset, ceilings, unprotect
    allow-list, delete confinement. A file that cannot be parsed is skipped: the runner fails closed on it."""
    got = _disk_baseline(st)
    if got is None:
        return []
    base, removals, owner = got
    docs: dict[str, dict] = {}
    for f in DISK_FILES:
        with contextlib.suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            docs[f] = tomllib.loads(_read_regular(conf / f, 8 << 20).decode("utf-8"))
    bad, _w, _r = doc_invariants(docs, base, task_catalog(conf), allowed=removals, owner_unprotect=owner, need_protected=False)
    return bad


def _disk_watch(conf: Path, st: Path, hooks: Hooks, now: float) -> list[str]:
    """Check the files on disk; record and announce (once per distinct problem set) when they are unsafe."""
    try:
        probs = disk_problems(conf, st)
        cur = dict(read_current(st) or {})
        sig = sha_bytes("\n".join(probs).encode())[:12] if probs else ""
        old = cur.get("disk_unsafe") or {}
        if sig == (old.get("sig") or ""):
            return probs
        cur["disk_unsafe"] = {"sig": sig, "ts": round(now, 3), "errors": [e[:300] for e in probs[:20]]} if probs else None
        cur.setdefault("hash", "")
        _write_current(st, cur)
        if probs:
            rec = {"ts": round(now, 3), "from": cur.get("hash") or None, "to": cur.get("hash") or "", "added": [], "removed": [], "modified": [],
                   "valid": False, "errors": [e[:300] for e in probs[:10]], "applied": False, "kind": "disk_unsafe", "dedupe": f"rules-disk-{sig}",
                   "unadopted": not cur.get("hash")}
            _append_history(st, rec)
            _deliver(hooks, notice_payload(rec, {}, {}, [], _host()))
        return probs
    except Exception as exc:  # noqa: BLE001 - a watch must never break the tick
        print(f"[warn] rules disk watch: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
        return []


def _record_error(st: Path, hooks: Hooks, now: float, text: str) -> None:
    """A sync that raised (a failed replace, a full disk ...): remember it in current.json and announce each distinct error once (again after
    ERROR_RENOTICE_S). Best effort: the failure may be the state dir itself."""
    try:
        cur = dict(read_current(st) or {})
        le = cur.get("last_error") or {}
        same = le.get("error") == text and now - float(le.get("ts") or 0) < ERROR_RENOTICE_S
        cur["last_error"] = {"ts": le["ts"] if same else round(now, 3), "last": round(now, 3), "error": text, "count": int(le.get("count") or 0) + 1 if same else 1}
        cur.setdefault("hash", "")
        _write_current(st, cur)
        if not same:
            rec = {"ts": round(now, 3), "from": cur.get("hash") or None, "to": cur.get("hash") or "", "added": [], "removed": [], "modified": [],
                   "valid": True, "errors": [text[:300]], "applied": False, "kind": "error", "dedupe": f"rules-error-{sha_bytes(text.encode())[:8]}"}
            _append_history(st, rec)
            _deliver(hooks, notice_payload(rec, {}, {}, [], _host()))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- history, status, rollback
def history(n: int = 20, state_dir: Path | str | None = None) -> list[dict]:
    rows = _read_jsonl(rules_state(state_dir) / "history.jsonl", HISTORY_FILE_MAX * 2)
    return rows[-n:] if n > 0 else rows


def applied_hashes(state_dir: Path | str | None = None) -> list[str]:
    """Hashes that were applied, oldest first, each once, that still have a snapshot."""
    st = _state(state_dir)
    out: list[str] = []
    for r in history(0, st):
        h = r.get("to")
        if r.get("applied") and isinstance(h, str) and (st / "rules" / "snapshots" / h).is_dir():
            if h in out:
                out.remove(h)
            out.append(h)
    return out


def status(conf_dir: Path | str | None = None, state_dir: Path | str | None = None) -> dict:
    """Cheap health of the registry for `rules_registry`, self_health and publish: valid / in sync / pending / drift / safe. No parsing of rules."""
    conf, st = _conf(conf_dir), _state(state_dir)
    rdir = rules_dir(conf)
    cur = read_current(st) or {}
    files, _data, errs = scan_registry(rdir=rdir)
    h = registry_hash(files) if files else ""
    gen = cur.get("generated") or {}
    inv = cur.get("invalid") or {}
    done = (cur.get("intent") or {}).get("files") or {}
    drift = sorted(n for n in gen if not _generated_ok(conf, {n: gen[n]}) and not _intent_done(conf, n, done))
    pending = bool(files) and h != cur.get("hash")
    unsafe = list((cur.get("disk_unsafe") or {}).get("errors", []))
    lerr = cur.get("last_error") if isinstance(cur.get("last_error"), dict) else None
    return {"present": rdir.is_dir(), "hash": cur.get("hash") or "", "adopted": bool(cur.get("hash")), "registry_hash": h, "synced_at": cur.get("synced_at"),
            "last_tick": cur.get("last_tick"), "rules_count": cur.get("rules_count", 0), "pending": pending, "drift": drift, "scan_errors": errs,
            "valid": not inv and not errs, "errors": list(inv.get("errors", []))[:5] or errs[:5], "safe": not unsafe, "unsafe": unsafe[:5],
            "last_error": lerr, "half_done": bool(cur.get("intent")),
            "in_sync": bool(cur) and not pending and not drift and not inv and not unsafe and not lerr}


def rollback(target: str | None = None, conf_dir: Path | str | None = None, state_dir: Path | str | None = None, *,
             hooks: Hooks | None = None, now: float | None = None, wait: bool | float = True, trust: bool = True) -> SyncResult:
    """Restore the registry of an applied snapshot (default: the one before the current), validate it BEFORE touching rules.d, then
    sync. The current baseline file is kept (a rollback must not weaken the shipped protections); unapplied edits are saved first."""
    shutil = _lazy("shutil")
    conf, st = _conf(conf_dir), _state(state_dir)
    hooks = hooks if hooks is not None else default_hooks()
    rd = rules_dir(conf)
    try:
        with _flock(st, wait) as got:
            if not got:
                return SyncResult("locked")
            hs = applied_hashes(st)
            cur = (read_current(st) or {}).get("hash")
            if target:
                m = [x for x in hs if x.startswith(target)]
                if len(m) != 1:
                    return SyncResult("error", errors=[f"no unique applied snapshot matches {target!r} (see: rules history)"])
                want = m[0]
            else:
                older = [x for x in hs if x != cur]
                if not older:
                    return SyncResult("error", errors=["there is no earlier applied registry to go back to"])
                want = older[-1]
            sdir = st / "rules" / "snapshots" / want / "rules.d"
            snap = {p.name: p.read_bytes() for p in sorted(sdir.glob("*.toml"))}
            keep = {}
            with contextlib.suppress(OSError):
                keep = {BASELINE_FILE: _read_regular(rd / BASELINE_FILE)}
            files = {**snap, **keep}
            tmp = st / "rules" / f".rollback-{os.getpid()}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            os.chmod(tmp, 0o755)
            for n, b in files.items():
                (tmp / n).write_bytes(b)
                os.chmod(tmp / n, 0o644)
            try:
                an = analyze(conf, rdir=tmp, trust=False, consumers=True)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not an.ok:
                return SyncResult("invalid", want, cur or "", errors=["that snapshot no longer validates: " + e for e in an.errors[:5]])
            cur_files, cur_data, _e = scan_registry(rdir=rd)
            if cur_files and registry_hash(cur_files) not in applied_hashes(st):
                back = st / "rules" / "snapshots" / f"unapplied-{registry_hash(cur_files)[:8]}-{int(_now(now))}"
                (back / "rules.d").mkdir(parents=True, exist_ok=True)
                for n, b in cur_data.items():
                    (back / "rules.d" / n).write_bytes(b)
            for n, b in files.items():
                atomic_write(rd / n, b, 0o644)
            for n in cur_data:
                if n not in files:
                    with contextlib.suppress(OSError):
                        (rd / n).unlink()
            res = _sync_locked(conf, st, rd, hooks, _now(now), False, trust, want)
            return res
    except Exception as exc:  # noqa: BLE001
        return SyncResult("error", errors=[f"{type(exc).__name__}: {str(exc)[:200]}"])


# =========================================================================== public export for the website (read-only mirror)
_SECRET_KEY = re.compile(r"(?i)(pass|secret|token|key|auth|cookie|credential|bearer|private|session|signature|hmac|webhook|dsn|salt)")
_DROP_KEYS = frozenset({"env", "headers", "header", "to", "sms", "email", "emails", "phone", "recipients", "recipient", "address", "handle",
                        "notify_handle", "bridge", "body", "stdin"})            # never published, whatever the value
_CMD_KEYS = frozenset({"command", "argv", "args", "cmd", "exec"})              # published as "program (+N args)" only
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,10}://)[^\s/@]+@")
_URL_ANY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,10})://([^\s/?#\"']*)([^\s\"']*)")
_BEARER_RX = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_ARG_SECRET = re.compile(r"(?i)(--?(?:pass(?:word|wd|phrase)?|secret|token|api[-_]?key|key|auth\w*|user|header|cookie|credential\w*)(?:=|\s+))(\S+)")
_ARG_U = re.compile(r"(?<!\S)(-u\s+)\S+")
_KV_SECRET = re.compile(r"(?i)\b(pass(?:word|wd|phrase)?|secret|token|api[_-]?key|authorization|cookie|credential\w*)\b(\s*[:=]\s*)(?!\[redacted\])\S+")
_BLOB = re.compile(r"(?<![/\w.-])(?=[A-Za-z0-9_+=]*[A-Za-z])(?=[A-Za-z0-9_+=]*\d)[A-Za-z0-9_+=]{32,}(?![\w/-])")      # hashes, HMACs, base64: not a hyphenated container name
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.+-])(?:\+\d{10,15}(?!\d)|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-]))")
_HOME = re.compile(r"/home/[^/\s\"'\\]+")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_TOKEN_AFTER = frozenset({"push", "hooks", "hook", "webhook", "webhooks", "token", "key", "services", "bot"})
_ASCII = re.compile(r"[^\x20-\x7e]")


def _pub_url(m: re.Match) -> str:
    """scheme://host kept; the rest is hidden for a host outside this machine (webhook paths and query strings ARE the secret), and for a
    loopback host token-looking path segments and any query are hidden."""
    scheme, netloc, rest = m.group(1), m.group(2), m.group(3)
    host = netloc.rsplit("@", 1)[-1]
    host = host.split(":")[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if host.lower() not in _LOOPBACK:
        return f"{scheme}://{netloc}" + ("/[redacted]" if rest.strip("/") else "")
    path, _q, query = rest.partition("?")
    segs, prev, out = path.split("/"), "", []
    for sg in segs:
        tok = len(sg) >= 8 and bool(re.search(r"\d", sg)) and bool(re.search(r"[A-Za-z]", sg))
        out.append("[redacted]" if sg and (tok or prev.lower() in _TOKEN_AFTER) else sg)
        prev = sg
    return f"{scheme}://{netloc}" + "/".join(out) + ("?[redacted]" if query else "")


def _pub_str(v: str) -> str:
    """A string that may leave the host: URL credentials, bearer tokens, --flag secrets, opaque blobs and home directories hidden."""
    v = _URL_USERINFO.sub(r"\1", v)
    v = _BEARER_RX.sub(r"\1 [redacted]", v)
    v = _ARG_SECRET.sub(r"\1[redacted]", v)
    v = _ARG_U.sub(r"\1[redacted]", v)
    v = _KV_SECRET.sub(r"\1\2[redacted]", v)
    v = _URL_ANY.sub(_pub_url, v)
    v = _BLOB.sub("[redacted]", v)
    v = _EMAIL.sub("[email]", v)
    v = _PHONE.sub("[phone]", v)
    return _HOME.sub("~", v)


def _cmd_summary(v: Any) -> str:
    """["/usr/local/sbin/backup.sh", "--a", "b"] -> "backup.sh (+2 args)": what runs, never with what arguments."""
    items = [str(x) for x in v] if isinstance(v, list) else str(v).split()
    if not items:
        return ""
    return posixpath.basename(items[0])[:60] + (f" (+{len(items) - 1} arg{'s' if len(items) > 2 else ''})" if len(items) > 1 else "")


def redact(v: Any, key: str = "", _secret: bool = False) -> Any:
    """Copy of `v` safe for a public file or a notice. By KEY NAME (case-insensitive substring: pass, secret, token, key, auth, cookie, ...)
    strings and containers are replaced; env/headers/recipients/phone-like keys are dropped from tables ("_hidden" names them); a command
    is reduced to its program; every other string loses URL credentials/paths of foreign hosts, bearer tokens, --flag secrets, opaque
    blobs and /home/<user>. Numbers and booleans pass: a threshold is what the website shows."""
    lk = str(key or "").lower()
    if lk in _DROP_KEYS:
        return "[hidden]"
    if lk in _CMD_KEYS and isinstance(v, (list, tuple, str)):
        return _cmd_summary(list(v) if isinstance(v, tuple) else v)
    secret = _secret or bool(_SECRET_KEY.search(lk))
    if isinstance(v, dict):
        out, hidden = {}, []
        for k, x in v.items():
            if str(k).lower() in _DROP_KEYS:
                hidden.append(str(k))
            else:
                out[k] = redact(x, str(k), secret)
        if hidden:
            out["_hidden"] = sorted(hidden)
        return out
    if isinstance(v, (list, tuple)):
        return [redact(x, key, secret) for x in v]
    if isinstance(v, str):
        return "[redacted]" if secret and v else _pub_str(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    return v


public_params = redact


def _as_epoch(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.datetime.strptime(v, fmt).timestamp()
            except ValueError:
                continue
    return None


_HIST_RX = re.compile(rb'^\{"t":\s*([0-9.eE+-]+),\s*"kind":\s*"(task|job)",\s*"task":\s*"([^"\\]*)",\s*"status":\s*"([^"\\]*)"'
                      rb'(?:,\s*"alert":\s*(true|false))?')


def _load_history(src: Any, now: float, days: int = 30) -> list[tuple[float, str, str, bool]]:
    """(t, name, status, alert) of task/job runs in the last `days` days from history.jsonl (a Path) or already-parsed records."""
    out: list[tuple[float, str, str, bool]] = []
    cutoff = now - days * 86400
    if isinstance(src, (str, Path)):
        try:
            with open(src, "rb") as f:
                size = f.seek(0, 2)
                f.seek(max(0, size - 24 * 1024 * 1024))
                lines = f.read().splitlines()
        except OSError:
            return out
        for ln in lines:
            m = _HIST_RX.match(ln)
            if m and float(m.group(1)) >= cutoff:
                out.append((float(m.group(1)), m.group(3).decode(), m.group(4).decode(), m.group(5) != b"false"))
    else:
        for r in src or []:
            if isinstance(r, dict) and r.get("kind") in ("task", "job") and isinstance(r.get("t"), (int, float)) and r["t"] >= cutoff:
                out.append((float(r["t"]), str(r.get("task")), str(r.get("status")), r.get("alert") is not False))
    out.sort()
    return out


def _load_audit(src: Any, now: float, days: int = 30) -> list[tuple[float, str, str, str]]:
    """(t, task, outcome, target) of audit.jsonl records in the last `days` days."""
    rows: list[dict] = []
    if isinstance(src, (str, Path)):
        rows = _read_jsonl(Path(src), 6 * 1024 * 1024)
    else:
        rows = [r for r in (src or []) if isinstance(r, dict)]
    cutoff = now - days * 86400
    out = []
    for r in rows[-40000:]:
        t = _as_epoch(r.get("ts"))
        if t is not None and t >= cutoff:
            out.append((t, str(r.get("task", "")), str(r.get("outcome", "")), str(r.get("target", ""))))
    out.sort()
    return out


def _clusters(times: list[float], gap: float = 300.0) -> tuple[int, float | None]:
    """How many separate bursts (a burst = records less than `gap` s apart) and when the last one started."""
    n, last, prev = 0, None, None
    for t in sorted(times):
        if prev is None or t - prev > gap:
            n += 1
            last = t
        prev = t
    return n, last


def _last_burst(times: list[float], gap: float = 300.0) -> int:
    """How many records the newest burst holds."""
    n, prev = 0, None
    for t in sorted(times, reverse=True):
        if prev is not None and prev - t > gap:
            break
        n += 1
        prev = t
    return n


def _episodes(runs: list[tuple[float, str, str, bool]]) -> tuple[int, float | None]:
    """Problem episodes: a warn/crit/error run (alerting) after a run that was not."""
    n, last, bad = 0, None, False
    for t, _name, st_, alert in runs:
        now_bad = alert and st_ in ("warn", "crit", "error")
        if now_bad and not bad:
            n += 1
            last = t
        bad = now_bad
    return n, last


def _related(r: dict) -> list[str]:
    out = list(r.get("applies_to") or [])
    ref_t = r.get("target") or ""
    if r.get("file") == "maint.toml":
        m = re.match(r"tasks\.([A-Za-z0-9_-]+)", ref_t)
        if m:
            out.append(m.group(1))
    nm = (r.get("params") or {}).get("name")
    if isinstance(nm, str) and r.get("file") in ("jobs.toml", "probes.toml"):
        out.append(nm)
    return list(dict.fromkeys(out))


def crossref(rules: list[dict], status_doc: dict | None, history_src: Any, audit_src: Any, probes_state: dict | None,
             now: float) -> dict[str, dict]:
    """rule id -> {last_evaluated, last_triggered, triggers_30d, last_result, related}, from status.json, history.jsonl,
    audit.jsonl and (optionally) probes.json. Pure; tolerant of every input being missing."""
    tasks = (status_doc or {}).get("tasks") if isinstance((status_doc or {}).get("tasks"), dict) else {}
    runs: dict[str, list] = {}
    for rec in _load_history(history_src, now):
        runs.setdefault(rec[1], []).append(rec)
    audit = _load_audit(audit_src, now)
    by_task: dict[str, list] = {}
    for rec in audit:
        by_task.setdefault(rec[1], []).append(rec)
    pstate = (probes_state or {}).get("probes") if isinstance((probes_state or {}).get("probes"), dict) else {}
    out: dict[str, dict] = {}
    for r in rules:
        rel = _related(r)
        evals, trig_t, trig_n, res_txt = [], [], 0, None
        kind, paths = r.get("kind"), _paths_in(r.get("params") or {})
        for n in rel:
            e = tasks.get(n) if isinstance(tasks.get(n), dict) else None
            if e and isinstance(e.get("last_run"), (int, float)):
                evals.append(float(e["last_run"]))
                res_txt = res_txt or _result_text(e.get("status"), e.get("summary"))
            p = pstate.get(n) if isinstance(pstate.get(n), dict) else None
            if p and isinstance(p.get("last_run"), (int, float)):
                evals.append(float(p["last_run"]))
                res_txt = res_txt or _result_text(p.get("state") or p.get("status"), p.get("detail"))
                if p.get("state") in ("warn", "down") and isinstance(p.get("since"), (int, float)):
                    trig_t.append(float(p["since"]))
            if kind in ("check", "spike", "alert", "job", "policy", "schedule"):
                cnt, last = _episodes(runs.get(n, []))
                trig_n += cnt
                if last:
                    trig_t.append(last)
            if kind == "cleanup":
                rows = [x for x in by_task.get(n, []) if x[2] in ("done", "dry-run") and (not paths or any(x[3].startswith(p_) for p_ in paths))]
                cnt, last = _clusters([x[0] for x in rows])
                trig_n += cnt
                if last:
                    trig_t.append(last)
                if rows and not res_txt:                       # counts and the RULE's own path only: the audited file names stay on the host
                    where = f" under {_ascii(paths[0], 80)}" if paths else ""
                    res_txt = f"{rows[-1][2]}: {_last_burst([x[0] for x in rows])} item(s){where}"
            if kind == "safety" and ("max_gib_per_run" in (r.get("params") or {}) or "max_items_per_run" in (r.get("params") or {})):
                refused = [x[0] for x in (by_task.get(n, []) if n else audit) if x[2] == "refused-cap"]
                cnt, last = _clusters(refused)
                trig_n += cnt
                if last:
                    trig_t.append(last)
        if kind == "protection":
            rxs = []
            for p_ in (r.get("params") or {}).get("patterns", []) if isinstance((r.get("params") or {}).get("patterns"), list) else []:
                try:
                    rxs.append(re.compile(p_, re.I))
                except (re.error, TypeError):
                    pass
            hits = [x[0] for x in audit if x[2] == "refused-protected" and any(rx.search(x[3]) for rx in rxs)] if rxs else []
            cnt, last = _clusters(hits)
            trig_n += cnt
            if last:
                trig_t.append(last)
        out[r["id"]] = {"last_evaluated": max(evals) if evals else None, "last_triggered": max(trig_t) if trig_t else None,
                        "triggers_30d": trig_n, "last_result": res_txt, "related": rel}
    return out


def _ascii(s: Any, n: int) -> str:
    return _ASCII.sub("?", str(redact(str(s)))).strip()[:n]


def _result_text(status_word: Any, summary: Any) -> str | None:
    if not status_word:
        return None
    word, text = _ascii(status_word, 12), _ascii(summary or "", 110)
    if text.lower().startswith(word.lower() + ":"):
        text = text[len(word) + 1:].strip()
    return f"{word}: {text}".rstrip(": ")


def applied_rules(conf_dir: Path | str | None = None, state_dir: Path | str | None = None) -> tuple[list[dict], dict, str, bool]:
    """(rules, categories meta, hash, applied?) of what the script RUNS under: the last applied snapshot, else rules.d as it is."""
    st = _state(state_dir)
    cur = read_current(st) or {}
    h = cur.get("hash") or ""
    snap = load_snapshot(st, h) if h else None
    if snap:
        return list(snap[0].values()), snap[1].get("categories") or {}, h, True
    reg = load_registry(conf_dir, trust=False)
    return [r.as_dict() for r in reg.rules], reg.meta, reg.hash, False


def _public_history(st: Path, n: int = 50) -> list[dict]:
    out = []
    for r in reversed(history(0, st)[-n:]):
        added, removed, mod = r.get("added") or [], r.get("removed") or [], r.get("modified") or []
        out.append({"ts": r.get("ts"), "from": short(r.get("from")), "to": short(r.get("to")), "valid": r.get("valid"),
                    "applied": r.get("applied"), "kind": r.get("kind"), "added": added[:20], "added_count": r.get("added_count", len(added)),
                    "removed": removed[:20], "removed_count": r.get("removed_count", len(removed)),
                    "modified": [{"id": m.get("id"), "fields": (m.get("fields") or [])[:6], "before": _pub_change(m.get("before")),
                                  "after": _pub_change(m.get("after"))} for m in mod[:10]],
                    "modified_count": r.get("modified_count", len(mod)), "errors": [_ascii(e, 160) for e in (r.get("errors") or [])[:3]],
                    "baseline_weakened": len((r.get("baseline_change") or {}).get("weakened") or []),
                    "rollback_to": short(r.get("rollback_to")) or None})
    return out


def _pub_change(d: Any) -> Any:
    """before/after of a history record: {"params.<key>": value}, each value redacted under ITS key (a recipient or a secret never leaves)."""
    if not isinstance(d, dict):
        return d
    return {f: redact(v, str(f).split(".", 1)[-1]) for f, v in d.items()}


def build_rules_json(conf_dir: Path | str | None = None, state_dir: Path | str | None = None, *, status: Any = None, history_src: Any = None,
                     audit_src: Any = None, probes: Any = None, now: float | None = None, max_bytes: int = RULES_JSON_MAX) -> dict:
    """rules.json (SPEC6 S5). `status` / `probes` may be parsed dicts or Paths; `history_src` / `audit_src` Paths or record lists.
    Defaults: STATE_DIR/status.json, history.jsonl, probes.json and LOG_DIR/audit.jsonl. Reads only; never raises on missing inputs."""
    now = _now(now)
    st = _state(state_dir)
    rules, cats, h, applied = applied_rules(conf_dir, st)

    def doc(v: Any, default: Path) -> dict | None:
        if isinstance(v, dict):
            return v
        d = core.read_json(Path(v) if v else default, None)
        return d if isinstance(d, dict) else None
    status_doc = doc(status, st / "status.json")
    probes_doc = doc(probes, st / "probes.json")
    xr = crossref(rules, status_doc, history_src if history_src is not None else st / "history.jsonl",
                  audit_src if audit_src is not None else core.LOG_DIR / "audit.jsonl", probes_doc, now)
    out_rules = []
    for r in sorted(rules, key=lambda x: (x.get("category", ""), x.get("source_file", ""), x.get("order", 0), x["id"])):
        x = xr.get(r["id"], {})
        out_rules.append({"id": r["id"], "category": r.get("category"), "title": _pub_str(str(r.get("title") or "")), "kind": r.get("kind"),
                          "why": _pub_str(str(r.get("why") or "")), "does": _pub_str(str(r.get("does") or "")), "applies_to": r.get("applies_to") or [],
                          "params": public_params(r.get("params") or {}),
                          "mode": r.get("mode"), "enabled": r.get("enabled", True), "severity": r.get("severity", "none"),
                          "destructive": bool(r.get("destructive")), "proof": _pub_str(str(r.get("proof") or "")), "principle": r.get("principle") or "",
                          "since": r.get("since") or "", "source_file": r.get("source_file"), "file": r.get("file"), "target": r.get("target"),
                          "last_evaluated": x.get("last_evaluated"), "last_triggered": x.get("last_triggered"),
                          "triggers_30d": x.get("triggers_30d", 0), "last_result": x.get("last_result"), "related": x.get("related", [])})
    counts: dict[str, int] = {}
    for r in out_rules:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    categories = [{"id": c, "title": (cats.get(c) or {}).get("title") or CATEGORIES.get(c, c.title()), "blurb": (cats.get(c) or {}).get("blurb", ""),
                   "count": counts[c]} for c in sorted(counts, key=lambda c: list(CATEGORIES).index(c) if c in CATEGORIES else 99)]
    cur = read_current(st) or {}
    stats = {"total": len(out_rules), "enabled": sum(1 for r in out_rules if r["enabled"]), "disabled": sum(1 for r in out_rules if not r["enabled"]),
             "destructive": sum(1 for r in out_rules if r["destructive"]), "apply_mode": sum(1 for r in out_rules if r["mode"] == "apply"),
             "evaluated_24h": sum(1 for r in out_rules if r["last_evaluated"] and now - r["last_evaluated"] < 86400),
             "triggered_30d": sum(1 for r in out_rules if r["triggers_30d"]), "placeholder": sum(1 for r in out_rules if TODO in (r["why"] + r["does"])),
             "by_kind": {k: sum(1 for r in out_rules if r["kind"] == k) for k in KINDS if any(r["kind"] == k for r in out_rules)}}
    res = {"schema": SCHEMA, "generated_at": round(now, 3), "registry_hash": h, "registry_synced_at": cur.get("synced_at"), "applied": applied,
           "valid": not cur.get("invalid"), "categories": categories, "rules": out_rules, "history": _public_history(st), "stats": stats}
    return _fit(res, max_bytes)


def _fit(res: dict, max_bytes: int) -> dict:
    """Keep the file under the cap, in stages (biggest params, history, long text, side text, all params, titles, finally the tail of the
    rule list); says so in stats.truncated. Sizes are tracked per rule, never by re-dumping the whole document."""
    def enc(o: Any) -> int:
        return len(json.dumps(o, separators=(",", ":"), ensure_ascii=False, default=str).encode())
    rules = res["rules"]
    lens = [enc(r) for r in rules]
    fixed = [enc({**res, "rules": []})]

    def total() -> int:
        return fixed[0] + sum(lens) + max(len(rules) - 1, 0)
    if total() <= max_bytes:
        return res
    res["stats"]["truncated"] = True
    max_bytes -= 64                                                     # room for the two stats keys this adds
    fixed[0] = enc({**res, "rules": []})

    def refit(i: int) -> None:
        lens[i] = enc(rules[i])
    for i in sorted(range(len(rules)), key=lambda i: -len(json.dumps(rules[i]["params"], default=str))):
        if total() <= max_bytes:
            return res
        if len(json.dumps(rules[i]["params"], default=str)) > 200:
            rules[i]["params"] = {"_truncated": True, "keys": sorted(rules[i]["params"])[:12]}
            refit(i)
    while res["history"] and total() > max_bytes:
        res["history"].pop()
        fixed[0] = enc({**res, "rules": []})
    for limit in (200, 100, 60):                                        # long prose, progressively
        if total() <= max_bytes:
            return res
        for i, r in enumerate(rules):
            if len(r["why"]) > limit or len(r["does"]) > limit:
                r["why"], r["does"] = r["why"][:limit], r["does"][:limit]
                refit(i)
    for fn in (lambda r: r.update(proof="", principle="", last_result=None, target=None, file=None),       # side text next
               lambda r: r.update(params={"_truncated": True}),
               lambda r: r.update(title=r["title"][:60], why="", does="")):
        if total() <= max_bytes:
            return res
        for i, r in enumerate(rules):
            fn(r)
            refit(i)
    if total() > max_bytes:                                             # last resort: the tail of the list, said so
        omitted = 0
        while rules and total() > max_bytes:
            rules.pop()
            lens.pop()
            omitted += 1
        res["stats"]["omitted_rules"] = omitted
    return res


def _runner_version() -> str:
    try:
        from . import __version__  # type: ignore[attr-defined]
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unversioned"


def build_manifest(public_dir: Path | str, conf_dir: Path | str | None = None, state_dir: Path | str | None = None, *,
                   runner_version: str | None = None, now: float | None = None, files: dict | None = None) -> dict:
    """manifest.json: schema + generated_at of every public JSON file, the runner version and the registry state."""
    now = _now(now)
    cur = read_current(_state(state_dir)) or {}
    pub = Path(public_dir)
    fl: dict[str, dict] = {}
    if files is None:
        with contextlib.suppress(OSError):
            for p in sorted(pub.rglob("*.json")):
                rel = p.relative_to(pub).as_posix()
                if rel == "manifest.json" or p.name.startswith("."):
                    continue
                try:
                    stt = p.stat()
                    d = json.loads(p.read_bytes()) if stt.st_size <= 512 * 1024 else None
                except (OSError, ValueError):
                    fl[rel[:-5]] = {"schema": None, "generated_at": None, "unreadable": True}
                    continue
                ga = d.get("generated_at") if isinstance(d, dict) else None
                fl[rel[:-5]] = {"schema": d.get("schema") if isinstance(d, dict) else None,
                                "generated_at": ga if isinstance(ga, (int, float)) else round(stt.st_mtime, 3),
                                **({} if isinstance(ga, (int, float)) else {"approx": True})}
    else:
        fl = {str(k): {"schema": v.get("schema"), "generated_at": v.get("generated_at")} for k, v in files.items() if isinstance(v, dict)}
    return {"schema": SCHEMA, "generated_at": round(now, 3), "runner_version": runner_version or _runner_version(),
            "registry_hash": cur.get("hash") or "", "registry_synced_at": cur.get("synced_at"), "rules_count": cur.get("rules_count", 0),
            "registry_valid": not cur.get("invalid"), "files": fl}


def _published_hash(path: Path) -> tuple[str, bool, float] | None:
    """(registry_hash, valid, mtime) of an existing rules.json, from its first bytes (the keys are written in this order)."""
    try:
        with open(path, "rb") as f:
            head = f.read(700).decode("utf-8", "replace")
        m = re.search(r'"registry_hash":"([0-9a-f]*)"', head)
        return (m.group(1), '"valid":false' not in head.replace(" ", ""), path.stat().st_mtime) if m else None
    except OSError:
        return None


def write_public(public_dir: Path | str | None = None, conf_dir: Path | str | None = None, state_dir: Path | str | None = None, *,
                 rules_max_age_s: float = 300.0, **kw: Any) -> list[str]:
    """Write rules.json, then manifest.json (so it lists rules.json), both 0644, atomically. Never raises; returns the names written.
    Call it LAST in publish(): the manifest describes the files that are already there. rules.json is rebuilt at most every
    `rules_max_age_s` seconds unless the registry changed (it reads status, history and audit tails: too much for a once-a-minute publish);
    the small manifest is rewritten every call."""
    written: list[str] = []
    try:
        pub = Path(public_dir) if public_dir is not None else _state(state_dir) / "public"
        pub.mkdir(parents=True, exist_ok=True)
        now = _now(kw.get("now"))
        cur = read_current(state_dir) or {}
        old = _published_hash(pub / "rules.json")
        fresh = bool(old) and old[0] == (cur.get("hash") or "") and old[1] == (not cur.get("invalid")) and 0 <= now - old[2] < rules_max_age_s
        if not fresh:
            rj = build_rules_json(conf_dir, state_dir, **{k: v for k, v in kw.items() if k in ("status", "history_src", "audit_src", "probes", "max_bytes")}, now=now)
            atomic_write(pub / "rules.json", json.dumps(rj, separators=(",", ":"), ensure_ascii=False, default=str).encode(), 0o644, ".pub-")
            written.append("rules.json")
        mf = build_manifest(pub, conf_dir, state_dir, runner_version=kw.get("runner_version"), now=now)
        atomic_write(pub / "manifest.json", json.dumps(mf, separators=(",", ":"), default=str).encode(), 0o644, ".pub-")     # publish.py sweeps .pub-*.tmp
        written.append("manifest.json")
    except Exception as exc:  # noqa: BLE001
        print(f"homelab-maint rules export: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
    return written


# =========================================================================== the `rules_registry` check task (registered by the glue)
def check_task(ctx: Any) -> Any:
    """C0: is the registry valid, applied, in sync, and are the files the runner reads safe? Reads current.json only (one stat+read of rules.d),
    so it costs milliseconds."""
    s = status(core.CONF_DIR, core.STATE_DIR)
    R = core.Result
    if not s["present"]:
        return R("info", "rules registry not set up (no rules.d); config files are hand-maintained", {"present": 0}, alert=False)
    m = {"present": 1, "valid": int(s["valid"]), "in_sync": int(s["in_sync"]), "rules": s["rules_count"], "pending": int(s["pending"]),
         "drift_files": len(s["drift"]), "unsafe": len(s["unsafe"]), "synced_age_min": round((ctx.now - s["synced_at"]) / 60) if s.get("synced_at") else None,
         "tick_age_min": round((ctx.now - s["last_tick"]) / 60) if s.get("last_tick") else None}
    if not s["safe"] and not s["adopted"]:      # first hour: the files are the ones from BEFORE the registry (the owner's), nothing here chose them; they only nag until adopted
        return R("warn", "rules registry not adopted yet (the old config breaks a safety rule): rules diff, then sudo homelab-maint rules sync --adopt", m,
                 [{"problem": _ascii(e, 160)} for e in s["unsafe"]])
    if not s["safe"]:
        return R("crit", ("a config file the runner reads breaks a safety rule: " + s["unsafe"][0])[:140], m, [{"problem": _ascii(e, 160)} for e in s["unsafe"]])
    if not s["valid"]:
        return R("warn", ("rules registry invalid, last good config in force: " + "; ".join(s["errors"][:1]))[:140], m,
                 [{"problem": _ascii(e, 160)} for e in s["errors"][:5]])
    le = s.get("last_error")
    if le and (not s.get("synced_at") or float(le.get("last") or le.get("ts") or 0) >= float(s["synced_at"])):
        return R("warn", ("rules sync is failing" + (", config files may be half updated" if s["half_done"] else "") + ": " + str(le.get("error")))[:140], m)
    if s["pending"]:
        return R("warn", "rules.d changed but is not applied yet: is the tick running `rules sync`?", m)
    if s["drift"]:
        return R("info", f"generated config edited by hand ({', '.join(s['drift'][:3])}); the next sync reverts it", m, alert=False)
    return R("ok", f"rules registry in sync ({s['rules_count']} rules, {short(s['hash'])})", m)


def register_tasks() -> None:
    """Glue: call from cli.load_tasks (or add "registry" to EXTRA_TASK_MODULES and call this at import) to register `rules_registry`."""
    if "rules_registry" not in core.REGISTRY:
        core.task("rules_registry", klass="C0", tier="check", title="Rules registry", timeout=30)(check_task)


# =========================================================================== migrate: legacy config files -> rules.d, with proof
SPIKE_TASKS = frozenset({"spike_sampler", "stuck_detector", "orphan_report", "image_ledger", "pressure_state", "pressure_response",
                         "bulkhead_check", "qos_classes"})
OUT_FILES = {"checks": "10-checks.toml", "spike": "20-spike.toml", "cleanup": "30-cleanup.toml", "protection": "40-protection.toml",
             "alerts": "50-alerts.toml", "schedule": "60-schedule.toml", "monitoring": "70-monitoring.toml", "jobs": "80-jobs.toml",
             "safety": "90-safety.toml", "ack": "95-ack.toml"}
OUT_BLURB = {"checks": "What the script verifies and when it complains.", "spike": "How the script reacts to memory, IO, CPU and GPU pressure.",
             "cleanup": "What the script cleans or trims, under which limits (report-only until you say apply).",
             "protection": "Workloads and paths the script must never kill, restart or delete.",
             "alerts": "Who is told what, when and how often; acknowledgements.", "schedule": "When each maintenance step may run.",
             "monitoring": "Probes and the live monitor: what is watched and how.", "jobs": "Scheduled jobs and the timers the host runs.",
             "safety": "Global limits and settings.", "ack": "Acknowledged known issues: how an error is recognised again."}
DESTRUCTIVE_JOB = re.compile(r"prune|purge|delete|remove|clean|recycle|reclaim|restart|rm\b", re.I)


@dataclass
class MRule:
    """A migrated rule: its registry fields, the registry file it goes to, and where its data sits in the legacy document."""
    rule: dict
    cat: str
    loc: tuple                       # ("table", file, path) | ("elem", file, path of the list, index, parent loc | None)


class _Ids:
    def __init__(self) -> None:
        self.used: set[str] = set()

    def take(self, base: str) -> str:
        s = _slug(base, 70)
        if len(s) < 3:
            s = "x." + s
        cand, n = s, 1
        while cand in self.used:
            n += 1
            cand = f"{s}-{n}"
        self.used.add(cand)
        return cand


@dataclass
class _MCtx:
    catalog: dict[str, TaskInfo]
    today: str
    names: set[str]
    ids: _Ids = field(default_factory=_Ids)
    out: list[MRule] = field(default_factory=list)

    def add(self, rid: str, title: str, kind: str, cat: str, file: str, target: str, params: dict, loc: tuple, *, merge: str = "set",
            order: int = 0, mode: str | None = None, applies: Iterable[str] = (), destructive: bool = False, severity: str = "none",
            src: str = "") -> str:
        r: dict[str, Any] = {"id": rid, "title": title[:120], "kind": kind,
                             "why": f"{TODO}: say why this rule exists (name the principle it follows).",
                             "does": f"{TODO}: describe exactly what the script does under this rule."}
        ap = [a for a in dict.fromkeys(applies) if a in self.names and re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", a)]
        if ap:
            r["applies_to"] = ap
        r["file"], r["target"] = file, target
        if merge != "set":
            r["merge"] = merge
        if mode:
            r["mode"] = mode
        if severity != "none":
            r["severity"] = severity
        if destructive:
            r["destructive"] = True
            r["proof"] = f"{TODO}: how safety is established for this rule (in-use proof, caps, protected list, report-first)."
        r["since"] = self.today
        if order:
            r["order"] = order
        r["owner_notes"] = f"migrated from {file} {src or target or '(root)'}"
        r["params"] = params
        self.out.append(MRule(r, cat, loc))
        return rid


def _tables(doc: dict, boundary: str) -> list[tuple[tuple, dict]]:
    """Root + boundary tables in document order with their OWN keys (child boundary tables carved out)."""
    rx = re.compile(boundary)

    def is_b(p: tuple) -> bool:
        return bool(rx.fullmatch(".".join(p)))

    def below(node: dict, p: tuple) -> bool:
        return any(isinstance(v, dict) and (is_b(p + (k,)) or below(v, p + (k,))) for k, v in node.items())
    out: list[tuple[tuple, dict]] = []

    def emit(node: dict, path: tuple) -> None:
        own, kids = {}, []
        for k, v in node.items():
            if isinstance(v, dict) and (is_b(path + (k,)) or below(v, path + (k,))):
                kids.append((k, v))
            else:
                own[k] = v
        if (not path and own) or (path and (is_b(path) or own)):
            out.append((path, own))
        for k, v in kids:
            emit(v, path + (k,))
    emit(doc, ())
    return out


def _lists(own: dict) -> tuple[dict, dict[str, list]]:
    keep, lst = {}, {}
    for k, v in own.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lst[k] = v
        else:
            keep[k] = v
    return keep, lst


def _elem_name(item: dict, i: int) -> str:
    for k in ("name", "group", "path"):
        if isinstance(item.get(k), str) and item[k]:
            return item[k]
    return f"item{i + 1}"


def _elements(c: _MCtx, file: str, tpath: tuple, key: str, items: list[dict], prefix: str, cat: str, kind: str, base_loc: tuple, *,
              ref: str | None = None, title_of: Callable[[dict, str], str] | None = None, destructive: Callable[[dict], bool] | None = None,
              applies: Callable[[dict], list[str]] | None = None, severity: str = "none", owner_destructive: bool = False,
              step_key: str | None = None) -> None:
    """One rule per element of an array of tables; nested arrays of tables (probes of a group) become rules relative to it."""
    for i, item in enumerate(items):
        keep, nested = _lists(item)
        steps = None
        if step_key and isinstance(keep.get(step_key), list) and keep[step_key]:
            steps = keep.pop(step_key)
        nm = _elem_name(item, i)
        rid = c.ids.take(f"{prefix}.{nm}")
        tgt = fmt_target(tpath + (key,), array=True, ref=ref)
        loc = ("elem", file, tpath + (key,), i, base_loc)
        title = title_of(item, nm) if title_of else f"{prefix}: {nm}"
        c.add(rid, title, kind, cat, file, tgt, keep, loc, merge="append", order=i + 1, applies=applies(item) if applies else (),
              destructive=(destructive(item) if destructive else owner_destructive), severity=severity, src=f"[[{'.'.join(tpath + (key,))}]] #{i + 1} {nm}")
        for k2, items2 in nested.items():
            _elements(c, file, (), k2, items2, "probe" if k2 == "probes" else f"{rid}.{k2}", cat, kind, loc, ref=rid,
                      title_of=lambda it, n_: f"Probe: {it.get('title') or n_}", applies=lambda it: [it.get("name", "")], severity=severity)
        for j, st in enumerate(steps or []):
            sn = st if isinstance(st, str) else (st.get("task") or st.get("name") or f"step{j + 1}") if isinstance(st, dict) else f"step{j + 1}"
            c.add(c.ids.take(f"{rid}.step.{sn}"), f"{nm} routine, step {j + 1}: {sn}", kind, cat, file, f"@{rid}:", {step_key: [st]},
                  ("step", file, tpath + (key,), i, j), merge="append", order=j + 1, applies=[sn, f"routine_{sn}"],
                  src=f"[[{key}]] {nm} steps[{j}]")


def _table_rule(c: _MCtx, file: str, path: tuple, own: dict, rid: str, title: str, kind: str, cat: str, *, mode_lift: bool = False,
                applies: Iterable[str] = (), destructive: bool = False, severity: str = "none", prefix: str | None = None,
                elem: dict[str, dict] | None = None) -> None:
    """One rule for a table (its scalars, lists and plain sub-tables) plus one rule per element of every array of tables in it.
    `elem[key]` customises those element rules: prefix, cat, kind, title(item, name), destructive(item), applies(item), steps (list key)."""
    keep, lists = _lists(own)
    mode = None
    if mode_lift and keep.get("mode") in MODES:
        mode = keep.pop("mode")
    dest = destructive or mode == "apply"
    loc = ("table", file, path)
    ap = tuple(applies)
    if path or keep or not lists:                       # the root table of a file that only holds arrays needs no rule of its own
        c.add(c.ids.take(rid), title, kind, cat, file, fmt_target(path), keep, loc, mode=mode, applies=ap, destructive=dest, severity=severity,
              src="[" + ".".join(path) + "]" if path else "(root)")
    for k, items in lists.items():
        sp = (elem or {}).get(k, {})
        _elements(c, file, path, k, items, sp.get("prefix", prefix or rid), sp.get("cat", cat), sp.get("kind", kind), loc,
                  title_of=sp.get("title"), destructive=sp.get("destructive"), applies=sp.get("applies") or (lambda it, ap=ap: list(ap)),
                  severity=severity, owner_destructive=dest, step_key=sp.get("steps"))


def _conv_maint(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"global|caps|tasks\.[^.]+|live"):
        if not path:
            _table_rule(c, "maint.toml", path, own, "config.root", "Global settings (root)", "policy", "safety")
        elif path[0] == "tasks" and len(path) == 2:
            name = path[1]
            info = c.catalog.get(name)
            klass, tier = (info.klass, info.tier) if info else ("C0", "check")
            if info is None:                                    # a task nobody declared that names paths, modes or exemptions is judged like a cleaner
                hits: set[str] = set()
                _sens_hits(own, set(), hits)
                klass = "C1" if hits else klass
            if name == "probes":
                kind, cat = "probe", "monitoring"
            elif name in SPIKE_TASKS:
                kind, cat = "spike", "spike"
            elif name.startswith("routine_") or name.startswith("report_"):
                kind, cat = "schedule", "schedule"
            elif klass == "C0":
                kind, cat = "check", "checks"
            else:
                kind, cat = "cleanup", "cleanup"
            sev = "warn" if klass == "C0" and tier == "check" and kind == "check" else "none"
            pre = {"retention": "retention", "growth_watch": "growth", "c2_candidates": "c2"}.get(name, name)
            _table_rule(c, "maint.toml", path, own, f"task.{name}", info.title if info and info.title != name else f"Task {name}", kind, cat,
                        mode_lift=True, applies=[name], destructive=klass in ("C1", "C2") and kind != "check", severity=sev, prefix=pre)
        elif path == ("live",):
            _table_rule(c, "maint.toml", path, own, "live.settings", "Live monitor settings", "probe", "monitoring", prefix="live.service")
        else:
            _table_rule(c, "maint.toml", path, own, f"config.{'.'.join(path)}", f"Settings [{'.'.join(path)}]", "safety" if path[0] == "caps" else "policy", "safety",
                        applies=[])


ELEM_ROUTINE = {
    "routine": {"prefix": "routine", "cat": "schedule", "kind": "schedule", "steps": "steps",
                "title": lambda it, n: f"Routine: {n} ({it.get('cadence', '?')}, window {it.get('window', '?')})"},
    "system": {"prefix": "routine.system", "cat": "schedule", "kind": "schedule",
               "title": lambda it, n: f"Known system job: {it.get('title') or n}", "applies": lambda it: [it.get("name", "")]}}


def _conv_routine(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"settings|canary|continuous|windows|freeze|steps\.[^.]+"):
        n = ".".join(path)
        if len(path) > 1 and path[0] == "steps":
            _table_rule(c, "routine.toml", path, own, f"routine.stepopts.{path[1]}", f"Options of the {path[1]} step", "schedule", "schedule",
                        applies=[path[1], f"routine_{path[1]}"])
        else:
            _table_rule(c, "routine.toml", path, own, f"routine.{n or 'root'}", f"Routine {n or 'settings (root)'}", "schedule", "schedule",
                        elem=ELEM_ROUTINE)


def _job_destructive(it: dict) -> bool:
    return bool(it.get("disruptive")) or bool(DESTRUCTIVE_JOB.search(f"{it.get('name', '')} {' '.join(map(str, it.get('command', [])))}"))


ELEM_JOBS = {
    "job": {"prefix": "job", "cat": "jobs", "kind": "job", "title": lambda it, n: f"Job: {it.get('title') or n}",
            "destructive": _job_destructive, "applies": lambda it: [it.get("name", "")]},
    "external": {"prefix": "external", "cat": "jobs", "kind": "schedule", "title": lambda it, n: f"External: {it.get('title') or n}",
                 "applies": lambda it: [it.get("name", "")]}}


def _conv_jobs(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"scheduler|scheduler\.[^.]+"):
        n = ".".join(path)
        _table_rule(c, "jobs.toml", path, own, f"sched.{'.'.join(path[1:]) or 'settings' if path else 'root'}", f"Scheduler [{n or 'root'}]", "job", "jobs",
                    elem=ELEM_JOBS)


ELEM_PROBES = {
    "group": {"prefix": "probe-group", "cat": "monitoring", "kind": "probe",
              "title": lambda it, n: f"Probe group: {n} (class {it.get('class', '?')}, every {it.get('interval_s', '?')} s)"},
    "probe": {"prefix": "probe", "cat": "monitoring", "kind": "probe", "title": lambda it, n: f"Probe: {it.get('title') or n}",
              "applies": lambda it: [it.get("name", "")]}}


def _conv_probes(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"defaults|kuma"):
        _table_rule(c, "probes.toml", path, own, f"probes.{'.'.join(path) or 'root'}", f"Probe settings [{'.'.join(path) or 'root'}]", "probe", "monitoring",
                    elem=ELEM_PROBES)


_CLASSES_B = r"classes|units|policy|defaults\.[^.]+|floors|ladder|ladder\.(throttle|max_per_day)|ladder\.signals\.[^.]+|bulkheads"


def _conv_classes(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, _CLASSES_B):
        n = ".".join(path)
        who = ["qos_classes"] if path and path[0] in ("defaults", "floors") else ["bulkhead_check"] if path and path[0] == "bulkheads" else \
            ["pressure_state", "pressure_response"] if path and path[0] == "ladder" else []
        _table_rule(c, "classes.toml", path, own, f"classes.{n or 'root'}", f"Service classes and ladder [{n or 'root'}]", "spike", "spike", applies=who)


def _conv_notify(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"transport|site|routes|routes\.[^.]+|significant|task_routes|todo|escalation|dedupe|dedupe\.[^.]+|budget|"
                                  r"budget\.[^.]+|quiet_hours|retry|ack|log"):
        n = ".".join(path)
        _table_rule(c, "notify.toml", path, own, f"notify.{n or 'root'}", f"Notifications [{n or 'root'}]", "alert", "alerts")


def _conv_ack(c: _MCtx, doc: dict, _raw: str) -> None:
    for path, own in _tables(doc, r"ack|inbox|key\.[^.]+"):
        n = ".".join(path)
        _table_rule(c, "ack.toml", path, own, f"ack.{n or 'root'}", f"Acknowledgements [{n or 'root'}]", "policy", "ack", applies=path[1:2] if path[:1] == ("key",) else [])


def _protect_groups(raw: str, patterns: list) -> list[tuple[str, list]]:
    """Split protected.toml `patterns` by the comment lines that head its groups; falls back to one group if the text disagrees."""
    m = re.search(r"^patterns\s*=\s*\[[^\n]*\n", raw, re.M)
    one = [("all protected patterns", list(patterns))]
    if not m:
        return one
    groups: list[tuple[str, list]] = []
    title, items = "protected patterns", []
    for ln in raw[m.end():].splitlines():
        s = ln.strip()
        if s == "]":
            break
        if s.startswith("#"):
            if items:
                groups.append((title, items))
            title, items = s.lstrip("# ").strip() or title, []
        elif s:
            try:
                items += tomllib.loads(f"x = [\n{s}\n]")["x"]
            except tomllib.TOMLDecodeError:
                return one
    if items:
        groups.append((title, items))
    return groups if [p for _t, g in groups for p in g] == list(patterns) else one


def _conv_protected(c: _MCtx, doc: dict, raw: str) -> None:
    pats = doc.get("patterns")
    if isinstance(pats, list) and all(isinstance(p, str) for p in pats):
        for i, (title, items) in enumerate(_protect_groups(raw, pats)):
            words = re.sub(r"\(.*?\)", "", title)
            c.add(c.ids.take("protect." + _slug(" ".join(words.split()[:5]), 40)), f"Never touch: {title}"[:120], "protection", "protection",
                  "protected.toml", "", {"patterns": items}, ("items", "protected.toml", (), "patterns", len(items)), merge="append", order=i + 1,
                  src="patterns group " + title[:40])
    rest = {k: v for k, v in doc.items() if k != "patterns"}
    for path, own in _tables(rest, r"busy"):
        _table_rule(c, "protected.toml", path, own, f"protect.{'.'.join(path) or 'root'}", f"Busy probes and gates [{'.'.join(path) or 'root'}]", "protection", "protection")


CONVERTERS: dict[str, Callable[[_MCtx, dict, str], None]] = {
    "maint.toml": _conv_maint, "routine.toml": _conv_routine, "jobs.toml": _conv_jobs, "probes.toml": _conv_probes,
    "classes.toml": _conv_classes, "notify.toml": _conv_notify, "ack.toml": _conv_ack, "protected.toml": _conv_protected}


def migrate_build(src: dict[str, dict], raw: dict[str, str] | None = None, *, catalog: dict[str, TaskInfo] | None = None,
                  today: str | None = None) -> list[MRule]:
    """Rules for every logical unit of the given legacy documents (file name -> parsed data). Pure and deterministic."""
    raw = raw or {}
    names = known_names(Compiled(docs=src), catalog or {})
    c = _MCtx(catalog or {}, today or _dt.date.today().isoformat(), names)
    for f in MANAGED_FILES:
        if f in src:
            CONVERTERS[f](c, src[f], raw.get(f, ""))
    return c.out


# --------------------------------------------------------------------------- registry file text
HUMAN_FIELDS = ("title", "why", "does", "applies_to", "severity", "destructive", "proof", "principle", "owner_notes", "since")


def render_registry(mrules: list[MRule], carry: dict[str, Rule] | None = None) -> dict[str, str]:
    """registry file name -> TOML text. `carry` (existing rules by id) keeps the human-written fields of ids that already exist."""
    by_cat: dict[str, list[dict]] = {}
    for m in mrules:
        r = dict(m.rule)
        old = (carry or {}).get(r["id"])
        if old:
            for f in HUMAN_FIELDS:
                v = getattr(old, f)
                if v not in ("", None, [], False) or f in ("why", "does", "title"):
                    r[f] = v
        by_cat.setdefault(m.cat, []).append({k: r[k] for k in RULE_FIELDS if k in r})
    out = {}
    for cat, rules in sorted(by_cat.items(), key=lambda kv: list(OUT_FILES).index(kv[0])):
        head = (f"# homelab-maint rules registry: {CATEGORIES[cat]}. This file DEFINES what the script does; the legacy config files are\n"
                f"# generated from it (homelab-maint rules check | diff | sync). {TODO} marks text still to be written by the owner or an agent.\n\n")
        out[OUT_FILES[cat]] = head + dumps({"meta": {"category": cat, "title": CATEGORIES[cat], "blurb": OUT_BLURB[cat]}, "rule": rules},
                                           aot_keys=("rule",))
    return out


# --------------------------------------------------------------------------- baseline (the shipped safety floor)
MIN_ROOT_DEPTH = 2
NEVER_TOUCH = [
    r"^/var/lib/(docker|libvirt|containerd)(/|$)", r"^/mnt/backup(/|$)", r"^/media/(Immich|nextcloud)(/|$)", r"^/var/snap/plexmediaserver",
    r"^/media/SandiskSSD/plex", r"/Plex Media Server(/|$)", r"^/volume1/docker/plex(/|$)", r"^/usr/share/ollama", r"/\.ollama(/|$)",
    r"/comfyui/models", r"(surreal|notebook)_data|pgdata", r"/\.config/Cursor(/|$)", r"/\.cursor(/|$)", r"^/home/[^/]+/(models|\.ssh|\.gnupg)(/|$)",
    r"(^|/)ai-stack(/|$)", r"^/volume1/docker/kavita(/(?!config/(logs|backups|cache)(/|$))|$)",
    r"^/volume1/docker/(radarr|sonarr|prowlarr|bazarr|lidarr|readarr|sabnzbd|seerr|overseerr|jellyseerr)(/(?!config/cache(/|$))|$)",
    r"/\.docker-data(/(?!tunarr/cache/)|$)"]
LIMITS = [
    ("maint.toml", "caps.max_gib_per_run", 100, "per-run byte cap; the shipped value is 40 GiB, a typo must not lift it to terabytes"),
    ("maint.toml", "caps.max_items_per_run", 2000, "per-run action cap; shipped 500"),
    ("maint.toml", "tasks.*.max_gib_per_run", 100, "a task may lower its cap, never lift it past the hard limit"),
    ("maint.toml", "tasks.*.max_items_per_run", 2000, "same for the item cap"),
    ("notify.toml", "budget.hard_cap_per_day", 60, "nothing passes this many messages a day (a loop must not flood the phone); shipped 40"),
    ("classes.toml", "ladder.max_restarts_per_6h", 6, "restarts of proven-stuck containers; shipped 2"),
    ("classes.toml", "ladder.max_per_day.restart", 12, "shipped 4"),
    ("classes.toml", "ladder.max_per_day.emergency", 6, "shipped 3"),
    ("ack.toml", "ack.max_days", 365, "an acknowledgement silences an exact error for at most a year")]


def baseline_text(patterns: list[str], *, today: str, derived_from: str = "etc/protected.toml") -> str:
    """00-baseline-invariants.toml: the protection floor derived from protected.toml, the never-touch regexes, the hard limits and the
    four doc-only rules that explain them. Shipped by the release (replace it on upgrade), never edited by the owner."""
    doc = {"meta": {"category": "safety", "title": "Baseline invariants",
                    "blurb": "The safety floor the registry cannot go below: protected patterns, hard caps, delete confinement."},
           "baseline": {"version": 1, "derived_from": f"{derived_from} ({today})", "protected_patterns": list(patterns), "never_touch": NEVER_TOUCH,
                        "min_root_depth": MIN_ROOT_DEPTH, "limit": [{"file": f, "path": p, "max": m, "why": w} for f, p, m, w in LIMITS],
                        "unprotect": [{"file": f, "path": pth, "allow": list(a), "why": w} for f, pth, a, w in UNPROTECT_ALLOW],
                        "apply_keys": [{"task": t, "key": k, "why": "the ladder rung switches (reclaim ships on)"} for t, k in APPLY_KEYS]},
           "rule": [
               {"id": "safety.protected-superset", "title": "Protected patterns can only grow", "kind": "safety", "severity": "crit", "since": today,
                "principle": "never kill by size; protect what must not be touched",
                "why": "Everything the script may kill, restart or delete is checked against protected.toml first. Losing a pattern would silently "
                       "expose a database or a media library, so the registry refuses to drop any baseline pattern.",
                "does": "`rules check` compares the compiled protected.toml with the baseline's protected_patterns (this file AND the copy pinned "
                        "in the release: the stricter of the two counts, a weaker file is refused). A missing pattern blocks the sync and raises "
                        "an alert. Every `unprotect` regex (a per-task exemption from protected.toml) must be on the baseline allow-list. Only "
                        "the owner can allow a removal or an extra exemption: [meta] allow_baseline_removal / allow_unprotect in "
                        "99-owner-overrides.toml; that is logged loudly and mentioned in the notice."},
               {"id": "safety.hard-caps", "title": "Per-run caps stay under hard limits", "kind": "safety", "severity": "crit", "since": today,
                "principle": "bounded blast radius",
                "why": "A cleaner that deletes too much in one run cannot be undone. Caps bound one run; hard limits bound the caps themselves.",
                "does": "Every [[baseline.limit]] entry is a numeric ceiling on a compiled config key (caps, per-task caps, the notification hard "
                        "cap, the restart budgets). A value above its ceiling blocks the sync. A change of the effective baseline itself (a lower "
                        "ceiling raised, a pattern dropped) is recorded in the history and announced as significant."},
               {"id": "safety.delete-confinement", "title": "Deleting rules stay inside allowed_roots", "kind": "safety", "severity": "crit",
                "since": today, "principle": "least privilege for deletion",
                "why": "A retention or cache-trim rule with a wrong path (or a path that normalises to somewhere else) is how a cleaner eats data.",
                "does": "For every delete-type task (class C1, or a task the code does not declare) the COMPILED config is checked, whichever rule, "
                        "kind or flag wrote it: each path (and each glob joined to its path) must be absolute and normalised, lie inside the task's "
                        "allowed_roots (themselves at least two components deep) and match no never_touch regex; a glob must stay relative. "
                        "Plan-only (C2) tasks are exempt: a human approves the exact plan."},
               {"id": "safety.destructive-explicit", "title": "Apply mode must be explicit", "kind": "safety", "severity": "warn", "since": today,
                "principle": "report first, change second",
                "why": "Every cleaner ships in report mode. Turning one on is the owner's decision, one task at a time, and must be visible.",
                "does": "Every \"apply\" the compiled config holds under [tasks.*] must come from a rule's own `mode = \"apply\"` field (or a "
                        "baseline-listed ladder rung key) on a rule flagged destructive = true; an apply hidden in params or in a nested table is "
                        "refused, and so is a cleaner's path or exemption written by a rule that is not flagged destructive. A destructive rule "
                        "without a mode field compiles without one, which the runner reads as report."}]}
    return ("# homelab-maint rules registry: baseline invariants. SHIPPED with the release and replaced on upgrade; do not edit. It is read by\n"
            "# `homelab-maint rules check|sync`, never compiled into a config file. protected_patterns below = etc/protected.toml at release time.\n\n"
            + dumps(doc, aot_keys=("rule",)))


# --------------------------------------------------------------------------- the proof
@dataclass
class Proof:
    ok: bool
    files: dict[str, list[str]]                 # legacy file -> differences (empty = proven equal)
    errors: list[str]
    warnings: list[str] = field(default_factory=list)


def _parse_legacy(path: Path) -> tuple[dict, str] | None:
    try:
        raw = path.read_bytes()
        return tomllib.loads(raw.decode("utf-8")), raw.decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path.name}: {type(exc).__name__}: {str(exc)[:120]}") from exc


def load_legacy(src_dir: Path, files: Iterable[str] = MANAGED_FILES) -> tuple[dict[str, dict], dict[str, str]]:
    docs, raws = {}, {}
    for f in files:
        got = _parse_legacy(src_dir / f)
        if got:
            docs[f], raws[f] = got
    return docs, raws


def prove(src_docs: dict[str, dict], an: Analysis) -> Proof:
    """Compile the registry, parse every generated text back with tomllib and demand it equals the parse of the legacy file
    (key order and comments ignored; list order, scalar types and every key matter)."""
    errs = list(an.errors)
    files: dict[str, list[str]] = {}
    if an.comp is None or not an.comp.texts and an.comp.docs:
        return Proof(False, files, errs or ["the registry does not compile"])
    for f, doc in src_docs.items():
        text = an.comp.texts.get(f)
        if text is None:
            files[f] = ["not produced by the registry"]
            continue
        diffs = diff_docs(doc, tomllib.loads(text))
        files[f] = [f"{p}: {_brief(a if a is not _MISSING else '<missing>')} != {_brief(b if b is not _MISSING else '<missing>')}" for p, a, b in diffs[:20]]
    extra = [f for f in an.comp.texts if f not in src_docs]
    return Proof(not errs and all(not d for d in files.values()), files, errs + [f"registry also produces {f} (no such legacy file)" for f in extra])


def migrate(src_dir: Path | str, out_dir: Path | str, *, force: bool = False, dry_run: bool = False, today: str | None = None,
            files: Iterable[str] = MANAGED_FILES, catalog: dict[str, TaskInfo] | None = None) -> tuple[Proof, dict[str, str]]:
    """Build rules.d from the legacy config files in `src_dir`, PROVE that compiling it gives the same data, and only then write it to
    `out_dir`. Refuses to overwrite an existing registry unless force (which keeps the human text of ids that survive). Returns (proof, texts)."""
    src, out = Path(src_dir), Path(out_dir)
    today = today or _dt.date.today().isoformat()
    docs, raws = load_legacy(src, files)
    if "protected.toml" not in docs:
        raise ValueError("protected.toml is required: the baseline protection floor is derived from it")
    cat = catalog if catalog is not None else task_catalog()
    mr = migrate_build(docs, raws, catalog=cat, today=today)
    existing = {}
    if out.is_dir() and any(p.name != BASELINE_FILE for p in out.glob("*.toml")):      # the shipped baseline alone is not a registry yet
        if not force:
            raise FileExistsError(f"{out} already holds a registry; use --force to regenerate it (human-written text of surviving ids is kept)")
        existing = {r.id: r for r in load_registry(rdir=out, trust=False).rules}
    texts = render_registry(mr, existing)
    base = out / BASELINE_FILE
    texts[BASELINE_FILE] = base.read_text() if base.is_file() else baseline_text(
        list(dict.fromkeys([*package_floor()["protected_patterns"], *docs["protected.toml"].get("patterns", [])])), today=today)
    with _lazy("tempfile").TemporaryDirectory(prefix="hm-migrate-") as td:
        tmp = Path(td) / "rules.d"
        tmp.mkdir()
        for n, t in texts.items():
            (tmp / n).write_text(t)
        an = analyze(rdir=tmp, trust=False, catalog=cat)
        proof = prove(docs, an)
        proof.warnings = an.warnings
    if proof.ok and not dry_run:
        out.mkdir(parents=True, exist_ok=True)
        for n, t in sorted(texts.items()):
            if n == BASELINE_FILE and base.is_file():
                continue
            atomic_write(out / n, t.encode(), 0o644)
    return proof, texts


# =========================================================================== CLI: homelab-maint rules ...
def _when(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "-"


def _leaves(doc: Any, path: tuple = ()) -> dict[tuple, Any]:
    """Every leaf of a document keyed by its path (list elements by index; an empty table is a leaf)."""
    if isinstance(doc, dict) and doc:
        return {k: v for key, x in doc.items() for k, v in _leaves(x, path + (key,)).items()}
    if isinstance(doc, list) and doc and all(isinstance(x, dict) for x in doc):
        return {k: v for i, x in enumerate(doc) for k, v in _leaves(x, path + (i,)).items()}
    return {path: doc}


def _at(doc: Any, path: tuple) -> Any:
    for p in path:
        doc = doc[p]
    return doc


def _fmt(v: Any) -> str:
    s = _inline(v) if not isinstance(v, str) else json.dumps(v)
    return s if len(s) <= 90 else s[:87] + "..."


def _cat_ok(an: Analysis) -> bool:
    return an.reg.present and an.comp is not None


def cmd_list(a: Any) -> int:
    reg = load_registry()
    if not reg.present:
        print(f"no registry at {reg.dir} (run: homelab-maint rules migrate)")
        return 1
    rows = [r for r in sorted(reg.rules, key=lambda r: (r.source, r.order, r.id))
            if (not a.category or r.category == a.category) and (not a.kind or r.kind == a.kind)
            and (not a.enabled or r.enabled) and (not a.disabled or not r.enabled) and (not a.destructive or r.destructive)]
    if a.json:
        print(json.dumps([r.as_dict() for r in rows], indent=1, default=str))
    else:
        for r in rows:
            print(f"{r.id:<44} {r.kind:<10} {r.category:<10} {r.mode or '-':<6} {'on ' if r.enabled else 'OFF'} {'D' if r.destructive else ' '}  {r.title}")
        print(f"{len(rows)} of {len(reg.rules)} rules, registry {short(reg.hash)}" + (f", {len(reg.errors)} error(s): run `rules check`" if reg.errors else ""))
    return 0


def cmd_show(a: Any) -> int:
    reg = load_registry()
    r = reg.by_id().get(a.id)
    if r is None:
        print(f"no rule {a.id!r}" + _close(a.id, [x.id for x in reg.rules]), file=sys.stderr)
        return 1
    d = r.as_dict()
    if a.json:
        print(json.dumps(d, indent=1, default=str))
        return 0
    for k in ("id", "title", "kind", "category", "source_file", "enabled", "destructive", "severity", "mode", "since", "principle"):
        print(f"{k:<12} {d[k]}")
    for k in ("why", "does", "proof", "owner_notes"):
        if d[k]:
            print(f"{k:<12} {d[k]}")
    print(f"{'applies_to':<12} {', '.join(d['applies_to']) or '-'}")
    if r.file:
        print(f"{'writes':<12} {r.file}  {r.target or '(root)'}  merge={r.merge}" + (f"  order={r.order}" if r.order else ""))
        for k, v in d["params"].items():
            print(f"  {k} = {_fmt(v)}")
    else:
        print("writes       nothing (a documented policy)")
    return 0


def cmd_check(a: Any) -> int:
    an = analyze(consumers=True)
    reg = an.reg
    if not reg.present:
        print(f"no registry at {reg.dir}")
        return 1
    if a.todo:
        for r in reg.rules:
            if TODO in (r.why + r.does + r.proof):
                print(f"{r.id}: " + ", ".join(f for f in ("why", "does", "proof") if TODO in getattr(r, f)))
        return 0
    if a.json:
        print(json.dumps({"hash": reg.hash, "valid": not an.errors, "errors": an.errors, "warnings": an.warnings, "rules": len(reg.rules)}, indent=1))
        return 1 if an.errors else 0
    for e in an.errors:
        print(f"ERROR   {e}")
    for w in an.warnings:
        print(f"warning {w}")
    n = len(reg.rules)
    print(f"registry {short(reg.hash)}: {_plural(n, 'rule')}, {_plural(len(an.errors), 'error')}, {_plural(len(an.warnings), 'warning')}"
          + (" -> OK" if not an.errors else " -> BLOCKED: the last good generated config stays in force"))
    return 1 if an.errors else 0


def cmd_diff(_a: Any) -> int:
    st, conf = core.STATE_DIR, core.CONF_DIR
    an = analyze()
    if not an.reg.present:
        print(f"no registry at {an.reg.dir}")
        return 1
    cur = read_current(st) or {}
    old = (load_snapshot(st, cur["hash"]) or ({}, {}))[0] if cur.get("hash") else {}
    new = snapshot_rules(an.reg)
    ch = diff_rules(old, new)
    print(f"== registry {short(an.reg.hash)} vs last applied {short(cur.get('hash')) or '(none)'}")
    if not (ch["added"] or ch["removed"] or ch["modified"]):
        print("   no pending rule changes")
    for i in ch["added"]:
        print(f"   + {i}")
    for i in ch["removed"]:
        print(f"   - {i}")
    for m in ch["modified"]:
        print(f"   ~ {m['id']}: " + ", ".join(f"{f} {_brief(m['before'].get(f))} -> {_brief(m['after'].get(f))}" for f in m["fields"][:5]))
    print("== compiled registry vs generated files on disk")
    if an.comp is None or not an.ok:
        print("   (the registry does not validate: run `rules check`)")
        return 1
    drift = 0
    for f in fill_orphans(an.comp, conf, cur.get("generated") or {}):
        print(f"   {f}: no rule writes it any more: it becomes an empty generated file")
    for f, doc in sorted(an.comp.docs.items()):
        try:
            raw = _read_regular(conf / f, 8 << 20)
        except OSError:
            print(f"   {f}: missing on disk")
            drift += 1
            continue
        if raw == an.comp.texts[f].encode():
            print(f"   {f}: identical")
            continue
        drift += 1
        try:
            dd = diff_docs(tomllib.loads(raw.decode("utf-8")), doc)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            print(f"   {f}: on disk is not valid TOML")
            continue
        print(f"   {f}: " + ("same data, different text (header/comments/format)" if not dd else f"{len(dd)} difference(s)"))
        for p, x, y in dd[:8]:
            print(f"      {p}: disk {_brief(x if x is not _MISSING else '<missing>')} -> registry {_brief(y if y is not _MISSING else '<missing>')}")
    return 0 if not drift and not (ch["added"] or ch["removed"] or ch["modified"]) else 1


def cmd_sync(a: Any) -> int:
    res = sync(wait=True, adopt=a.adopt, hooks=NO_HOOKS if a.no_notify else None)
    print(res.line())
    for e in res.errors[:10]:
        print(f"  ERROR {e}")
    for w in res.warnings[:5]:
        print(f"  warning {w}")
    if len(res.warnings) > 5:
        print(f"  ... {len(res.warnings) - 5} more warnings (rules check)")
    print(f"  ({res.ms} ms)")
    return {"applied": 0, "unchanged": 0, "no_registry": 0, "locked": 3, "locked-too-long": 3}.get(res.status, 1)


def cmd_history(a: Any) -> int:
    rows = history(a.n)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        flag = "applied" if r.get("applied") else ("REFUSED" if r.get("kind") else "-")
        flag = r.get("kind", "").upper() if r.get("kind") in ("error", "disk_unsafe") else flag
        print(f"{_when(r.get('ts'))}  {short(r.get('from')) or '-':<12} -> {short(r.get('to')):<12} +{r.get('added_count', len(r.get('added') or []))} "
              f"-{r.get('removed_count', len(r.get('removed') or []))} ~{r.get('modified_count', len(r.get('modified') or []))}  {flag}"
              + (f"  rollback to {short(r['rollback_to'])}" if r.get("rollback_to") else "") + (f"  drift: {', '.join(r['drift'])}" if r.get("drift") else "")
              + (f"  BASELINE WEAKENED ({len(r['baseline_change']['weakened'])})" if (r.get("baseline_change") or {}).get("weakened") else ""))
        for e in (r.get("errors") or [])[:3]:
            print(f"    problem: {e}")
        for m in (r.get("modified") or [])[:3]:
            print(f"    ~ {m['id']}: " + ", ".join(f"{f} {_brief(m['before'].get(f))} -> {_brief(m['after'].get(f))}" for f in m["fields"][:3]))
    if not rows:
        print("no registry changes recorded yet")
    return 0


def cmd_rollback(a: Any) -> int:
    res = rollback(a.hash, hooks=NO_HOOKS if a.no_notify else None)
    print(res.line())
    for e in res.errors[:5]:
        print(f"  ERROR {e}")
    return 0 if res.status in ("applied", "unchanged") else 1


def cmd_export(a: Any) -> int:
    if a.write:
        names = write_public()
        print("wrote " + ", ".join(names) if names else "nothing written")
        return 0 if names else 1
    print(json.dumps(build_rules_json(), indent=1 if a.pretty else None, separators=None if a.pretty else (",", ":"), ensure_ascii=False, default=str))
    return 0


def cmd_explain(a: Any) -> int:
    an = analyze()
    if not _cat_ok(an):
        print("the registry does not compile: run `rules check`", file=sys.stderr)
        return 1
    comp = an.comp
    assert comp is not None
    mine = [r for r in an.reg.rules if a.task in _related(r.as_dict()) or r.target.startswith(f"tasks.{a.task}")]
    print(f"task {a.task}: {_plural(len(mine), 'rule')}")
    for r in mine:
        print(f"  {r.id}  [{r.kind}{', destructive' if r.destructive else ''}{', mode ' + r.mode if r.mode else ''}{'' if r.enabled else ', DISABLED'}]  {r.title}")
    tbl = (comp.docs.get("maint.toml", {}).get("tasks") or {}).get(a.task)
    if tbl is None:
        print("  no [tasks." + a.task + "] table is compiled: the task runs on its defaults")
        return 0 if mine else 1
    print("effective values ([tasks." + a.task + "] in maint.toml):")
    for k, v in tbl.items():
        w = comp.writers.get(("maint.toml", ("tasks", a.task, k))) or ",".join(x[1] for x in comp.lists.get(("maint.toml", ("tasks", a.task, k)), []))
        print(f"  {k} = {_fmt(v)}   <- {w or '?'}")
    return 0


def cmd_where(a: Any) -> int:
    an = analyze()
    if not _cat_ok(an):
        print("the registry does not compile: run `rules check`", file=sys.stderr)
        return 1
    comp = an.comp
    assert comp is not None
    want_file, _, key = a.key.rpartition(":") if ":" in a.key else ("", "", a.key)
    hits = 0
    src = {r.id: r.source for r in an.reg.rules}
    for (f, path), rid in sorted(comp.writers.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        d = _dotted(path)
        if (not want_file or f == want_file) and (d == key or d.endswith("." + key) or path[-1] == key):
            try:
                v = _fmt(_at(comp.docs[f], path))
            except (KeyError, IndexError, TypeError):
                v = "?"
            print(f"{f}  {d} = {v}   <- {rid} ({src.get(rid, '?')})")
            hits += 1
    for (f, path), runs in sorted(comp.lists.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        d = _dotted(path)
        if (not want_file or f == want_file) and (d == key or d.endswith("." + key) or path[-1] == key):
            print(f"{f}  {d}   <- " + ", ".join(f"{rid}@{i}" for i, rid in runs[:6]) + (" ..." if len(runs) > 6 else ""))
            hits += 1
    if not hits:
        print(f"no rule writes {a.key!r}")
    return 0 if hits else 1


def cmd_orphans(_a: Any) -> int:
    an = analyze()
    if not _cat_ok(an):
        print("the registry does not compile: run `rules check`", file=sys.stderr)
        return 1
    comp = an.comp
    assert comp is not None
    n = 0
    for f, doc in sorted(comp.docs.items()):
        try:
            disk = tomllib.loads(_read_regular(core.CONF_DIR / f, 8 << 20).decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            print(f"{f}: not readable on disk (nothing to compare)")
            continue
        have, want = _leaves(disk), _leaves(doc)
        for p in sorted(set(have) - set(want), key=str):
            print(f"{f}: {_dotted(p)} = {_fmt(have[p])}   no rule owns it (the next sync removes it)")
            n += 1
    print(f"{_plural(n, 'orphan key')}")
    return 1 if n else 0


def write_baseline(src_dir: Path | str, dest: Path | str, *, today: str | None = None, force: bool = False) -> int:
    """Regenerate the shipped baseline file from src_dir/protected.toml. Refuses to drop a pattern the existing baseline lists (force)."""
    docs, _raws = load_legacy(Path(src_dir), ("protected.toml",))
    if "protected.toml" not in docs:
        raise ValueError(f"{src_dir}/protected.toml not found")
    pats = docs["protected.toml"].get("patterns", [])
    if not (isinstance(pats, list) and all(isinstance(p, str) for p in pats)):
        raise ValueError("protected.toml: patterns must be a list of strings")
    pats = list(dict.fromkeys([*package_floor()["protected_patterns"], *pats]))       # the mirror is never weaker than the release floor
    dest = Path(dest)
    if dest.is_file() and not force:
        old = (load_registry(rdir=dest.parent, trust=False).baseline or {}).get("protected_patterns", [])
        lost = [p for p in old if p not in pats]
        if lost:
            raise ValueError(f"the new baseline would drop {len(lost)} pattern(s) (first: {lost[0]!r}); use --force to shrink it on purpose")
    atomic_write(dest, baseline_text(pats, today=today or _dt.date.today().isoformat()).encode(), 0o644)
    return len(pats)


def cmd_migrate(a: Any) -> int:
    src, out = Path(a.src or core.CONF_DIR), Path(a.out or rules_dir())
    try:
        if a.baseline_only:
            n = write_baseline(src, a.baseline_only, today=a.today, force=a.force)
            print(f"baseline written: {n} protected patterns -> {a.baseline_only}")
            return 0
        if a.verify:
            docs, _raw = load_legacy(src)
            an = analyze(rdir=out, trust=False)
            proof, texts = prove(docs, an), {}
        else:
            proof, texts = migrate(src, out, force=a.force, dry_run=a.dry_run, today=a.today)
    except (FileExistsError, ValueError) as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 2
    for f, diffs in sorted(proof.files.items()):
        print(f"  {f}: " + ("EQUAL (compile(rules.d) parses to the same data)" if not diffs else f"{len(diffs)} DIFFERENCE(S)"))
        for d in diffs[:5]:
            print(f"      {d}")
    for e in proof.errors[:10]:
        print(f"  ERROR {e}")
    if not proof.ok:
        print("migrate: proof FAILED, nothing was written", file=sys.stderr)
        return 1
    if texts:
        print(f"  {len(texts)} registry file(s): " + ", ".join(sorted(texts)) + ("   (dry run: nothing written)" if a.dry_run else f"   -> {out}"))
    print("proof OK" + (f" ({_plural(len(proof.warnings), 'warning')} from the checks)" if proof.warnings else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    argparse = _lazy("argparse")
    ap = argparse.ArgumentParser(prog="homelab-maint rules", description="The rules registry: what the script does, defined once on the host.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser("list", help="rules, filtered")
    p.add_argument("--category", choices=list(CATEGORIES))
    p.add_argument("--kind", choices=KINDS)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enabled", action="store_true")
    g.add_argument("--disabled", action="store_true")
    p.add_argument("--destructive", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one rule")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="validate the registry: every error and warning")
    p.add_argument("--todo", action="store_true", help="list rules that still carry TODO-CONTENT")
    p.add_argument("--json", action="store_true")
    sub.add_parser("diff", help="registry vs last applied vs generated files")
    p = sub.add_parser("sync", help="validate + compile + record (idempotent; the tick calls it)")
    p.add_argument("--adopt", action="store_true", help="replace hand-maintained config files that differ (the original is kept)")
    p.add_argument("--no-notify", action="store_true")
    p = sub.add_parser("history", help="recorded registry changes")
    p.add_argument("n", nargs="?", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rollback", help="restore an applied registry (default: the previous one)")
    p.add_argument("hash", nargs="?")
    p.add_argument("--no-notify", action="store_true")
    p = sub.add_parser("export", help="print rules.json (or --write it with manifest.json into the public dir)")
    p.add_argument("--write", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p = sub.add_parser("migrate", help="one time: build rules.d from the current config files and PROVE the compile equals them")
    p.add_argument("--from", dest="src", help="directory of the legacy files (default: the conf dir)")
    p.add_argument("--out", help="registry directory to write (default: <conf>/rules.d)")
    p.add_argument("--force", action="store_true", help="regenerate over an existing registry, keeping the text of ids that survive")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true", help="only prove an existing registry against the legacy files")
    p.add_argument("--baseline-only", metavar="FILE", help="only (re)write the baseline invariants file from the legacy protected.toml")
    p.add_argument("--today", help=argparse.SUPPRESS)
    p = sub.add_parser("explain", help="which rules configure a task, and its effective values")
    p.add_argument("task")
    p = sub.add_parser("where", help="which rule sets a config key (FILE:path, a.b.c or a bare key)")
    p.add_argument("key")
    sub.add_parser("orphans", help="keys in the generated files that no rule owns")
    a = ap.parse_args(argv)
    fn = {"list": cmd_list, "show": cmd_show, "check": cmd_check, "diff": cmd_diff, "sync": cmd_sync, "history": cmd_history,
          "rollback": cmd_rollback, "export": cmd_export, "migrate": cmd_migrate, "explain": cmd_explain, "where": cmd_where,
          "orphans": cmd_orphans}[a.cmd]
    try:
        return int(fn(a))
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - a CLI must end in a message, not a traceback
        print(f"rules {a.cmd}: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
