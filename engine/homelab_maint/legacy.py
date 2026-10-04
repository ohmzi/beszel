"""legacy: retire what the umbrella replaces, one reversible cutover at a time (SPEC4 S11).

    homelab-maint migrate status|plan|check ITEM|cutover ITEM|rollback ITEM|journal|retired|audit|runbook|validate|export

WHAT IT IS. etc/legacy-retirement.toml lists every legacy scheduled thing and hook found on this host (one [[item]] each) with
a MODE: adapter (the umbrella schedules/gates/logs/alerts the proven script, the old timer or cron line goes), port (native
task replaces it; timer disabled, script moved), retire (superseded, nothing replaces it), observe (OS-managed, verified by
`os_jobs`, never touched) or keep (daemon/app plumbing, only probed). This module runs the cutovers.

SAFETY MODEL (the same rules as core.Ctx.act: report-only by default, fail closed, everything audited, PAUSE honoured)
  * `cutover`/`rollback` are DRY RUNS unless --apply: they print the exact commands and change nothing.
  * Cutover refuses unless (1) PAUSE/PAUSE.migrate is absent, (2) every depends_on item is retired for its soak_days, and when that
    item handed a job to the tick the tick has recorded `verified_runs` green runs of it since the cutover (the soak counts from the
    first one: calendar days with a job the tick never launched prove nothing), (3) nothing is running: no unit of the item, no job of
    the item under the tick (sched.json + a live process), and for backup items no held /run/lock/backup-*.lock (never cut over, and
    never roll back, under a running backup; not overridable; the tick's own lock is held meanwhile so it cannot launch the job in
    between), (4) the parity check is green (replacement healthy N consecutive runs over M hours, config really able to act,
    notification path proven; for an adapter the job equals the legacy unit and scheduler validate is clean). --force --reason "..."
    overrides (2) and (4) only, and is recorded as forced in the journal.
  * Retire = disable units (never mask, never delete), MOVE scripts to <legacy_root>/<item>/ with a README (never rm), comment
    crontab lines with a tag (every other line is proven unchanged before and after), leave a forwarding stub where something
    else (smartd) still calls the old path: a script that gets a stub is COPIED first and the stub replaces it in one rename(2), so the
    path is never absent (smartd calls its hook once and does not retry). Nothing is deleted anywhere.
  * Rollback fidelity: before each action its prior state is recorded (UnitFileState/ActiveState, file mode/owner/sha256, the
    original cron line), persisted write-ahead in STATE_DIR/migration.json. Rollback replays the inverse from that record, in
    reverse order, and verifies the state equals the record (an action that was already satisfied before we started is NOT
    touched on rollback). A failure while retiring undoes this run's earlier actions automatically.
  * Idempotent: a second cutover finds every action satisfied and changes nothing. A re-run keeps the records of an earlier run for
    every step that is still in effect, whatever state that run ended in (retired and drifting, partial after a kill, attention after
    an undo that could not finish): an interrupted `started` step that now holds is promoted to done keeping the prior state recorded
    BEFORE it touched anything, so a later rollback still undoes everything. Only a completed rollback, or a step an undo marked
    "undone", starts clean. Cutover and rollback hold a flock.
  * Every mutating step goes through Migrator._act (audit.jsonl `migrate`: dry-run | done | refused-paused | failed: ...), plus
    STATE_DIR/maintenance-journal.jsonl (website journal), STATE_DIR/changes.jsonl (change log) and one `maintenance` message
    through notify.send (rollback is `significant`: text + email).
  * All system access goes through Host (an injectable command runner and an optional path prefix `root`), so the whole
    executor is tested against a fake systemctl/crontab and a tmp-dir filesystem; nothing in the tests touches the host.

Parity (`check`): kinds task|job|probe (consecutive green runs from history.jsonl spanning min_hours; job = the tick's runs, history kind
"job"; a probe must also be up NOW with a fresh last_run), scheduled (the scheduler KNOWS the job: not proof that it runs), unit_equiv
(the effective jobs.toml job launches exactly what `systemctl show` says the legacy service launches: the stand-in for a proof run that
cannot exist before the cutover), scheduler_validate (`scheduler validate` is clean for the job: one jobs.toml typo makes the tick
schedule nothing), scheduler_health (the tick ran lately), job_attr (an attribute of the EFFECTIVE jobs.toml job equals / contains a value, e.g.
self_notifies), status_json / file_age (the legacy status file the adapter will parse), task_applies (the native task has mode = apply AND
something really passes --apply: the check-tier service ships without it, and a port that can only REPORT is not a replacement), config,
command (argv list, no shell, read-only), notify (a delivered notification of kind test|... in notifications.jsonl), os_job (os_jobs row
exists and is green), manual (unverifiable: needs --force). Nothing a check does mutates anything.

Two more read-only helpers keep the inventory true after the migration: `audit` lists every timer (system and user), active crontab line
and /etc/cron.* file that no item accounts for (a weekly C0 check can wrap audit_result), and `retired` lists the units and files an
installer must not put back. Owner additions to the inventory live in CONF_DIR/legacy-retirement.d/*.toml (same trust rules).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import getpass
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import core

MODES = ("adapter", "port", "retire", "observe", "keep")
KINDS = ("unit", "timer", "cron", "script", "hook")
VIAS = ("job", "task", "probe", "os_job", "route", "none")
ACTION_KINDS = ("disable", "move", "stub", "cron_comment", "job_mode", "manual")
PARITY_KINDS = ("task", "job", "probe", "scheduled", "scheduler_health", "scheduler_validate", "unit_equiv", "job_attr", "task_applies",
                "status_json", "file_age", "config", "command", "notify", "os_job", "manual", "none")
LEGACY_ROOT = "/usr/local/lib/homelab-maint/legacy"
ACTIVE = {"active", "activating", "reloading", "deactivating"}
ENABLED = {"enabled", "enabled-runtime"}
GREEN = {"ok", "info"}
CRON_MARK = "#HM-RETIRED["              # a retired crontab line is "#HM-RETIRED[<item>] <original line>"
NAME_RX = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}")
UNIT_RX = re.compile(r"[A-Za-z0-9:_.@\\-]+\.(service|timer|socket|path)")
SCOPE_RX = re.compile(r"system|user:[a-z_][a-z0-9_-]{0,31}")
USER_RX = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
TAG_RX = re.compile(r"[A-Za-z0-9._-]{3,64}")
# An inventory typo must never be able to disable the platform itself. Of the umbrella's own units only the three runner timers
# that the scheduler tick replaces may be retired. The CHECK timer is deliberately not among them: it is the one runner that does
# not depend on the tick, so it is what notices (probe umbrella-tick) when the tick dies or stops reading its jobs.toml.
NEVER_DISABLE = re.compile(r"(homelab-maint-(tick|www|live|check)\.|docker\.|containerd\.|ssh\.|sshd\.|systemd-|cloudflared\.|tailscaled\.|"
                           r"NetworkManager\.|cron\.|fail2ban\.|smartmontools\.|smartd\.|ufw\.|dbus\.|snapd\.)")
RUNNER_TIMERS = re.compile(r"homelab-maint-(daily|weekly|metrics)\.timer")
JOB_MODES = ("managed", "observe", "retired")
JOB_NAME_RX = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
MOVE_ROOTS = ("/usr/local/sbin/", "/usr/local/bin/", "/etc/systemd/system/", "/home/")
STUB_ROOTS = ("/usr/local/sbin/", "/usr/local/bin/")
PARITY_DEFAULTS = {"green": 8, "min_hours": 48, "fresh_h": 2}


class InventoryError(Exception):
    """The inventory file is unusable; .problems lists every reason."""
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        self.problems = problems


class Refused(Exception):
    """A precondition says no. Fail closed: nothing was changed by the step that raised it."""


class OpError(Exception):
    """A mutating step failed or did not verify."""


# =================================================================== inventory
@dataclass
class Item:
    name: str
    title: str
    kind: str
    location: str
    mode: str
    wave: int
    scope: str = "system"
    schedule: str = ""
    replaced_by: list[str] = field(default_factory=list)
    via: str = "none"
    parity: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    soak_days: float = 0.0
    require_idle: list[str] = field(default_factory=list)
    pre_retired: str = ""
    notes: str = ""
    order: int = 0
    job: str = ""                                   # the jobs.toml job this item hands over to (adds a job_mode action)
    pause_on_rollback: list[str] = field(default_factory=list)   # mutating native tasks to PAUSE if the legacy thing comes back
    verified_runs: int = 0                          # green recorded runs of the handed-over job a DEPENDENT needs before the soak counts
    require_idle_jobs: list[str] = field(default_factory=list)   # tick jobs that must not be running (cutover AND rollback)
    backup: bool = False                            # also refuse while a /run/lock/backup-*.lock is held (a backup the tick started)
    keeps_job: str = ""                             # keep items only: the job that stays in observe mode on purpose (its legacy driver stays)

    @property
    def retirable(self) -> bool:
        """Something concrete gets retired (disabled / moved / commented). Adapter items with no such action (an unscheduled
        script being adopted, a manual-only step) and observe/keep items are 'adopted' instead."""
        return self.mode in ("adapter", "port", "retire") and any(a.get("do") not in ("manual", "job_mode") for a in self.actions)


@dataclass
class Inventory:
    meta: dict
    items: list[Item]

    def get(self, name: str) -> Item | None:
        return next((i for i in self.items if i.name == name), None)

    @property
    def legacy_root(self) -> str:
        return str(self.meta.get("legacy_root", LEGACY_ROOT))


def _trusted(p: Path) -> bool:
    """The inventory names units, paths and argv lists that run as root: only root/runner-owned, non-writable files count."""
    try:
        st = p.stat()
    except OSError:
        return False
    return st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022


def inventory_path() -> tuple[Path, bool]:
    """(path, must_be_trusted): the installed copy (/etc/homelab-maint/legacy-retirement.toml) first, else the one shipped next to
    the package (part of the code tree, which is exactly as trusted as the code itself)."""
    inst = core.CONF_DIR / "legacy-retirement.toml"
    return (inst, True) if inst.exists() else (Path(__file__).resolve().parent.parent / "etc" / "legacy-retirement.toml", False)


def _path_ok(p: Any, roots: tuple[str, ...]) -> bool:
    return (isinstance(p, str) and p.startswith(roots) and os.path.normpath(p) == p and not p.endswith("/")
            and "\0" not in p and len(p) < 400)


def _strs(v: Any) -> list[str]:
    return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _check_action(a: Any, where: str, item_scope: str, mode: str, out: list[str]) -> None:
    if not isinstance(a, dict) or a.get("do") not in ACTION_KINDS:
        out.append(f"{where}: unknown action {a!r}")
        return
    do = a["do"]
    if mode in ("observe", "keep") and do != "manual":
        out.append(f"{where}: mode {mode} must not change the host (action {do})")
    if a.get("scope") is not None and not SCOPE_RX.fullmatch(str(a["scope"])):
        out.append(f"{where}: bad scope {a['scope']!r}")
    if do == "disable":
        u = a.get("unit")
        if not isinstance(u, str) or not UNIT_RX.fullmatch(u):
            out.append(f"{where}: disable needs a valid unit")
        elif NEVER_DISABLE.match(u) or (u.startswith("homelab-maint") and not RUNNER_TIMERS.fullmatch(u)):
            out.append(f"{where}: refusing to ever disable {u} (platform unit)")
    elif do == "move":
        if not _path_ok(a.get("src"), MOVE_ROOTS):
            out.append(f"{where}: move src must be a normalised absolute path under {', '.join(MOVE_ROOTS)}")
        elif a["src"].startswith("/etc/systemd/system/") and not a["src"].endswith(".conf"):
            out.append(f"{where}: only drop-in .conf files may be moved out of /etc/systemd/system (units are disabled, not moved)")
        dn = a.get("dst_name")
        if dn is not None and (not isinstance(dn, str) or "/" in dn or dn in ("", ".", "..")):
            out.append(f"{where}: bad dst_name")
        if a.get("reload") is not None and not SCOPE_RX.fullmatch(str(a["reload"])):
            out.append(f"{where}: bad reload scope")
    elif do == "stub":
        m = str(a.get("mode", "0755"))
        if not _path_ok(a.get("path"), STUB_ROOTS):
            out.append(f"{where}: stub path must be under /usr/local/sbin or /usr/local/bin")
        if not isinstance(a.get("content"), str) or not a["content"].startswith("#!"):
            out.append(f"{where}: stub content must be a script starting with #!")
        if not re.fullmatch(r"0?[0-7]{3,4}", m) or int(m, 8) & 0o022:
            out.append(f"{where}: stub mode must be octal and not group/world writable")
    elif do == "cron_comment":
        if not USER_RX.fullmatch(str(a.get("user", ""))):
            out.append(f"{where}: cron_comment needs a user")
        if bool(a.get("tag")) == bool(a.get("match")):
            out.append(f"{where}: cron_comment needs exactly one of tag / match")
        elif a.get("tag") and not TAG_RX.fullmatch(str(a["tag"])):
            out.append(f"{where}: bad cron tag")
        elif a.get("match") and (not isinstance(a["match"], str) or len(a["match"]) < 8):
            out.append(f"{where}: cron match must be a string of at least 8 characters")
    elif do == "job_mode":
        if not (isinstance(a.get("job"), str) and JOB_NAME_RX.fullmatch(a["job"])) or a.get("mode") not in JOB_MODES:
            out.append(f"{where}: job_mode needs a job name and a mode in {JOB_MODES}")
    elif do == "manual" and not (isinstance(a.get("note"), str) and a["note"].strip()):
        out.append(f"{where}: manual action needs a note")


def _check_parity(p: Any, where: str, out: list[str]) -> None:
    if not isinstance(p, dict) or p.get("kind") not in PARITY_KINDS:
        out.append(f"{where}: unknown parity check {p!r}")
        return
    k = p["kind"]
    need = {"task": ("name",), "job": ("name",), "probe": (), "scheduled": ("job",), "status_json": ("path",),
            "file_age": ("path", "max_age_s"), "config": ("key",), "command": ("argv",), "notify": (), "os_job": ("name",),
            "manual": ("note",), "none": ("note",), "scheduler_health": (), "job_attr": ("job", "attr"), "task_applies": ("name", "tier_job"),
            "scheduler_validate": (), "unit_equiv": ("job", "unit")}[k]
    for key in need:
        if p.get(key) is None or p.get(key) in ("", []):
            out.append(f"{where}: parity {k} needs {key}")
    if k == "probe" and not _strs(p.get("names") or p.get("name")):
        out.append(f"{where}: parity probe needs name or names")
    if k == "job_attr" and ("equals" in p) == ("contains" in p):
        out.append(f"{where}: parity job_attr needs exactly one of equals / contains")
    if k == "command" and not (isinstance(p.get("argv"), list) and p["argv"] and all(isinstance(x, str) for x in p["argv"])):
        out.append(f"{where}: parity command argv must be a non-empty list of strings (no shell)")
    if k == "unit_equiv" and (not UNIT_RX.fullmatch(str(p.get("unit", ""))) or not SCOPE_RX.fullmatch(str(p.get("scope", "system")))):
        out.append(f"{where}: parity unit_equiv needs a valid unit (and scope)")
    if k == "scheduler_validate" and not all(JOB_NAME_RX.fullmatch(j) for j in _strs(p.get("jobs") or p.get("job"))):
        out.append(f"{where}: parity scheduler_validate job names are not valid")


def _parse_item(raw: Any, idx: int, meta: dict, out: list[str]) -> Item | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        out.append(f"item[{idx}]: missing name")
        return None
    name = raw["name"]
    where = f"item {name}"
    n0 = len(out)
    if not NAME_RX.fullmatch(name):
        out.append(f"{where}: bad name")
    mode, kind = raw.get("mode"), raw.get("kind")
    if mode not in MODES:
        out.append(f"{where}: mode must be one of {MODES}")
    if kind not in KINDS:
        out.append(f"{where}: kind must be one of {KINDS}")
    via = raw.get("via", "none")
    if via not in VIAS:
        out.append(f"{where}: via must be one of {VIAS}")
    scope = raw.get("scope", "system")
    if not SCOPE_RX.fullmatch(str(scope)):
        out.append(f"{where}: bad scope")
    if not isinstance(raw.get("location"), str) or not raw["location"]:
        out.append(f"{where}: location required")
    repl = _strs(raw.get("replaced_by", []))
    actions = raw.get("retire_actions", [])
    if not isinstance(actions, list):
        out.append(f"{where}: retire_actions must be a list")
        actions = []
    job = raw.get("job", "")
    if job:
        if mode in ("observe", "keep") or not isinstance(job, str) or not JOB_NAME_RX.fullmatch(job):
            out.append(f"{where}: `job` must be a jobs.toml job name and only adapter/port/retire items may hand over to a job")
        else:                                                    # the scheduler mode flip is always the LAST action (and the first to undo)
            actions = [*actions, {"do": "job_mode", "job": job, "mode": raw.get("job_mode", "managed" if mode == "adapter" else "retired")}]
    keeps = raw.get("keeps_job", "")
    if keeps and (mode != "keep" or not isinstance(keeps, str) or not JOB_NAME_RX.fullmatch(keeps) or job):
        out.append(f"{where}: `keeps_job` is only for keep items (a job that stays in observe mode on purpose) and excludes `job`")
    idle_jobs = _strs(raw.get("require_idle_jobs", []))
    if any(not JOB_NAME_RX.fullmatch(j) for j in idle_jobs):
        out.append(f"{where}: bad require_idle_jobs name")
    vr = raw.get("verified_runs", 1 if (job and mode == "adapter") else 0)
    if isinstance(vr, bool) or not isinstance(vr, int) or not 0 <= vr <= 10:
        out.append(f"{where}: verified_runs must be an integer 0..10")
        vr = 0
    for i, a in enumerate(actions):
        _check_action(a, f"{where} action[{i}]", str(scope), str(mode), out)
    if mode in ("port", "retire") and not any(isinstance(a, dict) and a.get("do") not in ("job_mode",) for a in actions) \
            and not raw.get("pre_retired"):
        out.append(f"{where}: mode {mode} needs at least one retire action (or pre_retired)")
    par = raw.get("parity_check")
    par = [] if par is None else [par] if isinstance(par, dict) else par
    if not isinstance(par, list):
        out.append(f"{where}: parity_check must be a table or a list of tables")
        par = []
    if raw.get("unmonitored"):
        if mode not in ("observe", "keep") or par or raw.get("replaced_by"):
            out.append(f"{where}: `unmonitored` is only for observe/keep items with no parity_check and no replaced_by")
        par = [{"kind": "none", "note": str(raw["unmonitored"])}]
    if not par and mode != "retire" and repl:                       # shorthand: derive the obvious check from replaced_by
        d = meta["parity_defaults"]
        par = {"task": [{"kind": "task", "name": n, **d} for n in repl], "job": [{"kind": "job", "name": n, **d} for n in repl],
               "probe": [{"kind": "probe", "names": repl, **d}], "os_job": [{"kind": "os_job", "name": n} for n in repl]}.get(via, [])
    for i, p in enumerate(par):
        _check_parity(p, f"{where} parity[{i}]", out)
    d = meta["parity_defaults"]
    par = [{**d, **p} if isinstance(p, dict) and p.get("kind") in ("task", "job", "probe", "os_job") else p for p in par]
    if job and mode == "adapter" and isinstance(job, str) and not any(isinstance(p, dict) and p.get("kind") == "scheduler_validate" for p in par):
        par = [*par, {"kind": "scheduler_validate", "job": job}]     # a job the tick is about to run must load and validate cleanly, always
    if not par and not raw.get("pre_retired"):
        out.append(f"{where}: no parity_check and nothing to derive one from (what proves nothing is lost?)")
    for key in ("pre_retired",):
        if raw.get(key) and not re.fullmatch(r"\d{4}-\d\d-\d\d", str(raw[key])):
            out.append(f"{where}: pre_retired must be YYYY-MM-DD")
    if len(out) > n0:
        return None
    return Item(name=name, title=str(raw.get("title") or name), kind=kind, location=raw["location"], mode=mode,
                wave=int(raw.get("wave", 0)), scope=str(scope), schedule=str(raw.get("schedule", "")), replaced_by=repl,
                via=via, parity=[dict(p) for p in par], actions=[dict(a) for a in actions],
                depends_on=_strs(raw.get("depends_on", [])), soak_days=float(raw.get("soak_days", 0)),
                require_idle=_strs(raw.get("require_idle", [])), pre_retired=str(raw.get("pre_retired", "")),
                notes=str(raw.get("owner_notes", "")).strip(), order=idx, job=str(job or ""),
                pause_on_rollback=_strs(raw.get("pause_on_rollback", [])), verified_runs=int(vr), require_idle_jobs=idle_jobs,
                backup=bool(raw.get("backup", False)), keeps_job=str(keeps or ""))


def parse_inventory(doc: dict) -> Inventory:
    """Validate a parsed TOML document into an Inventory, or raise InventoryError listing EVERY problem."""
    meta = doc.get("meta", {}) if isinstance(doc.get("meta"), dict) else {}
    meta.setdefault("parity_defaults", dict(PARITY_DEFAULTS))
    for k, v in PARITY_DEFAULTS.items():
        meta["parity_defaults"].setdefault(k, v)
    out: list[str] = []
    items = [it for i, raw in enumerate(doc.get("item", [])) if (it := _parse_item(raw, i, meta, out))]
    seen: set[str] = set()
    for it in items:
        if it.name in seen:
            out.append(f"item {it.name}: duplicate name")
        seen.add(it.name)
    by = {i.name: i for i in items}
    for it in items:
        for d in it.depends_on:
            if d not in by:
                out.append(f"item {it.name}: depends_on unknown item {d}")
            elif by[d].wave > it.wave:
                out.append(f"item {it.name}: depends on {d} which is in a LATER wave")
        for u in it.require_idle:
            if not UNIT_RX.fullmatch(u):
                out.append(f"item {it.name}: bad require_idle unit {u}")
    if not out and len(order_items(items)) != len(items):
        out.append("depends_on contains a cycle")
    if out:
        raise InventoryError(out)
    return Inventory(meta, items)


def load_inventory(path: Path | None = None) -> Inventory:
    """The shipped inventory plus, at the default location, the owner's additions in CONF_DIR/legacy-retirement.d/*.toml (extra [[item]]
    tables, same rules, same trust check: they name units and paths that run as root). An untrusted file refuses the WHOLE inventory:
    a half-read inventory would hide things from `status` and from the audit."""
    p, must = (Path(path), True) if path else inventory_path()
    if must and not _trusted(p):
        raise InventoryError([f"{p}: missing, or not owned by root/runner, or group/world writable"])
    try:
        with open(p, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InventoryError([f"{p}: unreadable ({type(exc).__name__}: {exc})"]) from None
    extra = core.CONF_DIR / "legacy-retirement.d"
    if path is None and extra.is_dir():
        if not _trusted(extra):
            raise InventoryError([f"{extra}: not owned by root/runner, or group/world writable"])
        for f in sorted(extra.glob("*.toml")):
            if not _trusted(f):
                raise InventoryError([f"{f}: not owned by root/runner, or group/world writable"])
            try:
                with open(f, "rb") as fh:
                    part = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise InventoryError([f"{f}: unreadable ({type(exc).__name__}: {exc})"]) from None
            doc.setdefault("item", []).extend(part.get("item", []) if isinstance(part.get("item", []), list) else [])
    return parse_inventory(doc)


def order_items(items: list[Item]) -> list[Item]:
    """Safe cutover order: by wave, then file order, with every item after its dependencies (Kahn; a cycle drops items)."""
    by = {i.name: i for i in items}
    done: list[Item] = []
    placed: set[str] = set()
    rest = sorted(items, key=lambda i: (i.wave, i.order))
    while rest:
        nxt = next((i for i in rest if all(d in placed or d not in by for d in i.depends_on)), None)
        if nxt is None:
            break
        rest.remove(nxt)
        done.append(nxt)
        placed.add(nxt.name)
    return done


# =================================================================== host access
@dataclass
class Op:
    """One mutating step: `text` is the exact command/description a dry run prints and the audit log records."""
    text: str
    fn: Callable[[], None]


def _default_run(argv: list[str], input_: str | None = None, timeout: int = 60):
    return core.sh(argv, timeout=timeout, input_=input_)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class JobsApi:
    """The scheduler's side of a cutover (jobs.py): which jobs jobs.toml defines (with their shipped mode), the per-job mode
    override file STATE_DIR/job-modes.json, and the effective job attributes parity checks read. Tests inject a fake."""

    def known(self) -> dict[str, str]:
        from . import jobs
        return {n: j.mode for n, j in jobs.load(apply_modes=False).jobs.items()}

    def override(self, name: str) -> str | None:
        from . import jobs
        return jobs.load_modes().get(name)

    def set(self, name: str, mode: str | None) -> None:
        from . import jobs
        jobs.set_mode(name, mode)

    def attr(self, name: str, dotted: str) -> Any:
        from . import jobs
        cur: Any = jobs.load().jobs[name]
        for part in dotted.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part)
        return cur


class Host:
    """Everything the migrator touches. `run` is the command runner (argv list, input_, timeout) -> CompletedProcess-like;
    `root` prefixes every filesystem path (tests / staging) while commands and printed text keep the logical path."""

    def __init__(self, run: Callable | None = None, root: str | Path = "", uids: dict[str, int] | None = None,
                 jobs: JobsApi | None = None):
        self._run = run or _default_run
        self.root = str(root).rstrip("/") if root else ""
        self.uids = dict(uids or {})
        self.jobs = jobs or JobsApi()

    def p(self, path: str | Path) -> Path:
        return Path(self.root + str(path)) if self.root else Path(path)

    def run(self, argv: list[str], input_: str | None = None, timeout: int = 60):
        return self._run(argv, input_=input_, timeout=timeout)

    def must(self, argv: list[str], input_: str | None = None, timeout: int = 60) -> None:
        r = self.run(argv, input_=input_, timeout=timeout)
        if r.returncode != 0:
            raise OpError(f"{shlex.join(argv[:6])} failed rc={r.returncode}: {(r.stderr or r.stdout or '').strip()[-160:]}")

    def uid(self, user: str) -> int:
        if user not in self.uids:
            self.uids[user] = pwd.getpwnam(user).pw_uid
        return self.uids[user]

    def ctl_argv(self, scope: str, args: list[str]) -> list[str]:
        if scope == "system":
            return ["systemctl", *args]
        user = scope.split(":", 1)[1]
        return ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{self.uid(user)}", "systemctl", "--user", *args]

    def unit(self, scope: str, unit: str) -> dict[str, str]:
        r = self.run(self.ctl_argv(scope, ["show", "-p", "LoadState,UnitFileState,ActiveState,SubState,Type,Persistent", unit]), timeout=20)
        if r.returncode != 0:
            raise Refused(f"cannot read the state of {unit} ({scope}): rc={r.returncode}")
        kv = dict(ln.split("=", 1) for ln in (r.stdout or "").splitlines() if "=" in ln)
        if "LoadState" not in kv:
            raise Refused(f"unparsable systemctl show output for {unit}")
        return kv

    def crontab_read(self, user: str) -> str:
        r = self.run(["crontab", "-l", "-u", user], timeout=20)
        if r.returncode == 0:
            return r.stdout or ""
        if "no crontab" in (r.stderr or ""):
            return ""
        raise Refused(f"cannot read the crontab of {user}: rc={r.returncode}")

    def crontab_write(self, user: str, text: str) -> None:
        self.must(["crontab", "-u", user, "-"], input_=text, timeout=20)

    def write_atomic(self, path: str, text: str, mode: int) -> None:
        """tmp + rename in the same directory, O_EXCL (never follows a pre-planted file), final mode exact."""
        dst = self.p(path)
        tmp = dst.with_name(f".{dst.name}.hm-{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def safe_move(self, src: str, dst: str) -> None:
        """rename(2); across filesystems copy + verify sha256 + only then unlink the source. Never overwrites."""
        s, d = self.p(src), self.p(dst)
        if os.path.lexists(d):
            raise OpError(f"destination exists: {dst}")
        try:
            os.rename(s, d)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        st = os.lstat(s)
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(s), d)
        else:
            shutil.copy2(s, d, follow_symlinks=False)
            with contextlib.suppress(PermissionError):
                os.chown(d, st.st_uid, st.st_gid)
            if sha256_file(s) != sha256_file(d):
                os.unlink(d)
                raise OpError(f"copy of {src} did not verify; source kept")
        os.unlink(s)

    def safe_copy(self, src: str, dst: str) -> None:
        """Verified copy that leaves the source exactly where it is: tmp beside the destination, sha256 compared, then rename(2).
        Never overwrites. The first half of 'put a stub where a script was' (see MoveAction.swap): the script is never absent."""
        s, d = self.p(src), self.p(dst)
        if os.path.lexists(d):
            raise OpError(f"destination exists: {dst}")
        st = os.lstat(s)
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(s), d)
            return
        tmp = d.with_name(f".{d.name}.hm-{os.getpid()}.tmp")
        try:
            shutil.copy2(s, tmp, follow_symlinks=False)
            with contextlib.suppress(PermissionError):
                os.chown(tmp, st.st_uid, st.st_gid)
            if sha256_file(s) != sha256_file(tmp):
                raise OpError(f"copy of {src} did not verify; nothing was changed")
            os.rename(tmp, d)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def replace_from(self, src: str, dst: str) -> None:
        """Atomically put the file `src` at `dst`, replacing whatever is there (rename(2) over it: `dst` is never absent). Across
        filesystems: copy beside the destination, verify, os.replace, and only then drop the now-duplicate source."""
        s, d = self.p(src), self.p(dst)
        try:
            os.rename(s, d)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        tmp = d.with_name(f".{d.name}.hm-{os.getpid()}.tmp")
        try:
            shutil.copy2(s, tmp, follow_symlinks=False)
            if sha256_file(s) != sha256_file(tmp):
                raise OpError(f"copy of {src} did not verify; the stub was left in place")
            os.replace(tmp, d)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        os.unlink(s)


def _readme(item: Item, legacy_dir: str, pairs: list[tuple[str, str]], stubs: list[str], now: float) -> str:
    cmds = "\n".join(f"    mv {shlex.quote(dst)} {shlex.quote(src)}" for src, dst in pairs)
    stub = "".join(f"- a forwarding stub was left at `{s}`; `mv` of the original over it replaces the stub in one step\n" for s in stubs)
    return (f"# legacy/{item.name}\n\n{item.title}\n\n"
            f"- retired: {_iso(now)} by homelab-maint (mode {item.mode}, replaced by {', '.join(item.replaced_by) or 'nothing'})\n"
            f"- original location: {item.location}\n{stub}"
            f"- put it back: `homelab-maint migrate rollback {item.name} --apply` (restores enablement and these files exactly)\n"
            f"- by hand, if the tool is unavailable (then re-enable the unit yourself):\n\n{cmds}\n\n"
            "Nothing here was deleted. Files are kept for the rollback window and removed only by the owner.\n")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


# =================================================================== cron text (pure functions)
def _cron_active(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def cron_find(text: str, tag: str | None, match: str | None) -> list[int]:
    """Indexes of ACTIVE lines selected by a trailing `# tag` comment or by a command substring."""
    rx = re.compile(r"\s#\s*" + re.escape(tag) + r"\s*$") if tag else None
    return [i for i, ln in enumerate(text.split("\n")) if _cron_active(ln)
            and ((rx is not None and rx.search(ln)) or (match is not None and match in ln))]


def cron_comment(text: str, item: str, idx: int) -> str:
    lines = text.split("\n")
    lines[idx] = f"{CRON_MARK}{item}] {lines[idx]}"
    return "\n".join(lines)


def cron_uncomment(text: str, item: str, original: str | None = None) -> tuple[str, list[int]]:
    """Strip OUR marker from the lines we commented (all of the item's markers, or only the one matching `original`)."""
    pre, lines, hit = f"{CRON_MARK}{item}] ", text.split("\n"), []
    for i, ln in enumerate(lines):
        if ln.startswith(pre) and (original is None or ln[len(pre):] == original):
            lines[i] = ln[len(pre):]
            hit.append(i)
    return "\n".join(lines), hit


def cron_only_changed(old: str, new: str, idx: list[int], fn: Callable[[str], str]) -> bool:
    """The invariant behind 'never loses unrelated lines': same line count, every other line byte-identical, and the changed
    lines are exactly fn(old line)."""
    a, b = old.split("\n"), new.split("\n")
    return len(a) == len(b) and all((b[i] == fn(a[i])) if i in idx else a[i] == b[i] for i in range(len(a)))


# =================================================================== actions
@dataclass
class Env:
    host: Host
    item: Item
    legacy_dir: str                      # logical path of <legacy_root>/<item>/
    state_dir: Path                      # real path (backups of crontabs)
    now: float
    moved: set = field(default_factory=set)      # dry-run overlay: paths earlier actions in the plan move away


class Action:
    """One retire action. probe() is read-only and returns the PRIOR STATE that rollback restores; forward() returns the exact
    ops that retire; backward(pre) the ops that restore `pre`; verify/restored are read-only post-conditions."""
    kind = ""

    def __init__(self, spec: dict, env: Env):
        self.s, self.env, self.h = spec, env, env.host

    def scope(self) -> str:
        return self.s.get("scope") or self.env.item.scope

    def label(self) -> str: raise NotImplementedError
    def probe(self) -> dict: return {}
    def satisfied(self, pre: dict) -> bool: return True
    def forward(self, pre: dict) -> list[Op]: return []
    def verify(self, pre: dict) -> bool: return self.satisfied(self.probe())
    def backward(self, pre: dict) -> list[Op]: return []
    def restored(self, pre: dict) -> bool: return True
    def virtual(self) -> None: pass
    def nominal(self) -> tuple[list[str], list[str]]: return [], []
    def persist(self, pre: dict) -> dict: return pre            # what goes into migration.json (no bulky text)

    def ctl_op(self, args: list[str]) -> Op:
        argv = self.h.ctl_argv(self.scope(), args)
        return Op(shlex.join(argv), lambda: self.h.must(argv))

    def ctl_text(self, args: list[str], scope: str | None = None) -> str:
        sc = scope or self.scope()
        return shlex.join(["systemctl", *(["--user"] if sc != "system" else []), *args]) \
            + (f"   # as {sc.split(':', 1)[1]}" if sc != "system" else "")


class DisableAction(Action):
    kind = "disable"

    def label(self) -> str:
        return f"disable {self.s['unit']}" + ("" if self.scope() == "system" else f" ({self.scope()})")

    def probe(self) -> dict:
        r = self.h.unit(self.scope(), self.s["unit"])
        return {"load": r.get("LoadState", ""), "enabled": r.get("UnitFileState", ""), "active": r.get("ActiveState", ""),
                "type": r.get("Type", ""), "persistent": r.get("Persistent") == "yes"}

    def satisfied(self, pre: dict) -> bool:
        if pre["load"] == "not-found":
            return True
        if pre["enabled"] in ENABLED or (self.s.get("now", True) and pre["active"] in ACTIVE):
            return False
        return True

    def forward(self, pre: dict) -> list[Op]:
        if pre["enabled"] in ("linked", "linked-runtime"):
            raise Refused(f"{self.s['unit']} is a linked unit: its state cannot be restored exactly, retire it by hand")
        if pre["enabled"] in ENABLED:
            args = ["disable"] + (["--runtime"] if pre["enabled"] == "enabled-runtime" else []) \
                + (["--now"] if self.s.get("now", True) else []) + [self.s["unit"]]
        else:
            args = ["stop", self.s["unit"]]                  # not enabled (static/transient) but running
        return [self.ctl_op(args)]

    def _startable(self, pre: dict) -> bool:
        return self.s["unit"].endswith((".timer", ".socket", ".path")) or pre.get("type") != "oneshot"

    def backward(self, pre: dict) -> list[Op]:
        if pre["load"] == "not-found":
            return []
        ops = []
        if pre["enabled"] in ENABLED:
            ops.append(self.ctl_op(["enable"] + (["--runtime"] if pre["enabled"] == "enabled-runtime" else []) + [self.s["unit"]]))
        if pre["active"] in ACTIVE and self._startable(pre):
            ops.append(self.ctl_op(["start", self.s["unit"]]))     # a oneshot service is never started: that would run the job
        return ops

    def restored(self, pre: dict) -> bool:
        if pre["load"] == "not-found":
            return True
        cur = self.probe()
        if cur["enabled"] != pre["enabled"]:
            return False
        return not (self._startable(pre) and (pre["active"] in ACTIVE) != (cur["active"] in ACTIVE))

    def nominal(self) -> tuple[list[str], list[str]]:
        u = self.s["unit"]
        return [self.ctl_text(["disable", "--now", u] if self.s.get("now", True) else ["disable", u])], \
               [self.ctl_text(["enable", "--now", u])]


def _fstat(host: "Host", path: str) -> dict | None:
    """What is at `path` (a symlink is described, never followed): mode/owner/size/link/dir/sha256. None when nothing is there."""
    p = host.p(path)
    try:
        st = os.lstat(p)
        link = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
        return {"mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid, "size": st.st_size, "link": link,
                "dir": stat.S_ISDIR(st.st_mode), "sha": sha256_file(p) if stat.S_ISREG(st.st_mode) else None}
    except FileNotFoundError:
        return None
    except PermissionError:                                      # e.g. backup-notify.sh is 0750 root: only root can fingerprint it
        raise Refused(f"cannot read {path}: permission denied (run this as root: sudo homelab-maint migrate ...)") from None
    except OSError as exc:
        raise Refused(f"cannot inspect {path}: {exc.strerror}") from None


def _same(a: dict | None, b: dict | None) -> bool:
    """The same file content (or the same symlink target); None is never the same as anything."""
    if a is None or b is None:
        return False
    return a["link"] == b["link"] if (a["link"] is not None or b["link"] is not None) else a["sha"] == b["sha"]


class MoveAction(Action):
    """Put a retired script into <legacy_root>/<item>/. When a `stub` action of the same item targets the same path (something still calls
    it: smartd, a jobs.toml hook) the pair is a SWAP: the script is COPIED (verified), stays in place, and the stub then replaces it with one
    rename(2). The path is never absent, so an event that fires mid-cutover (smartd calls its hook once and does not retry) finds a
    script. Rollback is the mirror image: one rename of the verified copy over the stub."""
    kind = "move"

    def dst(self) -> str:
        return f"{self.env.legacy_dir}/{self.s.get('dst_name') or os.path.basename(self.s['src'])}"

    def swap(self) -> bool:
        return any(a.get("do") == "stub" and a.get("path") == self.s["src"] for a in self.env.item.actions)

    def label(self) -> str:
        return f"{'copy' if self.swap() else 'move'} {self.s['src']} -> {self.dst()}"

    def _our_stub(self, path: str) -> bool:
        """True when `path` holds the forwarding stub that a stub action of the same item put there after the move."""
        for a in self.env.item.actions:
            if a.get("do") == "stub" and a.get("path") == path:
                p = self.h.p(path)
                with contextlib.suppress(OSError):
                    return (not p.is_symlink() and p.is_file() and stat.S_IMODE(p.stat().st_mode) == int(str(a.get("mode", "0755")), 8)
                            and p.read_text() == a["content"].replace("{legacy_dir}", self.env.legacy_dir))
        return False

    def _stat(self, path: str) -> dict | None:
        if path in self.env.moved or (path == self.s["src"] and self._our_stub(path)):
            return None
        return _fstat(self.h, path)

    def probe(self) -> dict:
        return {"src": self._stat(self.s["src"]), "dst": self._stat(self.dst())}

    def satisfied(self, pre: dict) -> bool:
        if pre["src"] is None:                                 # moved already (or never existed: nothing left to retire)
            return True
        return self.swap() and _same(pre["src"], pre["dst"])   # the verified copy is there already; only the stub is missing

    def _reload(self) -> list[Op]:
        sc = self.s.get("reload")
        if not sc:
            return []
        argv = self.h.ctl_argv(sc, ["daemon-reload"])
        return [Op(shlex.join(argv), lambda: self.h.must(argv))]

    def forward(self, pre: dict) -> list[Op]:
        if pre["src"]["dir"]:
            raise Refused(f"{self.s['src']} is a directory: only files and symlinks are moved")
        if pre["dst"] is not None:
            raise Refused(f"{self.dst()} already exists: refusing to overwrite a legacy copy")
        d, src, dst, it = self.env.legacy_dir, self.s["src"], self.dst(), self.env.item
        pairs = [(a["src"], f"{d}/{a.get('dst_name') or os.path.basename(a['src'])}") for a in it.actions if a.get("do") == "move"]
        stubs = [a["path"] for a in it.actions if a.get("do") == "stub"]

        def readme() -> None:
            p = self.h.p(f"{d}/README.md")
            if p.exists():
                with open(p, "a") as f:
                    f.write(f"\n- {_iso(self.env.now)}: also moved {src}\n")
            else:
                self.h.write_atomic(f"{d}/README.md", _readme(it, d, pairs, stubs, self.env.now), 0o644)

        def mkdir() -> None:
            self.h.p(d).mkdir(parents=True, exist_ok=True, mode=0o755)

        if self.swap():
            put = Op(f"cp -p {shlex.quote(src)} {shlex.quote(dst)}   # verified copy; the original stays until the stub replaces it",
                     lambda: self.h.safe_copy(src, dst))
        else:
            put = Op(f"mv {shlex.quote(src)} {shlex.quote(dst)}", lambda: self.h.safe_move(src, dst))
        return [Op(f"mkdir -p {d}", mkdir), Op(f"write {d}/README.md", readme), put, *self._reload()]

    def verify(self, pre: dict) -> bool:
        cur = self.probe()
        if self.swap():
            return _same(pre["src"], cur["dst"])
        return cur["src"] is None and cur["dst"] is not None and (pre["src"] is None or cur["dst"]["sha"] == pre["src"]["sha"])

    def backward(self, pre: dict) -> list[Op]:
        cur = self.probe()
        if pre["src"] is None:
            return []
        if self.swap():
            if cur["src"] is not None:                          # the original never left its path (or the stub step already put it back)
                return []
            raise Refused(f"the forwarding stub still stands at {self.s['src']}: the stub step is undone first, then the original is put back")
        if cur["src"] is not None:
            raise Refused(f"something now exists at {self.s['src']}: not overwriting it")
        if cur["dst"] is None:
            raise Refused(f"the legacy copy {self.dst()} is gone: cannot restore {self.s['src']}")
        if cur["dst"]["sha"] != pre["src"]["sha"]:
            raise Refused(f"the legacy copy {self.dst()} was modified since it was moved")
        src, dst, m = self.s["src"], self.dst(), pre["src"]

        def restore() -> None:
            self.h.safe_move(dst, src)
            p = self.h.p(src)
            if m["link"] is None:
                os.chmod(p, m["mode"])
            with contextlib.suppress(PermissionError):
                os.chown(p, m["uid"], m["gid"], follow_symlinks=False)

        def note() -> None:
            with open(self.h.p(f"{self.env.legacy_dir}/README.md"), "a") as f:
                f.write(f"\n- {_iso(self.env.now)}: {src} was put back by `migrate rollback`\n")

        return [Op(f"mv {shlex.quote(dst)} {shlex.quote(src)}", restore), Op(f"note rollback in {self.env.legacy_dir}/README.md", note),
                *self._reload()]

    def restored(self, pre: dict) -> bool:
        if pre["src"] is None:
            return True
        cur = self.probe()["src"]
        if self.swap():                                         # the copy step never touched the original: any non-stub file there is it
            return cur is not None
        return cur is not None and cur["sha"] == pre["src"]["sha"] and (cur["link"] is not None or cur["mode"] == pre["src"]["mode"])

    def virtual(self) -> None:
        if not self.swap():
            self.env.moved.add(self.s["src"])

    def nominal(self) -> tuple[list[str], list[str]]:
        s, d = shlex.quote(self.s["src"]), shlex.quote(self.dst())
        rl = [self.ctl_text(["daemon-reload"], self.s["reload"])] if self.s.get("reload") else []
        if self.swap():
            return [f"mkdir -p {self.env.legacy_dir}", f"cp -p {s} {d}   # verified copy; the original stays until the stub replaces it", *rl], rl
        return [f"mkdir -p {self.env.legacy_dir}", f"mv {s} {d}", *rl], [f"mv {d} {s}", *rl]


class StubAction(Action):
    """Leave a forwarding script where something still calls the old path. Next to a `move` of the same path it REPLACES the original with
    one atomic rename, but only after the verified copy in the legacy dir matches it byte for byte (see MoveAction.swap)."""
    kind = "stub"

    def text(self) -> str:
        return self.s["content"].replace("{legacy_dir}", self.env.legacy_dir)

    def mode(self) -> int:
        return int(str(self.s.get("mode", "0755")), 8)

    def twin(self) -> str | None:
        """Legacy-dir path of the copy a move action of this item made of the script this stub replaces (None: no such move)."""
        for a in self.env.item.actions:
            if a.get("do") == "move" and a.get("src") == self.s["path"]:
                return f"{self.env.legacy_dir}/{a.get('dst_name') or os.path.basename(a['src'])}"
        return None

    def label(self) -> str:
        return f"leave a forwarding stub at {self.s['path']}"

    def probe(self) -> dict:
        p = self.h.p(self.s["path"])
        if self.s["path"] in self.env.moved or not os.path.lexists(p):
            return {"exists": False, "same": False, "orig": None}
        same = False
        with contextlib.suppress(OSError):
            same = (not p.is_symlink() and p.is_file() and p.read_text() == self.text() and stat.S_IMODE(p.stat().st_mode) == self.mode())
        return {"exists": True, "same": same, "orig": None if same or self.twin() is None else _fstat(self.h, self.s["path"])}

    def satisfied(self, pre: dict) -> bool:
        return pre["same"]

    def forward(self, pre: dict) -> list[Op]:
        path, twin = self.s["path"], self.twin()
        if pre["exists"] and twin is None:
            raise Refused(f"{path} is occupied by something that is not our stub")
        if pre["exists"] and pre["orig"] and pre["orig"]["dir"]:
            raise Refused(f"{path} is a directory: cannot put a stub there")
        replace = twin is not None and pre["exists"]

        def write() -> None:
            if replace:                                           # only replace a script that is verifiably preserved byte for byte
                cur = _fstat(self.h, path)
                if cur is not None and not _same(cur, _fstat(self.h, twin)):
                    raise OpError(f"{path} no longer matches its legacy copy {twin} (changed after the copy): not replacing it")
            self.h.write_atomic(path, self.text(), self.mode())

        return [Op(f"write stub {path} (mode {self.s.get('mode', '0755')})" + ("   # one atomic rename over the original: never absent" if replace else ""),
                   write)]

    def backward(self, pre: dict) -> list[Op]:
        path, twin, orig = self.s["path"], self.twin(), pre.get("orig")
        cur = self.probe()
        if twin is None or not orig:                              # a stub on an empty path: removing it is the whole undo
            if not cur["exists"]:
                return []
            if not cur["same"]:
                raise Refused(f"{path} was modified since the stub was written: not removing it")
            return [Op(f"rm {path}   # our stub", lambda: os.unlink(self.h.p(path)))]
        if cur["exists"] and not cur["same"]:
            if _same(cur["orig"], orig):                          # the stub step never ran: the original is still here
                return []
            raise Refused(f"{path} was modified since the stub was written: not removing it")
        cp = _fstat(self.h, twin)
        if cp is None:
            raise Refused(f"the legacy copy {twin} is gone: cannot restore {path}")
        if not _same(cp, orig):
            raise Refused(f"the legacy copy {twin} was modified since it was made")

        def restore() -> None:
            self.h.replace_from(twin, path)                       # rename(2) over our stub: the path is never absent
            p = self.h.p(path)
            if orig["link"] is None:
                os.chmod(p, orig["mode"])
            with contextlib.suppress(PermissionError):
                os.chown(p, orig["uid"], orig["gid"], follow_symlinks=False)

        def note() -> None:
            rd = self.h.p(f"{self.env.legacy_dir}/README.md")
            if rd.exists():
                with open(rd, "a") as f:
                    f.write(f"\n- {_iso(self.env.now)}: {path} was put back by `migrate rollback` (replacing the forwarding stub)\n")

        return [Op(f"mv {shlex.quote(twin)} {shlex.quote(path)}   # one atomic rename over our stub", restore),
                Op(f"note rollback in {self.env.legacy_dir}/README.md", note)]

    def restored(self, pre: dict) -> bool:
        cur, orig = self.probe(), pre.get("orig")
        if self.twin() is None or not orig:
            return not cur["exists"]
        o = cur["orig"]
        return cur["exists"] and not cur["same"] and _same(o, orig) and (o["link"] is not None or o["mode"] == orig["mode"])

    def nominal(self) -> tuple[list[str], list[str]]:
        twin = self.twin()
        if twin is None:
            return [f"write stub {self.s['path']}"], [f"rm {self.s['path']}   # our stub only"]
        return ([f"write stub {self.s['path']}   # one atomic rename over the original, after the copy is verified"],
                [f"mv {shlex.quote(twin)} {shlex.quote(self.s['path'])}   # one atomic rename over our stub"])


class CronAction(Action):
    kind = "cron_comment"

    def label(self) -> str:
        return f"comment the {self.s.get('tag') or self.s.get('match')} line in the {self.s['user']} crontab"

    def probe(self) -> dict:
        text = self.h.crontab_read(self.s["user"])
        hits = cron_find(text, self.s.get("tag"), self.s.get("match"))
        marked = f"{CRON_MARK}{self.env.item.name}] " in text
        return {"text": text, "hits": hits, "marked": marked, "line": text.split("\n")[hits[0]] if len(hits) == 1 else None}

    def satisfied(self, pre: dict) -> bool:
        return not pre["hits"]

    def persist(self, pre: dict) -> dict:
        t = pre["text"]
        return {"line": pre["line"], "sha": hashlib.sha256(t.encode()).hexdigest(), "backup": pre.get("backup")}

    def forward(self, pre: dict) -> list[Op]:
        if len(pre["hits"]) != 1:
            raise Refused(f"{len(pre['hits'])} active crontab lines match {self.s.get('tag') or self.s.get('match')!r} "
                          f"in {self.s['user']}'s crontab: expected exactly one")
        user, old, it = self.s["user"], pre["text"], self.env.item.name
        new = cron_comment(old, it, pre["hits"][0])
        if not cron_only_changed(old, new, pre["hits"], lambda ln: f"{CRON_MARK}{it}] {ln}"):
            raise Refused("crontab edit would change more than the one line")
        bak = self.env.state_dir / "migration" / it / f"crontab-{user}-{int(self.env.now)}.before"
        pre["backup"] = str(bak)

        def backup() -> None:
            bak.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(bak.parent, 0o700)
            fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(old)

        def write() -> None:
            if self.h.crontab_read(user) != old:                  # somebody edited it since we read it: do not clobber
                raise OpError("the crontab changed while the cutover was running; nothing was written")
            self.h.crontab_write(user, new)
            if self.h.crontab_read(user) != new:
                self.h.crontab_write(user, old)
                raise OpError("crontab did not read back as written; the original was restored")

        return [Op(f"save crontab backup {bak}", backup), Op(f"crontab -u {user} -   # comments 1 line: {pre['line'][:60]!r}", write)]

    def verify(self, pre: dict) -> bool:
        cur = self.probe()
        return not cur["hits"] and cur["marked"] and cron_only_changed(pre["text"], cur["text"], [pre["hits"][0]],
                                                                         lambda ln: f"{CRON_MARK}{self.env.item.name}] {ln}")

    def _line(self, pre: dict) -> str | None:
        return pre.get("line")

    def backward(self, pre: dict) -> list[Op]:
        user, it, line = self.s["user"], self.env.item.name, self._line(pre)
        if line is None:
            return []
        cur = self.h.crontab_read(user)
        if f"{CRON_MARK}{it}] {line}" not in cur.split("\n"):
            raise Refused(f"the marker line for {it} is no longer in {user}'s crontab: restore it by hand ({line!r})")
        new, hit = cron_uncomment(cur, it, line)
        if len(hit) != 1 or not cron_only_changed(cur, new, hit, lambda ln: ln[len(f"{CRON_MARK}{it}] "):]):
            raise Refused("crontab restore would change more than the one line")

        def write() -> None:
            if self.h.crontab_read(user) != cur:
                raise OpError("the crontab changed while the rollback was running; nothing was written")
            self.h.crontab_write(user, new)
            if self.h.crontab_read(user) != new:
                self.h.crontab_write(user, cur)
                raise OpError("crontab did not read back as written; the previous text was restored")

        return [Op(f"crontab -u {user} -   # uncomments 1 line: {line[:60]!r}", write)]

    def restored(self, pre: dict) -> bool:
        line = self._line(pre)
        if line is None:
            return True
        return self.h.crontab_read(self.s["user"]).split("\n").count(line) == 1

    def nominal(self) -> tuple[list[str], list[str]]:
        u, w = self.s["user"], self.s.get("tag") or self.s.get("match")
        return [f"crontab -u {u} -   # the line tagged/matching {w!r} gets the prefix {CRON_MARK}{{item}}] ; all other lines untouched"], \
               [f"crontab -u {u} -   # strip that prefix again"]


class JobModeAction(Action):
    """Hand a job to (or take it back from) the scheduler tick: `homelab-maint job mode JOB managed|retired|observe`. Always the
    LAST forward action (the legacy driver is already off, so the tick's interlock lets the job run) and the FIRST undo (the tick
    stops launching it before the legacy driver comes back). Undo restores the recorded OVERRIDE, not just a mode (None clears)."""
    kind = "job_mode"

    def label(self) -> str:
        return f"scheduler: job {self.s['job']} -> {self.s['mode']}"

    def probe(self) -> dict:
        known = self.h.jobs.known()
        if self.s["job"] not in known:
            raise Refused(f"job {self.s['job']} is not defined in jobs.toml: add it there first")
        ov = self.h.jobs.override(self.s["job"])
        return {"shipped": known[self.s["job"]], "override": ov, "effective": ov or known[self.s["job"]]}

    def satisfied(self, pre: dict) -> bool:
        return pre["effective"] == self.s["mode"]

    def forward(self, pre: dict) -> list[Op]:
        j, m = self.s["job"], self.s["mode"]
        return [Op(f"homelab-maint job mode {j} {m}   # writes job-modes.json", lambda: self.h.jobs.set(j, m))]

    def backward(self, pre: dict) -> list[Op]:
        j, ov = self.s["job"], pre["override"]
        return [Op(f"homelab-maint job mode {j} {ov or 'reset'}", lambda: self.h.jobs.set(j, ov))]

    def restored(self, pre: dict) -> bool:
        # The override file alone decides this. Reading jobs.toml here would make a rollback impossible exactly when it is needed
        # most: a jobs.toml that no longer loads (typo, untrusted owner) must not stop the owner from taking a job back from the tick.
        return self.h.jobs.override(self.s["job"]) == pre["override"]

    def nominal(self) -> tuple[list[str], list[str]]:
        return [f"homelab-maint job mode {self.s['job']} {self.s['mode']}"], [f"homelab-maint job mode {self.s['job']} reset   # or the previous override"]


class ManualAction(Action):
    kind = "manual"

    def label(self) -> str:
        return "MANUAL: " + self.s["note"]


ACTIONS: dict[str, type[Action]] = {c.kind: c for c in (DisableAction, MoveAction, StubAction, CronAction, JobModeAction, ManualAction)}


# =================================================================== parity
@dataclass
class Check:
    kind: str
    name: str
    ok: bool | None             # None = could not be verified (counts as not green)
    detail: str


@dataclass
class Parity:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok is True for c in self.checks)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": [{"kind": c.kind, "name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}


# The three reads below are what scheduler.py does too (sched.json, jobs.proc_alive, scheduler.locked_files), kept here on purpose: a
# rollback is needed most when the scheduler module itself no longer imports (a bad deploy), and it must still be able to tell
# whether a job is running. They read only; nothing here changes anything.
def _sched_jobs() -> dict:
    d = core.read_json(core.STATE_DIR / "sched.json", None)
    return d["jobs"] if isinstance(d, dict) and isinstance(d.get("jobs"), dict) else {}


def _pid_alive(pid: Any, start: Any) -> bool:
    try:
        from . import jobs
        return bool(jobs.proc_alive(pid, start))                    # pid AND start time: a recycled pid is not the supervisor
    except ImportError:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError as exc:
            return exc.errno == errno.EPERM


def _group_alive(pgid: Any) -> bool:
    try:
        os.killpg(int(pgid), 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def _locked_backup_files(patterns: tuple[str, ...] = ("/run/lock/backup-*.lock",)) -> tuple[list[str], str]:
    """(backup lock files somebody holds flock() on, error text). /proc/locks is read, never the lock itself: taking it could make a
    backup that is just starting fail its own `flock -n`."""
    import glob
    try:
        text = Path("/proc/locks").read_text()
    except OSError as exc:
        return [], f"cannot read /proc/locks ({type(exc).__name__})"
    held = {tok for ln in text.splitlines() for tok in ln.split() if re.fullmatch(r"[0-9a-f]+:[0-9a-f]+:\d+", tok)}
    out = []
    for pat in patterns:
        for p in glob.glob(pat):
            with contextlib.suppress(OSError):
                st = os.stat(p)
                if f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}" in held:
                    out.append(p)
    return out, ""


class Sources:
    """Where parity reads from; every member is injectable so tests never touch the host."""

    def __init__(self, now: Callable[[], float] | None = None, history: Callable | None = None, config: Callable | None = None,
                 run: Callable | None = None, explain: Callable | None = None, os_names: Callable | None = None,
                 notifications: Callable | None = None, probe_rows: Callable | None = None, health: Callable | None = None,
                 job_attr: Callable | None = None, validate: Callable | None = None, ctl: Callable | None = None,
                 job_history: Callable | None = None, job_last: Callable | None = None, job_running: Callable | None = None,
                 locks: Callable | None = None):
        self.now = now or time.time
        self.history = history or core.read_history
        self.config = config or core.load_config
        self.run = run or _default_run
        self._explain, self._os_names, self._notifs = explain, os_names, notifications
        self._probe_rows, self._health, self._job_attr = probe_rows, health, job_attr
        self._validate, self._ctl, self._job_history, self._job_last = validate, ctl, job_history, job_last
        self._job_running, self._locks = job_running, locks
        self._jh: tuple[float, float, list] | None = None          # (monotonic stamp, since_s, kind=job history): one read per rows()/check()

    def explain(self, now: float) -> list[dict]:
        if self._explain:
            return self._explain(now)
        from . import scheduler                                   # S8; absent until built => the check fails closed
        return scheduler.explain(now)

    def health(self, now: float) -> tuple[str, str]:
        if self._health:
            return self._health(now)
        from . import scheduler
        return scheduler.health(now)

    def validate(self) -> list[str]:
        """Every problem `homelab-maint scheduler validate` would print (jobs.toml unreadable/untrusted, bad jobs, missing executables...)."""
        if self._validate:
            return list(self._validate())
        from . import scheduler
        return list(scheduler.validate())

    def ctl(self, scope: str, args: list[str]) -> list[str]:
        """argv for `systemctl ARGS` in a scope (system, or user:NAME through runuser), the same wrapper the cutover itself uses."""
        return (self._ctl or Host().ctl_argv)(scope, args)

    def job_attr(self, job: str, attr: str) -> Any:
        return self._job_attr(job, attr) if self._job_attr else JobsApi().attr(job, attr)

    def job_history(self, name: str, since_s: float) -> list[dict]:
        """Runs of a job the TICK recorded in history.jsonl (kind "job": finished runs, plus expiries as status "skipped"). Quiet jobs
        (monitors) only record failures there, so their successes are not in this list: see job_last."""
        if self._job_history:
            return list(self._job_history(name, since_s))
        mono = time.monotonic()
        if not (self._jh and mono - self._jh[0] < 2.0 and self._jh[1] >= since_s):
            self._jh = (mono, since_s, self.history(since_s, "job"))
        return [{"t": float(r["t"]), "status": str(r.get("status", ""))} for r in self._jh[2]
                if r.get("task") == name and isinstance(r.get("t"), (int, float))]

    def job_last(self, name: str) -> dict | None:
        """The scheduler's own record of the job's latest finished run (sched.json): {"t", "status", "bad"}, or None."""
        if self._job_last:
            return self._job_last(name)
        js = _sched_jobs().get(name) or {}
        if not js.get("last_end") or not js.get("last_status"):
            return None
        return {"t": float(js["last_end"]), "status": str(js["last_status"]), "bad": bool(js.get("last_bad"))}

    def job_running(self, name: str) -> tuple[bool, str]:
        """Is the tick running this job right now? sched.json says so AND a process of it is really alive (a stale record, left by a
        tick that died, must not block a rollback). Raises when it cannot tell: the caller treats that as busy."""
        if self._job_running:
            return self._job_running(name)
        r = (_sched_jobs().get(name) or {}).get("running")
        if not isinstance(r, dict):
            return False, ""
        pid, run = r.get("pid"), core.read_json(Path(r["run"]), None) if r.get("run") else None
        pgid = run.get("job_pgid") if isinstance(run, dict) else None
        if (pid and _pid_alive(pid, r.get("sup_start"))) or (pgid and _group_alive(pgid)):
            return True, f"pid {pid or pgid}, since {_iso(float(r.get('started', 0)))}"
        if not pid and self.now() - float(r.get("started", 0)) < 30:
            return True, "just launched"                           # intent saved, process not spawned yet
        return False, ""

    def locks(self) -> tuple[list[str], str]:
        """(held backup lock files, error text): /run/lock/backup-*.lock that somebody has flock()ed (read from /proc/locks)."""
        if self._locks:
            return self._locks()
        return _locked_backup_files()

    def os_names(self) -> set[str]:
        if self._os_names:
            return set(self._os_names())
        from .tasks import native
        cfg = self.config().get("tasks", {}).get("os_jobs", {})
        rows = [j["name"] for j in [*native.DEFAULT_OS_JOBS, *cfg.get("extra_jobs", [])] if isinstance(j, dict) and "name" in j]
        return set(rows) - set(cfg.get("ignore", []))

    def probe_rows(self) -> list[dict]:
        if self._probe_rows:
            return self._probe_rows()
        from . import probes
        return probes.snapshot()

    def notifications(self) -> list[dict]:
        if self._notifs:
            return self._notifs()
        out = []
        with contextlib.suppress(OSError):
            for ln in (core.STATE_DIR / "notifications.jsonl").read_text().splitlines()[-2000:]:
                with contextlib.suppress(ValueError):
                    out.append(json.loads(ln))
        return out


def _streak(recs: list[dict], good: Callable[[dict], bool]) -> list[dict]:
    out: list[dict] = []
    for r in sorted(recs, key=lambda r: r.get("t", 0), reverse=True):
        if not good(r):
            break
        out.append(r)
    return out


def _chk_runs(spec: dict, src: Sources, kind: str) -> list[Check]:
    """Consecutive green runs from history.jsonl: kind "task" (history kind task), "job" (the TICK's runs: kind job; monitors are quiet and
    record only failures there, so give a monitor a task/probe check instead) or "probe". A probe counts only if it is up NOW with a fresh
    last_run (a paused, skipped or never evaluated probe is not 'no bad marks'), and only runs that evaluated at least one probe count."""
    green, min_h, fresh_h = int(spec["green"]), float(spec["min_hours"]), float(spec["fresh_h"])
    now = src.now()
    since = (min_h * 1.5 + 24) * 3600
    names = _strs(spec.get("names") or spec.get("name"))
    out = []
    known = {r["name"]: r for r in src.probe_rows()} if kind == "probe" else {}
    recs = src.history(since, kind)
    for n in names:
        if kind == "probe":
            row = known.get(n)
            if row is None:
                out.append(Check(kind, n, False, "probe is not defined in probes.toml"))
                continue
            seen = row.get("last_run")
            if row.get("state") != "up" or not isinstance(seen, (int, float)) or now - seen > fresh_h * 3600:
                when = f"last evaluated {(now - seen) / 3600:.1f} h ago" if isinstance(seen, (int, float)) else "never evaluated"
                out.append(Check(kind, n, False, f"probe is {row.get('state', 'unknown')!r} now (want 'up'), {when}"))
                continue
            # "ev" (the names a run evaluated) is optional in the record: when probes.py writes it, a run that did not evaluate this probe is no evidence
            mine = [r for r in recs if isinstance(r.get("bad"), dict) and r.get("n", 0) > 0 and (not isinstance(r.get("ev"), list) or n in r["ev"])]
            good = (lambda r, n=n: n not in r["bad"])
        else:
            mine = [r for r in recs if r.get("task") == n]
            good = lambda r: r.get("status") in GREEN                      # noqa: E731
        if not mine:
            out.append(Check(kind, n, False, "no runs recorded yet"))
            continue
        newest = max(r["t"] for r in mine)
        if now - newest > fresh_h * 3600:
            out.append(Check(kind, n, False, f"last run {(now - newest) / 3600:.1f} h ago (limit {fresh_h:g} h)"))
            continue
        st = _streak(mine, good)
        span = (st[0]["t"] - st[-1]["t"]) / 3600 if st else 0.0
        ok = len(st) >= green and span >= min_h
        why = f"{len(st)} green runs over {span:.1f} h (need {green} over {min_h:g} h)" if st else "latest run is not green"
        out.append(Check(kind, n, ok, why))
    return out


# systemd prints time spans like "infinity", "30min", "1h 30min", "2min 30s"
_SPAN_S = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0, "month": 2629800.0, "y": 31557600.0}


def _span_s(text: str) -> float | None:
    t = text.strip()
    if t == "infinity":
        return 0.0                                              # TimeoutStartSec=infinity == jobs.toml timeout_s = 0 (none)
    parts = re.findall(r"(\d+(?:\.\d+)?)(us|ms|min|month|s|h|d|w|y)", t)
    return sum(float(n) * _SPAN_S[u] for n, u in parts) if parts and "".join(n + u for n, u in parts) == t.replace(" ", "") else None


def _unit_equiv(spec: dict, src: Sources) -> Check:
    """Pre-cutover equivalence: a live proof run of an adapter job cannot exist before the cutover (the legacy-unit interlock forbids
    it), so prove the next best thing: the EFFECTIVE jobs.toml job launches exactly what the legacy service launches (argv, user, nice,
    ionice, timeout, RequiresMountsFor, working directory, environment variable NAMES) according to `systemctl show`. A job that
    differs here would run the backup differently from the unit that has been running it for years."""
    job, unit, scope = spec["job"], spec["unit"], spec.get("scope", "system")
    props = "ExecStart,User,Nice,IOSchedulingClass,IOSchedulingPriority,TimeoutStartUSec,RequiresMountsFor,WorkingDirectory,Environment"
    r = src.run(src.ctl(scope, ["show", "--no-pager", "-p", props, unit]), timeout=20)
    if r.returncode != 0:
        return Check("unit_equiv", unit, False, f"cannot read {unit} ({scope}): rc={r.returncode}")
    kv: dict[str, list[str]] = {}
    for ln in (r.stdout or "").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv.setdefault(k, []).append(v)
    one = lambda k: (kv.get(k) or [""])[0]                       # noqa: E731
    diffs: list[str] = []
    cmd = [{"{self}": "/usr/local/sbin/homelab-maint", "{python}": "/usr/bin/python3"}.get(x, x) for x in src.job_attr(job, "command")]
    execs = [m.group(1) for v in kv.get("ExecStart", []) if (m := re.search(r"argv\[\]=(.*?) ; ignore_errors", v))]
    if len(execs) != 1:
        diffs.append(f"the unit has {len(execs)} ExecStart lines (the job runs one command)")
    elif execs[0] != " ".join(cmd):
        diffs.append("command differs from ExecStart")
    user = one("User") or ("root" if scope == "system" else scope.split(":", 1)[1])
    if user != src.job_attr(job, "user"):
        diffs.append(f"user: unit {user}, job {src.job_attr(job, 'user')}")
    nice = int(one("Nice") or 0)
    if nice != int(src.job_attr(job, "nice") or 0):
        diffs.append(f"nice: unit {nice}, job {src.job_attr(job, 'nice') or 0}")
    icls = int(one("IOSchedulingClass") or 0) or 2               # systemd reports 0 or 2 for 'never set': best-effort
    jcls = src.job_attr(job, "ionice_class") or 2
    if icls != jcls:
        diffs.append(f"ionice class: unit {icls}, job {jcls}")
    jprio = src.job_attr(job, "ionice_prio")
    if jprio is not None and int(one("IOSchedulingPriority") or 4) != int(jprio):
        diffs.append(f"ionice priority: unit {one('IOSchedulingPriority')}, job {jprio}")
    tmo = _span_s(one("TimeoutStartUSec"))
    if tmo is None or abs(tmo - float(src.job_attr(job, "timeout_s") or 0)) > 0.5:
        diffs.append(f"timeout: unit {one('TimeoutStartUSec') or '?'}, job {src.job_attr(job, 'timeout_s') or 0} s")
    mounts, jm = set(one("RequiresMountsFor").split()), set(src.job_attr(job, "requires_mounts") or [])
    if mounts != jm:
        diffs.append(f"RequiresMountsFor: unit {sorted(mounts)}, job {sorted(jm)}")
    wd = one("WorkingDirectory")
    if wd and not wd.startswith("!") and wd != (src.job_attr(job, "workdir") or ""):
        diffs.append("working directory differs")
    jenv = src.job_attr(job, "env") or {}
    try:
        uenv = dict(t.split("=", 1) for t in shlex.split(one("Environment")) if "=" in t)
    except ValueError:
        uenv = {}
    wrong = sorted(k for k, v in uenv.items() if jenv.get(k) != v)             # names only: a value may be a secret
    if wrong:
        diffs.append(f"environment differs for {', '.join(wrong)}")
    return Check("unit_equiv", f"{job} = {unit}", not diffs, "; ".join(diffs) if diffs else
                 "jobs.toml launches exactly what the legacy service launches (command, user, nice, ionice, timeout, mounts, environment)")


def _scheduler_validate(spec: dict, src: Sources) -> Check:
    """`homelab-maint scheduler validate` must be clean for the named job(s) (and for everything global: an unreadable or untrusted
    jobs.toml schedules NOTHING, which is how one typo silences every job the tick was given)."""
    jobs_ = _strs(spec.get("jobs") or spec.get("job"))
    probs = [p for p in src.validate()
             if not jobs_ or not p.startswith(("job ", "task ")) or any(p.startswith((f"job {j}:", f"task {j}:")) for j in jobs_)]
    return Check("scheduler_validate", ", ".join(jobs_) or "all jobs", not probs,
                 "jobs.toml loads and validates" if not probs else f"{len(probs)} problem(s): {probs[0][:90]}")


def _cfg_get(cfg: dict, key: str) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _task_applies(spec: dict, src: Sources) -> Check:
    """A port that cannot act is not a replacement. The runner lets a C1 task change anything only when ALL hold: tasks.X.mode is
    "apply", the run was started with --apply, and no PAUSE exists. Who adds --apply depends on who starts the task: the tick does it
    for a task with its own cron `schedule`; otherwise the tier job (or, until that job is cut over, the tier's systemd service). The
    check-tier service ships WITHOUT --apply, so a check-tier C1 task (comfyui_idle_reclaim, immich_recycle) would only ever report
    while the legacy timer it replaced is already gone. This check proves the --apply is really there."""
    name, tier_job = spec["name"], spec["tier_job"]
    cfg = src.config().get("tasks", {}).get(name, {})
    if cfg.get("mode") != "apply":
        return Check("task_applies", name, False, f"tasks.{name}.mode is {cfg.get('mode', 'report')!r}, not 'apply': it would only report")
    if cfg.get("schedule"):
        return Check("task_applies", name, True, f"mode apply, and its own schedule {cfg['schedule']!r}: the tick starts it with --apply")
    if src.job_attr(tier_job, "mode") == "managed":
        via, ok = f"job {tier_job}", "--apply" in src.job_attr(tier_job, "command")
    else:                                                       # the legacy tier service still drives the tier
        units = [r.split(":", 1)[1] for r in src.job_attr(tier_job, "retire") if r.startswith("system:")]
        svc = re.sub(r"\.timer$", ".service", units[0]) if units else ""
        r = src.run(["systemctl", "show", "-p", "ExecStart", "--value", svc], timeout=20) if svc else None
        via, ok = f"{svc or 'the legacy tier service'}", bool(r and r.returncode == 0 and "--apply" in (r.stdout or ""))
    if ok:
        return Check("task_applies", name, True, f"mode apply, and {via} starts the tier with --apply")
    return Check("task_applies", name, False, f"mode is apply but nothing runs it with --apply: {via} has none and the task has no schedule. Add --apply "
                                              f"to the {tier_job} command and its legacy service, or give [tasks.{name}] a cron `schedule`")


def run_check(spec: dict, src: Sources) -> list[Check]:
    """Evaluate one parity spec. Never raises: any problem is a not-green Check (fail closed)."""
    k = spec["kind"]
    try:
        now = src.now()
        if k in ("task", "job", "probe"):
            return _chk_runs(spec, src, k)
        if k == "scheduled":
            # Only proves the scheduler KNOWS the job and parses its schedule (observe jobs get a next_due too). That it really runs is
            # proven after the cutover (verified_runs, see Migrator.deps) and before it by unit_equiv + scheduler_validate.
            rows = src.explain(now)
            row = next((r for r in rows if r.get("job") == spec["job"]), None)
            ok = bool(row and row.get("next_due"))
            return [Check(k, spec["job"], ok, f"scheduler lists it (mode {row.get('mode', '?')}), next due set; not proof that it runs" if ok else
                          "the scheduler does not list this job")]
        if k == "scheduler_health":
            st, why = src.health(now)
            return [Check(k, "tick", st == "ok", f"{st}: {why}")]
        if k == "scheduler_validate":
            return [_scheduler_validate(spec, src)]
        if k == "unit_equiv":
            return [_unit_equiv(spec, src)]
        if k == "job_attr":
            val = src.job_attr(spec["job"], spec["attr"])
            if "contains" in spec:
                return [Check(k, f"{spec['job']}.{spec['attr']}", spec["contains"] in val, f"{spec['attr']} = {val!r} (want it to contain {spec['contains']!r})")]
            return [Check(k, f"{spec['job']}.{spec['attr']}", val == spec["equals"], f"{spec['attr']} = {val!r} (want {spec['equals']!r})")]
        if k == "task_applies":
            return [_task_applies(spec, src)]
        if k in ("status_json", "file_age"):
            p = Path(spec["path"])
            age = now - p.stat().st_mtime
            lim = float(spec["max_age_s"]) if k == "file_age" else float(spec.get("max_age_hours", 200)) * 3600
            if age > lim:
                return [Check(k, p.name, False, f"{age / 3600:.1f} h old (limit {lim / 3600:.1f} h)")]
            if k == "status_json":
                val = _cfg_get(json.loads(p.read_text()), spec.get("result_key", "result"))
                good = _strs(spec.get("ok_values", ["ok"]))
                return [Check(k, p.name, val in good, f"{spec.get('result_key', 'result')}={val!r} (want {good})")]
            return [Check(k, p.name, True, f"{age / 3600:.1f} h old")]
        if k == "config":
            val = _cfg_get(src.config(), spec["key"])
            ok = (val == spec["equals"]) if "equals" in spec else (val in spec["in"]) if "in" in spec else bool(val)
            return [Check(k, spec["key"], ok, f"{spec['key']} = {val!r}" + (f" (want {spec['equals']!r})" if "equals" in spec else ""))]
        if k == "command":
            r = src.run(list(spec["argv"]), timeout=int(spec.get("timeout", 60)))
            ok = r.returncode in spec.get("ok_codes", [0]) and (not spec.get("regex") or re.search(spec["regex"], r.stdout or ""))
            first = next((ln.strip() for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines() if ln.strip()), "")
            return [Check(k, os.path.basename(spec["argv"][0]), bool(ok), f"rc={r.returncode}" + (f": {first[:80]}" if not ok and first else ""))]
        if k == "notify":
            kinds, lim = _strs(spec.get("kinds", ["test"])), now - float(spec.get("max_age_days", 30)) * 86400
            hit = [r for r in src.notifications() if r.get("ok") and r.get("kind") in kinds and float(r.get("ts", 0)) >= lim]
            return [Check(k, "/".join(kinds), bool(hit), f"{len(hit)} delivered in the last {spec.get('max_age_days', 30)} days "
                                                        f"(run: homelab-maint notify-test)")]
        if k == "os_job":
            if spec["name"] not in src.os_names():
                return [Check(k, spec["name"], False, "not covered by the os_jobs task (add it to [tasks.os_jobs].extra_jobs)")]
            runs = _chk_runs({**spec, "name": "os_jobs"}, src, "task")
            return [Check(k, spec["name"], c.ok, c.detail) for c in runs]
        if k == "none":
            return [Check("none", "-", True, "nothing to verify: " + str(spec.get("note", ""))[:100])]
        return [Check("manual", "manual", None, str(spec.get("note", "cannot be verified by the tool")))]
    except Exception as exc:  # noqa: BLE001 - a broken check is a failed check, never a crash and never a pass
        return [Check(k, str(spec.get("name") or spec.get("key") or spec.get("job") or k), False, f"check failed: {type(exc).__name__}: {str(exc)[:80]}")]


# =================================================================== migrator
@dataclass
class Step:
    idx: int
    action: Action
    label: str
    state: str                   # todo | satisfied | refused | manual
    pre: dict | None
    ops: list[Op] = field(default_factory=list)
    note: str = ""


@dataclass
class Outcome:
    item: str
    verb: str
    apply: bool
    ok: bool = False
    rc: int = 1
    lines: list[str] = field(default_factory=list)
    refused: str = ""

    def say(self, s: str) -> None:
        self.lines.append(s)


class Migrator:
    def __init__(self, inv: Inventory | None = None, host: Host | None = None, sources: Sources | None = None,
                 audit: Callable | None = None, notifier: Callable | None = None, paused: Callable | None = None,
                 now: Callable[[], float] | None = None, state_dir: Path | None = None, run_dir: Path | None = None,
                 conf_dir: Path | None = None, user: str | None = None, tick_lock: Callable | None = None):
        self.inv = inv or load_inventory()
        self.host = host or Host()
        self._now = now or time.time
        self.src = sources or Sources(now=self._now)
        if self.src._ctl is None:
            self.src._ctl = self.host.ctl_argv                      # unit_equiv reads user units through the same runuser wrapper
        self._tick_lock = tick_lock                                 # () -> context manager that keeps the scheduler tick from launching jobs
        self.tick_wait_s = 20.0                                     # how long to wait for a tick that is mid-run (it normally takes tens of ms)
        self._audit = audit or core.audit
        self._notifier = notifier
        self._paused = paused or core.paused
        self.state_dir = Path(state_dir) if state_dir else core.STATE_DIR
        self.run_dir = Path(run_dir) if run_dir else core.RUN_DIR
        self.conf_dir = Path(conf_dir) if conf_dir else core.CONF_DIR
        self.user = user or _whoami()
        self.apply = False

    # ---- state -------------------------------------------------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.state_dir / "migration.json"

    def load_state(self) -> dict:
        st = core.read_json(self.state_path, None)
        return st if isinstance(st, dict) and isinstance(st.get("items"), dict) else {"v": 1, "items": {}}

    def save_state(self, st: dict) -> None:
        core.write_json_atomic(self.state_path, st, 0o600)

    def _rec(self, st: dict, name: str) -> dict:
        return st["items"].setdefault(name, {"state": "pending", "history": []})

    def effective(self, it: Item, st: dict | None = None) -> tuple[str, float | None]:
        """(state, retired_at): the recorded state, else retired-before-migrate for pre_retired items, else pending."""
        rec = (st or self.load_state())["items"].get(it.name)
        if rec and rec.get("state") != "pending":
            return rec["state"], rec.get("cutover_at")
        if it.pre_retired:
            return "retired", datetime.strptime(it.pre_retired, "%Y-%m-%d").timestamp()
        return "pending", None

    # ---- environment per item -----------------------------------------------------------------------------------------------
    def env(self, it: Item) -> Env:
        return Env(self.host, it, f"{self.inv.legacy_root}/{it.name}", self.state_dir, self._now())

    def actions(self, it: Item, env: Env | None = None) -> list[Action]:
        env = env or self.env(it)
        return [ACTIONS[a["do"]](a, env) for a in it.actions]

    # ---- audit gate (ctx.act semantics) -------------------------------------------------------------------------------------
    def _act(self, it: Item, what: str, target: str, fn: Callable[[], None], undo: bool = False) -> bool:
        task = "migrate"
        if not target or not target.strip():
            self._audit(task, f"{it.name}:{what}", target, 0, "refused-empty-selector")
            return False
        if not self.apply:
            self._audit(task, f"{it.name}:{what}", target, 0, "dry-run")
            return False
        if not undo and self._paused(task):                     # an in-flight undo restores the status quo, so it ignores PAUSE
            self._audit(task, f"{it.name}:{what}", target, 0, "refused-paused")
            raise Refused("PAUSE is present: nothing mutates")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self._audit(task, f"{it.name}:{what}", target, 0, f"failed: {str(exc)[:120]}")
            raise
        self._audit(task, f"{it.name}:{what}", target, 0, "done")
        return True

    # ---- planning (read-only) -----------------------------------------------------------------------------------------------
    def plan_steps(self, it: Item) -> list[Step]:
        env = self.env(it)
        steps = []
        for i, a in enumerate(self.actions(it, env)):
            if a.kind == "manual":
                steps.append(Step(i, a, a.label(), "manual", None))
                continue
            try:
                pre = a.probe()
                if a.satisfied(pre):
                    steps.append(Step(i, a, a.label(), "satisfied", pre, note="already in effect"))
                else:
                    steps.append(Step(i, a, a.label(), "todo", pre, a.forward(pre)))
                    a.virtual()                                             # later steps see this one's effect (dry run only)
            except (Refused, OSError) as exc:                               # unreadable state is a refusal, never a traceback
                steps.append(Step(i, a, a.label(), "refused", None, note=str(exc)))
        return steps

    def anchor(self, di: Item, state: str, at: float | None) -> tuple[float | None, bool, str]:
        """(soak anchor, verified, detail) for a retired item. An item that handed a job to the tick (managed) only counts as soaking from its
        FIRST green run recorded by the tick after the cutover, and a dependent waits for `verified_runs` of them: calendar time with
        the job never launched (pressure deferral, a bad mount, a jobs.toml the tick ignores) proves nothing. Monitors are quiet (only
        failures reach history), so for those the scheduler's own last-run record is the proof of life (one run) and the soak runs from the
        cutover."""
        job = next((a["job"] for a in di.actions if a["do"] == "job_mode" and a["mode"] == "managed"), None)
        if not job or state != "retired" or at is None:
            return at, True, ""
        need = max(1, di.verified_runs)
        try:
            runs = [r for r in self.src.job_history(job, self._now() - at + 3600) if r["t"] >= at and r.get("status") in GREEN]
            last = self.src.job_last(job)
        except Exception as exc:  # noqa: BLE001 - cannot read the evidence => not verified
            return at, False, f"cannot read the run history of job {job}: {type(exc).__name__}"
        if len(runs) >= need:
            return min(r["t"] for r in runs), True, f"{len(runs)} green run(s) of job {job} recorded since the cutover"
        if need <= 1 and last and last["t"] >= at and last["status"] in GREEN and not last.get("bad"):
            return at, True, f"job {job} last ran green at {_iso(last['t'])}"
        return at, False, (f"job {job} has {len(runs)} of {need} green run(s) recorded since the cutover: the tick has not proven it runs. "
                           f"Start one attended: homelab-maint job run {job}")

    def deps(self, it: Item, st: dict | None = None) -> list[Check]:
        st, now, out = st or self.load_state(), self._now(), []
        for d in it.depends_on:
            di = self.inv.get(d)
            state, at = self.effective(di, st)
            if state not in ("retired", "adopted"):
                out.append(Check("depends", d, False, f"{d} is {state}; cut it over first"))
                continue
            at, verified, why = self.anchor(di, state, at)
            if not verified:
                out.append(Check("depends", d, False, f"{d} is retired but unproven: {why}"))
            elif di.soak_days and at is not None and now < at + di.soak_days * 86400:
                left = (at + di.soak_days * 86400 - now) / 86400
                out.append(Check("depends", d, False, f"{d} is still soaking: {left:.1f} more day(s) of {di.soak_days:g}" + (f" ({why})" if why else "")))
            else:
                out.append(Check("depends", d, True, f"{d} {state}" + (f" for {(now - at) / 86400:.1f} d" if at else "") + (f", {why}" if why else "")))
        return out

    def parity(self, it: Item) -> Parity:
        p = Parity()
        for spec in it.parity:
            p.checks.extend(run_check(spec, self.src))
        return p

    def busy(self, it: Item) -> list[str]:
        """Why nothing may be changed for this item right now (empty = idle). Three independent sources, all fail-closed: a legacy unit
        that is active, a job the TICK is running (a cut-over backup is never backup-system.service again, so the unit check alone would
        always say idle), and for backup items a held /run/lock/backup-*.lock (a script started by hand)."""
        out = []
        for u in it.require_idle:
            try:
                if self.host.unit(it.scope, u).get("ActiveState") in ACTIVE:
                    out.append(f"{u} is running right now")
            except Refused as exc:
                out.append(str(exc))                                      # cannot tell => treat as busy
        for j in dict.fromkeys([*([it.job] if it.job else []), *it.require_idle_jobs]):
            try:
                running, why = self.src.job_running(j)
            except Exception as exc:  # noqa: BLE001
                running, why = True, f"cannot tell ({type(exc).__name__})"
            if running:
                out.append(f"job {j} is running under the tick ({why})")
        if it.backup:
            try:
                held, err = self.src.locks()
            except Exception as exc:  # noqa: BLE001
                held, err = [], f"cannot read the backup locks ({type(exc).__name__})"
            if err:
                out.append(err)
            elif held:
                out.append(f"a backup is running: {os.path.basename(held[0])} is held")
        return out

    @contextlib.contextmanager
    def _tick_hold(self, it: Item):
        """While a job is handed over or taken back, the tick must not launch it between the idle check and the switch: hold tick.lock
        (the tick never takes migrate.lock, so the two cannot deadlock)."""
        if not (it.job or it.require_idle_jobs):
            yield
            return
        lock = self._tick_lock
        if lock is None:
            try:
                from . import scheduler
            except Exception:  # noqa: BLE001 - no tick can run without that module, so nothing can launch behind our back
                yield
                return
            lock = lambda: scheduler.tick_lock(wait_s=self.tick_wait_s)          # noqa: E731
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(lock())
            except core.Locked:
                raise Refused("the scheduler tick holds its lock right now (it may be starting a job): try again in a minute") from None
            yield

    # ---- commands -----------------------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _lock(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        f = open(self.run_dir / "migrate.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise Refused("another migrate run is in progress") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    def check(self, name: str) -> Outcome:
        it = self._item(name)
        out = Outcome(name, "check", False)
        par, dp = self.parity(it), self.deps(it)
        for c in [*dp, *par.checks]:
            out.say(f"  [{'ok' if c.ok else '??' if c.ok is None else 'NO'}] {c.kind} {c.name}: {c.detail}")
        if not it.parity and it.pre_retired:
            out.say("  retired by hand before this tool existed: nothing to check")
        out.ok = (par.ok or (not it.parity and bool(it.pre_retired))) and all(c.ok for c in dp)
        out.rc = 0 if out.ok else 1
        out.say(f"{name}: parity {'GREEN' if out.ok else 'NOT green'}")
        return out

    def _item(self, name: str) -> Item:
        it = self.inv.get(name)
        if it is None:
            raise KeyError(name)
        return it

    def cutover(self, name: str, apply: bool = False, force: bool = False, reason: str = "") -> Outcome:
        it = self._item(name)
        self.apply = apply
        out = Outcome(name, "cutover", apply)
        tag = "" if apply else "DRY RUN (nothing will change; add --apply) - "
        out.say(f"{tag}cutover {name}: {it.title} [{it.mode}, wave {it.wave}]")
        try:
            with (self._lock() if apply else contextlib.nullcontext()), (self._tick_hold(it) if apply else contextlib.nullcontext()):
                return self._cutover(it, out, force, reason)
        except Refused as exc:
            out.refused = str(exc)
            out.say(f"REFUSED: {exc}")
            return out

    def _cutover(self, it: Item, out: Outcome, force: bool, reason: str) -> Outcome:
        st = self.load_state()
        state, _ = self.effective(it, st)
        if force and len(reason.strip()) < 8:
            raise Refused("--force needs --reason with at least 8 characters (it is written to the journal)")
        if self.apply and self._paused("migrate"):
            self._audit("migrate", f"{it.name}:cutover", it.name, 0, "refused-paused")
            raise Refused("PAUSE (or PAUSE.migrate) is present: cutovers are frozen; `homelab-maint resume migrate` first")
        steps = self.plan_steps(it)
        for s in steps:
            out.say(f"  {s.state:<9} {s.label}" + (f"  ({s.note})" if s.note and s.state != 'todo' else ""))
            for op in s.ops:
                out.say(f"      $ {op.text}")
        bad = [s for s in steps if s.state == "refused"]
        if bad:
            raise Refused(f"{bad[0].label}: {bad[0].note}")
        todo = [s for s in steps if s.state == "todo"]
        if state in ("retired", "adopted") and not todo:
            out.ok, out.rc = True, 0
            out.say(f"{it.name}: already {state}; nothing to do (idempotent)")
            return out
        busy = self.busy(it)
        if busy:
            raise Refused("busy: " + "; ".join(busy) + " (never cut over under a running job; try again when it has finished)")
        dp, par = self.deps(it, st), self.parity(it)
        for c in [*dp, *par.checks]:
            out.say(f"  [{'ok' if c.ok else '??' if c.ok is None else 'NO'}] {c.kind} {c.name}: {c.detail}")
        green = par.ok and all(c.ok for c in dp)
        if not green and not force:
            raise Refused("parity is not green (see the checks above); fix that, or override with --force --reason '...'")
        if not green:
            out.say(f"  FORCED past a non-green check: {reason}")
        for line in (it.notes.splitlines() if it.notes and self.apply else []):
            out.say(f"  note: {line}")
        manual = [s for s in steps if s.state == "manual"]
        if not self.apply:
            for s in manual:
                out.say(f"  {s.label}")
            out.ok, out.rc = True, 0
            out.say(f"{it.name}: dry run complete ({len(todo)} step(s) would run)")
            for s in todo:
                for op in s.ops:
                    self._act(it, s.action.kind, op.text, op.fn)           # audits "dry-run" for each exact command
            return out
        return self._execute(it, steps, par, force, reason, out)

    def _execute(self, it: Item, steps: list[Step], par: Parity, force: bool, reason: str, out: Outcome) -> Outcome:
        st = self.load_state()
        rec = self._rec(st, it.name)
        now = self._now()
        prev_state = rec.get("state")
        # Records of an EARLIER run survive for every step that is (still) in effect, whatever state that run ended in: partial (killed),
        # attention (an undo could not finish), failed with leftovers, or retired and drifting. They hold the prior state rollback must
        # restore, so a step this run finds already satisfied is NOT re-recorded as "pre_satisfied" (rollback would then leave it as it
        # is: a timer that stays disabled while the tool says it is restored). Only a completed rollback, or a step the undo marked
        # "undone", starts clean.
        old = {} if prev_state in (None, "pending", "rolled_back") else \
            {r["idx"]: r for r in rec.get("actions", []) if "idx" in r and r.get("status") in ("done", "started", "pre_satisfied")}
        rec.update(state="partial", actions=[dict(old[i]) for i in sorted(old)], started_at=now)
        self.save_state(st)                                                 # write-ahead: a crash leaves a record to roll back
        done: list[tuple[int, Action, dict]] = []                           # only what THIS run changed: a failed repair undoes just that
        fresh = self.actions(it)                                            # new Env: the dry-run overlay of plan_steps must not leak in

        def put(entry: dict) -> dict:
            rec["actions"] = sorted([r for r in rec["actions"] if r["idx"] != entry["idx"]] + [entry], key=lambda r: r["idx"])
            return entry

        for s in steps:
            a = fresh[s.idx]
            pre: dict = {}
            keep: dict | None = None
            mine = False
            if a.kind == "manual":
                put({"idx": s.idx, "kind": "manual", "status": "manual", "label": s.label})
                continue
            try:
                pre = a.probe()                                             # fresh: earlier steps changed the world
                prior = old.get(s.idx)
                prior = prior if prior and prior.get("kind") == a.kind else None
                if a.satisfied(pre):
                    if prior is None:
                        put({"idx": s.idx, "kind": a.kind, "status": "pre_satisfied", "label": s.label})
                    elif prior["status"] == "started":                      # an interrupted run's effect landed: keep ITS prior state
                        put({**prior, "status": "done", "label": s.label})
                    continue
                ops = a.forward(pre)
                # an interrupted attempt's write-ahead record holds the state from BEFORE it touched anything (the probe now may be half-changed)
                keep = prior["pre"] if prior and prior["status"] == "started" and "pre" in prior else None
                cur = put({"idx": s.idx, "kind": a.kind, "status": "started", "label": s.label, "pre": keep if keep is not None else a.persist(pre)})
                mine = True
                self.save_state(st)
                for op in ops:
                    self._act(it, a.kind, op.text, op.fn)
                if keep is None:
                    cur["pre"] = a.persist(pre)                             # forward() may have added e.g. the crontab backup path
                if not a.verify(pre):
                    raise OpError(f"{a.label()}: verification failed after the change")
                cur["status"] = "done"
                done.append((s.idx, a, keep if keep is not None else pre))
            except (Refused, OpError, OSError) as exc:
                out.say(f"FAILED at step {s.idx + 1} ({a.label()}): {exc}")
                if mine:
                    done.append((s.idx, a, keep if keep is not None else pre))   # partially applied: undo it too
                return self._failed(it, st, rec, done, out, str(exc), prev_state)
        pause_note = self._resume_replacements(it, rec)
        rec.update(state="adopted" if not it.retirable else "retired", cutover_at=now, by=self.user, forced=force,
                   reason=reason.strip(), parity=par.to_dict())
        rec["history"].append({"ts": now, "event": "cutover", "forced": force, "reason": reason.strip()})
        self.save_state(st)
        manual = [s.label for s in steps if s.state == "manual"]
        for m in manual:
            out.say(f"  {m}")
        job = next((x["job"] for x in it.actions if x["do"] == "job_mode" and x["mode"] == "managed"), None)
        first = (f"; start the first run ATTENDED: homelab-maint job run {job} (nothing that depends on it counts until the tick has recorded "
                 f"{max(1, it.verified_runs)} green run(s))") if job else ""
        n_done = sum(1 for x in rec["actions"] if x["status"] == "done")
        detail = f"{n_done} action(s) done" + (f"; {len(manual)} manual step(s) left for the owner" if manual else "") + pause_note + first
        self._record(it, "cutover", detail, True, bool(manual) is False, force, reason)
        out.ok, out.rc = True, 0
        out.say(f"{it.name}: {rec['state']} ({detail}). Roll back with: homelab-maint migrate rollback {it.name} --apply")
        return out

    def _failed(self, it: Item, st: dict, rec: dict, done: list, out: Outcome, why: str, prev_state: str | None = None) -> Outcome:
        left = self._undo(it, done, out, rec)
        # a failed REPAIR of an item that was already retired leaves it as it was (retired, drifting), not "failed"; effects of an EARLIER
        # run that are still in place (a re-run of a crashed cutover that fails again) are not "fully undone" either
        still = [r for r in rec["actions"] if r.get("status") in ("done", "started")]
        rec["state"] = ("attention" if left else prev_state if prev_state in ("retired", "adopted") else "attention" if still else "failed")
        rec["history"].append({"ts": self._now(), "event": "failed", "detail": why[:200], "undo_left": left})
        self.save_state(st)
        self._record(it, "cutover failed", why[:200] + ("" if not left else f"; NOT restored: {', '.join(left)}"), False, False, False, "")
        out.rc = 4
        out.say(f"{it.name}: cutover failed and this run's changes were {'undone' if not left else 'only partly undone: ' + ', '.join(left)}")
        return out

    def _undo(self, it: Item, done: list[tuple[int, Action, dict]], out: Outcome, rec: dict) -> list[str]:
        """Replay the inverse of `done` (reverse order). Returns labels that could NOT be restored; the records of what WAS restored are
        marked "undone" so a later run never mistakes them for something still in effect."""
        left: list[str] = []
        for idx, a, pre in reversed(done):
            try:
                if not a.restored(pre):
                    for op in a.backward(pre):
                        self._act(it, f"undo-{a.kind}", op.text, op.fn, undo=True)
                    if not a.restored(pre):
                        raise OpError("state does not match the recorded one after the undo")
                for r in rec["actions"]:
                    if r["idx"] == idx and r.get("status") in ("done", "started"):
                        r["status"] = "undone"
            except (Refused, OpError, OSError) as exc:
                left.append(a.label())
                out.say(f"  could not restore {a.label()}: {exc}")
        return left

    def _resume_replacements(self, it: Item, rec: dict) -> str:
        """After a re-cutover, remove the PAUSE.<replacement> files a previous rollback created (only those)."""
        made = rec.pop("paused_replacements", [])
        for n in made:
            with contextlib.suppress(OSError):
                self._act(it, "resume", f"rm {self.conf_dir}/PAUSE.{n}", lambda n=n: os.unlink(self.conf_dir / f"PAUSE.{n}"), undo=True)
        return f"; resumed {', '.join(made)}" if made else ""

    def rollback(self, name: str, apply: bool = False, force: bool = False, reason: str = "") -> Outcome:
        it = self._item(name)
        self.apply = apply
        out = Outcome(name, "rollback", apply)
        out.say(("" if apply else "DRY RUN (nothing will change; add --apply) - ") + f"rollback {name}: {it.title}")
        try:
            with (self._lock() if apply else contextlib.nullcontext()), (self._tick_hold(it) if apply else contextlib.nullcontext()):
                return self._rollback(it, out, force, reason)
        except Refused as exc:
            out.refused = str(exc)
            out.say(f"REFUSED: {exc}")
            return out

    def _rollback(self, it: Item, out: Outcome, force: bool, reason: str) -> Outcome:
        st = self.load_state()
        rec = st["items"].get(it.name)
        if force and len(reason.strip()) < 8:
            raise Refused("--force needs --reason with at least 8 characters")
        if not rec or rec.get("state") in (None, "pending", "rolled_back"):
            if it.pre_retired:
                raise Refused(f"{it.name} was retired by hand on {it.pre_retired}, before this tool existed: there is no record to restore from")
            out.ok, out.rc = True, 0
            out.say(f"{it.name}: nothing to roll back (state: {rec['state'] if rec else 'pending'})")
            return out
        if self.apply and self._paused("migrate") and not force:
            self._audit("migrate", f"{it.name}:rollback", it.name, 0, "refused-paused")
            raise Refused("PAUSE is present; a rollback while paused needs --force --reason '...'")
        dependents = [i.name for i in self.inv.items if it.name in i.depends_on and self.effective(i, st)[0] in ("retired", "adopted")]
        if dependents and not force:
            raise Refused(f"{', '.join(dependents)} depend on {it.name} and are retired: roll those back first (or --force)")
        busy = self.busy(it)           # --force cannot override this: re-enabling a Persistent timer under a running backup makes a false FAILURE
        if busy and self.apply:
            raise Refused("busy: " + "; ".join(busy) + " (never roll back under a running job: wait for it to finish, or stop that run yourself first)")
        if busy:
            out.say("  NOTE: --apply would be refused right now: " + "; ".join(busy))
        env, todo = self.env(it), []
        acts = {i: a for i, a in enumerate(self.actions(it, env))}
        for r in reversed(rec.get("actions", [])):
            if r.get("status") not in ("done", "started"):
                continue
            a = acts.get(r["idx"])
            if a is None or a.kind != r["kind"]:
                raise Refused("the inventory changed since the cutover (actions differ): restore by hand using the legacy README")
            todo.append((a, r["pre"]))
        left: list[str] = []
        for a, pre in todo:
            try:
                if a.restored(pre):
                    out.say(f"  satisfied {a.label()} (already restored)")
                    continue
                ops = a.backward(pre)
                out.say(f"  todo      undo {a.label()}")
                if pre.get("persistent") and pre.get("enabled") in ENABLED:
                    out.say(f"      note: {a.s['unit']} is Persistent=yes: once it is enabled and started again systemd may run an occurrence it missed "
                            f"at once (the job's own lock still applies)")
                for op in ops:
                    out.say(f"      $ {op.text}")
                    # PAUSE was decided above (refuse, or --force): once a rollback has started it finishes, never half-way
                    self._act(it, f"undo-{a.kind}", op.text, op.fn, undo=True)
                if self.apply and not a.restored(pre):
                    raise OpError("state does not match the recorded one after the undo")
            except (Refused, OpError, OSError) as exc:
                left.append(a.label())
                out.say(f"  FAILED undo {a.label()}: {exc}")
                break                                                       # never carry on half-way into an inconsistent state
        if not self.apply:
            if it.pause_on_rollback:
                out.say(f"  todo      pause {', '.join(it.pause_on_rollback)} (PAUSE.<name>) so the restored legacy job and its replacement never both run")
            out.ok, out.rc = not left, 0 if not left else 4
            out.say(f"{it.name}: dry run complete")
            return out
        now = self._now()
        if left:
            rec["state"] = "attention"
            rec["history"].append({"ts": now, "event": "rollback-failed", "left": left})
            self.save_state(st)
            self._record(it, "rollback failed", f"not restored: {', '.join(left)}", False, False, force, reason)
            out.rc = 4
            out.say(f"{it.name}: rollback stopped; the state is 'attention'. Fix the cause above and run rollback again.")
            return out
        paused = self._pause_replacements(it, rec)
        rec.update(state="rolled_back", rolled_back_at=now)
        rec["history"].append({"ts": now, "event": "rollback", "reason": reason.strip(), "forced": force})
        self.save_state(st)
        self._record(it, "rollback", "legacy state restored exactly" + (f"; paused {', '.join(paused)} so both do not run" if paused else ""),
                     True, True, force, reason, significant=True)
        out.ok, out.rc = True, 0
        out.say(f"{it.name}: rolled back." + (f" Paused {', '.join(paused)} (remove with: homelab-maint resume NAME, or cut over again)." if paused else ""))
        return out

    def _pause_replacements(self, it: Item, rec: dict) -> list[str]:
        """A restored legacy thing and a MUTATING native replacement must never both act: PAUSE.<task> (the kill switch) for
        the tasks the item lists in pause_on_rollback. (A job needs no PAUSE: its job_mode action is undone first.)"""
        made = []
        for n in it.pause_on_rollback:
            p = self.conf_dir / f"PAUSE.{n}"
            if not p.exists():
                self._act(it, "pause", f"touch {p}", lambda p=p: p.write_text(_iso(self._now()) + "\n"), undo=True)
                made.append(n)
        rec["paused_replacements"] = made
        return made

    # ---- journal / change log / notification ---------------------------------------------------------------------------------
    def _record(self, it: Item, verb: str, detail: str, ok: bool, verified: bool, forced: bool, reason: str, significant: bool = False) -> None:
        ts = _iso(self._now())
        title = f"Legacy {verb}: {it.title}"
        text = detail + (f" (FORCED: {reason.strip()})" if forced else "")
        _append(self.state_dir / "maintenance-journal.jsonl", {"ts": ts, "title": title[:120], "detail": text[:600], "tag": "migrate", "item": it.name})
        _append(self.state_dir / "changes.jsonl", {"ts": round(self._now(), 3), "task": "migrate", "kind": "config",     # epoch, like routine.record_change
                                                   "detail": f"{it.name}: {verb}: {text}"[:200], "bytes": 0,
                                                   "outcome": "done" if ok else "failed", "verified": bool(verified)})
        ev = {"task": "migrate", "title": title, "summary": f"{it.name}: {text}"[:200], "done": [text], "significant": significant or not ok,
              "facts": {"Item": it.name, "Mode": it.mode, "Replaced by": ", ".join(it.replaced_by) or "-"}}
        try:
            (self._notifier or _default_notify)(ev)
        except Exception:  # noqa: BLE001 - telling the owner is best effort; the journal above is the record
            pass

    # ---- status / export ---------------------------------------------------------------------------------------------------
    def rows(self, live: bool = True) -> list[dict]:
        st, now, out = self.load_state(), self._now(), []
        for it in order_items(self.inv.items):
            state, at = self.effective(it, st)
            row = {"name": it.name, "title": it.title, "mode": it.mode, "kind": it.kind, "wave": it.wave, "state": state,
                   "replaced_by": ", ".join(it.replaced_by), "via": it.via, "cutover_at": at, "manual": sum(1 for a in it.actions if a.get("do") == "manual"),
                   "retirable": it.retirable, "blocked_by": [c.name for c in self.deps(it, st) if not c.ok]}
            if live and it.actions:
                try:
                    steps = self.plan_steps(it)
                    auto = [s for s in steps if s.state != "manual"]
                    row["satisfied"] = f"{sum(s.state == 'satisfied' for s in auto)}/{len(auto)}"
                    if state in ("retired", "adopted") and any(s.state == "todo" for s in auto):
                        row["drift"] = True                                   # recorded retired, but the host says otherwise
                    if state == "pending" and auto and all(s.state == "satisfied" for s in auto) and not it.pre_retired:
                        row["state"] = "partial" if not it.retirable else "retired-by-hand"
                except (Refused, OSError):
                    pass
            if state in ("retired", "adopted") and it.soak_days and at:
                anchor, verified, why = self.anchor(it, state, at)
                if verified:
                    row["soak_left_d"] = max(0.0, round((anchor - now) / 86400 + it.soak_days, 1))
                else:                                                         # the soak has not even started: the tick has not proven the job runs
                    row.update(soak_left_d=float(it.soak_days), unproven=why[:120])
            if it.job and self._paused(it.job):
                row["paused"] = True                                          # the kill switch is on: the tick will not start this job
            out.append(row)
        return out

    def pauses(self) -> list[str]:
        """Kill switches that are on right now: 'PAUSE' (everything the tick may start) and every PAUSE.<name>."""
        out = ["PAUSE"] if self._paused(None) else []
        with contextlib.suppress(OSError):
            out += sorted(p.name for p in self.conf_dir.glob("PAUSE.*"))
        return out

    def export(self) -> dict:
        """migration.json for the website (SPEC4 S12): state only, no subprocess, no secrets."""
        rows = self.rows(live=False)
        ret = [r for r in rows if r["retirable"]]
        done = [r for r in ret if r["state"] == "retired"]
        needed = {d for i in self.inv.items for d in i.depends_on}              # an adoptable keep item others wait for (notify-route, tier-check) is a step too
        nxt = next((r for r in rows if (r["retirable"] or r["name"] in needed) and r["state"] in ("pending", "rolled_back", "failed")
                    and not r["blocked_by"]), None)
        return {"generated_at": self._now(), "total": len(rows), "retirable": len(ret), "retired": len(done),
                "remaining": [r["name"] for r in ret if r["state"] != "retired"], "next": nxt["name"] if nxt else None,
                "complete": len(done) == len(ret), "paused": bool(self._paused(None)), "items": rows}

    def retired_targets(self) -> list[dict]:
        """What an installer must NOT put back: the units and files of every item recorded as retired (pure: state + inventory, no
        host reads). install.sh asks `homelab-maint migrate retired --kind unit|path` so a re-run never re-enables a retired timer
        or re-installs the retired gate drop-in."""
        st, out = self.load_state(), []
        for it in order_items(self.inv.items):
            if self.effective(it, st)[0] != "retired":
                continue
            for a in it.actions:
                if a["do"] == "disable":
                    out.append({"item": it.name, "kind": "unit", "scope": a.get("scope") or it.scope, "ref": a["unit"]})
                elif a["do"] == "move":
                    out.append({"item": it.name, "kind": "path", "scope": "system", "ref": a["src"]})
        return out

    def journal(self, n: int = 20) -> list[dict]:
        p = self.state_dir / "maintenance-journal.jsonl"
        out = []
        with contextlib.suppress(OSError):
            for ln in p.read_text().splitlines():
                with contextlib.suppress(ValueError):
                    r = json.loads(ln)
                    if isinstance(r, dict) and r.get("tag") == "migrate":
                        out.append(r)
        return out[-n:]


def export_public(now: float | None = None) -> dict | None:
    """migration.json for publish.py. Never raises: None (inventory unusable) makes the website hide the Migration card."""
    try:
        return Migrator(now=(lambda: now) if now is not None else None).export()
    except Exception:  # noqa: BLE001
        return None


def _whoami() -> str:
    with contextlib.suppress(Exception):
        return os.environ.get("SUDO_USER") or getpass.getuser()
    return "unknown"


def _append(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _default_notify(ev: dict) -> None:
    """One `maintenance` message through the umbrella's only notification path. Never under pytest (belt and braces:
    notify.py refuses real transports there too)."""
    if "pytest" in sys.modules:
        return
    from . import notify
    notify.send(notify.maintenance_event(ev["task"], ev["title"], ev["summary"], done=ev["done"], significant=ev["significant"],
                                         facts=ev["facts"]))


# =================================================================== audit: is anything scheduled that the inventory does not know?
_TOKEN_RX = re.compile(r"[A-Za-z0-9@._-]+")
CRON_DIRS = ("/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly")


def _corpus(inv: Inventory) -> set[str]:
    """Every word the inventory uses to name a unit, a script or a cron file (brace lists and paths split into single names)."""
    words: set[str] = set()
    for it in inv.items:
        for text in (it.name, it.title, it.location, it.schedule, *(str(a.get("unit", "")) for a in it.actions)):
            words.update(_TOKEN_RX.findall(text))
    return words


def audit(host: Host | None = None, inv: Inventory | None = None, users: tuple[str, ...] = ("ohmz",)) -> dict:
    """Read-only completeness check of the inventory against the live host: every timer unit (system and per-user), every ACTIVE line of the
    root and per-user crontabs, and every file in /etc/cron.*, must be accounted for by an item. Returns what is NOT. This is what keeps
    'one place schedules everything' true after the migration: a timer or cron line somebody adds later shows up here. Never raises."""
    host, inv = host or Host(), inv or load_inventory()
    known, out = _corpus(inv), {"timers": [], "cron": [], "files": [], "errors": []}
    for scope in ("system", *(f"user:{u}" for u in users)):
        r = host.run(host.ctl_argv(scope, ["list-unit-files", "--type=timer", "--no-legend", "--no-pager"]), timeout=30)
        if r.returncode != 0:
            out["errors"].append(f"cannot list the {scope} timers (rc={r.returncode})")
            continue
        for ln in (r.stdout or "").splitlines():
            unit = ln.split(None, 1)[0] if ln.strip() else ""
            if unit.endswith(".timer") and unit not in known:
                out["timers"].append({"scope": scope, "unit": unit})
    selectors: dict[str, list[tuple[str | None, str | None]]] = {}
    for it in inv.items:
        for a in it.actions:
            if a["do"] == "cron_comment":
                selectors.setdefault(a["user"], []).append((a.get("tag"), a.get("match")))
    for user in ("root", *users):
        try:
            text = host.crontab_read(user)
        except Refused as exc:
            out["errors"].append(str(exc))
            continue
        seen = {i for tag, match in selectors.get(user, []) for i in cron_find(text, tag, match)}
        for i, ln in enumerate(text.split("\n")):
            if _cron_active(ln) and not re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", ln) and i not in seen:     # NAME=value lines are not jobs
                out["cron"].append({"user": user, "line": ln.strip()[:120]})
    for d in CRON_DIRS:
        try:
            names = sorted(os.listdir(host.p(d)))
        except OSError:
            continue
        out["files"].extend(f"{d}/{n}" for n in names if not n.startswith(".") and n not in known)
    out["ok"] = not (out["timers"] or out["cron"] or out["files"] or out["errors"])
    return out


def audit_result(host: Host | None = None) -> core.Result:
    """core.Result for a weekly C0 check (the lead registers it: see the glue note). `warn` when something schedules itself outside the umbrella."""
    try:
        a = audit(host)
    except Exception as exc:  # noqa: BLE001 - an unusable inventory is itself a finding
        return core.Result("warn", f"legacy audit could not run: {type(exc).__name__}: {str(exc)[:80]}", alert=False)
    rows = [*({"kind": "timer", "what": f"{t['scope']} {t['unit']}"} for t in a["timers"]), *({"kind": "cron", "what": f"{c['user']}: {c['line']}"} for c in a["cron"]),
            *({"kind": "file", "what": f} for f in a["files"])]
    if rows:
        return core.Result("warn", f"{len(rows)} scheduled thing(s) are not in the migration inventory, e.g. {rows[0]['what']}"[:140],
                           {"unaccounted": len(rows)}, rows[:12], alert=False)
    if a["errors"]:
        return core.Result("info", ("audit incomplete: " + "; ".join(a["errors"]))[:140], {"unaccounted": 0}, alert=False)
    return core.Result("ok", "every timer, cron line and cron file on the host is in the migration inventory", {"unaccounted": 0}, alert=False)


# =================================================================== text output
def render_status(rows: list[dict]) -> str:
    out = [f"{'ITEM':<30} {'MODE':<8} {'WAVE':>4} {'STATE':<16} {'REPLACED BY':<26} NOTES"]
    for r in rows:
        note = []
        if r.get("blocked_by"):
            note.append("blocked by " + ",".join(r["blocked_by"]))
        if r.get("unproven"):
            note.append("UNPROVEN: " + r["unproven"])
        elif r.get("soak_left_d"):
            note.append(f"soak {r['soak_left_d']} d left")
        if r.get("paused"):
            note.append("PAUSED: the tick will not start it")
        if r.get("drift"):
            note.append("DRIFT: recorded retired but the host disagrees")
        if r["manual"]:
            note.append(f"{r['manual']} manual step(s)")
        if "satisfied" in r:
            note.append(f"actions {r['satisfied']}")
        out.append(f"{r['name']:<30} {r['mode']:<8} {r['wave']:>4} {r['state']:<16} {r['replaced_by'][:26]:<26} {'; '.join(note)}")
    ret = [r for r in rows if r["retirable"]]
    done = sum(r["state"] in ("retired", "retired-by-hand") for r in ret)
    out.append(f"\n{done}/{len(ret)} retirable items retired; {sum(r['state'] == 'adopted' for r in rows)} adopted "
               f"({sum(r['mode'] in ('observe', 'keep') for r in rows)} observe/keep).")
    return "\n".join(out)


def calendar(inv: Inventory) -> dict[str, int]:
    """Earliest day (day 0 = a healthy install with the notification path proven) each item can be cut over: the soak of its slowest
    task/job/probe parity check, and for every dependency that dependency's own day plus its soak_days. Pure arithmetic over the
    inventory, so the planning table in docs/MIGRATION.md can never disagree with what `cutover` will enforce."""
    day: dict[str, float] = {}
    for it in order_items(inv.items):
        if it.pre_retired:
            day[it.name] = 0.0
            continue
        need = max([float(p.get("min_hours", 0)) / 24 for p in it.parity if p["kind"] in ("task", "job", "probe", "os_job")] or [0.0])
        for d in it.depends_on:
            need = max(need, day[d] + inv.get(d).soak_days)
        day[it.name] = need
    return {k: int(-(-v // 1)) for k, v in day.items()}


def runbook(inv: Inventory) -> str:
    """The per-item cutover runbook (docs/MIGRATION.md embeds this; a test keeps the doc in sync with the inventory)."""
    L = ["Generated by `python3 -m homelab_maint.legacy runbook` from etc/legacy-retirement.toml. Do not edit by hand.\n"]
    cal = calendar(inv)
    needed = {d for i in inv.items for d in i.depends_on}              # an adopted keep item that others wait for (notify-route) is a step too
    todo = [i for i in order_items(inv.items) if (i.mode in ("adapter", "port", "retire") and not i.pre_retired) or i.name in needed]
    L.append("### Earliest calendar\n")
    L.append("Day 0 is a healthy install with `notify-test` delivered. A day is the soonest the parity window and the soak of everything the item "
             "depends on allow; the tool enforces the same numbers, and a cutover you postpone only pushes later items back. The soak of a job the tick "
             "took over counts from that job's FIRST green run, so add the wait for it (a daily job about a day, a weekly backup up to a week): "
             "the days below are a lower bound.\n")
    L.append("| Day | Item | Mode | Waits for |\n|----:|------|------|-----------|")
    for it in sorted(todo, key=lambda i: (cal[i.name], i.wave, i.order)):
        waits = ", ".join(f"`{d}`" + (f" +{inv.get(d).soak_days:g} d" if inv.get(d).soak_days else "") for d in it.depends_on) or "-"
        L.append(f"| {cal[it.name]} | `{it.name}` | {it.mode} | {waits} |")
    L.append("")
    wave_names = inv.meta.get("waves", {})
    for w in sorted({i.wave for i in inv.items}):
        its = [i for i in order_items(inv.items) if i.wave == w]
        L.append(f"### Wave {w}: {wave_names.get(str(w), '')}\n")
        for it in its:
            L.append(f"#### `{it.name}` ({it.mode}): {it.title}\n")
            L.append(f"- Legacy: {it.kind} `{it.location}`" + (f", {it.schedule}" if it.schedule else "") + (f" (user `{it.scope.split(':', 1)[1]}`)" if it.scope != "system" else ""))
            L.append(f"- Replaced by: {', '.join(f'`{n}`' for n in it.replaced_by) or 'nothing'}" + (f" ({it.via})" if it.via != "none" else ""))
            if it.pre_retired:
                L.append(f"- Already retired by hand on {it.pre_retired}; recorded for completeness, no command to run.")
            if it.depends_on:
                def _waits(d: str) -> str:
                    di = inv.get(d)
                    runs = max(1, di.verified_runs) if any(a["do"] == "job_mode" and a["mode"] == "managed" for a in di.actions) else 0
                    return (f"`{d}` is cut over" + (f", the tick has recorded {runs} green run(s) of its job" if runs else "")
                            + (f" and it has soaked {di.soak_days:g} days (counted from its first green run)" if di.soak_days and runs else
                               f" and it has soaked {di.soak_days:g} days" if di.soak_days else ""))
                L.append("- Do not do this until: " + ", ".join(_waits(d) for d in it.depends_on))
            if it.soak_days:
                later = [i.name for i in inv.items if it.name in i.depends_on]
                L.append(f"- Soak after cutover: {it.soak_days:g} days" + (f" before {', '.join(f'`{d}`' for d in later)} may follow." if later else
                         " of watching it (nothing later waits for it)."))
            idle = [f"`{u}`" for u in it.require_idle] + [f"job `{j}` under the tick" for j in dict.fromkeys([*([it.job] if it.job else []), *it.require_idle_jobs])] \
                + (["any held `/run/lock/backup-*.lock`"] if it.backup else [])
            if idle:
                L.append("- Refuses (cutover AND rollback, not overridable) while running: " + ", ".join(idle))
            if it.parity:
                L.append("- Parity (all must be green): " + "; ".join(_spec_text(p, inv) for p in it.parity))
            if it.actions:
                env = Env(Host(), it, f"{inv.legacy_root}/{it.name}", Path("/var/lib/homelab-maint"), 0.0)
                fw, bw = [], []
                for a in (ACTIONS[x["do"]](x, env) for x in it.actions):
                    f, b = a.nominal()
                    fw += [x.replace("{item}", it.name) for x in f] if a.kind != "manual" else [a.label()]
                    bw = [x.replace("{item}", it.name) for x in b] + bw
                L.append("- Cutover does, in order:\n" + "\n".join(f"  {n}. `{c}`" if not c.startswith("MANUAL") else f"  {n}. {c}" for n, c in enumerate(fw, 1)))
                if bw:
                    L.append("- Rollback does, in order (from the recorded prior state):\n" + "\n".join(f"  {n}. `{c}`" for n, c in enumerate(bw, 1)))
            else:
                L.append("- Cutover only records the adoption (no host change); the replacement keeps watching it."
                         + (f" The job `{it.keeps_job}` stays in observe mode on purpose." if it.keeps_job else ""))
            if any(a["do"] == "job_mode" and a["mode"] == "managed" for a in it.actions):
                L.append(f"- After cutover: start the first run attended, `homelab-maint job run {it.job}`. Whatever depends on this item waits for "
                         f"{max(1, it.verified_runs)} green run(s) recorded by the tick, and its soak counts from the first of them.")
            L.append(f"- Commands: `homelab-maint migrate check {it.name}`, then `homelab-maint migrate cutover {it.name}` (dry run), then `... --apply`.")
            if it.notes:
                L.append("- Notes: " + " ".join(it.notes.split()))
            L.append("")
    return "\n".join(L)


def _spec_text(p: dict, inv: Inventory) -> str:
    k = p["kind"]
    d = inv.meta["parity_defaults"]
    def runs(g: Any, h: Any) -> str:
        g, h = int(g), float(h)
        return f"{g} consecutive green run{'s' if g != 1 else ''}" + (f" over {h:g} h" if h else "")
    if k in ("task", "job"):
        return f"{k} `{p['name']}` {runs(p.get('green', d['green']), p.get('min_hours', d['min_hours']))}"
    if k == "probe":
        return f"probe(s) {', '.join('`' + n + '`' for n in _strs(p.get('names') or p.get('name')))} {runs(p.get('green', d['green']), p.get('min_hours', d['min_hours']))}"
    return {"scheduled": lambda: f"scheduler lists job `{p.get('job')}`", "scheduler_health": lambda: "the scheduler tick ran in the last 5 minutes",
            "job_attr": lambda: (f"job `{p.get('job')}` has {p.get('attr')} = {p.get('equals')!r} in jobs.toml" if "equals" in p else
                                 f"job `{p.get('job')}` {p.get('attr')} contains {p.get('contains')!r} in jobs.toml"),
            "scheduler_validate": lambda: "`scheduler validate` is clean for " + (", ".join(f"`{j}`" for j in _strs(p.get("jobs") or p.get("job"))) or "every job"),
            "unit_equiv": lambda: f"jobs.toml job `{p.get('job')}` launches exactly what `{p.get('unit')}` launches (command, user, nice, ionice, timeout, mounts, environment names)",
            "task_applies": lambda: f"`{p.get('name')}` really applies: mode = apply and `--apply` is passed by its own schedule or by `{p.get('tier_job')}`", "status_json": lambda: f"`{p.get('path')}` {p.get('result_key', 'result')} is ok (the LEGACY driver's last run: a baseline, not proof about the umbrella)",
            "file_age": lambda: f"`{p.get('path')}` newer than {float(p.get('max_age_s', 0)) / 3600:g} h",
            "config": lambda: f"config `{p.get('key')}` = {p.get('equals', 'set')!r}",
            "command": lambda: str(p["note"]) if p.get("note") else "`" + shlex.join(p.get("argv", [])) + "` exits 0",
            "notify": lambda: "a delivered test notification in the last " + f"{p.get('max_age_days', 30)} days", "os_job": lambda: f"os_jobs covers `{p.get('name')}` and is green",
            "manual": lambda: "manual: " + str(p.get("note", "")), "none": lambda: "nothing the tool can verify (" + str(p.get("note", ""))[:90] + ")"}[k]()


def update_doc(doc_path: Path, inv: Inventory) -> bool:
    """Replace the generated block of docs/MIGRATION.md; returns True when the file changed."""
    text = doc_path.read_text()
    b, e = "<!-- BEGIN GENERATED RUNBOOK -->", "<!-- END GENERATED RUNBOOK -->"
    new = text.split(b)[0] + b + "\n\n" + runbook(inv) + "\n" + e + text.split(e, 1)[1]
    if new != text:
        doc_path.write_text(new)
    return new != text


# =================================================================== CLI
def main(argv: list[str] | None = None, migrator: Migrator | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="homelab-maint migrate", description="retire legacy jobs one reversible cutover at a time")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.add_argument("--fast", action="store_true", help="no host reads")
    p = sub.add_parser("plan"); p.add_argument("item", nargs="?")
    c = sub.add_parser("check"); c.add_argument("item")
    for n in ("cutover", "rollback"):
        x = sub.add_parser(n); x.add_argument("item")
        x.add_argument("--apply", action="store_true", help="actually do it (default: print the exact commands only)")
        x.add_argument("--force", action="store_true"); x.add_argument("--reason", default="")
    j = sub.add_parser("journal"); j.add_argument("-n", type=int, default=20)
    r = sub.add_parser("runbook"); r.add_argument("--write", metavar="MIGRATION.md")
    sub.add_parser("validate")
    sub.add_parser("export")
    au = sub.add_parser("audit", help="read only: timers, cron lines and cron files on the host that no inventory item accounts for")
    au.add_argument("--json", action="store_true")
    rt = sub.add_parser("retired", help="units/files of retired items, for install.sh (one ref per line with --kind)")
    rt.add_argument("--kind", choices=("unit", "path"))
    a = ap.parse_args(argv)
    try:
        inv = migrator.inv if migrator else load_inventory()
    except InventoryError as exc:
        print("inventory error:\n  " + "\n  ".join(exc.problems), file=sys.stderr)
        return 2
    if a.cmd == "validate":
        print(f"inventory ok: {len(inv.items)} items, {sum(i.retirable for i in inv.items)} retirable")
        return 0
    if a.cmd == "runbook":
        if a.write:
            print("updated" if update_doc(Path(a.write), inv) else "already up to date")
        else:
            print(runbook(inv))
        return 0
    if a.cmd in ("cutover", "rollback") and a.apply and migrator is None and os.geteuid() != 0:
        print("--apply changes systemd units, scripts and crontabs: run it as root (sudo homelab-maint migrate ...). "
              "Without --apply it is a dry run that anyone may do.", file=sys.stderr)
        return 2
    m = migrator or Migrator(inv)
    try:
        if a.cmd == "audit":
            res = audit(m.host, inv)
            if a.json:
                print(json.dumps(res, indent=1))
            else:
                for t in res["timers"]:
                    print(f"UNACCOUNTED timer  {t['scope']:<10} {t['unit']}")
                for c in res["cron"]:
                    print(f"UNACCOUNTED cron   {c['user']:<10} {c['line']}")
                for f in res["files"]:
                    print(f"UNACCOUNTED file   {f}")
                for e in res["errors"]:
                    print(f"could not check: {e}")
                print("audit: everything on the host is in the inventory" if res["ok"] else "audit: add an [[item]] for each line above (or retire the thing)")
            return 0 if res["ok"] else 1
        if a.cmd == "retired":
            for r in m.retired_targets():
                if a.kind is None:
                    print(f"{r['kind']}\t{r['scope']}\t{r['ref']}\t{r['item']}")
                elif r["kind"] == a.kind and r["scope"] == "system":
                    print(r["ref"])
            return 0
        if a.cmd == "status":
            rows = m.rows(live=not a.fast)
            if a.json:
                print(json.dumps(rows, indent=1, default=str))
            else:
                if m.pauses():                                                  # a kill switch silently stops every job the tick was given
                    print(f"!! {', '.join(m.pauses())} present: PAUSE stops every managed job the tick would start, backups included unless the "
                          f"job has pausable = false. Remove with: homelab-maint resume [NAME]\n")
                print(render_status(rows))
            return 0
        if a.cmd == "export":
            print(json.dumps(m.export(), indent=1, default=str))
            return 0
        if a.cmd == "journal":
            for e in m.journal(a.n):
                print(f"{e['ts']}  {e['title']}: {e['detail']}")
            return 0
        if a.cmd == "plan":
            names = [a.item] if a.item else [i.name for i in order_items(inv.items) if i.retirable]
            for n in names:
                it = m._item(n)
                print(f"== {n} (wave {it.wave}, {it.mode}) {m.effective(it)[0]}")
                for st in m.plan_steps(it):
                    print(f"  {st.state:<9} {st.label}" + (f"  ({st.note})" if st.note else ""))
                    for op in st.ops:
                        print(f"      $ {op.text}")
            return 0
        out = {"check": lambda: m.check(a.item), "cutover": lambda: m.cutover(a.item, a.apply, a.force, a.reason),
               "rollback": lambda: m.rollback(a.item, a.apply, a.force, a.reason)}[a.cmd]()
    except KeyError as exc:
        print(f"unknown item {exc}; see: homelab-maint migrate status", file=sys.stderr)
        return 2
    print("\n".join(out.lines))
    return out.rc


if __name__ == "__main__":
    sys.exit(main())
