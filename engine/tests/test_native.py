"""Tests for tasks/native.py: the ports of the small legacy scripts and the os_jobs verifier.

Nothing here touches the host. `native.sh` is replaced (an unmocked command returns rc 127), `core.sh` is stubbed so
`logger` never reaches the journal, `native._notify` records instead of sending, and every path lives under tmp_path.

Parity is proven three ways (see the PARITY.md sections in the module docstring):
  1. FROZEN TABLES: (inputs -> outputs) literals extracted from each legacy script by reading it end to end;
  2. SANDBOXED LEGACY: the real script text is copied into tmp_path, every absolute path rewritten into the sandbox,
     fake `docker`/`curl`/`nvidia-smi`/`runuser`/`logger`/`du`/`df` placed first on PATH (a guard refuses to run a copy
     that still names a host path), and its decisions are compared with the port's on identical inputs;
  3. MEASUREMENT: the port's size/free-space arithmetic is compared with the real `find`/`du`/`df` on tmp trees.
The sandbox tests skip (the frozen tables still run) when a legacy script has already been retired from this host.

Alert-path tests come in two kinds. The task-level ones read a task's own decisions off a recording `_notify`. The "real stack"
ones (class Wire) run the task, core's Notifier (the notify-backed HermesNotifier) and the real notify.send (routing, dedupe window,
budgets) end to end with ONLY the last hop faked, and assert what would really reach the owner. The smartd hook tests also run the
hook in subprocesses against a SCRATCH copy of the package with sibling modules deliberately broken; those subprocesses are
pinned to the scratch tree (cwd, PYTHONPATH, HOMELAB_MAINT_* and PYTEST_CURRENT_TEST, see hook_env): a subprocess that imports
the live package would reach the real notification transport.
"""
import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)

import dataclasses
import glob
import itertools
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from homelab_maint import core
from homelab_maint import smart_hook as sm
from homelab_maint.core import GIB
from homelab_maint.tasks import checks_health, gates
from homelab_maint.tasks import native as nv

MIB = 1024 ** 2
DAY = 86400
NOW = 1_800_000_000.0
HOST = socket.gethostname()
REAL_NOTIFY = nv._notify                    # the autouse fixture replaces nv._notify per test
REAL_SM_NOTIFY = sm._notify                 # ... and sm._notify (the smartd hook's own bridge)
PROTECTED = {"patterns": ["comfyui", "immich", "surreal", "postgres", "plexmediaserver", "tunarr", "/mnt/backup"]}
OK_SENT = {"ok": True, "handled": True, "rc": 0, "note": ""}


# --------------------------------------------------------------------------- harness
class FakeSh:
    """`sh` stand-in. rows: (prefix, response); response = (rc, stdout, stderr) or a callable(cmd_str) -> that."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.calls: list[str] = []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        for prefix, resp in self.rows:
            if key.startswith(prefix):
                rc, out, err = resp(key) if callable(resp) else resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 127, "", "unmocked: " + key)

    def with_prefix(self, prefix: str) -> list[str]:
        return [c for c in self.calls if c.startswith(prefix)]


def ok(out=""):
    return (0, out, "")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Every global that points at the host is redirected; yields the list of events `_notify` was asked to send."""
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "conf").mkdir()
    monkeypatch.setattr(core, "STATE_DIR", state)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    monkeypatch.setattr(gates, "STATE_DIR", state)
    monkeypatch.setattr(gates, "CGROUP", tmp_path / "cg")
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))   # audit -> logger
    monkeypatch.setattr(nv, "sh", FakeSh())
    sent: list[dict] = []
    monkeypatch.setattr(nv, "_notify", lambda ev: (sent.append(ev), dict(OK_SENT))[1])
    monkeypatch.setattr(sm, "_notify", lambda ev: (sent.append(ev), dict(OK_SENT))[1])         # the smartd hook has its own bridge
    yield sent


def use_sh(monkeypatch, *rows) -> FakeSh:
    f = FakeSh(*rows)
    monkeypatch.setattr(nv, "sh", f)
    return f


def mk(name, *, apply=False, now=NOW, protected=None, cfg_tasks=None, **opts):
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED if protected is None else protected,
           "tasks": {name: {"mode": "apply" if apply else "report", **opts}, **(cfg_tasks or {})}}
    return core.Ctx(cfg, name, apply, now)


def step(fn, name, now, *, apply=False, **kw):
    """One scheduled run: fresh Ctx (state re-read from disk), run, persist state like core.run_task does."""
    ctx = mk(name, apply=apply, now=now, **kw)
    res = fn(ctx)
    ctx.save_state()
    return res, ctx


def audit_rows(tmp_path) -> list[dict]:
    p = tmp_path / "log" / "audit.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def outcomes(tmp_path) -> list[str]:
    return [r["outcome"] for r in audit_rows(tmp_path)]


def check_result(res):
    """The runner contract every Result must meet (SPEC.md rule 7 + metrics are shipped to the browser)."""
    assert len(res.summary) <= 140 and res.summary.isascii() and "\n" not in res.summary, res.summary
    assert len(res.items) <= 12
    json.dumps(res.metrics)
    json.dumps(res.items)
    return res


def cid(n: int) -> str:
    return f"{n:064x}"


class Wire:
    """The REAL notification stack with only the last hop faked: core.Notifier -> notify.send (routing, quiet hours, dedupe window,
    budgets incl. the crit bypass, escalation, state files under tmp) -> a transport that records what would reach the owner's
    phone and inbox. A task's own pages (`nv._notify` -> notify.send) take the same road. `up = False` makes the transport fail."""

    def __init__(self, monkeypatch):
        from homelab_maint import notify
        self.notify, self.now, self.up, self.msgs, self._ev = notify, NOW, True, [], None
        real_send, real_flush = notify.send, notify.flush_pending

        def send(ev, cfg=None, now=None, **kw):
            self._ev = ev
            return real_send(ev, cfg, self.now, transport=self.transport)

        monkeypatch.setattr(notify, "send", send)
        monkeypatch.setattr(notify, "flush_pending", lambda cfg=None, now=None, **kw: real_flush(cfg, self.now, transport=self.transport))
        monkeypatch.setattr(nv, "_notify", REAL_NOTIFY)

    def transport(self, msg, cfg):
        if not self.up:
            return self.notify.TransportResult(ok=False, fatal="smtp down")
        ev = self._ev
        self.msgs.append({"kind": ev.kind, "key": ev.dedupe_key, "severity": ev.severity, "subject": msg.subject, "sms": msg.sms,
                          "plain": msg.plain, "channels": list(msg.channels)})
        return self.notify.TransportResult(ok=True, legs={c: "sent" for c in msg.channels})


def hermes_pages(wire, res, now, confirm=2, name="surrealdb_health", title="SurrealDB (Open Notebook)"):
    """cmd_run's Notifier step with the notify-backed HermesNotifier (the SPEC4 glue) over a Wire."""
    n = wire.notify.HermesNotifier({"global": {"alert_confirm_runs": confirm, "alert_reminder_hours": 24, "alert_daily_budget": 8}})
    n.evaluate(name, title, res, now)
    n.save()


# --------------------------------------------------------------------------- sandboxed legacy scripts
# RETIRED COPIES WIN. A script the port replaced is moved under lib/.../legacy/ and the file left
# at the same name in /usr/local/sbin is a FORWARDING STUB -- it calls the umbrella and only falls
# back to the legacy copy. Searching the sbin dirs first therefore read the stub and reported the
# real script as "changed: LOGFILE=... not found", which is what made the smart_event parity checks
# fail. The port implements the retired script, so that is the one to read.
LEGACY_DIRS = [*sorted(glob.glob("/usr/local/lib/homelab-maint/legacy/*")),
               "/usr/local/sbin", "/usr/local/bin"]
_HOST_PATH = re.compile(r"(?<![\w$./-])/(?:var|run|etc|usr/local|home|volume1|sys|proc|media|mnt|root|opt|srv)/")
_DANGEROUS = ("docker", "systemctl", "runuser", "curl", "nvidia-smi", "logger", "sudo", "kill", "snap", "apt-get")


def legacy(name: str) -> Path:
    for d in LEGACY_DIRS:
        p = Path(d) / name
        if p.is_file():
            return p
    pytest.skip(f"legacy script {name} is not on this host any more (retired?); the frozen tables still run")


def prep_script(src: Path, subs, dest: Path) -> Path:
    """Copy of a legacy script with its absolute paths rewritten into the sandbox. Fails loudly when a substitution no
    longer matches (the script changed: re-read it) or when the copy still names a host path outside comments."""
    text = src.read_text()
    for old, new in subs:
        assert old in text, f"{src.name} changed: {old!r} not found, re-read the script and update the port + tests"
        text = text.replace(old, new)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    leaks = _HOST_PATH.findall(code)
    assert not leaks, f"sandbox copy of {src.name} still names host paths: {leaks}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    dest.chmod(0o755)
    return dest


def write_bin(root: Path, name: str, body: str) -> Path:
    p = root / "bin" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    return p


def bash(script: Path, root: Path, env=None, args=()):
    """Run a sandboxed script with a minimal environment; every command that could touch the host defaults to a stub
    that exits 97 unless the test provided a fake for it."""
    for n in _DANGEROUS:
        if not (root / "bin" / n).exists():
            write_bin(root, n, "echo \"sandbox: unexpected $0 $*\" >&2\nexit 97\n")
    (root / "fake").mkdir(exist_ok=True)
    (root / "home").mkdir(exist_ok=True)
    e = {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "HOME": str(root / "home"), "LC_ALL": "C", "TZ": "UTC",
         "FAKE_DIR": str(root / "fake"), **(env or {})}
    return subprocess.run(["/bin/bash", str(script), *args], env=e, capture_output=True, text=True, timeout=60,
                          cwd=root, stdin=subprocess.DEVNULL)


FAKE_RUNUSER = 'shift 3\nexec "$@"\n'                       # runuser -u ohmz -- CMD ARGS...
FAKE_LOGGER = "echo \"$@\" >> \"$FAKE_DIR/logger.log\"\nexit 0\n"
FAKE_BRIDGE = ('#!/bin/bash\nprintf \'%s\\x1e%s\\x1e%s\\x1f\' "$1" "$2" "$3" >> "$FAKE_DIR/bridge.rec"\n'
               'if [ -n "${BRIDGE_ERR:-}" ]; then printf "%b" "$BRIDGE_ERR" >&2; fi\nexit ${BRIDGE_RC:-0}\n')


def bridge_calls(root: Path) -> list[list[str]]:
    p = root / "fake" / "bridge.rec"
    return [r.split("\x1e") for r in p.read_text().split("\x1f")[:-1]] if p.exists() else []


def mask_ts(line: str) -> str:
    return re.sub(r"^\S+ ", "TS ", line)


# =========================================================================== registry and shared helpers
def test_tasks_registered_with_spec_classes():
    r = core.REGISTRY
    want = {"surrealdb_health": ("C0", "check"), "comfyui_idle_reclaim": ("C1", "check"),
            "immich_recycle": ("C1", "check"), "openwebui_media_prune": ("C1", "daily"),
            "docker_containers_prune": ("C1", "weekly"), "docker_prune_parity": ("C0", "weekly"),
            "docker_prune_exposure": ("C0", "check"), "os_jobs": ("C0", "check")}
    for name, (klass, tier) in want.items():
        assert (r[name].klass, r[name].tier) == (klass, tier), name
        assert r[name].timeout >= 60, name
    assert "mem_guard" not in r and "smart_event" not in r        # mem-guard is retired; smart_event is a hook, not a task


def test_ports_table_names_real_replacements():
    tasks = set(core.REGISTRY)
    for row in nv.PORTS:
        assert {"legacy", "location", "replaced_by", "mode", "check"} <= set(row) and row["mode"] in ("port", "retire", "observe")
        if row["mode"] == "port" and row["replaced_by"] != "smart-event":
            assert all(n in tasks for n in row["replaced_by"].split("+")), row
    assert callable(nv.smart_event_main) and nv.smart_event is sm.smart_event       # re-exports of the stdlib-only hook module
    assert {r["legacy"] for r in nv.PORTS} >= {"notebook-db-alert.timer", "docker-prune.timer"}


def test_ascii_and_scrub():
    assert nv._ascii("café → ok\nnext", 140) == "caf? ? ok next"
    assert len(nv._ascii("x" * 500, 140)) == 140
    s = nv._scrub("failed: password=hunter2 token: abc to me@example.com call +1 (555) 123-4567 now")
    assert "hunter2" not in s and "abc" not in s and "me@example.com" not in s and "555" not in s
    assert "<redacted>" in s and "<email>" in s and "<number>" in s
    assert nv._scrub("rc=3 for /dev/sda: 12345") == "rc=3 for /dev/sda: 12345"        # short numbers survive


@pytest.mark.parametrize("v,lo,hi,want", [(5, 0, 10, 5), (5.5, None, None, 5.5), (-1, 0, None, None), (11, 0, 10, None),
                                          (True, None, None, None), ("5", None, None, None), (float("nan"), None, None, None),
                                          (float("inf"), None, None, None), (None, None, None, None)])
def test_num_validation(v, lo, hi, want):
    assert nv._num(v, lo, hi) == want


def test_opts_bad_values_fall_back_and_are_remembered():
    ctx = mk("surrealdb_health", wal_max_mb="lots", container="a b; rm -rf /", data_dir="relative/path", restart_gt=-3)
    o = nv._Opts(ctx)
    assert o.num("wal_max_mb", 1024, 0) == 1024 and o.name("container", "x") == "x"
    assert o.path("data_dir", "/d") == "/d" and o.num("restart_gt", 3, 0) == 3
    assert o.bad == ["wal_max_mb", "container", "data_dir", "restart_gt"]


# --------------------------------------------------------------------------- _notify: the only outbound path
def test_notify_without_the_notify_module_audits_a_failed_send(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 2 and fromlist and "notify" in fromlist:
            raise ImportError("no notify")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = REAL_NOTIFY({"kind": "alert", "title": "t", "summary": "s"})
    assert r == {"ok": False, "handled": False, "rc": 127, "note": "notify module unavailable"}
    rows = audit_rows(tmp_path)
    assert rows and rows[-1]["task"] == "notify" and rows[-1]["action"] == "send"
    assert rows[-1]["outcome"].startswith("failed rc=127")      # what checks_health.alert_path_health reads


def test_notify_bridges_to_notify_send_with_only_known_event_fields(monkeypatch):
    from homelab_maint import notify
    got = {}

    def fake_send(ev, *a, **k):
        got["ev"] = ev
        return notify.Delivery(kind="alert", ok=True, handled=True, note="sent")

    monkeypatch.setattr(notify, "send", fake_send)
    r = REAL_NOTIFY({"kind": "alert", "severity": "crit", "title": "T", "summary": "S", "bogus": 1, "task": "x"})
    assert r == {"ok": True, "handled": True, "rc": 0, "note": "sent"}
    assert isinstance(got["ev"], notify.Event) and got["ev"].title == "T" and not hasattr(got["ev"], "bogus")


def test_notify_policy_suppression_is_handled_not_failed(monkeypatch):
    from homelab_maint import notify
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: notify.Delivery(ok=False, handled=True, skipped="dedupe"))
    r = REAL_NOTIFY({"kind": "alert", "title": "T"})
    assert (r["ok"], r["handled"], r["note"]) == (False, True, "dedupe")


def test_notify_exception_is_a_failed_send_never_a_raise(monkeypatch):
    from homelab_maint import notify

    def boom(*a, **k):
        raise RuntimeError("smtp password=hunter2 exploded")

    monkeypatch.setattr(notify, "send", boom)
    r = REAL_NOTIFY({"kind": "alert", "title": "T"})
    assert r["ok"] is False and r["handled"] is False and "hunter2" not in r["note"]


def test_notify_accepts_plain_dict_and_bool_deliveries(monkeypatch):
    from homelab_maint import notify
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: {"ok": True, "note": "n"})
    assert REAL_NOTIFY({"kind": "alert", "title": "T"})["ok"] is True
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: True)
    assert REAL_NOTIFY({"kind": "alert", "title": "T"})["ok"] is True
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: None)
    assert REAL_NOTIFY({"kind": "alert", "title": "T"})["ok"] is False


# =========================================================================== surrealdb_health  <-  notebook-db-alert.sh
@dataclasses.dataclass
class Scn:
    """What the world looks like for one run of the legacy script (fake docker/du/df, real find over a tmp tree)."""
    wal: int = 5 * MIB            # bytes in the *.log files of the RocksDB directory (sparse)
    du: int | None = 2            # what `du -s --block-size=1G` prints (None: du fails)
    df: int | None = 900          # what `df --block-size=1G --output=avail` prints (None: df fails)
    oom: int = 0
    restarts: int = 0
    state: str = "running"
    docker: bool = True


SURREAL_CID = "ab" * 32
SURREAL_PREVS = [None, "", "OK", "wal,", "disk,", "wal,db,", "wal,db,disk,oom,restart,"]
SURREAL_SCENARIOS = {
    "clean": Scn(),
    "wal_exactly_limit": Scn(wal=1024 * MIB),
    "wal_one_byte_over_floors_to_limit": Scn(wal=1024 * MIB + 1),
    "wal_one_mb_over": Scn(wal=1025 * MIB),
    "wal_just_under_floor": Scn(wal=1025 * MIB - 1),
    "db_at_limit": Scn(du=25),
    "db_over": Scn(du=26),
    "du_fails": Scn(du=None),
    "disk_at_limit": Scn(df=40),
    "disk_under": Scn(df=39),
    "disk_zero": Scn(df=0),
    "df_fails": Scn(df=None),
    "oom": Scn(oom=1),
    "restarts_at_limit": Scn(restarts=3),
    "restarts_over": Scn(restarts=4),
    "docker_absent": Scn(docker=False),
    "container_stopped": Scn(state="exited"),
    "everything_bad": Scn(wal=2000 * MIB, du=90, df=3, oom=2, restarts=9, state="restarting"),
    "wal_and_disk": Scn(wal=1500 * MIB, df=10),
}
# Frozen decision table, read off the shell: (wal_mb, db_gb, disk_gb, oom, restarts) -> failing checks, in order.
SURREAL_TABLE = [
    ((0, 0, 900, 0, 0), ""),
    ((1024, 25, 40, 0, 3), ""),
    ((1025, 0, 900, 0, 0), "wal,"),
    ((0, 26, 900, 0, 0), "db,"),
    ((0, 0, 39, 0, 0), "disk,"),
    ((0, 0, 0, 0, 0), "disk,"),
    ((0, 0, 900, 1, 0), "oom,"),
    ((0, 0, 900, 0, 4), "restart,"),
    ((2000, 90, 3, 2, 9), "wal,db,disk,oom,restart,"),
    ((1500, 0, 10, 0, 0), "wal,disk,"),
]


def surreal_sandbox(root: Path, scn: Scn, prev: str | None, *, fix=False, bridge_rc=0) -> dict:
    """Run a sandbox copy of notebook-db-alert.sh once. Returns what it measured, logged, stored and sent."""
    src = legacy("notebook-db-alert.sh")
    rocks = root / "compose" / "surreal_data" / "mydatabase.db"
    (rocks / "sub").mkdir(parents=True)
    with open(rocks / "000001.log", "wb") as f:
        f.truncate(scn.wal)
    with open(rocks / "000009.sst", "wb") as f:                 # not a WAL: must never count
        f.truncate(3 * GIB)
    with open(rocks / "sub" / "old.log", "wb") as f:            # depth 2: find -maxdepth 1 ignores it
        f.truncate(5 * GIB)
    (rocks / "LOG").write_text("x")
    fake = root / "fake"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "cid").write_text(SURREAL_CID)
    (fake / "restarts").write_text(str(scn.restarts))
    (fake / "state").write_text(scn.state)
    if not scn.docker:
        (fake / "nodocker").write_text("")
    if scn.du is None:
        (fake / "nodu").write_text("")
    else:
        (fake / "du").write_text(str(scn.du))
    if scn.df is None:
        (fake / "nodf").write_text("")
    else:
        (fake / "df").write_text(str(scn.df))
    ev = root / "cg" / "system.slice" / f"docker-{SURREAL_CID}.scope" / "memory.events"
    ev.parent.mkdir(parents=True)
    ev.write_text(f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {scn.oom}\n")
    (root / "state").mkdir()
    if prev is not None:
        (root / "state" / "state").write_text(prev)
    write_bin(root, "docker", '[ -f "$FAKE_DIR/nodocker" ] && exit 1\ncase "$3" in\n'
              "  '{{.Id}}') cat \"$FAKE_DIR/cid\" ;;\n  '{{.RestartCount}}') cat \"$FAKE_DIR/restarts\" ;;\n"
              "  '{{.State.Status}}') cat \"$FAKE_DIR/state\" ;;\n  *) exit 98 ;;\nesac\n")
    write_bin(root, "du", '[ -f "$FAKE_DIR/nodu" ] && exit 1\nprintf \'%s\\t%s\\n\' "$(cat "$FAKE_DIR/du")" "$3"\n')
    write_bin(root, "df", '[ -f "$FAKE_DIR/nodf" ] && exit 1\nprintf \'Avail\\n  %s\\n\' "$(cat "$FAKE_DIR/df")"\n')
    write_bin(root, "runuser", FAKE_RUNUSER)
    write_bin(root, "logger", FAKE_LOGGER)
    bridge = root / "bridge"
    bridge.write_text(FAKE_BRIDGE)
    bridge.chmod(0o755)
    subs = [("COMPOSE_DIR=/home/ohmz/StudioProjects/open-notebook", f"COMPOSE_DIR={root}/compose"),
            ("LOGFILE=/var/log/notebook-db-alert.log", f"LOGFILE={root}/alert.log"),
            ("STATEFILE=/var/lib/notebook-db-alert/state", f"STATEFILE={root}/state/state"),
            ("BRIDGE=/usr/local/sbin/backup-notify-hermes.py", f"BRIDGE={bridge}"),
            ('events="/sys/fs/cgroup/system.slice/docker-$cid.scope/memory.events"', f'events="{ev}"')]
    if fix:        # the one-line recovery fix: the signature never contains "|BAD", so test for "was not OK" instead
        subs.append(('[[ "$prev" == *"|BAD" ]]', '[[ "$prev" != "OK" ]]'))
    script = prep_script(src, subs, root / "script.sh")
    r = bash(script, root, env={"BRIDGE_RC": str(bridge_rc)})
    assert r.returncode == 0, r.stderr
    log = (root / "alert.log").read_text().splitlines()
    hb = next(ln for ln in log if " status=" in ln)
    m = re.search(r"status=(\S+) wal=(\d+)MB db=(\d*)GB disk_free=(\d+)GB oom_kill=(\d+) restarts=(\d+) (\d+) problem", hb)
    assert m, hb
    alert = next((ln.split(" ALERT: ", 1)[1] for ln in log if " ALERT: " in ln), None)
    return {"reading": nv.Reading(int(m[2]), int(m[3]) if m[3] else None, int(m[4]), int(m[5]), int(m[6]), m[1]),
            "n_problems": int(m[7]), "alert": alert, "state": (root / "state" / "state").read_text(),
            "calls": bridge_calls(root), "log": log, "root": root, "rocks": rocks}


@pytest.mark.parametrize("rows", SURREAL_TABLE, ids=[r[1] or "clean" for r in SURREAL_TABLE])
def test_surreal_frozen_decision_table(rows):
    (wal, db, disk, oom, restarts), failing = rows
    rd = nv.Reading(wal, db, disk, oom, restarts)
    problems = nv.surreal_problems(rd, nv.Limits())
    assert "".join(k + "," for k, _ in problems) == failing
    assert nv.surreal_signature(problems) == (failing or "OK")


def test_surreal_problem_texts_are_the_scripts_wording():
    rd = nv.Reading(2000, 90, 3, 2, 9)
    assert [t for _, t in nv.surreal_problems(rd, nv.Limits())] == [
        "WAL is 2000 MB (limit 1024 MB)", "store is 90 GB (limit 25 GB)",
        "only 3 GB free on the store's filesystem (want >= 40 GB)",
        "container hit its memory cap 2x (cgroup oom_kill)", "container has restarted 9x"]


@pytest.mark.parametrize("prev", SURREAL_PREVS, ids=lambda p: f"prev={p!r}")
@pytest.mark.parametrize("name", list(SURREAL_SCENARIOS))
def test_surreal_port_matches_sandboxed_script(tmp_path, name, prev):
    out = surreal_sandbox(tmp_path / "run", SURREAL_SCENARIOS[name], prev)
    rd = out["reading"]
    problems = nv.surreal_problems(rd, nv.Limits())
    assert len(problems) == out["n_problems"]
    assert out["state"] == nv.surreal_signature(problems)                       # signature stored on every run
    trans = nv.surreal_transition(prev, problems)
    sent = out["calls"]
    if trans != "alert":
        assert out["alert"] is None                                             # the ALERT line comes after the suppression test
    if trans == "alert":                                                        # legacy sends exactly when the set changed
        assert out["alert"] == " ".join(t for _, t in problems)                 # byte-exact problem texts
        assert len(sent) == 1
        handle, subject, body = sent[0]
        ev = nv._surreal_event(rd, nv.Limits(), problems, nv.surreal_signature(problems),
                               str(out["rocks"]), "open-notebook-surrealdb-1")
        assert (handle, subject) == ("ohmz", ev["title"]) and subject == f"Open Notebook DB pressure on {HOST}"
        assert ev["details"].replace(str(out["rocks"]), "ROCKS") == body.replace(str(out["rocks"]), "ROCKS")
    else:
        assert sent == []                                                       # suppressed, or nothing to say
    assert trans in ("alert", "suppressed", "none", "recovery")
    if trans == "suppressed":
        assert any("suppressed" in ln for ln in out["log"])


@pytest.mark.parametrize("prev", SURREAL_PREVS, ids=lambda p: f"prev={p!r}")
def test_legacy_resolved_never_fires_but_port_recovers(tmp_path, prev):
    """The bug the port fixes by design: the script tested for a `|BAD` suffix it never writes."""
    legacy_run = surreal_sandbox(tmp_path / "legacy", SURREAL_SCENARIOS["clean"], prev)
    assert legacy_run["calls"] == [], "the unpatched script must never send RESOLVED"
    fixed = surreal_sandbox(tmp_path / "fixed", SURREAL_SCENARIOS["clean"], prev, fix=True)
    problems = nv.surreal_problems(fixed["reading"], nv.Limits())
    assert problems == []
    trans = nv.surreal_transition(prev, problems)
    if trans == "recovery":                      # the one-line fix and the port agree on WHEN a recovery is due
        assert [c[1] for c in fixed["calls"]] == [f"RESOLVED on {HOST}: Open Notebook DB pressure"]
        assert fixed["calls"][0][2].startswith("All checks are back within limits.")
    else:
        assert fixed["calls"] == []
    assert trans == ("recovery" if prev not in (None, "", "OK") else "none")


@pytest.mark.parametrize("prev,problems,want", [
    (None, [], "none"), ("", [], "none"), ("OK", [], "none"), ("wal,", [], "recovery"),
    ("wal,db,disk,oom,restart,", [], "recovery"),
    (None, [("wal", "w")], "alert"), ("", [("wal", "w")], "alert"), ("OK", [("wal", "w")], "alert"),
    ("wal,", [("wal", "w")], "suppressed"), ("wal,", [("wal", "w"), ("db", "d")], "alert"),
    ("wal,db,", [("wal", "w")], "alert"),      # legacy re-alerts when the set SHRINKS too
    ("db,", [("wal", "w")], "alert"),
])
def test_surreal_transition_table(prev, problems, want):
    assert nv.surreal_transition(prev, problems) == want


def test_surreal_readings_unreadable_store_is_not_a_problem_but_unreadable_df_is():
    lim = nv.Limits()
    assert nv.surreal_problems(nv.Reading(0, None, 900, 0, 0), lim) == []
    assert [k for k, _ in nv.surreal_problems(nv.Reading(0, 2, 0, 0, 0), lim)] == ["disk"]


# --------------------------------------------------------------------------- measurement parity (real find / du / df)
def _mk_tree(root: Path) -> Path:
    root.mkdir(parents=True)
    for i, n in enumerate((0, 1, 4095, 4096, 4097, 123456, 5 * MIB + 17)):
        (root / f"f{i}.bin").write_bytes(b"\1" * n)
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "sub" / "deep" / "x.bin").write_bytes(b"x" * 10000)
    (root / "empty").mkdir()
    os.link(root / "f5.bin", root / "sub" / "hardlink.bin")           # du counts a hard link once
    os.symlink("/nonexistent/target", root / "dangling")              # a symlink is its own tiny object
    os.symlink(root / "sub", root / "linkdir")                        # never followed
    with open(root / "sparse.bin", "wb") as f:
        f.truncate(300 * MIB)                                         # allocated 0, apparent 300 MiB
    return root


def test_du_bytes_equals_real_du(tmp_path):
    tree = _mk_tree(tmp_path / "tree")
    real = int(subprocess.run(["du", "-s", "-B1", str(tree)], capture_output=True, text=True, check=True).stdout.split()[0])
    assert nv._du_bytes(str(tree)) == real
    one_g = int(subprocess.run(["du", "-s", "--block-size=1G", str(tree)], capture_output=True, text=True, check=True).stdout.split()[0])
    assert nv._db_gb(str(tree)) == one_g == 1                         # du rounds UP: a few MiB is "1", not "0"
    empty = tmp_path / "e"
    empty.mkdir()
    assert nv._db_gb(str(empty)) == int(subprocess.run(["du", "-s", "--block-size=1G", str(empty)], capture_output=True,
                                                       text=True, check=True).stdout.split()[0]) == 1


def test_du_bytes_unreadable_or_cut_off_is_none_not_zero(tmp_path, monkeypatch):
    assert nv._du_bytes(str(tmp_path / "missing")) is None
    tree = _mk_tree(tmp_path / "t")
    clock = iter(itertools.count(0, 100))
    monkeypatch.setattr(nv.time, "monotonic", lambda: next(clock))      # every step "takes" 100 s
    assert nv._du_bytes(str(tree), budget_s=60) is None


def test_wal_mb_equals_find_awk(tmp_path):
    rocks = _mk_tree(tmp_path / "rocks")
    for name, size in (("000001.log", 7 * MIB + 5), (".hidden.log", 3 * MIB), ("LOG.old.log", 1), ("x.log.1", 9 * MIB),
                       ("NOTLOG", 9 * MIB)):
        with open(rocks / name, "wb") as f:
            f.truncate(size)
    (rocks / "dir.log").mkdir()
    os.symlink("/some/where/else.log", rocks / "link.log")
    (rocks / "sub" / "deep.log").write_bytes(b"z" * (4 * MIB))          # depth 2: not counted
    cmd = ("find \"$1\" -maxdepth 1 -name '*.log' -printf '%s\\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'")
    legacy_bytes = int(subprocess.run(["bash", "-c", cmd, "_", str(rocks)], capture_output=True, text=True).stdout)
    assert nv._wal_mb(str(rocks)) == legacy_bytes // 1048576
    assert nv._wal_mb(str(tmp_path / "missing")) == 0


@pytest.mark.parametrize("path", ["tmp", "/"])
def test_disk_free_gb_equals_real_df(tmp_path, path):
    p = str(tmp_path) if path == "tmp" else path
    for _ in range(5):                                # two reads microseconds apart can straddle a GiB boundary
        legacy_gb = int(re.sub(r"\D", "", subprocess.run(["df", "--block-size=1G", "--output=avail", p], capture_output=True,
                                                         text=True).stdout.splitlines()[-1]))
        if nv._disk_free_gb(p) == legacy_gb:
            return
    pytest.fail("port and df disagree about free space")


def test_disk_free_gb_failure_is_zero_like_the_script(tmp_path):
    assert nv._disk_free_gb(str(tmp_path / "missing")) == 0


# --------------------------------------------------------------------------- the task itself
def surreal_world(tmp_path, monkeypatch, *, wal=5 * MIB, db=2, disk=900, oom=0, restarts=0, state="running", docker=True):
    """Fake docker + cgroup + tree for the task. Returns the FakeSh and the option dict pointing at the tree."""
    data = tmp_path / "data"
    rocks = data / "mydatabase.db"
    rocks.mkdir(parents=True, exist_ok=True)
    with open(rocks / "000001.log", "wb") as f:
        f.truncate(wal)
    cg = gates.CGROUP / "system.slice" / f"docker-{cid(7)}.scope"
    cg.mkdir(parents=True, exist_ok=True)
    (cg / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom}\n")
    monkeypatch.setattr(nv, "_db_gb", lambda p: db)
    monkeypatch.setattr(nv, "_disk_free_gb", lambda p: disk)
    f = use_sh(monkeypatch, ("docker container inspect",
                             (0, f"{cid(7)}|{state}|{restarts}\n", "") if docker else (1, "", "Error: No such container")))
    return f, {"data_dir": str(data), "container": "open-notebook-surrealdb-1"}


def test_surrealdb_health_ok_run(tmp_path, monkeypatch, sandbox):
    f, opts = surreal_world(tmp_path, monkeypatch)
    res, ctx = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    check_result(res)
    assert res.status == "ok" and "WAL 5 MB" in res.summary and "2 GB" in res.summary
    assert res.metrics["signature"] == "OK" and res.metrics["transition"] == "none" and res.metrics["docker_ok"] is True
    assert [i["state"] for i in res.items] == ["ok"] * 5 and ctx.state["signature"] == "OK"
    assert sandbox == []
    # NEVER ask docker for Config.Cmd/Env: the container's command line carries the database password
    assert all("Config" not in c and "{{json" not in c for c in f.calls) and len(f.calls) == 1


def test_surrealdb_health_never_mutates_anything(tmp_path, monkeypatch):
    f, opts = surreal_world(tmp_path, monkeypatch, wal=2000 * MIB, restarts=9, oom=3)
    step(nv.surrealdb_health, "surrealdb_health", NOW, apply=True, **opts)
    assert all(c.startswith("docker container inspect") for c in f.calls)
    assert not (tmp_path / "log" / "audit.jsonl").exists()               # C0: nothing went through ctx.act


# --------------------------------------------------------------------------- data dir resolution (a hardcoded path went stale)
def test_surreal_data_dir_config_wins_without_touching_the_mount(tmp_path, monkeypatch):
    f = use_sh(monkeypatch)
    ctx = mk("surrealdb_health", data_dir=str(tmp_path))
    assert nv.surreal_data_dir(nv._Opts(ctx), "open-notebook-surrealdb-1") == str(tmp_path)
    assert f.calls == []                                                  # an explicit value means no extra inspect


def test_surreal_data_dir_uses_the_containers_bind_mount(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    f = use_sh(monkeypatch, ("docker container inspect --format {{range .Mounts}}", (0, f"{store}\n", "")))
    ctx = mk("surrealdb_health")                                          # no data_dir option set
    assert nv.surreal_data_dir(nv._Opts(ctx), "open-notebook-surrealdb-1") == str(store)
    assert f.calls == ['docker container inspect --format {{range .Mounts}}{{if eq .Destination "/mydata"}}'
                       '{{.Source}}{{end}}{{end}} open-notebook-surrealdb-1']
    assert "Config" not in f.calls[0] and "{{json" not in f.calls[0]      # never reads Cmd/Env: the DB password lives there


def test_surreal_data_dir_falls_back_to_the_shipped_default(tmp_path, monkeypatch):
    ctx = mk("surrealdb_health")
    assert nv.SURREAL_DEFAULTS["data_dir"] == "/home/ohmz/docker-container-data/open-notebook/surreal_data"
    # docker cannot answer
    use_sh(monkeypatch, ("docker container inspect --format {{range .Mounts}}", (1, "", "daemon down")))
    assert nv.surreal_data_dir(nv._Opts(ctx), "c") == nv.SURREAL_DEFAULTS["data_dir"]
    # docker answers without a /mydata mount (empty -f output)
    use_sh(monkeypatch, ("docker container inspect --format {{range .Mounts}}", (0, "\n", "")))
    assert nv.surreal_data_dir(nv._Opts(ctx), "c") == nv.SURREAL_DEFAULTS["data_dir"]
    # docker names a host path that no longer exists
    use_sh(monkeypatch, ("docker container inspect --format {{range .Mounts}}", (0, f"{tmp_path / 'gone'}\n", "")))
    assert nv.surreal_data_dir(nv._Opts(ctx), "c") == nv.SURREAL_DEFAULTS["data_dir"]


def mount_world(tmp_path, monkeypatch):
    """The task's world with NO data_dir option: docker's /mydata probe points at a real tree, and `_disk_free_gb`
    returns 0 for every other path -- so a stale hardcoded path reads as "0 GB free", exactly the false crit."""
    store = tmp_path / "store"
    (store / "mydatabase.db").mkdir(parents=True, exist_ok=True)
    with open(store / "mydatabase.db" / "000001.log", "wb") as fh:
        fh.truncate(5 * MIB)
    cg = gates.CGROUP / "system.slice" / f"docker-{cid(7)}.scope"
    cg.mkdir(parents=True, exist_ok=True)
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    monkeypatch.setattr(nv, "_db_gb", lambda p: 2)
    monkeypatch.setattr(nv, "_disk_free_gb", lambda p: 900 if p == str(store) else 0)
    f = FakeSh(("docker container inspect --format {{range .Mounts}}", (0, f"{store}\n", "")),
               ("docker container inspect", (0, f"{cid(7)}|running|0\n", "")))
    monkeypatch.setattr(nv, "sh", f)
    return f, store


def test_surrealdb_health_resolves_from_the_mount_and_a_healthy_store_is_not_crit(tmp_path, monkeypatch, sandbox):
    """REGRESSION: the shipped data dir went stale, statvfs failed on it and the store read as "0 GB free", which paged a
    crit while the filesystem had ~950 GB free. Resolving the dir from the container's bind mount fixes it."""
    f, store = mount_world(tmp_path, monkeypatch)
    res, ctx = step(nv.surrealdb_health, "surrealdb_health", NOW, container="open-notebook-surrealdb-1")
    check_result(res)
    assert res.status == "ok" and "900 GB free" in res.summary and res.metrics["signature"] == "OK"
    assert res.metrics["disk_free_gb"] == 900 and sandbox == []
    assert nv.SURREAL_DEFAULTS["data_dir"] != str(store)                  # the mount was measured, not the default
    assert all("Config" not in c and "{{json" not in c for c in f.calls)


def notifier_pages(res, now, confirm=2, name="surrealdb_health", title="SurrealDB (Open Notebook)"):
    """What cli.cmd_run does after a task ran: feed the result to the REAL core.Notifier (debounce state machine, reminders,
    recovery) and persist its state in alerts.json, which the task reads at the start of its next run. Only the transport
    (`_send`) is replaced. Returns the subjects the Notifier paged in this run."""
    n = core.Notifier({"global": {"alert_confirm_runs": confirm, "alert_reminder_hours": 24, "alert_daily_budget": 50}})
    pages: list[str] = []
    n._send = lambda nm, subject, body, t: (pages.append(subject), True)[1]
    n.evaluate(name, title, res, now)
    n.save()
    return pages


class SurrealReplay:
    """Runs surrealdb_health on a 15-minute tick clock together with a Notifier.
    FAKE mode (default): the real core.Notifier state machine with a recording `_send`, and the task's own pages recorded by the
    sandbox `_notify`, so the TASK's decisions can be read off exactly. That fake has no notify dedupe, so the Notifier's page at its
    confirmation shows up as a notifier page; in the real stack notify swallows it (see the real_stack tests).
    REAL mode (real=True): HermesNotifier -> notify.send (real routing, dedupe window, budgets) -> a fake transport, and the task's
    pages take the same road: `rp.log` then lists what REALLY reached the owner per run."""

    def __init__(self, tmp_path, monkeypatch, sandbox, confirm=2, real=False, **task_opts):
        self.tmp, self.mp, self.sent, self.confirm, self.t = tmp_path, monkeypatch, sandbox, confirm, 0
        self.opts, self.wire = task_opts, (Wire(monkeypatch) if real else None)
        self.log: list = []       # fake: (signature, task pages, notifier pages); real: (signature, [(kind, dedupe key) delivered])

    def run(self, **world):
        now = NOW + self.t
        before, wbefore = len(self.sent), len(self.wire.msgs) if self.wire else 0
        if self.wire:
            self.wire.now = now
        f, opts = surreal_world(self.tmp, self.mp, **world)
        res, ctx = step(nv.surrealdb_health, "surrealdb_health", now, **opts, **self.opts)
        if self.wire:
            hermes_pages(self.wire, res, now, self.confirm)
            pages = []
            self.log.append((res.metrics["signature"], [(m["kind"], m["key"]) for m in self.wire.msgs[wbefore:]]))
        else:
            pages = notifier_pages(res, now, self.confirm)
            self.log.append((res.metrics["signature"], len(self.sent) - before, len(pages)))
        self.t += 900
        self.res, self.ctx, self.notifier_subjects = res, ctx, pages
        return res

    def delivered(self) -> list:
        """REAL mode: [(kind, key)] per run, signatures dropped."""
        return [d for _, d in self.log]


def alerts_state(name="surrealdb_health") -> dict:
    return (core.read_json(core.STATE_DIR / "alerts.json", {}) or {}).get("tasks", {}).get(name, {})


def test_surrealdb_health_task_pages_first_sight_and_each_change_once(tmp_path, monkeypatch, sandbox):
    """What the TASK announces: a new incident at the first run that sees it (the script's behaviour; the Notifier alone would wait
    for its 2 confirming runs), with the key of the Notifier's own alert; every later change of the failing set once, own key."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    r1 = rp.run(wal=2000 * MIB)
    assert r1.status == "crit" and r1.metrics["transition"] == "alert" and r1.metrics["sig_page"] == "sent"
    assert "WAL is 2000 MB" in r1.summary
    assert rp.log[-1] == ("wal,", 1, 0)                     # first sight: paged at once; the Notifier is still confirming
    ev = sandbox[0]
    assert (ev["kind"], ev["severity"], ev["status"], ev["task"]) == ("alert", "crit", "crit", "surrealdb_health")
    assert ev["dedupe_key"] == "surrealdb_health" and ev["title"] == f"Open Notebook DB pressure on {HOST}"
    assert ev["facts"]["failing"] == "wal," and ev["facts"]["previous"] == "OK" and "WAL is 2000 MB" in ev["details"]
    assert ev["summary"].isascii() and len(ev["summary"]) <= 130 and "What to do" in ev["details"]
    r2 = rp.run(wal=2000 * MIB)
    assert r2.metrics["transition"] == "suppressed" and rp.log[-1] == ("wal,", 0, 1)     # the Notifier's confirmation (notify dedupes it)
    r3 = rp.run(wal=2000 * MIB, disk=10)                    # a second check starts failing at the same (crit) level
    assert r3.metrics["signature"] == "wal,disk," and r3.metrics["transition"] == "alert" and r3.metrics["sig_page"] == "sent"
    assert rp.log[-1] == ("wal,disk,", 1, 0) and len(sandbox) == 2
    ev = sandbox[1]
    assert ev["dedupe_key"] == "surrealdb_health:wal,disk,:1" and ev["facts"]["previous"] == "wal," and ev["facts"]["failing"] == "wal,disk,"
    assert "only 10 GB free" in ev["details"]
    r4 = rp.run(wal=2000 * MIB, disk=10)
    assert r4.metrics["transition"] == "suppressed" and rp.log[-1] == ("wal,disk,", 0, 0)
    # recovery: the Notifier paged this incident, so ITS recovery speaks (after its 2 clean runs); the task adds none
    rp.run(wal=1 * MIB)
    r6 = rp.run(wal=1 * MIB)
    assert r6.status == "ok" and rp.ctx.state["signature"] == "OK" and "lead" not in rp.ctx.state
    assert rp.notifier_subjects == ["OK SurrealDB (Open Notebook): recovered"] and len(sandbox) == 2


def test_surrealdb_health_a_change_before_the_notifier_confirmed_is_its_own_page(tmp_path, monkeypatch, sandbox):
    """Run 1 WAL, run 2 WAL + store: two DIFFERENT announcements (the script paged both), each once, each carrying the current
    list. The Notifier's confirmation at run 2 is swallowed by notify's dedupe window (real_stack tests): never a third page."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    rp.run(wal=2000 * MIB)
    rp.run(wal=2000 * MIB, db=30)
    assert [e["dedupe_key"] for e in sandbox] == ["surrealdb_health", "surrealdb_health:wal,db,:1"]
    assert "store is 30 GB" in sandbox[1]["details"] and "store is 30 GB" in rp.res.summary


def test_surrealdb_health_a_flap_through_ok_then_another_failure_is_announced(tmp_path, monkeypatch, sandbox):
    """REGRESSION: wal (paged) -> one clean run (the Notifier needs 2 to confirm a recovery, so it still holds level crit) ->
    a DIFFERENT failure. The Notifier sees no change of level and stays silent; the task must announce the new failure, and not
    as a 'first page' that notify's dedupe window (same key, 45 min ago) would swallow."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    rp.run(wal=2000 * MIB)                                      # the task pages
    rp.run(wal=2000 * MIB)                                      # the Notifier confirms (alerted)
    rp.run(wal=1 * MIB)                                         # one clean run: the Notifier's recovery is pending, the task waits
    assert rp.log[-1] == ("OK", 0, 0) and "lead" not in rp.ctx.state and len(sandbox) == 1
    res = rp.run(db=30)                                         # a different failure
    assert res.metrics["sig_page"] == "sent" and rp.log[-1] == ("db,", 1, 0)
    assert sandbox[-1]["facts"]["failing"] == "db," and sandbox[-1]["facts"]["previous"] == "OK"
    assert sandbox[-1]["dedupe_key"] == "surrealdb_health:db,:1"


def test_surrealdb_health_a_page_notify_did_not_accept_is_retried_next_run(tmp_path, monkeypatch, sandbox):
    """REGRESSION: the signature used to be persisted before the send, so a failed page was suppressed for ever."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, confirm=1)
    rp.run(wal=2000 * MIB)                                      # paged by the task (first sight)
    assert len(sandbox) == 1
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), {"ok": False, "handled": False, "rc": 1, "note": "smtp down"})[1])
    res = rp.run(wal=2000 * MIB, db=30)
    assert res.metrics["sig_page"] == "retry" and rp.ctx.state["signature"] == "wal," and "announced" not in rp.ctx.state
    res = rp.run(wal=2000 * MIB, db=30)                         # still failing to send: tried again, not given up on
    assert res.metrics["sig_page"] == "retry" and len(sandbox) == 3
    assert sandbox[1]["dedupe_key"] == sandbox[2]["dedupe_key"] == "surrealdb_health:wal,db,:1"      # a retry is the same event
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), dict(OK_SENT))[1])
    res = rp.run(wal=2000 * MIB, db=30)
    assert res.metrics["sig_page"] == "sent" and rp.ctx.state["signature"] == "wal,db," and rp.ctx.state["announced"] == 1
    assert len(sandbox) == 4
    res = rp.run(wal=2000 * MIB, db=30)
    assert res.metrics["sig_page"] == "" and len(sandbox) == 4  # delivered once, then quiet


def test_surrealdb_health_a_first_page_notify_did_not_accept_is_retried_too(tmp_path, monkeypatch, sandbox):
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), {"ok": False, "handled": False, "rc": 1, "note": "smtp down"})[1])
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    for _ in range(2):
        res = rp.run(wal=2000 * MIB)
        assert res.metrics["sig_page"] == "retry" and "signature" not in rp.ctx.state and "lead" not in rp.ctx.state
    assert [e["dedupe_key"] for e in sandbox] == ["surrealdb_health"] * 2


def test_surrealdb_health_policy_suppression_counts_as_handled_not_retried(tmp_path, monkeypatch, sandbox):
    """notify decided not to send (dedupe window, quiet hours): handled=True, so the task does not hammer it every run."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, confirm=1)
    rp.run(wal=2000 * MIB)
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), {"ok": False, "handled": True, "rc": 1, "note": "quiet hours"})[1])
    rp.run(wal=2000 * MIB, db=30)
    rp.run(wal=2000 * MIB, db=30)
    assert len(sandbox) == 2 and rp.ctx.state["signature"] == "wal,db,"


def test_surrealdb_health_flapping_back_to_an_earlier_signature_is_a_new_announcement(tmp_path, monkeypatch, sandbox):
    """REGRESSION: the dedupe key was surrealdb_health:<signature>, so wal -> wal,db -> wal within notify's dedupe window had its
    last page swallowed. Every announced change has its own key (the first page of an incident is the bare task name)."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, confirm=1)
    for world in (dict(wal=2000 * MIB), dict(wal=2000 * MIB, db=30), dict(wal=2000 * MIB), dict(wal=2000 * MIB, db=30)):
        rp.run(**world)
    keys = [e["dedupe_key"] for e in sandbox]
    assert keys == ["surrealdb_health", "surrealdb_health:wal,db,:1", "surrealdb_health:wal,:2", "surrealdb_health:wal,db,:3"]
    assert len(set(keys)) == 4


def test_surrealdb_health_pages_the_first_run_even_without_any_alert_state(tmp_path, monkeypatch, sandbox):
    f, opts = surreal_world(tmp_path, monkeypatch, wal=2000 * MIB)
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert len(sandbox) == 1 and res.metrics["sig_page"] == "sent" and not (core.STATE_DIR / "alerts.json").exists()


def test_surrealdb_health_without_first_sight_pages_the_task_waits_for_the_notifier(tmp_path, monkeypatch, sandbox):
    """first_sight_page = false restores the Notifier-only first page: the task pays no page of its own until the Notifier has
    paged (a change page before that would be a double page), and it still announces later changes afterwards."""
    f, opts = surreal_world(tmp_path, monkeypatch, wal=2000 * MIB)
    step(nv.surrealdb_health, "surrealdb_health", NOW, first_sight_page=False, **opts)
    monkeypatch.setattr(nv, "_disk_free_gb", lambda p: 10)
    step(nv.surrealdb_health, "surrealdb_health", NOW + 900, first_sight_page=False, **opts)
    assert sandbox == []
    (core.STATE_DIR / "alerts.json").write_text("{not json")
    assert nv._notifier_alerted("surrealdb_health") == 0
    core.write_json_atomic(core.STATE_DIR / "alerts.json", {"tasks": {"surrealdb_health": {"alerted": 2}, "x": "y"}})
    assert nv._notifier_alerted("surrealdb_health") == 2 and nv._notifier_alerted("x") == 0 and nv._notifier_alerted("nope") == 0
    monkeypatch.setattr(nv, "_db_gb", lambda p: 30)                       # the Notifier has paged: a new failing check is the task's page
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW + 1800, first_sight_page=False, **opts)
    assert res.metrics["sig_page"] == "sent" and [e["dedupe_key"] for e in sandbox] == ["surrealdb_health:wal,db,disk,:1"]


def test_surrealdb_health_signature_alerts_can_be_turned_off(tmp_path, monkeypatch, sandbox):
    f, opts = surreal_world(tmp_path, monkeypatch, wal=2000 * MIB)
    step(nv.surrealdb_health, "surrealdb_health", NOW, signature_alerts=False, **opts)
    monkeypatch.setattr(nv, "_disk_free_gb", lambda p: 10)
    step(nv.surrealdb_health, "surrealdb_health", NOW + 900, signature_alerts=False, **opts)
    assert len(sandbox) == 1 and sandbox[0]["dedupe_key"] == "surrealdb_health"      # the first page stays, the change page is off


def test_surrealdb_health_a_blip_the_notifier_never_paged_gets_its_recovery_from_the_task(tmp_path, monkeypatch, sandbox):
    """A one-run problem used to be invisible; now it pages, so it must also be closed: the Notifier never confirmed it, so it will
    never send a recovery. (An incident the Notifier DID page keeps its Notifier recovery: see the first test.)"""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    rp.run(wal=2000 * MIB)
    res = rp.run(wal=1 * MIB)
    assert res.metrics["sig_page"] == "closed" and rp.ctx.state["signature"] == "OK" and "lead" not in rp.ctx.state
    assert [e["kind"] for e in sandbox] == ["alert", "recovery"]
    rec = sandbox[1]
    assert rec["dedupe_key"] == "surrealdb_health" and rec["facts"]["was"] == "crit" and rec["severity"] == "ok"
    assert rec["summary"].startswith("SurrealDB is back to normal") and rec["summary"].isascii() and len(rec["summary"]) <= 130
    rp.run(wal=1 * MIB)
    assert len(sandbox) == 2                                              # once


def test_surrealdb_health_a_recovery_notify_did_not_accept_is_retried(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    rp.run(wal=2000 * MIB)
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), {"ok": False, "handled": False, "rc": 1, "note": "smtp down"})[1])
    for _ in range(2):
        res = rp.run(wal=1 * MIB)
        assert res.metrics["sig_page"] == "retry" and rp.ctx.state["signature"] == "wal," and "lead" in rp.ctx.state
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), dict(OK_SENT))[1])
    assert rp.run(wal=1 * MIB).metrics["sig_page"] == "closed" and rp.ctx.state["signature"] == "OK"
    assert [e["kind"] for e in sandbox] == ["alert", "recovery", "recovery", "recovery"]       # two failed tries, one delivered


def test_surrealdb_health_a_recovery_that_can_never_be_delivered_is_given_up_on(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox)
    rp.run(wal=2000 * MIB)
    monkeypatch.setattr(nv, "_notify", lambda ev: {"ok": False, "handled": False, "rc": 1, "note": "smtp down"})
    rp.t = nv.LEAD_RETRY_S + 60                                            # the page was 12 h and a minute ago
    res = rp.run(wal=1 * MIB)
    assert res.metrics["sig_page"] == "closed" and rp.ctx.state["signature"] == "OK" and "lead" not in rp.ctx.state


# --------------------------------------------------------------------------- surrealdb_health through the REAL notification stack
def test_surrealdb_health_real_stack_first_sight_pages_in_one_tick_and_the_notifier_page_is_swallowed(tmp_path, monkeypatch, sandbox):
    """REGRESSION (alert-path parity): the Notifier needs 2 consecutive runs (30 min) and sends '<title>: <summary>'; the script
    paged on first sight (15 min) with the full body. Now the task pages at run 1 (SMS + email: crit) and the page the Notifier
    sends when it confirms at run 2 is swallowed by notify's dedupe window (same key, same severity): ONE page, not two."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    rp.run(wal=2000 * MIB)
    assert rp.log[-1] == ("wal,", [("alert", "surrealdb_health")])
    m = rp.wire.msgs[0]
    assert m["severity"] == "crit" and "sms" in m["channels"] and "email" in m["channels"]
    assert "Open Notebook DB pressure" in m["subject"] and "WAL is 2000 MB" in m["plain"]
    rp.run(wal=2000 * MIB)
    assert rp.log[-1] == ("wal,", [])                           # the Notifier confirmed here: nothing new reached the owner
    assert alerts_state()["alerted"] == 2                       # ...and it believes it paged (no retry storm)
    recs = [json.loads(ln) for ln in (core.STATE_DIR / "notifications.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["dedupe_key"], bool(r["channels"])) for r in recs] == [("alert", "surrealdb_health", True),
                                                                                 ("alert", "surrealdb_health", False)]
    assert "same alert/crit" in recs[1]["note"]                 # the dedupe window did it


def test_surrealdb_health_real_stack_without_first_sight_the_notifier_alone_pages_at_run_two(tmp_path, monkeypatch, sandbox):
    """What the port did before the fix (and what first_sight_page = false still does): nothing at run 1, the page at run 2."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True, first_sight_page=False)
    rp.run(wal=2000 * MIB)
    rp.run(wal=2000 * MIB)
    assert rp.delivered() == [[], [("alert", "surrealdb_health")]]
    assert "CRIT" in rp.wire.msgs[0]["subject"] and "Open Notebook DB pressure" not in rp.wire.msgs[0]["subject"]


def test_surrealdb_health_real_stack_one_run_blip_is_paged_and_closed(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    for w in (dict(wal=2000 * MIB), dict(wal=1 * MIB), dict(wal=1 * MIB)):
        rp.run(**w)
    assert rp.delivered() == [[("alert", "surrealdb_health")], [("recovery", "surrealdb_health")], []]
    assert alerts_state()["alerted"] == 0                       # the Notifier never saw it long enough to page


def test_surrealdb_health_real_stack_lifecycle_pages_each_state_once(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    for w in (dict(wal=2000 * MIB), dict(wal=2000 * MIB), dict(wal=2000 * MIB, disk=10), dict(wal=2000 * MIB, disk=10),
              dict(wal=1 * MIB), dict(wal=1 * MIB), dict(wal=1 * MIB)):
        rp.run(**w)
    assert rp.delivered() == [[("alert", "surrealdb_health")], [], [("alert", "surrealdb_health:wal,disk,:1")], [], [],
                              [("recovery", "surrealdb_health")], []]


def test_surrealdb_health_real_stack_a_different_failure_after_a_flap_is_delivered_not_swallowed(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    for w in (dict(wal=2000 * MIB), dict(wal=2000 * MIB), dict(wal=1 * MIB), dict(db=30)):
        rp.run(**w)
    assert rp.delivered() == [[("alert", "surrealdb_health")], [], [], [("alert", "surrealdb_health:db,:1")]]


def test_surrealdb_health_real_stack_with_a_one_run_notifier_every_change_is_still_paged_once(tmp_path, monkeypatch, sandbox):
    """alert_confirm_runs = 1 for this task (the suggested glue) must not double the first page either."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, confirm=1, real=True)
    for w in (dict(wal=2000 * MIB), dict(wal=2000 * MIB, db=30), dict(wal=2000 * MIB, db=30), dict(wal=2000 * MIB)):
        rp.run(**w)
    assert rp.delivered() == [[("alert", "surrealdb_health")], [("alert", "surrealdb_health:wal,db,:1")], [],
                              [("alert", "surrealdb_health:wal,:2")]]


def test_surrealdb_health_real_stack_crit_page_survives_an_exhausted_alert_budget(tmp_path, monkeypatch, sandbox):
    """REGRESSION: core.Notifier._send dropped a page once 8 had gone out in 24 h, whatever its severity. The task's own page goes
    through notify.send, where crit bypasses the per-kind and total budgets (only the hard cap stops it)."""
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    n = rp.wire.notify
    for i in range(8):                                          # the other tasks have spent the per-kind alert budget
        n.send(n.Event("alert", "warn", f"noise {i}", "x", dedupe_key=f"noise-{i}", task=f"t{i}"))
    spent = len(rp.wire.msgs)
    assert spent == 8
    n.send(n.Event("alert", "warn", "one too many", "x", dedupe_key="noise-9", task="t9"))
    assert len(rp.wire.msgs) == spent                           # the budget really is spent: a warning is dropped
    rp.run(wal=2000 * MIB)
    assert rp.log[-1] == ("wal,", [("alert", "surrealdb_health")]) and len(rp.wire.msgs) == spent + 1


def test_surrealdb_health_real_stack_transport_outage_then_recovery_is_one_page(tmp_path, monkeypatch, sandbox):
    rp = SurrealReplay(tmp_path, monkeypatch, sandbox, real=True)
    rp.wire.up = False
    rp.run(wal=2000 * MIB)
    rp.run(wal=2000 * MIB)
    assert rp.delivered() == [[], []] and rp.res.metrics["sig_page"] == "retry" and alerts_state()["alerted"] == 0
    rp.wire.up = True
    rp.run(wal=2000 * MIB)
    assert rp.log[-1] == ("wal,", [("alert", "surrealdb_health")]) and rp.res.metrics["sig_page"] == "sent"
    rp.run(wal=2000 * MIB)
    assert rp.log[-1][1] == [] and alerts_state()["alerted"] == 2


@pytest.mark.parametrize("kw,key", [(dict(wal=1025 * MIB), "wal"), (dict(db=26), "db"), (dict(disk=39), "disk"),
                                    (dict(oom=1), "oom"), (dict(restarts=4), "restart")])
def test_surrealdb_health_each_check_is_crit(tmp_path, monkeypatch, kw, key):
    f, opts = surreal_world(tmp_path, monkeypatch, **kw)
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    check_result(res)
    assert res.status == "crit" and res.metrics["signature"] == key + "," and res.metrics["problems"] == 1
    assert [i["check"] for i in res.items if i["state"] == "BAD"] == [key]


def test_surrealdb_health_thresholds_come_from_config_with_the_script_defaults(tmp_path, monkeypatch):
    f, opts = surreal_world(tmp_path, monkeypatch, wal=300 * MIB, db=10, disk=100, restarts=2)
    ok_res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert ok_res.status == "ok" and ok_res.metrics["wal_max_mb"] == 1024 and ok_res.metrics["db_max_gb"] == 25
    assert ok_res.metrics["disk_min_gb"] == 40
    bad, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, wal_max_mb=256, db_max_gb=5, disk_min_gb=200, restart_gt=1, **opts)
    assert bad.metrics["signature"] == "wal,db,disk,restart,"


def test_surrealdb_health_stopped_or_missing_container_is_info_not_a_page(tmp_path, monkeypatch):
    f, opts = surreal_world(tmp_path, monkeypatch, state="exited")
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert res.status == "info" and "exited" in res.summary
    f, opts = surreal_world(tmp_path, monkeypatch, docker=False)
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert res.status == "info" and res.metrics["docker_ok"] is False


def test_surrealdb_health_bad_config_uses_defaults_and_says_so(tmp_path, monkeypatch):
    f, opts = surreal_world(tmp_path, monkeypatch)
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, wal_max_mb="huge", **opts)
    assert res.status == "ok" and res.metrics["bad_config"] == "wal_max_mb" and "bad config wal_max_mb" in res.summary


def test_surrealdb_health_reads_oom_from_the_containers_cgroup(tmp_path, monkeypatch):
    f, opts = surreal_world(tmp_path, monkeypatch, oom=4)
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert res.metrics["oom_kill"] == 4 and res.metrics["signature"] == "oom,"
    shutil.rmtree(gates.CGROUP)                                          # no cgroup file: the script printed 0
    res, _ = step(nv.surrealdb_health, "surrealdb_health", NOW, **opts)
    assert res.metrics["oom_kill"] == 0 and res.status == "ok"


# =========================================================================== comfyui_idle_reclaim  <-  comfyui-idle-vram.sh
QUEUES = {"empty": '{"queue_running": [], "queue_pending": []}',
          "run": '{"queue_running": [["id", 1]], "queue_pending": []}',
          "pend": '{"queue_running": [], "queue_pending": [["a"], ["b"]]}',
          "garbage": "<html>502 bad gateway</html>", "emptydict": "{}", "curlfail": None}
CPID = 100
# one symbol = one 5-minute check of the world: (queue answer, container pid or None=absent, nvidia-smi rows or None=fails)
STEPS = {
    "A": ("empty", CPID, [(CPID, 5000)]),           # idle and holding VRAM
    "B": ("run", CPID, [(CPID, 5000)]),             # a job is running
    "C": ("pend", CPID, [(CPID, 5000)]),            # jobs are queued
    "D": ("empty", CPID, [(CPID, 1000)]),           # idle and light
    "E": ("curlfail", CPID, [(CPID, 5000)]),        # queue endpoint unreachable
    "F": ("garbage", CPID, [(CPID, 5000)]),         # not JSON
    "G": ("empty", CPID, [(999, 8000)]),            # VRAM held by some other process, none by comfyui
    "H": ("empty", CPID, [(CPID, 3000)]),           # exactly the threshold: not "more than"
    "I": ("empty", CPID, [(CPID, 3001)]),           # one MiB over
    "J": ("empty", None, [(CPID, 5000)]),           # container missing (docker inspect fails)
    "K": ("empty", CPID, None),                     # nvidia-smi fails
}
# Frozen decision table: sequence -> indexes (0-based) of the checks that restart the container.
COMFY_TABLE = {
    "A": [], "AA": [1], "AAA": [1], "AAAA": [1, 3], "AAAAA": [1, 3], "ABA": [], "AAB": [1], "ADA": [], "ADAA": [3],
    "AEA": [], "AFA": [], "AGA": [], "AHA": [], "AIA": [1], "AII": [1], "AJA": [], "AKA": [], "ACAA": [3],
    "BAA": [2], "HHH": [], "IIII": [1, 3], "AAEAA": [1, 4],
}


def comfy_sandbox(root: Path):
    """A sandbox copy of comfyui-idle-vram.sh plus a runner that applies one world step and returns 'restarted?'."""
    script = prep_script(legacy("comfyui-idle-vram.sh"), [('STATE="/run/comfyui-idle-vram.strike"', f'STATE="{root}/strike"')],
                         root / "script.sh")
    fake = root / "fake"
    fake.mkdir(parents=True, exist_ok=True)
    write_bin(root, "curl", '[ -f "$FAKE_DIR/curl_rc" ] && exit "$(cat "$FAKE_DIR/curl_rc")"\ncat "$FAKE_DIR/queue"\n')
    write_bin(root, "docker", 'case "$1" in\n  inspect) [ -f "$FAKE_DIR/nopid" ] && exit 1; cat "$FAKE_DIR/pid" ;;\n'
              '  restart) echo "restart $2" >> "$FAKE_DIR/docker.log" ;;\n  *) exit 98 ;;\nesac\n')
    write_bin(root, "nvidia-smi", '[ -f "$FAKE_DIR/nosmi" ] && exit 9\ncat "$FAKE_DIR/smi"\n')
    write_bin(root, "logger", FAKE_LOGGER)

    def run(queue: str | None, pid: int | None, smi) -> bool:
        for n in ("curl_rc", "nopid", "nosmi"):
            (fake / n).unlink(missing_ok=True)
        if queue is None:
            (fake / "curl_rc").write_text("7")
        else:
            (fake / "queue").write_text(queue)
        if pid is None:
            (fake / "nopid").write_text("")
        else:
            (fake / "pid").write_text(str(pid))
        if smi is None:
            (fake / "nosmi").write_text("")
        else:
            (fake / "smi").write_text("".join(f"{p}, {mb}\n" for p, mb in smi))
        log = fake / "docker.log"
        before = len(log.read_text().splitlines()) if log.exists() else 0
        r = bash(script, root)
        assert r.returncode == 0, r.stderr
        after = len(log.read_text().splitlines()) if log.exists() else 0
        assert after - before in (0, 1)
        if after > before:
            assert log.read_text().splitlines()[-1] == "restart comfyui"
        return after > before

    run.struck = lambda: (root / "strike").exists()
    return run


class ComfyWorld:
    """Mutable fake host for the port: docker inspect/restart, the queue endpoint, nvidia-smi, the container cgroup."""

    def __init__(self, monkeypatch):
        self.queue: dict | None = {"queue_running": [], "queue_pending": []}
        self.queue_exc: Exception | None = None
        self.queue_seq: list = []
        self.pid: int | None = CPID
        self.state = "running"
        self.smi: list | None = [(CPID, 5000)]
        self.docker_ok = True
        self.restart_rc = 0
        self.restarts = 0
        self.urls: list[str] = []
        self.sh = use_sh(monkeypatch,
                         ("docker container inspect", self._inspect), ("nvidia-smi", self._smi), ("docker restart", self._restart))
        monkeypatch.setattr(gates, "http_json", self._http)

    def _inspect(self, cmd):
        if not self.docker_ok:
            return (1, "", "Cannot connect to the Docker daemon")
        if self.pid is None:
            return (1, "", "Error: No such container: comfyui")
        return ok(f"{cid(5)}|{self.state}|{self.pid}\n")

    def _smi(self, cmd):
        return (9, "", "NVIDIA-SMI has failed") if self.smi is None else ok("".join(f"{p}, {mb}\n" for p, mb in self.smi))

    def _restart(self, cmd):
        self.restarts += 1
        return (self.restart_rc, "", "boom: container is wedged" if self.restart_rc else "")

    def _http(self, url, timeout=3.0):
        self.urls.append(url)
        if self.queue_seq:                                   # scripted answers, consumed one per probe
            return self.queue_seq.pop(0)
        if self.queue_exc:
            raise self.queue_exc
        return self.queue

    def load(self, symbol: str) -> None:
        q, self.pid, self.smi = STEPS[symbol]
        self.queue_exc = None
        self.queue = None
        if q == "curlfail":
            self.queue_exc = ConnectionRefusedError("refused")
        elif q == "garbage":
            self.queue_exc = ValueError("Expecting value")
        else:
            self.queue = json.loads(QUEUES[q])

    def cgroup_procs(self, *pids):
        d = gates.CGROUP / "system.slice" / f"docker-{cid(5)}.scope"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))


def port_comfy_sequence(monkeypatch, symbols: str, **opts) -> list[int]:
    """Indexes of the checks at which the port restarts, for a sequence of 5-minute checks."""
    w = ComfyWorld(monkeypatch)
    hits = []
    for i, s in enumerate(symbols):
        w.load(s)
        before = w.restarts
        step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 300 * i, apply=True, unprotect=["^comfyui$"],
             min_gap_min=1, match_cgroup=False, **opts)
        if w.restarts > before:
            hits.append(i)
    return hits


def script_comfy_sequence(root: Path, symbols: str) -> list[int]:
    run = comfy_sandbox(root)
    return [i for i, s in enumerate(symbols) if run(QUEUES[STEPS[s][0]], STEPS[s][1], STEPS[s][2])]


@pytest.mark.parametrize("seq,want", list(COMFY_TABLE.items()))
def test_comfy_frozen_table_port(monkeypatch, seq, want):
    assert port_comfy_sequence(monkeypatch, seq) == want


@pytest.mark.parametrize("seq,want", list(COMFY_TABLE.items()))
def test_comfy_frozen_table_matches_sandboxed_script(tmp_path, seq, want):
    assert script_comfy_sequence(tmp_path / "run", seq) == want


def test_comfy_port_matches_sandboxed_script_on_random_sequences(tmp_path, monkeypatch):
    rnd = random.Random(20261001)
    alphabet = "ABCDEFGHIJK"
    for n in range(14):
        seq = "".join(rnd.choice(alphabet if rnd.random() < 0.5 else "AAAABDE") for _ in range(6))
        legacy_hits = script_comfy_sequence(tmp_path / f"s{n}", seq)
        assert port_comfy_sequence(monkeypatch, seq) == legacy_hits, f"sequence {seq}"
        shutil.rmtree(core.STATE_DIR / "tasks", ignore_errors=True)         # next sequence starts with no strikes


@pytest.mark.parametrize("n,thresh,mb,idle,want", [
    (0, 3000, 3001, True, "strike"), (1, 3000, 3001, True, "restart"), (0, 3000, 3000, True, "reset"),
    (1, 3000, 3000, True, "reset"), (1, 3000, 3001, False, "reset"), (2, 3000, 9000, True, "restart"),
])
def test_comfy_step_table(n, thresh, mb, idle, want):
    st = {"n": n, "t": NOW - 300} if n else {}
    action, new = nv.comfy_step(st, 0 if idle else None, mb, NOW, thresh)
    assert action == want
    assert new["n"] == (1 if want == "strike" else 0)


def test_comfy_step_busy_values():
    for busy in (None, 1, 5):
        assert nv.comfy_step({"n": 1, "t": NOW}, busy, 9000, NOW)[0] == "reset"


def test_comfy_strike_expires_after_ttl():
    st = {"n": 1, "t": NOW - 46 * 60}
    action, new = nv.comfy_step(st, 0, 9000, NOW, 3000, 2, 45 * 60)
    assert action == "strike" and new == {"n": 1, "t": NOW}                   # an old strike is a first strike again
    assert nv.comfy_step({"n": 1, "t": NOW - 44 * 60}, 0, 9000, NOW, 3000, 2, 45 * 60)[0] == "restart"


# --------------------------------------------------------------------------- intentional differences (script vs port)
def test_diff_empty_queue_object_is_idle_for_the_script_but_busy_for_the_port(tmp_path, monkeypatch):
    run = comfy_sandbox(tmp_path / "s")
    assert [run("{}", CPID, [(CPID, 5000)]), run("{}", CPID, [(CPID, 5000)])] == [False, True]
    w = ComfyWorld(monkeypatch)
    w.queue = {}
    for i in range(3):
        step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 300 * i, apply=True, unprotect=["^comfyui$"], min_gap_min=1)
    assert w.restarts == 0


def test_diff_two_gpu_rows_for_one_pid_are_summed_by_the_port(tmp_path, monkeypatch):
    rows = [(CPID, 2000), (CPID, 2000)]
    run = comfy_sandbox(tmp_path / "s")
    assert [run(QUEUES["empty"], CPID, rows) for _ in range(3)] == [False, False, False]   # `[ "2000\n2000" -gt N ]` errors
    w = ComfyWorld(monkeypatch)
    w.smi = rows
    assert nv.gpu_mem_mb({CPID}) == 4000
    for i in range(2):
        step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 300 * i, apply=True, unprotect=["^comfyui$"],
             min_gap_min=1, match_cgroup=False)
    assert w.restarts == 1


def test_diff_a_forked_worker_in_the_cgroup_counts_for_the_port_only(tmp_path, monkeypatch):
    run = comfy_sandbox(tmp_path / "s")
    assert [run(QUEUES["empty"], CPID, [(777, 9000)]) for _ in range(3)] == [False] * 3     # script only knows State.Pid
    w = ComfyWorld(monkeypatch)
    w.smi = [(777, 9000)]
    w.cgroup_procs(CPID, 777)
    for i in range(2):
        step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 300 * i, apply=True, unprotect=["^comfyui$"], min_gap_min=1)
    assert w.restarts == 1
    shutil.rmtree(core.STATE_DIR / "tasks")
    w2 = ComfyWorld(monkeypatch)
    w2.smi = [(777, 9000)]
    for i in range(3):
        step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 300 * i, apply=True, unprotect=["^comfyui$"],
             min_gap_min=1, match_cgroup=False)
    assert w2.restarts == 0                                                    # match_cgroup = false is the script's rule


def test_diff_strike_expires_in_the_port_but_not_in_the_script(tmp_path, monkeypatch):
    run = comfy_sandbox(tmp_path / "s")
    assert run(QUEUES["empty"], CPID, [(CPID, 5000)]) is False and run(QUEUES["empty"], CPID, [(CPID, 5000)]) is True
    w = ComfyWorld(monkeypatch)
    step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW, apply=True, unprotect=["^comfyui$"])
    step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 3 * 3600, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 0                                                    # 3 h later is a first strike again
    step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 3 * 3600 + 900, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 1


# --------------------------------------------------------------------------- the task itself
def comfy_run(i, **kw):
    return step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + 900 * i, **kw)


def test_comfy_default_mode_reports_and_never_restarts(tmp_path, monkeypatch):
    w = ComfyWorld(monkeypatch)
    r1, _ = comfy_run(0, unprotect=["^comfyui$"])
    assert r1.status == "ok" and "strike 1/2" in r1.summary and r1.metrics["mode"] == "report"
    r2, ctx = comfy_run(1, unprotect=["^comfyui$"])
    check_result(r2)
    assert r2.status == "info" and r2.summary.startswith("report: would restart comfyui") and w.restarts == 0
    assert r2.metrics["action"] == "would" and "dry-run" in outcomes(tmp_path)
    assert w.sh.with_prefix("docker restart") == [] and ctx.state["rn"] == 0 and "n" not in ctx.state   # the report lane


def comfy_at(seconds, **kw):
    """One run `seconds` after NOW (comfy_run counts in 15 min ticks; this one is for the sub-tick interleavings)."""
    kw.setdefault("unprotect", ["^comfyui$"])
    return step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + seconds, **kw)


def test_comfy_report_run_and_apply_run_never_share_strikes(monkeypatch):
    """REGRESSION: the check tier (no --apply: report lane) and the task's own cron schedule (--apply) share one state file. A
    report run 30 s before the apply run used to be strike 1 of the apply run's two-strike rule: restart at the first apply run."""
    w = ComfyWorld(monkeypatch)
    r, ctx = comfy_at(0)                                                   # report mode (apply=False), idle and holding VRAM
    assert r.metrics["mode"] == "report" and ctx.state["rn"] == 1 and "n" not in ctx.state
    r, ctx = comfy_at(30, apply=True)                                      # the cron run, 30 s later
    assert w.restarts == 0 and r.metrics["strikes"] == 1 and ctx.state["n"] == 1 and ctx.state["rn"] == 1
    r, ctx = comfy_at(330, apply=True)                                     # the next cron run, 5 min later: NOW it is sustained
    assert w.restarts == 1 and r.metrics["action"] == "done"


def test_comfy_report_lane_cannot_restart_and_apply_lane_never_reads_it(monkeypatch):
    w = ComfyWorld(monkeypatch)
    for t in (0, 300, 600, 900):                                           # four report runs: "would restart" twice
        r, ctx = comfy_at(t)
    assert w.restarts == 0 and "n" not in ctx.state and "last_restart" not in ctx.state
    r, ctx = comfy_at(960, apply=True)
    assert w.restarts == 0 and r.metrics["strikes"] == 1                   # the apply lane starts from nothing


def test_comfy_a_paused_apply_task_runs_in_the_report_lane(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_at(0, apply=True)
    (core.CONF_DIR / "PAUSE").write_text("")
    r, ctx = comfy_at(300, apply=True)                                     # ctx.apply is False under PAUSE
    assert r.metrics["mode"] == "report" and ctx.state["n"] == 1 and ctx.state["rn"] == 1 and w.restarts == 0


def test_comfy_two_apply_runners_within_one_minute_are_one_strike(monkeypatch):
    """REGRESSION: no minimum spacing between strikes: a second runner (manual `run --task`, scheduler catch-up, the check tier
    started with --apply) landing seconds after the first made one moment of idleness 'sustained'."""
    w = ComfyWorld(monkeypatch)
    r, _ = comfy_at(0, apply=True)
    assert r.metrics["action"] == "strike" and w.restarts == 0
    r, ctx = comfy_at(30, apply=True)
    check_result(r)
    assert r.metrics["action"] == "wait" and r.status == "ok" and "waiting" in r.summary and w.restarts == 0
    assert ctx.state["n"] == 1 and ctx.state["t"] == NOW                   # the early run neither counted nor moved the clock
    r, _ = comfy_at(59, apply=True)
    assert w.restarts == 0
    r, _ = comfy_at(300, apply=True)                                       # measured from the FIRST strike: 5 min >= 4 min
    assert r.metrics["action"] == "done" and w.restarts == 1


@pytest.mark.parametrize("dt,restarts", [(239, 0), (240, 1), (241, 1), (30, 0), (900, 1)])
def test_comfy_strike_min_gap_boundary(monkeypatch, dt, restarts):
    w = ComfyWorld(monkeypatch)
    comfy_at(0, apply=True)
    comfy_at(dt, apply=True)
    assert w.restarts == restarts


def test_comfy_strike_min_gap_is_configurable_and_zero_is_the_old_behaviour(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_at(0, apply=True, strike_min_gap_min=0)
    comfy_at(1, apply=True, strike_min_gap_min=0)
    assert w.restarts == 1
    w2 = ComfyWorld(monkeypatch)
    shutil.rmtree(core.STATE_DIR / "tasks", ignore_errors=True)
    comfy_at(0, apply=True, strike_min_gap_min=10)
    comfy_at(540, apply=True, strike_min_gap_min=10)
    assert w2.restarts == 0
    comfy_at(600, apply=True, strike_min_gap_min=10)
    assert w2.restarts == 1
    for bad in (-1, "4", True):
        res, _ = comfy_at(0, apply=True, strike_min_gap_min=bad)
        assert res.status == "skipped" and "bad config strike_min_gap_min" in res.summary


def test_comfy_a_busy_or_light_check_still_resets_inside_the_gap(monkeypatch):
    """Only an idle observation is ignored inside the gap; anything that says 'not idle' resets at once (the safe direction)."""
    w = ComfyWorld(monkeypatch)
    comfy_at(0, apply=True)
    w.load("B")                                                            # a job is running 20 s later
    r, ctx = comfy_at(20, apply=True)
    assert r.metrics["action"] == "reset" and ctx.state["n"] == 0
    w.load("A")
    comfy_at(40, apply=True)
    comfy_at(60, apply=True)
    assert w.restarts == 0


def test_comfy_reprobes_the_queue_right_before_the_restart(tmp_path, monkeypatch):
    """A job queued between the second strike's first probe and the restart must not be killed."""
    w = ComfyWorld(monkeypatch)
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    idle, busy = {"queue_running": [], "queue_pending": []}, {"queue_running": [["id", 7]], "queue_pending": []}
    w.queue_seq = [idle, busy]                                             # 1st probe idle, the re-probe sees a job
    res, ctx = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    check_result(res)
    assert w.restarts == 0 and res.metrics["action"] == "cancelled" and "got a job" in res.summary
    assert ctx.state["n"] == 0 and "last_restart" not in ctx.state and "done" not in outcomes(tmp_path)
    comfy_run(2, apply=True, unprotect=["^comfyui$"])                      # idle again: strike 1, not a restart
    assert w.restarts == 0
    w.queue_seq = [idle, idle]                                             # idle both times: it restarts
    res, _ = comfy_run(3, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 1 and res.metrics["action"] == "done" and len(w.urls) == 6


def test_comfy_unreadable_queue_at_the_reprobe_cancels_the_restart(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    w.queue_seq = [{"queue_running": [], "queue_pending": []}, "<html>502</html>"]
    res, _ = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 0 and res.metrics["action"] == "cancelled"


def test_comfy_report_mode_does_not_reprobe(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, unprotect=["^comfyui$"])
    comfy_run(1, unprotect=["^comfyui$"])
    assert len(w.urls) == 2                                                # one probe per run: nothing is about to be restarted


def test_comfy_step_wait_leaves_the_state_alone():
    st = {"n": 1, "t": NOW - 10}
    action, new = nv.comfy_step(st, 0, 9000, NOW, 3000, 2, 2700, 240)
    assert action == "wait" and new == {"n": 1, "t": NOW - 10} and st == {"n": 1, "t": NOW - 10}
    assert nv.comfy_step(st, 0, 9000, NOW, 3000, 2, 2700, 0)[0] == "restart"
    assert nv.comfy_step({}, 0, 9000, NOW, 3000, 2, 2700, 240)[0] == "strike"       # no strike yet: nothing to wait for
    assert nv.comfy_step({"n": 1, "t": NOW - 3000}, 0, 9000, NOW, 3000, 2, 2700, 240)[0] == "strike"   # expired: first strike


def test_comfy_apply_restarts_after_two_idle_checks_and_records_it(tmp_path, monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    res, ctx = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    check_result(res)
    assert res.status == "ok" and res.summary == "restarted comfyui: idle for 2 checks, held 5000 MB VRAM"
    assert w.sh.with_prefix("docker restart") == ["docker restart comfyui"]
    assert ctx.state["n"] == 0 and ctx.state["last_restart"] == NOW + 900 and outcomes(tmp_path)[-1] == "done"
    assert (res.metrics["action"], res.metrics["vram_mb"], res.metrics["busy"]) == ("done", 5000, 0)


def test_comfy_is_protected_unless_the_task_unprotects_it(tmp_path, monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, apply=True)
    res, _ = comfy_run(1, apply=True)                       # the global list protects "comfyui"
    assert res.status == "info" and "protected" in res.summary and w.restarts == 0
    assert "refused-protected" in outcomes(tmp_path)


def test_comfy_pause_file_stops_the_restart(tmp_path, monkeypatch):
    w = ComfyWorld(monkeypatch)
    (core.CONF_DIR / "PAUSE").write_text("")
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    res, _ = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 0 and "paused" in res.summary


def test_comfy_min_gap_suppresses_a_restart_loop(monkeypatch):
    w = ComfyWorld(monkeypatch)
    at = lambda t: step(nv.comfyui_idle_reclaim, "comfyui_idle_reclaim", NOW + t, apply=True, unprotect=["^comfyui$"])  # noqa: E731
    at(0)
    at(300)
    assert w.restarts == 1
    # the model reloaded straight into VRAM: strikes accumulate again within the 30 min gap but no second restart
    at(600)
    res, ctx = at(900)
    assert w.restarts == 1 and res.status == "info" and "suppressed" in res.summary and res.metrics["action"] == "suppressed"
    at(7200)
    res, _ = at(7500)
    assert w.restarts == 2 and res.status == "ok"             # 2 hours later it is allowed again


def test_comfy_failed_restart_is_a_warning_not_silent(tmp_path, monkeypatch):
    w = ComfyWorld(monkeypatch)
    w.restart_rc = 1
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    res, ctx = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    check_result(res)
    assert res.status == "warn" and "restart of comfyui failed" in res.summary and "wedged" in res.summary
    assert "last_restart" not in ctx.state and any(o.startswith("failed") for o in outcomes(tmp_path))


@pytest.mark.parametrize("state", ["exited", "created", "paused", "restarting"])
def test_comfy_not_running_container_is_left_alone(monkeypatch, state):
    w = ComfyWorld(monkeypatch)
    w.state = state
    res, ctx = comfy_run(0, apply=True, unprotect=["^comfyui$"])
    assert res.status == "ok" and "no VRAM to reclaim" in res.summary and ctx.state["n"] == 0 and w.restarts == 0


def test_comfy_missing_container_and_docker_down(monkeypatch):
    w = ComfyWorld(monkeypatch)
    w.pid = None
    res, _ = comfy_run(0, apply=True, unprotect=["^comfyui$"])
    assert res.status == "ok" and "absent" in res.summary
    w.docker_ok = False
    res, ctx = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    assert res.status == "skipped" and "docker unavailable" in res.summary and w.restarts == 0


def test_comfy_nvidia_smi_failure_skips_and_resets_the_strike(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    w.smi = None
    res, ctx = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    assert res.status == "skipped" and "nvidia-smi" in res.summary and ctx.state["n"] == 0 and w.restarts == 0
    w.smi = [(CPID, 5000)]
    comfy_run(2, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 0                                    # the strike really was reset


@pytest.mark.parametrize("exc", [ConnectionRefusedError("x"), ValueError("not json"), TimeoutError("t"), RuntimeError("HTTP 500")])
def test_comfy_every_queue_error_counts_as_busy(monkeypatch, exc):
    w = ComfyWorld(monkeypatch)
    w.queue_exc = exc
    res, ctx = comfy_run(0, apply=True, unprotect=["^comfyui$"])
    assert res.metrics["busy"] == -1 and res.metrics["action"] == "reset" and ctx.state["n"] == 0
    comfy_run(1, apply=True, unprotect=["^comfyui$"])
    assert w.restarts == 0


@pytest.mark.parametrize("q,want", [
    ({"queue_running": [], "queue_pending": []}, 0), ({"queue_running": [1], "queue_pending": [1, 2]}, 3),
    ({}, None), ({"queue_running": []}, None), ({"queue_running": "x", "queue_pending": []}, None), ([], None), (None, None)])
def test_queue_jobs_shapes(monkeypatch, q, want):
    monkeypatch.setattr(gates, "http_json", lambda *a, **k: q)
    assert nv.queue_jobs("http://x/queue") == want


def test_comfy_queue_url_option_and_global_default(monkeypatch):
    w = ComfyWorld(monkeypatch)
    comfy_run(0, protected={"patterns": [], "busy": {"comfyui_queue_url": "http://127.0.0.1:9999/queue"}})
    assert w.urls[-1] == "http://127.0.0.1:9999/queue"
    comfy_run(1, queue_url="http://127.0.0.1:1234/queue")
    assert w.urls[-1] == "http://127.0.0.1:1234/queue"
    comfy_run(2)
    assert w.urls[-1] == "http://127.0.0.1:8188/queue"


def test_comfy_bad_config_does_nothing(monkeypatch):
    w = ComfyWorld(monkeypatch)
    for kw in ({"threshold_mb": "lots"}, {"strikes": 0}, {"container": "a;b"}, {"queue_url": 7}):
        res, _ = comfy_run(0, apply=True, unprotect=["^comfyui$"], **kw)
        assert res.status == "skipped" and "bad config" in res.summary
    assert w.sh.calls == [] and w.restarts == 0


def test_comfy_three_strike_option(monkeypatch):
    w = ComfyWorld(monkeypatch)
    hits = []
    for i in range(4):
        before = w.restarts
        comfy_run(i, apply=True, unprotect=["^comfyui$"], strikes=3, min_gap_min=1)
        if w.restarts > before:
            hits.append(i)
    assert hits == [2]


def test_gpu_mem_mb_parsing(monkeypatch):
    use_sh(monkeypatch, ("nvidia-smi", ok("100, 5000\n200, 7\nbad line\n300, N/A\n100, 11\n")))
    assert nv.gpu_mem_mb({100}) == 5011 and nv.gpu_mem_mb({200, 300}) == 7 and nv.gpu_mem_mb(set()) == 0
    use_sh(monkeypatch, ("nvidia-smi", (9, "", "fail")))
    assert nv.gpu_mem_mb({100}) is None


# =========================================================================== immich_recycle  <-  immich-server-recycle.service + gate drop-in
UNIT_DIR = Path("/etc/systemd/system")


def unit_text(name: str) -> str:
    for d in (UNIT_DIR, *map(Path, glob.glob("/usr/local/lib/homelab-maint/legacy/*"))):
        p = d / name
        if p.is_file():
            return p.read_text()
    pytest.skip(f"{name} is not on this host any more (retired?)")


class ImmichWorld:
    def __init__(self, monkeypatch, state="running", busy=(False, "immich idle (max 2% of a core)")):
        self.state, self.busy, self.restarts, self.restart_rc, self.after = state, busy, 0, 0, None
        self.gate_calls: list[str] = []
        self.sh = use_sh(monkeypatch, ("docker container inspect --format {{.State.Status}}", self._state),
                         ("docker restart", self._restart))

        def busy_fn(name, cfg=None):
            self.gate_calls.append(name)
            return self.busy

        monkeypatch.setattr(gates, "busy", busy_fn)

    def _state(self, cmd):
        if self.state is None:
            return (1, "", "Cannot connect to the Docker daemon")
        return ok(self.state + "\n")

    def _restart(self, cmd):
        self.restarts += 1
        if self.restart_rc == 0:
            self.state = self.after or "running"
        return (self.restart_rc, "", "Error response from daemon: boom" if self.restart_rc else "")


def gate_cfg(max_defer=12):
    (core.CONF_DIR / "protected.toml").write_text(
        f'patterns = ["x"]\n[busy]\nmax_defer_hours = {{ "immich-recycle" = {max_defer} }}\n')


def immich_run(t, **kw):
    kw.setdefault("unprotect", ["^immich_server$"])
    return step(nv.immich_recycle, "immich_recycle", NOW + t, **kw)


def test_immich_unit_files_match_the_port_defaults():
    svc, timer = unit_text("immich-server-recycle.service"), unit_text("immich-server-recycle.timer")
    assert re.search(r"^ExecStart=/usr/bin/docker restart immich_server\s*$", svc, re.M)      # the action and its target
    assert re.search(r"^OnUnitActiveSec=2h\s*$", timer, re.M)                                  # the cadence
    ctx = mk("immich_recycle")
    o = nv._Opts(ctx)
    assert o.name("container", "immich_server") == "immich_server" and o.num("every_hours", 2) == 2
    assert gates.ALIASES["immich-recycle"] == "immich"
    p = UNIT_DIR / "immich-server-recycle.service.d" / "10-homelab-gate.conf"
    if p.is_file():
        assert "ExecCondition=/usr/local/sbin/homelab-maint gate immich-recycle" in p.read_text()


def test_immich_idle_gate_restarts_in_apply_mode(tmp_path, monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    res, ctx = immich_run(0, apply=True)
    check_result(res)
    assert res.status == "ok" and res.summary == "recycled immich_server (gate idle)"
    assert w.sh.with_prefix("docker restart") == ["docker restart immich_server"] and w.gate_calls == ["immich-recycle"]
    assert ctx.state["last_attempt"] == NOW and ctx.state["last_restart"] == NOW
    assert res.metrics["gate_rc"] == 0 and res.metrics["action"] == "done" and outcomes(tmp_path) == ["done"]


def test_immich_busy_gate_defers_and_records_the_deferral(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch, busy=(True, "immich_server at 40% of a core (limit 15%)"))
    res, ctx = immich_run(0, apply=True)
    assert res.status == "info" and res.summary.startswith("recycle deferred:") and "immich_server at 40%" in res.summary
    assert w.restarts == 0 and res.metrics["gate_rc"] == 1 and res.metrics["deferred"] == 1
    rec = core.read_json(core.STATE_DIR / "gates.json")["immich-recycle"]
    assert rec["count"] == 1 and "immich_server" in rec["reason"]
    assert ctx.state["last_attempt"] == NOW and "last_restart" not in ctx.state       # a deferred tick also counts


def test_immich_cadence_is_two_hours_from_the_last_attempt(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    immich_run(0, apply=True)
    res, _ = immich_run(3600, apply=True)
    assert res.status == "ok" and res.summary.startswith("next recycle check in about") and w.restarts == 1
    assert res.metrics["next_in_min"] == 50                                            # 2 h - 10 min slack - 1 h
    immich_run(2 * 3600 - 900, apply=True)
    assert w.restarts == 1 and len(w.gate_calls) == 1                                  # 15 min early: still waiting
    immich_run(2 * 3600 - 300, apply=True)                                             # inside the 10 min slack
    assert w.restarts == 2 and len(w.gate_calls) == 2


def test_immich_deferred_attempt_restarts_the_clock_so_busy_does_not_probe_every_check(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch, busy=(True, "busy"))
    for t in (0, 900, 1800, 2700):
        immich_run(t, apply=True)
    assert w.gate_calls == ["immich-recycle"]       # one probe, not four
    immich_run(2 * 3600, apply=True)
    assert len(w.gate_calls) == 2


def test_immich_every_hours_zero_leaves_the_cadence_to_the_scheduler(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    for t in (0, 3600, 7200):
        immich_run(t, apply=True, every_hours=0, min_gap_min=1)
    assert w.restarts == 3


# frozen gate table: (busy, hours deferred so far) -> proceed?   (legacy: ExecCondition exit code of `homelab-maint gate`)
@pytest.mark.parametrize("busy,since_h,proceed", [(False, None, True), (False, 30, True), (True, None, False), (True, 0.5, False),
                                                  (True, 11.9, False), (True, 12.1, True), (True, 40, True)])
def test_immich_gate_semantics_idle_busy_and_max_defer(tmp_path, monkeypatch, busy, since_h, proceed):
    gate_cfg(12)
    w = ImmichWorld(monkeypatch, busy=(busy, "immich busy" if busy else "immich idle"))
    if since_h is not None:
        core.write_json_atomic(core.STATE_DIR / "gates.json", {"immich-recycle": {
            "since": time.time() - since_h * 3600, "count": 5, "last": time.time() - 900, "reason": "busy"}})
    res, _ = immich_run(0, apply=True)
    assert (w.restarts == 1) is proceed, res.summary
    rec = (core.read_json(core.STATE_DIR / "gates.json") or {}).get("immich-recycle")
    assert (rec is None) == proceed                       # proceeding (idle or forced) clears the deferral clock
    assert ("defer-limit" in outcomes_text(tmp_path)) == (busy and proceed)


def outcomes_text(tmp_path) -> str:
    return " ".join(r["action"] + ":" + r["outcome"] for r in audit_rows(tmp_path))


def test_immich_any_gate_error_defers(monkeypatch):
    w = ImmichWorld(monkeypatch)

    def boom(name):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(gates, "cli_gate", boom)
    res, _ = immich_run(0, apply=True)
    assert res.status == "info" and "gate error" in res.summary and w.restarts == 0


def test_immich_unknown_gate_name_defers(monkeypatch):
    w = ImmichWorld(monkeypatch)
    res, _ = immich_run(0, apply=True, gate="no-such-gate")
    assert w.restarts == 0 and res.status == "info"


def test_immich_never_starts_a_stopped_container(monkeypatch):
    gate_cfg()
    for state in ("exited", "created", "absent"):
        w = ImmichWorld(monkeypatch, state=state)
        res, ctx = immich_run(0, apply=True)
        assert res.status == "skipped" and state in res.summary and w.restarts == 0 and w.gate_calls == []
        assert "last_attempt" not in ctx.state
        shutil.rmtree(core.STATE_DIR / "tasks", ignore_errors=True)
    w = ImmichWorld(monkeypatch, state=None)
    res, _ = immich_run(0, apply=True)
    assert res.status == "skipped" and "docker unavailable" in res.summary and w.restarts == 0


def test_immich_report_mode_asks_the_gate_but_restarts_nothing(tmp_path, monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    res, ctx = immich_run(0)
    assert res.status == "info" and res.summary == "report: would restart immich_server (gate idle)"
    assert w.restarts == 0 and res.metrics["mode"] == "report" and res.metrics["action"] == "would"
    assert outcomes(tmp_path) == ["dry-run"] and "last_restart" not in ctx.state


def test_immich_report_mode_never_writes_the_shared_deferral_record(monkeypatch):
    """The legacy unit's ExecCondition shares gates.json until its timer is retired: a report-mode probe must not touch it."""
    gate_cfg()
    gj = core.STATE_DIR / "gates.json"
    w = ImmichWorld(monkeypatch, busy=(True, "immich_server at 40% of a core"))
    res, _ = immich_run(0)
    assert res.status == "info" and "immich_server at 40%" in res.summary and not gj.exists() and w.gate_calls == ["immich-recycle"]
    core.write_json_atomic(gj, {"immich-recycle": {"since": 1.0, "count": 3, "last": 2.0, "reason": "x"}})
    before = gj.read_text()
    w2 = ImmichWorld(monkeypatch, busy=(False, "idle"))
    immich_run(7200)
    assert gj.read_text() == before and w2.restarts == 0                       # idle in report mode does not clear it either
    ImmichWorld(monkeypatch, busy=(False, "idle"))
    immich_run(14400, apply=True)
    assert "immich-recycle" not in core.read_json(gj)                          # apply mode owns (and clears) the record


def immich_events(world, events, hours=48):
    """Replay (time, runner) events against one shared state dir; runner = 'apply' (a run that carries --apply, mode apply) or
    'check' (the check tier WITHOUT --apply: same [tasks.immich_recycle] mode = apply, but ctx.apply is False)."""
    restarts_at = []
    for t, who in sorted(events):
        if t >= hours * 3600:
            break
        before = world.restarts
        immich_run(t, apply=(who == "apply"), mode="apply")
        if world.restarts > before:
            restarts_at.append(t)
    return restarts_at


def test_immich_check_tier_report_runs_do_not_consume_the_apply_cadence(monkeypatch):
    """REGRESSION: the task is registered in the check tier, so the check tier also runs it (no --apply) beside its own cron
    schedule (--apply). Its report-mode probe used to set last_attempt, so the 2 h cadence was always 'in progress' when the
    apply run came: 0 recycles in 48 h and nothing alerting. Memory growth is the reason this task exists."""
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    check = [(900 * i, "check") for i in range(0, 48 * 4 + 1)]                  # the check tier, every 15 min
    cron = [(7200 * k + 60, "apply") for k in range(0, 24)]                     # `24 */2 * * *` via the tick, 1 min after the check
    hits = immich_events(w, check + cron)
    assert hits == [7200 * k + 60 for k in range(24)], hits                      # exactly the legacy rate: every 2 h
    assert len(w.gate_calls) >= 24


def test_immich_two_apply_drivers_still_recycle_at_the_legacy_rate(monkeypatch):
    """REGRESSION: the check tier given --apply AND a cron schedule both start the task with --apply. With the default
    every_hours = 2 the cadence is shared (one lane), so the rate stays one per 2 h instead of one per min_gap (4x)."""
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    check = [(900 * i, "apply") for i in range(0, 48 * 4 + 1)]
    cron = [(7200 * k + 60, "apply") for k in range(0, 24)]
    hits = immich_events(w, check + cron)
    assert len(hits) == 24 and all(b - a >= 7200 - 900 for a, b in zip(hits, hits[1:])), hits


def test_immich_report_runs_have_their_own_lane_and_cadence(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    res, ctx = immich_run(0, mode="apply")                                       # mode apply, but no --apply on this run
    assert res.status == "info" and res.metrics["mode"] == "report" and w.restarts == 0
    assert ctx.state["report_attempt"] == NOW and "last_attempt" not in ctx.state and "last_restart" not in ctx.state
    immich_run(900, mode="apply")
    assert w.gate_calls == ["immich-recycle"]                                    # the report lane probes once per 2 h, not per tick
    res, ctx = immich_run(60, apply=True)                                        # the apply lane is untouched by all of that
    assert w.restarts == 1 and ctx.state["last_attempt"] == NOW + 60 and ctx.state["report_attempt"] == NOW
    res, ctx = immich_run(7200, mode="apply")
    assert ctx.state["report_attempt"] == NOW + 7200 and ctx.state["last_attempt"] == NOW + 60


def test_immich_a_paused_apply_run_does_not_touch_the_apply_lane(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    (core.CONF_DIR / "PAUSE").write_text("")
    res, ctx = immich_run(0, apply=True)
    assert w.restarts == 0 and "last_attempt" not in ctx.state and ctx.state["report_attempt"] == NOW
    (core.CONF_DIR / "PAUSE").unlink()
    res, ctx = immich_run(60, apply=True)
    assert w.restarts == 1                                                       # resumed: the first apply run acts at once


def test_immich_is_protected_unless_the_task_unprotects_it(tmp_path, monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    res, _ = immich_run(0, apply=True, unprotect=[])
    assert w.restarts == 0 and "protected" in res.summary and "refused-protected" in outcomes(tmp_path)


def test_immich_min_gap_after_a_restart(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    immich_run(0, apply=True)
    res, _ = immich_run(600, apply=True, every_hours=0)
    assert res.status == "skipped" and "min gap" in res.summary and w.restarts == 1


def test_immich_failed_restart_and_container_that_does_not_come_back(monkeypatch):
    gate_cfg()
    w = ImmichWorld(monkeypatch)
    w.restart_rc = 1
    res, ctx = immich_run(0, apply=True)
    assert res.status == "warn" and "restart of immich_server failed" in res.summary and "last_restart" not in ctx.state
    shutil.rmtree(core.STATE_DIR / "tasks")
    w = ImmichWorld(monkeypatch)
    w.after = "restarting"
    res, ctx = immich_run(0, apply=True)
    assert res.status == "warn" and "restarted but is restarting" in res.summary and ctx.state["last_restart"] == NOW


def test_immich_bad_config_does_nothing(monkeypatch):
    w = ImmichWorld(monkeypatch)
    for kw in ({"every_hours": "2h"}, {"container": "x;y"}, {"gate": 5}, {"min_gap_min": 0}):
        res, _ = immich_run(0, apply=True, **kw)
        assert res.status == "skipped" and "bad config" in res.summary
    assert w.restarts == 0 and w.sh.calls == []


# =========================================================================== openwebui_media_prune  <-  prune-openwebui-media.sh
def media_tree(root: Path, now: float) -> tuple[Path, Path]:
    """Both allow-listed directories with files of every age/name/type that matters. Returns (comfy_out, uploads)."""
    comfy, up = root / "comfy_out", root / "owui_uploads"
    (comfy / "sub").mkdir(parents=True)
    up.mkdir()
    outside = root / "outside"
    outside.mkdir()

    def mk_file(d, name, age_days, size=100):
        p = d / name
        p.write_bytes(b"m" * size)
        os.utime(p, (now - age_days * DAY, now - age_days * DAY))

    for name, age in (("owui_00001_.png", 10), ("owui_00002_.png", 7.9), ("owui_00003_.png", 8.01), ("owui_vid_001.webm", 30),
                      ("owui_vid_002.mp4", 30), ("ComfyUI_00001_.png", 30), ("owui_future.png", -3), ("owui_.hidden.png", 9),
                      ("owui_x.PNG", 30), ("owui_exactly7d.png", 7.0), ("owui_just8d.png", 8.0007), (".owui_dot.png", 30),
                      ("sub/owui_deep.png", 30)):
        mk_file(comfy, name, age)
    (comfy / "owui_dir.png").mkdir()
    os.utime(comfy / "owui_dir.png", (now - 30 * DAY, now - 30 * DAY))
    mk_file(outside, "target.png", 30)
    os.symlink(outside / "target.png", comfy / "owui_link.png")
    for name, age in (("abc_owui_vid.webm", 9), ("abc_owui_vid.html", 9), ("abc_generated-image.png", 9), ("abc_generated_image_1.jpg", 9),
                      ("user_upload.pdf", 30), ("report_generated-image.png.bak", 30), ("x_generated_imagefoo", 9),
                      ("_generated-image.png", 9), ("fresh_generated-image.png", 1), ("abc_owui_vid.webm.part", 30)):
        mk_file(up, name, age)
    return comfy, up


def survivors(*dirs: Path) -> set[str]:
    return {str(p.relative_to(dirs[0].parent)) for d in dirs for p in d.rglob("*") if p.is_file() or p.is_symlink() or p.is_dir()}


def media_rules(comfy: Path, up: Path) -> list[dict]:
    return [{"path": os.path.realpath(comfy), "patterns": nv.DEFAULT_MEDIA_RULES[0]["patterns"]},
            {"path": os.path.realpath(up), "patterns": nv.DEFAULT_MEDIA_RULES[1]["patterns"]}]


def media_sandbox(root: Path, now: float, retention="7"):
    comfy, up = media_tree(root, now)
    subs = [('COMFY_OUT="/volume1/docker/comfyui/output"', f'COMFY_OUT="{comfy}"'),
            ('OWUI_UPLOADS="/volume1/docker/openwebui/config/uploads"', f'OWUI_UPLOADS="{up}"')]
    script = prep_script(legacy("prune-openwebui-media.sh"), subs, root / "script.sh")
    write_bin(root, "logger", FAKE_LOGGER)
    r = bash(script, root, env={"RETENTION_DAYS": retention})
    assert r.returncode == 0, r.stderr
    return comfy, up, r.stdout.strip()


def test_openwebui_allow_list_and_retention_equal_the_scripts():
    text = legacy("prune-openwebui-media.sh").read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    dirs = {m[1]: m[2] for ln in code if (m := re.match(r'^(\w+)="(/[^"]+)"$', ln))}
    import shlex
    calls = [(m[1], shlex.split(m[2])) for ln in code if (m := re.match(r'^prune "\$(\w+)"\s+(.*)$', ln))]
    assert [{"path": dirs[v], "patterns": pats} for v, pats in calls] == nv.DEFAULT_MEDIA_RULES
    assert re.search(r'RETENTION_DAYS="\$\{RETENTION_DAYS:-7\}"', text)
    assert "find \"$dir\" -maxdepth 1 -type f" in text and "-mtime +\"$RETENTION_DAYS\"" in text      # the semantics ported
    svc = unit_text("prune-openwebui-media.service")
    assert re.search(r"^Environment=RETENTION_DAYS=7\s*$", svc, re.M)


def test_openwebui_port_selects_and_removes_exactly_what_the_script_removes(tmp_path):
    now = time.time()
    # legacy on tree A
    comfy_a, up_a, msg = media_sandbox(tmp_path / "a", now)
    removed_by_script = int(re.match(r"pruned (\d+) media", msg)[1])
    # port on an identical tree B
    comfy_b, up_b = media_tree(tmp_path / "b", now)
    survivors_before = survivors(comfy_b, up_b)
    rules = media_rules(comfy_b, up_b)
    protected = {"patterns": ["comfyui"]}
    res, ctx = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, protected=protected, rules=rules)
    check_result(res)
    assert survivors(comfy_a, up_a) == survivors(comfy_b, up_b)
    assert res.status == "ok" and f"pruned {removed_by_script} media file(s)" in res.summary
    assert len(survivors_before) - len(survivors(comfy_b, up_b)) == removed_by_script
    assert (comfy_b.parent / "outside" / "target.png").exists()             # the symlink's target is untouched


def test_openwebui_report_mode_lists_the_same_set_and_deletes_nothing(tmp_path):
    now = time.time()
    comfy_a, up_a, msg = media_sandbox(tmp_path / "a", now)
    comfy_b, up_b = media_tree(tmp_path / "b", now)
    before = survivors(comfy_b, up_b)
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, rules=media_rules(comfy_b, up_b))
    assert survivors(comfy_b, up_b) == before
    n = int(re.match(r"pruned (\d+)", msg)[1])
    assert res.summary.startswith(f"report: would prune {n} media file(s) older than 7 day(s)")
    assert res.status == "info" and res.metrics["selected"] == n and res.metrics["mode"] == "report"
    assert {os.path.basename(i["name"]) for i in res.items}.issubset({"owui_00001_.png", "owui_00003_.png", "owui_vid_001.webm",
                                                                      "owui_.hidden.png", "owui_just8d.png", "abc_owui_vid.webm",
                                                                      "abc_owui_vid.html", "abc_generated-image.png",
                                                                      "abc_generated_image_1.jpg", "x_generated_imagefoo",
                                                                      "_generated-image.png"})


@pytest.mark.parametrize("days", [0, 1, 3, 7, 30])
def test_openwebui_retention_days_boundaries_match_find_mtime(tmp_path, days):
    now = time.time()
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    for root in (root_a, root_b):
        d = root / "comfy_out"
        d.mkdir(parents=True)
        for k, age in enumerate((days - 0.5, days, days + 0.5, days + 0.999, days + 1.001, days + 1.5, days + 20)):
            if age <= 0:
                continue
            p = d / f"owui_{k}_.png"
            p.write_bytes(b"x")
            os.utime(p, (now - age * DAY, now - age * DAY))
        (root / "owui_uploads").mkdir()
    subs = [('COMFY_OUT="/volume1/docker/comfyui/output"', f'COMFY_OUT="{root_a / "comfy_out"}"'),
            ('OWUI_UPLOADS="/volume1/docker/openwebui/config/uploads"', f'OWUI_UPLOADS="{root_a / "owui_uploads"}"')]
    script = prep_script(legacy("prune-openwebui-media.sh"), subs, root_a / "script.sh")
    write_bin(root_a, "logger", FAKE_LOGGER)
    assert bash(script, root_a, env={"RETENTION_DAYS": str(days)}).returncode == 0
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, protected={"patterns": []},
                  retention_days=days, rules=[{"path": os.path.realpath(root_b / "comfy_out"), "patterns": ["owui_*.png"]}])
    assert sorted(p.name for p in (root_a / "comfy_out").iterdir()) == sorted(p.name for p in (root_b / "comfy_out").iterdir())
    assert res.status == "ok"


@pytest.mark.parametrize("age_s,days,want", [(7 * DAY, 7, False), (8 * DAY - 1, 7, False), (8 * DAY, 7, True), (30 * DAY, 7, True),
                                             (-5, 7, False), (0, 0, False), (DAY - 1, 0, False), (DAY, 0, True)])
def test_old_enough_is_find_mtime_plus_n(age_s, days, want):
    assert nv.old_enough(age_s, days) is want


def test_openwebui_default_rules_apply_to_the_two_real_directories():
    assert [r["path"] for r in nv.DEFAULT_MEDIA_RULES] == ["/volume1/docker/comfyui/output", "/volume1/docker/openwebui/config/uploads"]


def test_openwebui_protected_comfyui_output_needs_an_explicit_unprotect(tmp_path):
    now = time.time()
    comfy = tmp_path / "comfyui" / "output"
    comfy.mkdir(parents=True)
    f = comfy / "owui_old.png"
    f.write_bytes(b"x")
    os.utime(f, (now - 20 * DAY, now - 20 * DAY))
    rules = [{"path": os.path.realpath(comfy), "patterns": ["owui_*.png"]}]
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, rules=rules)
    assert f.exists() and "1 protected" in res.summary
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, rules=rules,
                  unprotect=[f"^{re.escape(os.path.realpath(comfy))}/owui_"])
    assert not f.exists() and res.summary.startswith("pruned 1 media file(s)")


def test_openwebui_caps_and_pause(tmp_path):
    now = time.time()
    d = tmp_path / "out"
    d.mkdir()
    for i in range(6):
        p = d / f"owui_{i}_.png"
        p.write_bytes(b"x")
        os.utime(p, (now - 20 * DAY, now - 20 * DAY))
    rules = [{"path": os.path.realpath(d), "patterns": ["owui_*.png"]}]
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, protected={"patterns": []}, rules=rules,
                  max_items_per_run=4)
    assert len(list(d.iterdir())) == 2 and "deferred by cap" in res.summary and res.status == "info"
    (core.CONF_DIR / "PAUSE").write_text("")
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, protected={"patterns": []}, rules=rules)
    assert len(list(d.iterdir())) == 2                                         # paused: nothing more removed


def test_openwebui_rule_validation(tmp_path):
    real = tmp_path / "d"
    real.mkdir()
    link = tmp_path / "ln"
    os.symlink(real, link)
    good = {"path": os.path.realpath(real), "patterns": ["owui_*.png"]}
    assert nv._valid_media_rule(good) == ""
    bad = [({}, "path"), ({"path": "relative/x", "patterns": ["owui_*.png"]}, "path"), ({"path": "/", "patterns": ["owui_*.png"]}, "path"),
           ({"path": str(real) + "/", "patterns": ["owui_*.png"]}, "path"), ({"path": str(link), "patterns": ["owui_*.png"]}, "symlink"),
           ({**good, "patterns": []}, "no patterns"), ({**good, "patterns": ["*"]}, "too broad"),
           ({**good, "patterns": ["owui_*.png", ""]}, "too broad"), ({**good, "patterns": ["?*"]}, "too broad"),
           ({**good, "patterns": ["*.*"]}, "too broad"), ({**good, "patterns": ["a/b*.png"]}, "too broad"),
           ({**good, "patterns": "owui_*.png"}, "no patterns"), ("not a table", "table")]
    for rule, why in bad:
        assert why in nv._valid_media_rule(rule), rule


def test_openwebui_bad_rule_is_refused_good_rule_still_runs_and_status_warns(tmp_path):
    now = time.time()
    d = tmp_path / "out"
    d.mkdir()
    f = d / "owui_old.png"
    f.write_bytes(b"x")
    os.utime(f, (now - 20 * DAY, now - 20 * DAY))
    rules = [{"path": os.path.realpath(d), "patterns": ["*"]}, {"path": os.path.realpath(d), "patterns": ["owui_*.png"]}]
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", now, apply=True, protected={"patterns": []}, rules=rules)
    assert not f.exists() and res.status == "warn" and "1 rule(s) refused" in res.summary and res.metrics["rules_refused"] == 1


@pytest.mark.parametrize("kw", [{"retention_days": "7"}, {"retention_days": 7.5}, {"retention_days": -1}, {"retention_days": True},
                                {"rules": []}, {"rules": "x"}, {"retention_days": float("nan")}])
def test_openwebui_bad_config_does_nothing(kw):
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", NOW, apply=True, **kw)
    assert res.status == "skipped" and "bad retention_days/rules config" in res.summary


def test_openwebui_missing_directory_is_not_an_error(tmp_path):
    rules = [{"path": str(tmp_path / "gone"), "patterns": ["owui_*.png"]}]
    res, _ = step(nv.openwebui_media_prune, "openwebui_media_prune", NOW, apply=True, protected={"patterns": []}, rules=rules)
    assert res.status == "ok" and res.summary.startswith("pruned 0 media file(s)")         # legacy: `[ -d "$dir" ] || return 0`


def test_unlink_checked_refuses_a_file_that_changed_after_the_scan(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = d / "owui_a.png"
    f.write_bytes(b"1")
    s = os.stat(f)
    nv._unlink_checked(str(d), "owui_a.png", s.st_ino, s.st_dev, s.st_mtime_ns)
    assert not f.exists()
    # replaced by another file (new inode) -> refuse
    f.write_bytes(b"1")
    s = os.stat(f)
    other = d / "other"
    other.write_bytes(b"2")
    os.replace(other, f)
    with pytest.raises(nv._Changed):
        nv._unlink_checked(str(d), "owui_a.png", s.st_ino, s.st_dev, s.st_mtime_ns)
    assert f.read_bytes() == b"2"
    # replaced by a symlink -> refuse and never touch the target
    s = os.stat(f)
    target = tmp_path / "precious"
    target.write_bytes(b"keep")
    f.unlink()
    os.symlink(target, f)
    with pytest.raises(nv._Changed):
        nv._unlink_checked(str(d), "owui_a.png", s.st_ino, s.st_dev, s.st_mtime_ns)
    assert target.read_bytes() == b"keep" and f.is_symlink()
    # gone entirely -> FileNotFoundError (counted as vanished by _Acts)
    f.unlink()
    with pytest.raises(FileNotFoundError):
        nv._unlink_checked(str(d), "owui_a.png", s.st_ino, s.st_dev, s.st_mtime_ns)


# =========================================================================== docker-prune.sh: parity + docker_containers_prune
def iso(t: float, digits=6) -> str:
    d = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    return f"{d}.{int((t % 1) * 10 ** digits):0{digits}d}" + ("0" * (9 - digits) if digits < 9 else "") + "Z"


def iid(name: str) -> str:
    """A stable image id for a name (what `docker container inspect {{.Image}}` prints)."""
    import hashlib
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def crow(n: int, name: str, status: str = "exited", created_d: float = 30, finished_d: float | None = 10,
         image: str = "busybox:latest", project: str = "", image_id: str | None = None) -> dict:
    return {"id": cid(1000 + n), "name": name, "status": status, "created": NOW - created_d * DAY,
            "finished": None if finished_d is None else NOW - finished_d * DAY, "image": image, "project": project,
            "image_id": image_id or iid(image)}


def irow(name: str | None, age_d: float = 30, size: int = 100 * MIB, parent: str = "", tag: str | None = None) -> dict:
    """A local image (`name` is the tag; None = untagged/dangling, identified by `tag` or its index)."""
    return {"id": iid(name or f"dangling-{tag}"), "created": NOW - age_d * DAY, "size": size, "parent": parent,
            "tags": [name] if name else []}


def docker_world(monkeypatch, rows, *, builders=("default",), stopped=(), ps_rc=0, drop_line=False, bad_field=False, rm_rc=None,
                 images=(), images_rc=0, timer=("disabled", "inactive"), timer_next=None):
    """Fake docker for the inventory: `docker ps -a -q --no-trunc`, `docker container inspect --format ...`, `docker rm`,
    the image inventory (`docker image ls/inspect`) and the legacy timer (`systemctl is-enabled/is-active/list-timers`)."""
    by_id = {r["id"]: r for r in rows}
    img_by_id = {i["id"]: i for i in images}
    rm_calls: list[str] = []

    def inspect(cmd):
        lines = []
        for i in re.findall(r"\b[0-9a-f]{64}\b", cmd):
            r = by_id[i]
            fin = "0001-01-01T00:00:00Z" if r["finished"] is None else iso(r["finished"], 9)
            lines.append(f"{r['id']}|/{r['name']}|{r['status']}|{iso(r['created'])}|{fin}|{r['image']}|{r['project']}|{r['image_id']}")
        if drop_line:
            lines = lines[:-1]
        if bad_field:
            lines[0] += "|extra"
        return ok("\n".join(lines) + "\n")

    def img_inspect(cmd):
        out = []
        for i in re.findall(r"sha256:[0-9a-f]{64}", cmd):
            m = img_by_id[i]
            out.append(f"{i}|{iso(m['created'], 9)}|{m['size']}|{m['parent']}|{json.dumps(m['tags']) if m['tags'] else 'null'}")
        return ok("\n".join(out) + "\n")

    timers = [{"unit": "docker-prune.timer", "next": int(timer_next * 1e6), "last": 0}] if timer_next else []

    def rm(cmd):
        rm_calls.append(cmd)
        if rm_rc is not None and cmd.split()[-1] in rm_rc:
            return (1, "", "Error response from daemon: cannot remove container: container is running")
        return ok(cmd.split()[-1] + "\n")

    nodes = [{"Name": b, "Driver": "docker-container", "Nodes": [{"Status": "inactive" if b in stopped else "running"}]}
             for b in builders]
    f = use_sh(monkeypatch, ("docker ps -a -q --no-trunc", (ps_rc, "\n".join(by_id) + "\n", "")),
               ("docker container inspect --format", inspect), ("docker rm", rm),
               ("docker image ls --all --quiet --no-trunc", (images_rc, "\n".join(img_by_id) + "\n", "")),
               ("docker image inspect --format", img_inspect),
               ("systemctl is-enabled docker-prune.timer", (0 if timer[0] == "enabled" else 1, timer[0] + "\n", "")),
               ("systemctl is-active docker-prune.timer", (0 if timer[1] == "active" else 3, timer[1] + "\n", "")),
               ("systemctl list-timers", ok(json.dumps(timers))),
               ("docker buildx ls --format json", ok("\n".join(json.dumps(n) for n in nodes) + "\n")))
    f.rm_calls = rm_calls
    return f


def prune_run(**kw):
    kw.setdefault("protected", PROTECTED)
    return step(nv.docker_containers_prune, "docker_containers_prune", NOW, **kw)


# the frozen table: what `docker container prune --filter until=168h` removes vs what the native task removes
PRUNE_TABLE_ROWS = [
    crow(1, "stopped-an-hour-ago", created_d=30, finished_d=1 / 24),     # legacy: yes (created long ago); native: no
    crow(2, "stopped-ten-days", created_d=30, finished_d=10),            # both
    crow(3, "running-old", "running", 30, None),                         # neither
    crow(4, "never-started", "created", 10, None),                       # both
    crow(5, "recent-and-stopped", created_d=3, finished_d=2),            # neither
    crow(6, "comfyui", created_d=60, finished_d=20),                     # legacy yes; native no (expected stopped)
    crow(7, "plexmediaserver-old", created_d=60, finished_d=20),         # legacy yes; native no (protected name)
    crow(8, "dead-one", "dead", 30, 10),                                 # both
    crow(9, "paused-old", "paused", 30, None),                           # neither: prune never removes paused
    crow(10, "restarting-old", "restarting", 30, None),                  # neither
    crow(11, "db-copy", created_d=30, finished_d=10, image="postgres:16"),                         # legacy yes; native no (image)
    crow(12, "web", created_d=30, finished_d=10, image="x/y:1", project="immich"),                 # legacy yes; native no (compose project)
    crow(13, "stopped-just-under-7d", created_d=30, finished_d=7 - 0.01),                          # legacy yes; native no
    crow(14, "stopped-just-over-7d", created_d=30, finished_d=7 + 0.01),                           # both
    crow(15, "created-just-under-7d", created_d=7 - 0.01, finished_d=7 - 0.01),                    # neither
]
LEGACY_REMOVES = {"stopped-an-hour-ago", "stopped-ten-days", "never-started", "comfyui", "plexmediaserver-old", "dead-one",
                  "db-copy", "web", "stopped-just-under-7d", "stopped-just-over-7d"}
NATIVE_REMOVES = {"stopped-ten-days", "never-started", "dead-one", "stopped-just-over-7d"}


def test_docker_prune_frozen_table_legacy_vs_native_selection():
    assert set(nv.legacy_container_prune_set(PRUNE_TABLE_ROWS, NOW, 168)) == LEGACY_REMOVES
    ctx = mk("docker_containers_prune", cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    cands = nv.stopped_candidates(PRUNE_TABLE_ROWS, NOW, 7, nv._expected_stopped(ctx), ctx.is_protected)
    assert {c["name"] for c in cands if not c["why_kept"]} == NATIVE_REMOVES
    kept = {c["name"]: c["why_kept"] for c in cands if c["why_kept"]}
    assert kept == {"comfyui": "kept by config", "plexmediaserver-old": "protected", "db-copy": "protected", "web": "protected"}
    assert [c["name"] for c in cands] == sorted([c["name"] for c in cands], key=lambda n: next((c["ref"], n) for c in cands if c["name"] == n))


def test_docker_prune_intentional_difference_is_the_clock_and_the_exclusions():
    only_legacy = LEGACY_REMOVES - NATIVE_REMOVES
    assert "stopped-an-hour-ago" in only_legacy and "comfyui" in only_legacy       # the two harmful cases the port fixes
    assert not NATIVE_REMOVES - LEGACY_REMOVES                                       # native never removes MORE than the script


def test_docker_prune_script_facts_from_the_real_script():
    text = legacy("docker-prune.sh").read_text()
    facts = nv.parse_docker_prune_script(text)
    assert facts == {"retention_h": 168, "cache_max_bytes": 20 * 10 ** 9, "container_prune": True, "image_prune_all": True,
                     "buildx_prune": True, "volume_prune": False}
    assert nv.LEGACY_DOCKER_PRUNE["retention_h"] == facts["retention_h"] and nv.LEGACY_DOCKER_PRUNE["cache_max_bytes"] == facts["cache_max_bytes"]
    svc, timer = unit_text("docker-prune.service"), unit_text("docker-prune.timer")
    assert f"Environment=DOCKER_CONFIG={nv.LEGACY_DOCKER_PRUNE['docker_config']}" in svc
    assert re.search(r"^OnCalendar=Sun \*-\*-\* 04:00:00\s*$", timer, re.M)


def test_parse_docker_prune_script_ignores_comments_and_sees_volume_prune():
    text = ("# docker volume prune would be bad\nRETENTION=72h\nBUILD_CACHE_MAX=1.5GB\n"
            "docker container prune --force \\\n  --filter \"until=$RETENTION\"\n"
            "docker image prune --all --force --filter \"until=$RETENTION\"\n"
            "docker buildx prune --builder x --all --force \\\n --filter \"until=$RETENTION\" --max-used-space \"$BUILD_CACHE_MAX\"\n")
    f = nv.parse_docker_prune_script(text)
    assert (f["retention_h"], f["cache_max_bytes"], f["volume_prune"]) == (72, 1_500_000_000, False)
    assert f["container_prune"] and f["image_prune_all"] and f["buildx_prune"]
    f = nv.parse_docker_prune_script(text + "docker volume prune -f\n")
    assert f["volume_prune"] is True
    assert nv.parse_docker_prune_script("echo nothing") == {
        "retention_h": None, "cache_max_bytes": None, "container_prune": False, "image_prune_all": False, "buildx_prune": False,
        "volume_prune": False}


BUILDX_LS = ("NAME/NODE                   DRIVER/ENDPOINT                   STATUS    BUILDKIT   PLATFORMS\n"
             "immaculaterr-builder*       docker-container                                       \n"
             " \\_ immaculaterr-builder0    \\_ unix:///var/run/docker.sock   running   v0.30.0    linux/amd64 (+3), linux/386\n"
             "default                     docker                                                 \n"
             " \\_ default                  \\_ default                       running   v0.22.0    linux/amd64 (+3), linux/386\n")


def test_docker_prune_script_runs_exactly_these_docker_commands(tmp_path):
    root = tmp_path / "s"
    script = prep_script(legacy("docker-prune.sh"), [("LOG_FILE=/var/log/docker-prune.log", f"LOG_FILE={root}/prune.log")],
                         root / "script.sh")
    (root / "fake").mkdir(parents=True)
    (root / "fake" / "ls").write_text(BUILDX_LS)
    write_bin(root, "docker", 'echo "$*" >> "$FAKE_DIR/docker.log"\ncase "$1 $2" in\n'
              '  "system df") printf "Images=12GB\\nContainers=0B\\n" ;;\n  "buildx ls") cat "$FAKE_DIR/ls" ;;\n'
              '  "buildx du") printf "ID  SIZE\\nTotal:\\t11.5GB\\n" ;;\n  "container prune"|"image prune"|"buildx prune") echo done ;;\n'
              '  *) exit 98 ;;\nesac\n')
    r = bash(script, root)
    assert r.returncode == 0, r.stderr
    calls = (root / "fake" / "docker.log").read_text().splitlines()
    f = "--filter until=168h"
    assert calls == [
        "system df --format {{.Type}}={{.Size}}",
        f"container prune --force {f}",
        f"image prune --all --force {f}",
        "buildx ls",
        "buildx du --builder immaculaterr-builder", f"buildx prune --builder immaculaterr-builder --all --force {f} --max-used-space 20GB",
        "buildx du --builder default", f"buildx prune --builder default --all --force {f} --max-used-space 20GB",
        "system df --format {{.Type}}={{.Size}}"]
    assert not any("volume" in c or c.startswith("system prune") for c in calls)             # volumes are never pruned
    facts = nv.parse_docker_prune_script(script.read_text())
    assert f"until={facts['retention_h']}h" in f and f"{facts['cache_max_bytes'] // 10 ** 9}GB" in calls[5]


def test_buildx_names_parses_json_lines_and_lists(monkeypatch):
    use_sh(monkeypatch, ("docker buildx ls --format json", ok('{"Name":"immaculaterr-builder","Driver":"docker-container"}\n'
                                                              '{"Name":"default","Driver":"docker"}\n')))
    assert nv._buildx_names() == ["default", "immaculaterr-builder"]
    use_sh(monkeypatch, ("docker buildx ls --format json", ok('[{"Name":"a"},{"Name":"b"}]')))
    assert nv._buildx_names() == ["a", "b"]
    use_sh(monkeypatch, ("docker buildx ls --format json", ok("not json")))
    assert nv._buildx_names() is None
    use_sh(monkeypatch, ("docker buildx ls --format json", (1, "", "x")))
    assert nv._buildx_names() is None


@pytest.mark.parametrize("ts,want", [("2026-09-01T12:00:00.123456789Z", 1788264000.123456), ("2026-09-01T12:00:00Z", 1788264000.0),
                                     ("0001-01-01T00:00:00Z", None), ("garbage", None), ("", None)])
def test_docker_ts(ts, want):
    got = nv._docker_ts(ts)
    assert got == want if want is None else abs(got - want) < 1e-3


def test_containers_prune_report_mode_selects_but_removes_nothing(tmp_path, monkeypatch):
    f = docker_world(monkeypatch, PRUNE_TABLE_ROWS)
    res, _ = prune_run(cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    check_result(res)
    assert res.status == "info" and f.rm_calls == [] and res.metrics["mode"] == "report"
    assert res.summary.startswith(f"report: would remove {len(NATIVE_REMOVES)} container(s) stopped >= 7 d; 4 kept")
    assert set(outcomes(tmp_path)) == {"dry-run"} and len(outcomes(tmp_path)) == len(NATIVE_REMOVES)


def test_containers_prune_apply_removes_only_the_selected_with_plain_docker_rm(tmp_path, monkeypatch):
    f = docker_world(monkeypatch, PRUNE_TABLE_ROWS)
    res, ctx = prune_run(apply=True, cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    check_result(res)
    names = {r["id"]: r["name"] for r in PRUNE_TABLE_ROWS}
    removed = {names[c.split()[-1]] for c in f.rm_calls}
    assert removed == NATIVE_REMOVES and res.summary.startswith(f"removed {len(NATIVE_REMOVES)} container(s)")
    assert all(re.fullmatch(r"docker rm [0-9a-f]{64}", c) for c in f.rm_calls)         # no -f, no -v: volumes survive
    assert outcomes(tmp_path).count("done") == len(NATIVE_REMOVES) and res.status == "ok"
    assert not any(" -f" in c or " -v" in c or "--force" in c or "--volumes" in c for c in f.calls)


def test_containers_prune_keep_option_and_default_cap(monkeypatch):
    rows = [crow(i, f"old-{i:02d}", finished_d=10 + i / 100) for i in range(40)] + [crow(99, "keepme", finished_d=40)]
    f = docker_world(monkeypatch, rows)
    res, ctx = prune_run(apply=True, keep=["keepme"])
    assert len(f.rm_calls) == 25 and cid(1099) not in " ".join(f.rm_calls)                 # small default batch; keep= honoured
    assert res.status == "info" and res.metrics["deferred"] == 15 and "15 deferred by cap" in res.summary
    f = docker_world(monkeypatch, rows)
    shutil.rmtree(core.STATE_DIR / "tasks", ignore_errors=True)
    prune_run(apply=True, keep=["keepme"], max_items_per_run=100)
    assert len(f.rm_calls) == 40


def test_containers_prune_oldest_first_and_failed_rm_is_reported_and_isolated(monkeypatch):
    rows = [crow(1, "a-new", finished_d=8), crow(2, "b-old", finished_d=50), crow(3, "c-mid", finished_d=20)]
    f = docker_world(monkeypatch, rows, rm_rc={cid(1002)})
    res, _ = prune_run(apply=True)
    assert [c.split()[-1] for c in f.rm_calls] == [cid(1002), cid(1003), cid(1001)]    # oldest first
    assert res.status == "warn" and "1 failed" in res.summary and res.metrics["failed"] == 1
    assert [i["name"] for i in res.items] == ["b-old", "c-mid", "a-new"]


def test_containers_prune_never_touches_running_paused_or_recent(monkeypatch):
    rows = [crow(1, "r", "running", 30, None), crow(2, "p", "paused", 30, None), crow(3, "x", "restarting", 30, None),
            crow(4, "recent", finished_d=6.9)]
    f = docker_world(monkeypatch, rows)
    res, _ = prune_run(apply=True)
    assert f.rm_calls == [] and res.summary.startswith("removed 0 container(s)")


def test_containers_prune_refuses_an_id_that_is_not_hex(monkeypatch):
    rows = [crow(1, "old", finished_d=40)]
    f = docker_world(monkeypatch, rows)
    real = f.rows[1][1]
    f.rows[1] = (f.rows[1][0], lambda cmd: ok(real(cmd)[1].replace(rows[0]["id"], "--force")))
    res, _ = prune_run(apply=True)
    assert res.status == "skipped" and f.rm_calls == []


@pytest.mark.parametrize("kw", [dict(ps_rc=1), dict(drop_line=True), dict(bad_field=True)])
def test_containers_prune_unsure_inventory_selects_nothing(monkeypatch, kw):
    f = docker_world(monkeypatch, [crow(1, "old", finished_d=40), crow(2, "old2", finished_d=40)], **kw)
    res, _ = prune_run(apply=True)
    assert res.status == "skipped" and f.rm_calls == []


def test_containers_prune_inventory_is_chunked_and_never_asks_for_config(monkeypatch):
    rows = [crow(i, f"c{i:03d}", "running", 1, None) for i in range(230)]
    f = docker_world(monkeypatch, rows)
    inv = nv._containers_inventory()
    assert len(inv) == 230 and len(f.with_prefix("docker container inspect")) == 3
    assert all("Config.Env" not in c and "Config.Cmd" not in c and "json" not in c for c in f.calls)


def test_containers_prune_bad_config_and_pause(monkeypatch):
    f = docker_world(monkeypatch, [crow(1, "old", finished_d=40)])
    for bad in ("7", 0, -1, True, float("nan")):
        res, _ = prune_run(apply=True, stopped_days=bad)
        assert res.status == "skipped"
    assert f.calls == []
    (core.CONF_DIR / "PAUSE").write_text("")
    res, _ = prune_run(apply=True)
    assert f.rm_calls == [] and res.metrics["mode"] == "report"
    (core.CONF_DIR / "PAUSE").unlink()


def test_containers_prune_odd_names_are_never_selected(monkeypatch):
    f = docker_world(monkeypatch, [crow(1, "bad name", finished_d=40), crow(2, "-rf", finished_d=40), crow(3, "ok-name", finished_d=40)])
    prune_run(apply=True)
    assert [c.split()[-1] for c in f.rm_calls] == [cid(1003)]


# --------------------------------------------------------------------------- docker_prune_parity
def write_legacy_script(tmp_path, **repl) -> Path:
    text = legacy("docker-prune.sh").read_text()
    for k, v in repl.items():
        text = text.replace(k, v)
    p = tmp_path / "docker-prune.sh"
    p.write_text(text)
    return p


def parity_run(tmp_path, monkeypatch, *, cache=None, images=None, cont=None, builders=("default",), owner=("immaculaterr-builder",),
               script=None, rows=(), imgs=(), timer=("disabled", "inactive"), timer_next=None, stopped=(), age=True, expected=()):
    """age=False leaves nv._cache_age_supported alone (the real thing: today cleaners.docker_cache has no age option)."""
    cfg = tmp_path / "dockercfg" / "buildx" / "instances"
    cfg.mkdir(parents=True, exist_ok=True)
    for b in owner:
        (cfg / b).write_text("{}")
    world = docker_world(monkeypatch, list(rows), builders=builders, stopped=stopped, images=list(imgs), timer=timer,
                         timer_next=timer_next)
    if age:
        monkeypatch.setattr(nv, "_cache_age_supported", lambda: True)
    tasks = {"docker_cache": cache or {}, "docker_images": images or {}, "docker_containers_prune": cont or {}}
    if expected:
        tasks["failed_units"] = {"expected_stopped_containers": list(expected)}
    res, ctx = step(nv.docker_prune_parity, "docker_prune_parity", NOW, legacy_script=[str(script or write_legacy_script(tmp_path))],
                    legacy_docker_config=str(tmp_path / "dockercfg"), cfg_tasks=tasks)
    parity_run.world = world
    return res, ctx


def verdicts(res) -> dict:
    return {i["behaviour"]: i["verdict"] for i in res.items}


def test_parity_default_config_reports_the_two_real_gaps(tmp_path, monkeypatch):
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "report"})
    check_result(res)
    v = verdicts(res)
    assert res.status == "warn" and res.alert is False and res.metrics["source"] == "script"      # a gap: not green, never paged
    assert v["build cache age filter"] == "gap" and v["builder discovery"] == "gap"        # root cannot see ohmz's builder
    assert v["stopped containers"] == "differs" and v["unused images"] == "differs" and v["build cache cap"] == "differs"
    assert v["volumes"] == "same" and v["schedule"] == "differs"
    assert res.metrics["gaps"] == 2 and res.metrics["cutover_ready"] is False
    assert [i["verdict"] for i in res.items][:2] == ["gap", "gap"]                          # gaps sort first
    assert res.summary.startswith("2 gap(s) vs docker-prune.sh:")


def test_parity_no_gaps_is_ok_and_ready_only_when_every_native_task_applies(tmp_path, monkeypatch):
    builders = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "report", "max_age_hours": 168}, builders=builders)
    assert res.status == "ok" and res.alert is False and res.metrics["gaps"] == 0 and res.metrics["cutover_ready"] is False
    assert "covered natively" in res.summary
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "apply", "max_age_hours": 168}, images={"mode": "apply"},
                        cont={"mode": "apply"}, builders=builders)
    assert res.metrics["cutover_ready"] is True
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "apply", "max_age_hours": 168}, images={"mode": "apply"},
                        cont={"mode": "apply", "enabled": False}, builders=builders)
    assert res.metrics["cutover_ready"] is False                                             # a disabled task is "missing"


def test_parity_cap_and_image_age_verdicts(tmp_path, monkeypatch):
    builders = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch, cache={"high_gib": 30, "max_age_hours": 1}, images={"unused_days": 3}, builders=builders)
    v = verdicts(res)
    assert v["build cache cap"] == "gap" and v["unused images"] == "gap" and res.metrics["gaps"] == 2
    res, _ = parity_run(tmp_path, monkeypatch, cache={"high_gib": 15}, images={"unused_days": 7}, builders=builders)
    assert verdicts(res)["unused images"] == "differs"                                     # exactly the script's age: no gap


def test_parity_uses_baked_constants_when_the_script_is_gone(tmp_path, monkeypatch):
    res, _ = parity_run(tmp_path, monkeypatch, script=tmp_path / "missing.sh")
    assert res.metrics["source"] == "baked" and res.metrics["retention_h"] == 168


def test_parity_notices_a_script_that_prunes_volumes_or_changed_retention(tmp_path, monkeypatch):
    p = write_legacy_script(tmp_path, **{"RETENTION=168h": "RETENTION=24h"})
    p.write_text(p.read_text() + "\ndocker volume prune -f\n")
    res, _ = parity_run(tmp_path, monkeypatch, script=p)
    assert verdicts(res)["volumes"] == "differs" and res.metrics["retention_h"] == 24
    assert verdicts(res)["unused images"] == "differs"                                     # 14 d >= 24 h: native is later


def test_parity_live_comparison_names_what_only_the_script_would_remove(tmp_path, monkeypatch):
    res, _ = parity_run(tmp_path, monkeypatch, rows=PRUNE_TABLE_ROWS)
    note = next(i["note"] for i in res.items if i["behaviour"] == "stopped containers")
    assert note.startswith(f"legacy would remove {len(LEGACY_REMOVES)}, native {len(NATIVE_REMOVES)}; only legacy:")
    res, _ = parity_run(tmp_path, monkeypatch, rows=[])
    assert next(i["note"] for i in res.items if i["behaviour"] == "stopped containers").startswith("legacy would remove 0, native 0")


# --------------------------------------------------------------------------- docker_prune_parity: what the legacy timer would delete
COMFY_IMG = "comfyui-local:tier2-gcc"
ROOT = Path(__file__).resolve().parent.parent


def comfy_rows():
    """The real situation of 2026-10-02: a long-stopped ComfyUI container created 18 days ago and exited yesterday (kept on
    purpose: failed_units.expected_stopped_containers), its hand-built 16 GB image used by nothing else, a running service."""
    return [crow(1, "comfyui", created_d=18, finished_d=1, image=COMFY_IMG), crow(2, "web", "running", 60, None, image="nginx:1")]


def comfy_images(**extra):
    return [irow(COMFY_IMG, age_d=65, size=16 * GIB), irow("nginx:1", age_d=40), irow("old-unused:1", age_d=100, size=5 * MIB),
            irow("young-unused:1", age_d=2), *extra.get("more", [])]


def exposure_run(tmp_path, monkeypatch, **kw):
    kw.setdefault("rows", comfy_rows())
    kw.setdefault("imgs", comfy_images())
    kw.setdefault("timer", ("enabled", "active"))
    kw.setdefault("expected", ["comfyui"])
    return parity_run(tmp_path, monkeypatch, **kw)


def test_parity_reports_what_the_legacy_timer_would_delete_as_information_never_as_status(tmp_path, monkeypatch):
    """REGRESSION (retirement-gate deadlock): the cutover of docker-prune is gated on this task being green (legacy.GREEN = ok/info),
    and the cutover is what ENDS an exposure; the exposure used to make the status `warn` (alert=True), so the gate could not be
    reached while the exposure existed. It is shown (rows, metrics, summary), never a status; docker_prune_exposure pages it."""
    kw = dict(cache={"mode": "apply", "max_age_hours": 168}, images={"mode": "apply"}, cont={"mode": "apply"},
              builders=("default", "immaculaterr-builder"))
    res, _ = exposure_run(tmp_path, monkeypatch, timer_next=NOW + 45.6 * 3600, **kw)
    check_result(res)
    from homelab_maint import legacy
    assert res.status == "info" and res.status in legacy.GREEN and res.alert is False
    assert res.summary.startswith("docker-prune.sh covered natively; docker-prune.timer still ON (next in 45.6 h) and deletes comfyui")
    assert res.metrics["exposed_containers"] == 1 and res.metrics["exposed_images"] == 1 and res.metrics["exposed_gib"] == 16.0
    assert res.metrics["legacy_timer"] == "enabled" and res.metrics["legacy_next_h"] == 45.6
    assert res.metrics["legacy_other_images"] == 1                          # old-unused:1 goes too, but docker_images would do that anyway
    top = res.items[:2]
    assert [i["verdict"] for i in top] == ["exposed", "exposed"]            # exposed rows sort first
    assert top[0]["legacy"] == "comfyui" and top[1]["legacy"] == COMFY_IMG and "keep a copy" in top[1]["note"]
    assert res.metrics["gaps"] == 0 and res.metrics["cutover_ready"] is True and res.metrics["cutover"] == "ready"


def test_parity_with_gaps_and_an_exposure_is_warn_for_the_gaps_and_still_names_the_exposure(tmp_path, monkeypatch):
    res, _ = exposure_run(tmp_path, monkeypatch, timer_next=NOW + 45.6 * 3600)
    check_result(res)
    assert res.status == "warn" and res.alert is False and res.metrics["gaps"] == 2
    assert res.summary.startswith("2 gap(s) vs docker-prune.sh:") and "docker-prune.timer still ON" in res.summary
    assert res.metrics["exposed_containers"] == 1 and res.metrics["cutover"] == "blocked: gaps"


def test_retirement_gate_for_docker_prune_is_green_with_an_exposure_and_red_with_a_gap(tmp_path, monkeypatch):
    """REGRESSION: feed the REAL docker_prune_parity output (exposure present) through the REAL retirement gate, as written in
    etc/legacy-retirement.toml (read, not copied). Exposure alone must not block the cutover; an open gap must."""
    import tomllib
    from homelab_maint import legacy
    item = legacy.parse_inventory(tomllib.loads((ROOT / "etc" / "legacy-retirement.toml").read_text())).get("docker-prune")
    gate = [p for p in item.parity if p.get("kind") == "task" and p.get("name") == "docker_prune_parity"]
    assert len(gate) == 1, "the docker-prune gate is expected to be the docker_prune_parity task; if it moved, re-read this test"
    assert not any("exposure" in str(p.get("name")) for p in item.parity)       # a page must never block the cutover it asks for

    def gate_green(res) -> bool:
        recs = [{"t": NOW - 60, "kind": "task", "task": "docker_prune_parity", "status": res.status, "metrics": {}}]
        src = legacy.Sources(now=lambda: NOW, history=lambda since, kind: [r for r in recs if r["kind"] == kind])
        checks = legacy.run_check(gate[0], src)
        return bool(checks) and all(c.ok for c in checks)

    both = ("default", "immaculaterr-builder")
    exposed, _ = exposure_run(tmp_path, monkeypatch, cache={"max_age_hours": 168}, builders=both, timer_next=NOW + 40 * 3600)
    assert exposed.metrics["exposed_containers"] == 1 and exposed.metrics["gaps"] == 0 and gate_green(exposed) is True
    gap, _ = exposure_run(tmp_path, monkeypatch, timer_next=NOW + 40 * 3600)
    assert gap.metrics["exposed_containers"] == 1 and gap.metrics["gaps"] > 0 and gate_green(gap) is False
    clean, _ = parity_run(tmp_path, monkeypatch, cache={"max_age_hours": 168}, builders=both)
    assert clean.status == "ok" and gate_green(clean) is True


def test_parity_no_alert_once_the_legacy_timer_is_off(tmp_path, monkeypatch):
    for timer in (("disabled", "inactive"), ("masked", "inactive"), ("not-found", "inactive"), ("disabled", "")):
        res, _ = exposure_run(tmp_path, monkeypatch, timer=timer)
        assert res.alert is False and res.metrics["exposed_containers"] == 0 and res.metrics["exposed_images"] == 0, timer
        assert verdicts(res)["legacy DELETES containers"] == "differs"      # still listed, no longer a danger
        assert " ON " not in res.summary and res.metrics["legacy_timer"] == timer[0]


@pytest.mark.parametrize("timer", [("enabled", "active"), ("enabled", "inactive"), ("disabled", "active"), ("static", "active"),
                                   ("enabled-runtime", "inactive"), ("alias", "inactive")])
def test_parity_enabled_or_running_timer_is_live(tmp_path, monkeypatch, timer):
    res, _ = exposure_run(tmp_path, monkeypatch, timer=timer)
    assert res.metrics["exposed_containers"] == 1 and verdicts(res)["legacy DELETES containers"] == "exposed" and res.alert is False


def test_parity_unknown_timer_state_is_treated_as_live(tmp_path, monkeypatch):
    """Fail closed: if systemd cannot say, assume the script will run."""
    exposure_run(tmp_path, monkeypatch)
    keep = [(k, v) for k, v in parity_run.world.rows if not k.startswith("systemctl is-")]
    f = use_sh(monkeypatch, *keep, ("systemctl is-", (1, "", "Failed to connect to bus")))
    res, _ = step(nv.docker_prune_parity, "docker_prune_parity", NOW, legacy_script=[str(tmp_path / "docker-prune.sh")],
                  legacy_docker_config=str(tmp_path / "dockercfg"), cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    assert res.metrics["exposed_containers"] == 1 and res.metrics["legacy_timer"] == "unknown" and res.metrics["legacy_next_h"] == -1


def test_parity_nothing_exposed_when_native_removes_what_legacy_removes(tmp_path, monkeypatch):
    rows = [crow(1, "scratch", created_d=18, finished_d=10, image="busybox:1")]
    res, _ = exposure_run(tmp_path, monkeypatch, rows=rows, imgs=[irow("busybox:1", 30)], expected=[])
    assert res.metrics["exposed_containers"] == 0 and res.alert is False
    assert "legacy DELETES containers" not in verdicts(res)
    assert parity_run.world.with_prefix("docker image") == []              # no image inventory when nothing can be exposed


def test_parity_an_image_a_surviving_container_uses_is_not_exposed(tmp_path, monkeypatch):
    rows = comfy_rows() + [crow(3, "keeper", "running", 60, None, image=COMFY_IMG)]
    res, _ = exposure_run(tmp_path, monkeypatch, rows=rows)
    assert res.metrics["exposed_containers"] == 1 and res.metrics["exposed_images"] == 0    # the container is exposed, its image is not
    assert "deletes comfyui" in res.summary and "image" not in res.summary.split("deletes comfyui")[1]


def test_parity_image_inventory_unavailable_still_names_the_container(tmp_path, monkeypatch):
    exposure_run(tmp_path, monkeypatch)
    rows = [(k, v) if k != "docker image ls --all --quiet --no-trunc" else (k, (1, "", "boom")) for k, v in parity_run.world.rows]
    use_sh(monkeypatch, *rows)
    res, _ = step(nv.docker_prune_parity, "docker_prune_parity", NOW, legacy_script=[str(tmp_path / "docker-prune.sh")],
                  legacy_docker_config=str(tmp_path / "dockercfg"), cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    assert "deletes comfyui" in res.summary and res.metrics["exposed_containers"] == 1 and res.metrics["exposed_images"] == 0
    assert verdicts(res)["legacy DELETES images"] == "exposed" and "unavailable" in next(
        i["note"] for i in res.items if i["behaviour"] == "legacy DELETES images")


def test_parity_cutover_metric_says_why_it_is_not_ready(tmp_path, monkeypatch):
    ok_b = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch)
    assert res.metrics["cutover"] == "blocked: gaps" and res.metrics["cutover_ready"] is False
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "report", "max_age_hours": 168}, builders=ok_b)
    assert res.metrics["cutover"] == "blocked: natives not in apply mode"
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "apply", "max_age_hours": 168}, images={"mode": "apply"},
                        cont={"mode": "apply"}, builders=ok_b)
    assert res.metrics["cutover"] == "ready" and res.metrics["cutover_ready"] is True


# --------------------------------------------------------------------------- docker_prune_exposure: the page, in the CHECK tier
def exp_run(tmp_path, monkeypatch, *, now=NOW, rows=None, imgs=None, timer=("enabled", "active"), timer_next="soon",
            expected=("comfyui",), tasks=None, **opts):
    """One docker_prune_exposure run on a fake docker/systemd. timer_next: 'soon' = NOW + 39.8 h (the real Sunday 04:13), epoch, or None."""
    nxt = NOW + 39.8 * 3600 if timer_next == "soon" else timer_next
    world = docker_world(monkeypatch, comfy_rows() if rows is None else list(rows), images=comfy_images() if imgs is None else list(imgs),
                         timer=timer, timer_next=nxt)
    cfg_tasks = {"failed_units": {"expected_stopped_containers": list(expected)}, **(tasks or {})}
    res, ctx = step(nv.docker_prune_exposure, "docker_prune_exposure", now, legacy_script=[str(tmp_path / "no-such-script")],
                    cfg_tasks=cfg_tasks, **opts)
    exp_run.world = world
    return res, ctx


def test_exposure_pages_at_first_sight_names_what_goes_and_what_to_do(tmp_path, monkeypatch, sandbox):
    """REGRESSION (high, live): docker-prune.timer (Sun 04:13) deletes the intentionally stopped comfyui container and then the 16 GB
    hand-built image. The check lived in a WEEKLY task whose status needed two more confirming runs: the first page would have come
    up to two weeks after a deliberate stop. This is a check-tier task that pages at the first run that sees it."""
    res, ctx = exp_run(tmp_path, monkeypatch)
    check_result(res)
    assert res.status == "crit" and res.alert is True
    assert res.summary == f"docker-prune.timer ON (next in 39.8 h): deletes container comfyui + image {COMFY_IMG} (16.0 GiB); natives keep"
    m = res.metrics
    assert (m["exposed_containers"], m["exposed_images"], m["exposed_gib"], m["legacy_next_h"], m["imminent"]) == (1, 1, 16.0, 39.8, False)
    assert m["sig_page"] == "sent" and m["transition"] == "alert" and m["legacy_timer"] == "enabled"
    assert [(i["what"], i["name"]) for i in res.items] == [("container", "comfyui"), ("image", COMFY_IMG)]
    [ev] = sandbox
    assert (ev["kind"], ev["severity"], ev["status"], ev["task"]) == ("alert", "crit", "crit", "docker_prune_exposure")
    assert ev["dedupe_key"] == "docker_prune_exposure" and ev["summary"].isascii() and len(ev["summary"]) <= 130
    assert ev["title"] == f"docker-prune.timer will delete a stopped container on {HOST}"
    for needle in ("docker start comfyui", "sudo systemctl stop docker-prune.timer", "homelab-maint migrate cutover docker-prune",
                   "docker save", COMFY_IMG, "16.0 GiB", "homelab-maint changed nothing", "CREATION time"):
        assert needle in ev["details"], needle
    assert ctx.state["signature"] == f"c:comfyui,i:{COMFY_IMG}" and "lead" in ctx.state


def test_exposure_task_is_read_only_and_asks_docker_and_systemd_only_to_read(tmp_path, monkeypatch):
    exp_run(tmp_path, monkeypatch)
    f = exp_run.world
    allowed = ("docker ps", "docker container inspect --format", "docker image ls", "docker image inspect --format", "systemctl is-enabled",
               "systemctl is-active", "systemctl list-timers")
    assert f.calls and all(c.startswith(allowed) for c in f.calls), f.calls
    verbs = {"rm", "rmi", "stop", "start", "restart", "kill", "pause", "unpause", "update", "prune", "disable", "enable", "mask", "unmask",
             "daemon-reload", "reset-failed", "set-property"}
    assert not any(verbs & set(c.split()[:3]) for c in f.calls), f.calls      # no verb that changes anything (unit NAMES may say "prune")
    assert not (tmp_path / "log" / "audit.jsonl").exists()                   # C0: nothing went through ctx.act
    assert core.REGISTRY["docker_prune_exposure"].klass == "C0"


def test_exposure_severity_follows_how_soon_the_timer_fires(tmp_path, monkeypatch, sandbox):
    res, _ = exp_run(tmp_path, monkeypatch, timer_next=NOW + 100 * 3600)
    assert res.status == "warn" and sandbox[-1]["severity"] == "warn" and sandbox[-1]["status"] == "warn"
    assert sandbox[-1]["dedupe_key"] == "docker_prune_exposure"
    shutil.rmtree(core.STATE_DIR / "tasks")
    res, _ = exp_run(tmp_path, monkeypatch, timer_next=NOW + 71 * 3600)
    assert res.status == "crit"                                              # inside crit_within_h = 72
    shutil.rmtree(core.STATE_DIR / "tasks")
    res, _ = exp_run(tmp_path, monkeypatch, timer_next=NOW + 100 * 3600, crit_within_h=200)
    assert res.status == "crit"                                              # configurable
    shutil.rmtree(core.STATE_DIR / "tasks")
    res, _ = exp_run(tmp_path, monkeypatch, timer_next=None)                 # live but systemd gives no next run: unknown = crit
    assert res.status == "crit" and res.metrics["legacy_next_h"] == -1 and "(enabled)" in res.summary


def test_exposure_pages_once_more_when_the_timer_turns_imminent(tmp_path, monkeypatch, sandbox):
    nxt = NOW + 39.8 * 3600
    exp_run(tmp_path, monkeypatch, now=NOW, timer_next=nxt)
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 900, timer_next=nxt)
    assert res.metrics["sig_page"] == "" and len(sandbox) == 1               # steady: quiet
    res, ctx = exp_run(tmp_path, monkeypatch, now=nxt - 5.9 * 3600, timer_next=nxt)           # inside imminent_h = 6
    assert res.metrics["imminent"] is True and res.metrics["sig_page"] == "sent" and len(sandbox) == 2
    assert sandbox[1]["dedupe_key"].startswith("docker_prune_exposure:") and sandbox[1]["dedupe_key"].endswith(":1")
    assert sandbox[1]["severity"] == "crit" and "next in 5.9 h" in sandbox[1]["summary"]
    res, ctx = exp_run(tmp_path, monkeypatch, now=nxt - 5.5 * 3600, timer_next=nxt)
    assert res.metrics["sig_page"] == "" and len(sandbox) == 2               # and then quiet again


def test_exposure_a_second_exposed_container_is_its_own_page(tmp_path, monkeypatch, sandbox):
    exp_run(tmp_path, monkeypatch)
    rows = comfy_rows() + [crow(3, "second", created_d=40, finished_d=2, image="x/y:1")]
    exp_run(tmp_path, monkeypatch, now=NOW + 900, rows=rows, imgs=comfy_images(more=[irow("x/y:1", 50)]), expected=("comfyui", "second"))
    assert len(sandbox) == 2 and "second" in sandbox[1]["details"] and sandbox[1]["dedupe_key"].endswith(":1")


def test_exposure_timer_off_is_ok_and_docker_is_not_even_asked(tmp_path, monkeypatch, sandbox):
    for timer in (("disabled", "inactive"), ("masked", "inactive"), ("not-found", "inactive")):
        res, _ = exp_run(tmp_path, monkeypatch, timer=timer, timer_next=None)
        check_result(res)
        assert res.status == "ok" and res.metrics["exposed_containers"] == 0 and f"is {timer[0]}" in res.summary, timer
        assert exp_run.world.with_prefix("docker") == [], timer
    assert sandbox == []


def test_exposure_unknown_timer_state_is_exposure(tmp_path, monkeypatch, sandbox):
    """Fail closed: systemd cannot say => assume the script will run."""
    exp_run(tmp_path, monkeypatch, timer=("weird", "weird"))
    assert sandbox and sandbox[0]["severity"] == "crit"


def test_exposure_docker_unavailable_is_skipped_and_leaves_the_announcement_alone(tmp_path, monkeypatch, sandbox):
    exp_run(tmp_path, monkeypatch)
    before = json.loads((core.STATE_DIR / "tasks" / "docker_prune_exposure.json").read_text())
    use_sh(monkeypatch, ("systemctl is-enabled", ok("enabled\n")), ("systemctl is-active", ok("active\n")),
           ("systemctl list-timers", ok("[]")), ("docker", (1, "", "Cannot connect to the Docker daemon")))
    res, _ = step(nv.docker_prune_exposure, "docker_prune_exposure", NOW + 900, legacy_script=[str(tmp_path / "x")])
    assert res.status == "skipped" and "exposure unknown" in res.summary and len(sandbox) == 1
    assert json.loads((core.STATE_DIR / "tasks" / "docker_prune_exposure.json").read_text()) == before


def test_exposure_nothing_exposed_when_the_natives_remove_what_the_script_removes(tmp_path, monkeypatch, sandbox):
    rows = [crow(1, "scratch", created_d=18, finished_d=10, image="busybox:1")]
    res, _ = exp_run(tmp_path, monkeypatch, rows=rows, imgs=[irow("busybox:1", 30)], expected=())
    assert res.status == "ok" and "deletes nothing the natives keep" in res.summary and sandbox == []
    assert exp_run.world.with_prefix("docker image") == []


def test_exposure_reads_keep_and_unprotect_from_the_prune_task_not_its_own(tmp_path, monkeypatch, sandbox):
    """REGRESSION: _expected_stopped read `keep` (and the parity task's is_protected) from the CALLING task's table, so a container
    kept through [tasks.docker_containers_prune].keep looked removable to the native selection, i.e. NOT exposed: the page for it
    was missed. `unprotect` likewise decides what the native task removes."""
    rows = [crow(1, "stale-job", created_d=40, finished_d=10, image="busybox:1"), crow(2, "tunarr-old", created_d=40, finished_d=10, image="busybox:2")]
    imgs = [irow("busybox:1", 60), irow("busybox:2", 60)]
    res, _ = exp_run(tmp_path, monkeypatch, rows=rows, imgs=imgs, expected=(), tasks={"docker_containers_prune": {"keep": ["stale-job"]}})
    assert res.status == "crit" and res.metrics["exposed_containers"] == 2          # kept by config + protected name (tunarr)
    shutil.rmtree(core.STATE_DIR / "tasks")
    sandbox.clear()
    res, _ = exp_run(tmp_path, monkeypatch, rows=rows, imgs=imgs, expected=(),
                     tasks={"docker_containers_prune": {"keep": ["stale-job"], "unprotect": ["^tunarr-old$"]}})
    assert res.metrics["exposed_containers"] == 1 and "stale-job" in res.summary and "tunarr-old" not in res.summary
    assert nv._expected_stopped(mk("docker_prune_exposure", cfg_tasks={"docker_containers_prune": {"keep": ["a"]}})) == {"a"}


def test_exposure_summary_stays_one_sms_with_many_exposures(tmp_path, monkeypatch, sandbox):
    rows = [crow(i, f"long-lived-service-{i}", created_d=40, finished_d=2, image=f"registry.example/team/app-{i}:2026.10.01") for i in range(1, 9)]
    imgs = [irow(f"registry.example/team/app-{i}:2026.10.01", 50, 1 * GIB) for i in range(1, 9)]
    res, _ = exp_run(tmp_path, monkeypatch, rows=rows, imgs=imgs, expected=[r["name"] for r in rows], timer_next=NOW + 3600)
    check_result(res)
    assert res.metrics["exposed_containers"] == 8 and res.metrics["exposed_images"] == 8 and "+5" in res.summary
    assert res.summary.startswith("docker-prune.timer ON (next in 60 min)") and len(res.items) == 12
    assert len(sandbox[0]["summary"]) <= 130 and sandbox[0]["summary"].isascii() and res.metrics["imminent"] is True


def test_exposure_the_same_world_gives_the_same_answer_as_the_parity_report(tmp_path, monkeypatch):
    p, _ = exposure_run(tmp_path, monkeypatch, timer_next=NOW + 39.8 * 3600)
    e, _ = exp_run(tmp_path, monkeypatch)
    for k in ("exposed_containers", "exposed_images", "exposed_gib", "legacy_timer", "legacy_next_h"):
        assert p.metrics[k] == e.metrics[k], k


def test_exposure_a_blip_the_notifier_never_paged_is_closed_by_the_task(tmp_path, monkeypatch, sandbox):
    """The owner starts the container (or stops the timer) before the Notifier's confirming run: it never paged, so it will never
    send a recovery; the task closes the announcement itself."""
    exp_run(tmp_path, monkeypatch)
    started = [crow(1, "comfyui", "running", 18, None, image=COMFY_IMG), crow(2, "web", "running", 60, None, image="nginx:1")]
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 900, rows=started)
    assert res.status == "ok" and res.metrics["sig_page"] == "closed" and ctx.state["signature"] == "OK" and "lead" not in ctx.state
    assert [e["kind"] for e in sandbox] == ["alert", "recovery"] and sandbox[1]["dedupe_key"] == "docker_prune_exposure"
    exp_run(tmp_path, monkeypatch, now=NOW + 1800, rows=started)
    assert len(sandbox) == 2                                                  # once
    exp_run(tmp_path, monkeypatch, now=NOW + 3600)                            # stopped again: a NEW incident, paged again
    assert [e["kind"] for e in sandbox] == ["alert", "recovery", "alert"]


def test_exposure_stopping_the_legacy_timer_closes_the_announcement(tmp_path, monkeypatch, sandbox):
    exp_run(tmp_path, monkeypatch)
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 900, timer=("disabled", "inactive"), timer_next=None)
    assert res.status == "ok" and res.metrics["sig_page"] == "closed" and [e["kind"] for e in sandbox] == ["alert", "recovery"]
    assert "docker-prune.timer no longer deletes anything the natives keep (disabled)" == sandbox[1]["summary"]


def test_exposure_an_incident_the_notifier_paged_keeps_its_notifier_recovery(tmp_path, monkeypatch, sandbox):
    exp_run(tmp_path, monkeypatch)
    core.write_json_atomic(core.STATE_DIR / "alerts.json", {"tasks": {"docker_prune_exposure": {"alerted": 2}}})
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 900, timer=("disabled", "inactive"), timer_next=None)
    assert res.status == "ok" and res.metrics["sig_page"] == "" and "lead" not in ctx.state and [e["kind"] for e in sandbox] == ["alert"]


def test_exposure_a_page_notify_did_not_accept_is_retried_next_run(tmp_path, monkeypatch, sandbox):
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), {"ok": False, "handled": False, "rc": 1, "note": "smtp down"})[1])
    res, ctx = exp_run(tmp_path, monkeypatch)
    assert res.metrics["sig_page"] == "retry" and "signature" not in ctx.state
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 900)
    assert res.metrics["sig_page"] == "retry" and len(sandbox) == 2
    monkeypatch.setattr(nv, "_notify", lambda ev: (sandbox.append(ev), dict(OK_SENT))[1])
    res, ctx = exp_run(tmp_path, monkeypatch, now=NOW + 1800)
    assert res.metrics["sig_page"] == "sent" and ctx.state["signature"].startswith("c:comfyui") and len(sandbox) == 3


def test_exposure_first_sight_page_can_be_turned_off(tmp_path, monkeypatch, sandbox):
    res, ctx = exp_run(tmp_path, monkeypatch, first_sight_page=False)
    assert res.status == "crit" and res.metrics["sig_page"] == "" and sandbox == []           # the Notifier alone pages it, when it confirms


def test_exposure_bad_config_falls_back_to_defaults_and_says_so(tmp_path, monkeypatch):
    res, _ = exp_run(tmp_path, monkeypatch, crit_within_h="soon", imminent_h=-1)
    assert res.status == "crit" and res.metrics["bad_config"] == "crit_within_h,imminent_h"


def test_exposure_real_stack_pages_in_one_tick_and_the_notifiers_confirmation_is_swallowed(tmp_path, monkeypatch, sandbox):
    """End to end through HermesNotifier -> notify.send: the deliberate `docker stop` is paged (SMS + email, crit) at the first
    15-minute tick; the page the Notifier sends when it confirms is swallowed by notify's dedupe window; the page when it turns
    imminent is a new, delivered message; the reminder comes a day later."""
    wire = Wire(monkeypatch)
    nxt = NOW + 39.8 * 3600
    log = []
    for t in (0, 900, 1800, 26 * 3600, nxt - NOW - 5.9 * 3600, nxt - NOW - 5.6 * 3600):
        wire.now = NOW + t
        before = len(wire.msgs)
        res, _ = exp_run(tmp_path, monkeypatch, now=NOW + t, timer_next=nxt)
        hermes_pages(wire, res, NOW + t, 2, name="docker_prune_exposure", title="docker-prune.timer exposure")
        log.append([(m["kind"], m["key"].split(":")[0], m["severity"]) for m in wire.msgs[before:]])
    assert log[0] == [("alert", "docker_prune_exposure", "crit")] and "sms" in wire.msgs[0]["channels"]
    assert log[1] == [] and log[2] == []                                      # the Notifier confirmed at run 2: nothing new for the owner
    assert log[3] == [("alert", "docker_prune_exposure", "crit")]             # 26 h later: the Notifier's reminder
    assert log[4] == [("alert", "docker_prune_exposure", "crit")]             # imminent (< 6 h): a new announcement
    assert log[5] == []
    assert alerts_state("docker_prune_exposure")["alerted"] == 2
    assert "docker-prune.timer will delete a stopped container" in wire.msgs[0]["subject"]
    assert "docker start comfyui" in wire.msgs[0]["plain"]


def test_exposure_real_stack_recovery_after_the_owner_acts(tmp_path, monkeypatch, sandbox):
    wire = Wire(monkeypatch)
    log = []
    started = [crow(1, "comfyui", "running", 18, None, image=COMFY_IMG), crow(2, "web", "running", 60, None, image="nginx:1")]
    for t, rows in ((0, None), (900, None), (1800, started), (2700, started)):
        wire.now = NOW + t
        before = len(wire.msgs)
        res, _ = exp_run(tmp_path, monkeypatch, now=NOW + t, rows=rows)
        hermes_pages(wire, res, NOW + t, 2, name="docker_prune_exposure", title="docker-prune.timer exposure")
        log.append([(m["kind"], m["key"]) for m in wire.msgs[before:]])
    assert log == [[("alert", "docker_prune_exposure")], [], [], [("recovery", "docker_prune_exposure")]]


# --------------------------------------------------------------------------- docker_prune_parity: the build-cache age option and builders
def test_parity_age_option_the_task_ignores_is_a_gap(tmp_path, monkeypatch):
    """REGRESSION: `max_age_hours` present in the config made the verdict `same`, but cleaners.docker_cache did not implement
    it: gaps went to 0 and the cutover gate went through while the age filter was silently ignored."""
    builders = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch, cache={"mode": "apply", "max_age_hours": 168}, images={"mode": "apply"},
                        cont={"mode": "apply"}, builders=builders, age=False)
    row = next(i for i in res.items if i["behaviour"] == "build cache age filter")
    assert nv._cache_age_supported() is False                       # the real cleaners module: no such option today
    assert row["verdict"] == "gap" and "ignores it" in row["native"] and "not implemented" in row["note"]
    assert res.metrics["gaps"] == 1 and res.metrics["cutover_ready"] is False and res.status == "warn"
    res, _ = parity_run(tmp_path, monkeypatch, builders=builders, age=False)
    assert "docker_cache has no age option yet" in next(i["note"] for i in res.items if i["behaviour"] == "build cache age filter")


def test_parity_age_option_the_task_implements(tmp_path, monkeypatch):
    builders = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch, cache={"max_age_hours": 168}, builders=builders)
    assert verdicts(res)["build cache age filter"] == "same" and res.metrics["gaps"] == 0
    res, _ = parity_run(tmp_path, monkeypatch, cache={"max_age_hours": 400}, builders=builders)
    row = next(i for i in res.items if i["behaviour"] == "build cache age filter")
    assert row["verdict"] == "differs" and "longer" in row["note"] and res.metrics["gaps"] == 0
    res, _ = parity_run(tmp_path, monkeypatch, builders=builders)
    assert "add max_age_hours" in next(i["note"] for i in res.items if i["behaviour"] == "build cache age filter")


def test_reads_option_looks_at_code_not_at_words():
    src = 'def f(ctx):\n    """max_age_hours is planned"""\n    # ctx.opt("max_age_hours")\n    x = ctx.opt("high_gib", 15)\n'
    assert nv._reads_option(src, "max_age_hours") is False
    assert nv._reads_option(src + '    y = ctx.opt("max_age_hours", 168)\n', "max_age_hours") is True
    assert nv._reads_option("    def f(self, ctx):\n        return ctx.opt('k')\n", "k") is True       # indented source (methods)
    assert nv._reads_option("def f(:", "k") is False and nv._reads_option("", "k") is False
    assert nv._reads_option('opt("k")', "k") is False                                                    # a bare call is not ctx.opt


def test_cache_age_support_follows_the_real_function_or_a_capability_table(monkeypatch):
    import inspect
    from homelab_maint.tasks import cleaners
    assert nv._cache_age_supported() == nv._reads_option(inspect.getsource(cleaners.docker_cache), "max_age_hours")
    monkeypatch.setattr(cleaners, "CAPABILITIES", {"docker_cache": {"max_age_hours", "high_gib"}}, raising=False)
    assert nv._cache_age_supported() is True
    monkeypatch.setattr(cleaners, "CAPABILITIES", {"docker_cache": {"high_gib"}}, raising=False)
    assert nv._cache_age_supported() is False
    monkeypatch.setattr(cleaners, "CAPABILITIES", {"docker_images": {"x"}}, raising=False)        # no entry for it: inspect the code
    assert nv._cache_age_supported() == nv._reads_option(inspect.getsource(cleaners.docker_cache), "max_age_hours")
    monkeypatch.setattr(inspect, "getsource", lambda f: (_ for _ in ()).throw(OSError("no source")))
    monkeypatch.delattr(cleaners, "CAPABILITIES")
    assert nv._cache_age_supported() is False                                                      # unknown = no


def test_parity_builder_discovery_counts_only_running_builders(tmp_path, monkeypatch):
    """REGRESSION: every `docker buildx ls` name was 'visible', but cleaners._builders() prunes RUNNING builders only."""
    both = ("default", "immaculaterr-builder")
    res, _ = parity_run(tmp_path, monkeypatch, cache={"max_age_hours": 168}, builders=both, stopped=("immaculaterr-builder",))
    row = next(i for i in res.items if i["behaviour"] == "builder discovery")
    assert row["verdict"] == "gap" and "immaculaterr-builder" in row["note"] and "runner prunes running: default" in row["native"]
    res, _ = parity_run(tmp_path, monkeypatch, cache={"max_age_hours": 168}, builders=both)
    assert verdicts(res)["builder discovery"] == "same"


def test_buildx_names_running_only(monkeypatch):
    use_sh(monkeypatch, ("docker buildx ls --format json",
                         ok('{"Name":"a","Nodes":[{"Status":"running"}]}\n{"Name":"b","Nodes":[{"Status":"inactive"}]}\n'
                            '{"Name":"c"}\n{"Name":"bad name","Nodes":[{"Status":"running"}]}\n')))
    assert nv._buildx_names() == ["a", "b", "bad name", "c"]
    assert nv._buildx_names(running_only=True) == ["a"]


# --------------------------------------------------------------------------- the legacy image prune model and the inventories
def test_legacy_image_prune_set_semantics():
    now = NOW
    used, unused_old, unused_young = irow("used:1", 60), irow("old:1", 60), irow("young:1", 3)
    got = nv.legacy_image_prune_set([used, unused_old, unused_young], {used["id"]}, now, 168)
    assert [i["tags"] for i in got] == [["old:1"]]                            # in use stays; younger than 7 d stays
    edge = irow("edge:1", 7)
    assert nv.legacy_image_prune_set([edge], set(), now, 168) == [edge]       # exactly 168 h old: eligible (the script's `until`)
    assert nv.legacy_image_prune_set([irow("e:1", 7 - 0.01)], set(), now, 168) == []
    unknown = {**irow("odd:1", 60), "created": None}
    assert nv.legacy_image_prune_set([unknown], set(), now, 168) == []        # unknown age: kept (fail closed)


def test_legacy_image_prune_set_parent_chain_is_protected():
    base = irow("base:1", 90)
    child_used = irow("child:1", 60, parent=base["id"])
    assert nv.legacy_image_prune_set([base, child_used], {child_used["id"]}, NOW, 168) == []           # parent of an image in use
    young_child = irow("fresh:1", 2, parent=base["id"])
    assert nv.legacy_image_prune_set([base, young_child], set(), NOW, 168) == []                       # a young child keeps its parent
    old_child = irow("oldchild:1", 60, parent=base["id"])
    assert {i["tags"][0] for i in nv.legacy_image_prune_set([base, old_child], set(), NOW, 168)} == {"base:1", "oldchild:1"}
    loop = [{**irow("a:1", 60), "parent": iid("b:1")}, {**irow("b:1", 60), "parent": iid("a:1")}]
    assert len(nv.legacy_image_prune_set(loop, set(), NOW, 168)) == 2                                   # a cycle cannot hang it


def test_legacy_image_exposure_splits_consequence_images_from_unused_ones():
    rows = comfy_rows()
    exposed, other = nv.legacy_image_exposure(rows, comfy_images(), ["comfyui"], NOW, 168)
    assert [_i["tags"] for _i in exposed] == [[COMFY_IMG]] and [_i["tags"] for _i in other] == [["old-unused:1"]]
    exposed, other = nv.legacy_image_exposure(rows, comfy_images(), [], NOW, 168)
    assert exposed == []                                                      # nothing exposed when native removes the same containers


def test_images_inventory_parses_untagged_and_multi_tag_images(monkeypatch):
    imgs = [irow("a:1", 40, 5 * MIB), irow(None, 3, 7, tag="x"), {**irow("multi:1", 20), "tags": ["multi:1", "multi:latest"]},
            irow("child:1", 9, parent=iid("a:1"))]
    docker_world(monkeypatch, [], images=imgs)
    got = nv._images_inventory()
    assert [(g["id"], g["tags"], g["size"], g["parent"]) for g in got] == sorted(
        [(i["id"], i["tags"], i["size"], i["parent"]) for i in imgs])
    assert all(abs(g["created"] - next(i["created"] for i in imgs if i["id"] == g["id"])) < 1e-3 for g in got)


def test_images_inventory_is_chunked_and_asks_for_no_config(monkeypatch):
    imgs = [irow(f"img-{i:03d}:1", 40) for i in range(230)]
    f = docker_world(monkeypatch, [], images=imgs)
    assert len(nv._images_inventory()) == 230 and len(f.with_prefix("docker image inspect")) == 3
    assert all("Config" not in c and "Env" not in c and "Cmd" not in c for c in f.calls)


def test_images_inventory_fails_closed(monkeypatch):
    docker_world(monkeypatch, [], images=[irow("a:1", 40)], images_rc=1)
    assert nv._images_inventory() is None
    f = docker_world(monkeypatch, [], images=[irow("a:1", 40), irow("b:1", 40)])
    real = f.rows[4][1]
    f.rows[4] = (f.rows[4][0], lambda cmd: ok(real(cmd)[1].splitlines()[0] + "\n"))                     # one line short
    assert nv._images_inventory() is None
    f = docker_world(monkeypatch, [], images=[irow("a:1", 40)])
    real = f.rows[4][1]
    f.rows[4] = (f.rows[4][0], lambda cmd: ok(real(cmd)[1].replace("|null", "|not-json").replace('|["a:1"]', "|not-json")))
    assert nv._images_inventory() is None
    use_sh(monkeypatch, ("docker image ls", ok("--force\n")))                                            # an id that is an option
    assert nv._images_inventory() is None


def test_legacy_timer_states(monkeypatch):
    def timer(e, a):
        use_sh(monkeypatch, ("systemctl is-enabled", (0, e + "\n", "")), ("systemctl is-active", (0, a + "\n", "")))
        return nv._legacy_timer("docker-prune.timer")
    assert timer("enabled", "active") == (True, "enabled") and timer("disabled", "active") == (True, "active")
    assert timer("disabled", "inactive") == (False, "disabled") and timer("masked", "inactive") == (False, "masked")
    assert timer("weird", "inactive") == (None, "unknown")
    use_sh(monkeypatch)
    assert nv._legacy_timer("docker-prune.timer") == (None, "unknown")


def test_parity_option_legacy_timer_is_validated(tmp_path, monkeypatch):
    exposure_run(tmp_path, monkeypatch)
    res, _ = step(nv.docker_prune_parity, "docker_prune_parity", NOW, legacy_script=[str(tmp_path / "docker-prune.sh")],
                  legacy_docker_config=str(tmp_path / "dockercfg"), legacy_timer="x; rm -rf /",
                  cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    assert not any("rm -rf" in c for c in parity_run.world.calls) and res.metrics["exposed_containers"] == 1
    res, _ = step(nv.docker_prune_exposure, "docker_prune_exposure", NOW, legacy_script=[str(tmp_path / "x")], legacy_timer="x; rm -rf /",
                  cfg_tasks={"failed_units": {"expected_stopped_containers": ["comfyui"]}})
    assert not any("rm -rf" in c for c in parity_run.world.calls) and res.status == "crit"      # the exposure task validates it too


def test_live_docker_prune_exposure_readonly_smoke(monkeypatch):
    """Reads this host's docker and systemd (ps, inspect, image ls/inspect, is-enabled): nothing is changed."""
    if shutil.which("docker") is None or shutil.which("systemctl") is None:
        pytest.skip("no docker/systemctl")
    monkeypatch.setattr(nv, "sh", REAL_SH)
    rows, images = nv._containers_inventory(), nv._images_inventory()
    if rows is None or images is None:
        pytest.skip("docker is not usable here")
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", r["image_id"]) for r in rows) and all(i["tags"] is not None for i in images)
    only = sorted(set(nv.legacy_container_prune_set(rows, time.time(), 168)))
    exposed, other = nv.legacy_image_exposure(rows, images, only, time.time(), 168)
    assert len(exposed) + len(other) <= len(images)
    live, word = nv._legacy_timer("docker-prune.timer")
    assert live in (True, False, None) and isinstance(word, str)


def test_parity_docker_down_is_a_gap_not_a_crash(tmp_path, monkeypatch):
    use_sh(monkeypatch)                                              # every command unmocked => rc 127
    res, _ = step(nv.docker_prune_parity, "docker_prune_parity", NOW, legacy_script=[str(write_legacy_script(tmp_path))],
                  legacy_docker_config=str(tmp_path / "nowhere"))
    check_result(res)
    assert res.status == "warn" and res.alert is False and verdicts(res)["builder discovery"] == "gap"


# =========================================================================== smart_event  <-  smart-alert.sh
SMART_ENV = {"SMARTD_DEVICE": "/dev/sda", "SMARTD_DEVICETYPE": "sat", "SMARTD_FAILTYPE": "CurrentPendingSector",
             "SMARTD_MESSAGE": "Device: /dev/sda [SAT], 8 Currently unreadable (pending) sectors"}
SMART_SCENARIOS = {
    "pending_sectors": (SMART_ENV, [], 0, ""),
    "bridge_fails_with_reason": (SMART_ENV, [], 3, "boom: could not send\nsecond line"),
    "bridge_fails_silently": (SMART_ENV, [], 1, ""),
    "temperature": ({**SMART_ENV, "SMARTD_FAILTYPE": "Temperature", "SMARTD_MESSAGE": "Device: /dev/nvme0, Temperature 71 Celsius"}, [], 0, ""),
    "email_test": ({**SMART_ENV, "SMARTD_FAILTYPE": "EmailTest", "SMARTD_MESSAGE": "TEST EMAIL from smartd"}, [], 0, ""),
    "args_fallback": ({}, ["root", "SMART error (Temperature) detected on host: h", "third arg"], 0, ""),
    "nothing_at_all": ({}, [], 0, ""),
    "device_from_env_message_from_args": ({"SMARTD_DEVICE": "/dev/sdb"}, ["root", "subj", "msg from args"], 0, ""),
    "long_error_tail": (SMART_ENV, [], 2, "x" * 300 + "\n" + "y" * 300),
}


def smart_sandbox(root: Path, env: dict, args: list, rc: int, err: str):
    patched = prep_script(legacy("smart-alert.sh"), [
        ("LOGFILE=/var/log/smart-alert.log", f"LOGFILE={root}/smart.log"),
        ("BRIDGE=/usr/local/sbin/backup-notify-hermes.py", f"BRIDGE={root}/bridge"),
        ("HOME=/home/$HANDLE PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 'HOME="$HOME" PATH="$PATH"')],
        root / "script.sh")
    (root / "fake").mkdir(parents=True)
    write_bin(root, "runuser", FAKE_RUNUSER)
    bridge = root / "bridge"
    bridge.write_text(FAKE_BRIDGE)
    bridge.chmod(0o755)
    r = bash(patched, root, env={**env, "BRIDGE_RC": str(rc), "BRIDGE_ERR": err.replace("\n", "\\n")}, args=args)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""            # smartd treats any output as a hook failure
    return (root / "smart.log").read_text().splitlines(), bridge_calls(root)


def port_smart(tmp_path, monkeypatch, env, args, rc, err):
    sent = []

    def fake_notify(ev):
        sent.append(ev)
        if rc == 0:
            return dict(OK_SENT)
        return {"ok": False, "handled": False, "rc": rc, "note": " ".join(err.split())}

    monkeypatch.setattr(sm, "_notify", fake_notify)
    log = tmp_path / "port.log"
    assert sm.smart_event(env, args, log_path=str(log), now=NOW) == (0 if rc == 0 else 1)     # 1 = not delivered: the stub falls back
    return log.read_text().splitlines(), sent


@pytest.mark.parametrize("name", list(SMART_SCENARIOS))
def test_smart_event_log_and_subject_match_the_sandboxed_script(tmp_path, monkeypatch, name):
    env, args, rc, err = SMART_SCENARIOS[name]
    legacy_log, calls = smart_sandbox(tmp_path / "s", env, args, rc, err)
    port_log, sent = port_smart(tmp_path, monkeypatch, env, args, rc, err)
    if rc and not err:        # intentional difference: a failure with no stderr says so instead of leaving ": " dangling
        assert legacy_log[-1].endswith("for /dev/sda: ")
        legacy_log = [*legacy_log[:-1], legacy_log[-1] + "no reason logged"]
    assert [mask_ts(x) for x in port_log] == [mask_ts(x) for x in legacy_log]
    assert len(calls) == 1 and len(sent) == 1
    assert sent[0]["title"] == calls[0][1]                                 # "SMART <failtype> on <host>: <device>"
    msg = env.get("SMARTD_MESSAGE") or (args[2] if len(args) > 2 else "no message")
    assert calls[0][2] == msg and msg in (sent[0]["summary"], sent[0]["details"]) or msg[:100] in sent[0]["details"]
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d ", port_log[0]) and re.match(r"^\S+ ", legacy_log[0])


def test_smart_event_log_lines_are_what_alert_path_health_parses(tmp_path, monkeypatch):
    for name, (env, args, rc, err) in SMART_SCENARIOS.items():
        run_dir = tmp_path / name
        run_dir.mkdir()
        log, _ = port_smart(run_dir, monkeypatch, env, args, rc, err)
        ev = checks_health._smart_log_events(log)
        if rc == 0:
            assert [e[0] for e in ev] == ["ok"], name
        else:
            assert [(e[0], e[2]) for e in ev] == [("fail", rc)], name                  # kind and rc survive the round trip
    log, _ = port_smart(tmp_path, monkeypatch, *SMART_SCENARIOS["bridge_fails_with_reason"])
    assert "boom: could not send second line" in checks_health._smart_log_events(log)[0][3]


@pytest.mark.parametrize("failtype,kind,sev", [
    ("Health", "alert", "crit"), ("FailedHealthCheck", "alert", "crit"), ("CurrentPendingSector", "alert", "crit"),
    ("OfflineUncorrectableSector", "alert", "crit"), ("SelfTest", "alert", "crit"), ("ErrorCount", "alert", "crit"),
    ("FailedReadSmartData", "alert", "crit"), ("FailedOpenDevice", "alert", "crit"), ("Usage", "alert", "warn"),
    ("Temperature", "alert", "warn"), ("EmailTest", "test", "info"), ("SomethingNew", "alert", "crit"), ("unknown", "alert", "crit")])
def test_smart_event_severity_table(tmp_path, monkeypatch, failtype, kind, sev):
    _, sent = port_smart(tmp_path, monkeypatch, {**SMART_ENV, "SMARTD_FAILTYPE": failtype}, [], 0, "")
    assert (sent[0]["kind"], sent[0]["severity"]) == (kind, sev)
    assert sent[0]["dedupe_key"] == f"smart:/dev/sda:{failtype}" and sent[0]["task"] == "smart_event"
    assert sent[0]["facts"]["device"] == "/dev/sda" and sent[0]["facts"]["failtype"] == failtype


def test_smart_event_writes_the_local_record_before_it_sends(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    seen = []

    def notify_(ev):
        seen.append(log.read_text())
        return dict(OK_SENT)

    monkeypatch.setattr(sm, "_notify", notify_)
    sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW)
    assert len(seen) == 1 and "device=/dev/sda type=sat failtype=CurrentPendingSector" in seen[0] and "alert sent" not in seen[0]


def test_smart_event_still_notifies_when_the_log_cannot_be_written(tmp_path, monkeypatch, sandbox):
    assert sm.smart_event(SMART_ENV, [], log_path=str(tmp_path / "no" / "such" / "dir" / "x.log"), now=NOW) == 0
    assert len(sandbox) == 1


def test_smart_event_never_raises_or_prints(tmp_path, monkeypatch, capsys):
    def boom(ev):
        raise RuntimeError("smtp password=hunter2 exploded")

    monkeypatch.setattr(sm, "_notify", boom)
    log = tmp_path / "x.log"
    assert sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW) == 1          # not delivered: 1, so the stub can fall back
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    text = log.read_text()
    assert "ALERT SEND FAILED rc=1 for /dev/sda" in text and "hunter2" not in text


def test_smart_event_policy_suppression_is_not_logged_as_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_notify", lambda ev: {"ok": False, "handled": True, "rc": 1, "note": "dedupe"})
    log = tmp_path / "x.log"
    sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW)
    lines = log.read_text().splitlines()
    assert lines[-1].endswith("alert not sent for /dev/sda: dedupe") and "FAILED" not in log.read_text()
    assert checks_health._smart_log_events(lines) == []                    # neither a failure nor a success


def test_smart_event_missing_notify_module_is_a_logged_failure_with_rc_127(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and fromlist and "notify" in fromlist:
            raise ImportError("gone")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(sm, "_notify", REAL_SM_NOTIFY)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    log = tmp_path / "x.log"
    assert sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW) == 1
    monkeypatch.undo()
    assert log.read_text().splitlines()[-1].endswith("ALERT SEND FAILED rc=127 for /dev/sda: notify module unavailable")


def test_smart_event_folds_newlines_and_scrubs_the_failure_note(tmp_path, monkeypatch):
    env = {**SMART_ENV, "SMARTD_MESSAGE": "line one\nline two\r\nline three", "SMARTD_DEVICE": "/dev/sda\nevil"}
    monkeypatch.setattr(sm, "_notify", lambda ev: {"ok": False, "handled": False, "rc": 1,
                                                   "note": "smtp token=abc123 for me@example.com +1 (555) 123-4567 failed"})
    log = tmp_path / "x.log"
    sm.smart_event(env, [], log_path=str(log), now=NOW)
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "line one line two line three" in lines[0] and "device=/dev/sda evil " in lines[0]
    assert "abc123" not in lines[1] and "me@example.com" not in lines[1] and "555" not in lines[1] and "<redacted>" in lines[1]


def test_smart_event_main_reads_the_process_environment(tmp_path, monkeypatch, sandbox):
    monkeypatch.setenv("SMARTD_DEVICE", "/dev/sdz")
    monkeypatch.setenv("SMARTD_FAILTYPE", "Health")
    monkeypatch.setattr(sm, "SMART_LOG", str(tmp_path / "default.log"))
    assert sm.smart_event_main(["root", "subject"]) == 0
    assert "device=/dev/sdz" in (tmp_path / "default.log").read_text() and sandbox[0]["facts"]["failtype"] == "Health"


def test_iso_now_matches_date_dash_i(monkeypatch):
    old = os.environ.get("TZ")
    try:
        for tz in ("UTC", "America/Toronto", "Asia/Kolkata"):
            os.environ["TZ"] = tz
            time.tzset()
            want = subprocess.run(["date", "-Is", "-d", f"@{int(NOW)}"], capture_output=True, text=True, env={**os.environ, "TZ": tz}).stdout.strip()
            assert sm._iso_now(NOW) == want, tz
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_smart_event_through_the_real_notify_module_with_a_fake_transport(tmp_path, monkeypatch):
    from homelab_maint import notify
    got = []

    def transport(msg, cfg):
        got.append(msg)
        return notify.TransportResult(ok=True, legs={"sms": "sent", "email": "sent"})

    real_send = notify.send
    monkeypatch.setattr(notify, "send", lambda ev, *a, **k: real_send(ev, transport=transport))
    monkeypatch.setattr(sm, "_notify", REAL_SM_NOTIFY)
    log = tmp_path / "x.log"
    assert sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW) == 0
    assert len(got) == 1 and "SMART" in got[0].subject and "/dev/sda" in got[0].subject
    assert got[0].sms.isascii() and len(got[0].sms) <= 130 and "http" not in got[0].sms
    assert log.read_text().splitlines()[-1].endswith("alert sent for /dev/sda")


# --------------------------------------------------------------------------- the hook must not depend on any other module
PKG = Path(__file__).resolve().parent.parent / "homelab_maint"
NOTIFY_STUB = '''
import dataclasses, json, os


@dataclasses.dataclass
class Event:
    kind: str
    severity: str = ""
    title: str = ""
    summary: str = ""
    details: object = None
    facts: object = None
    status: object = None
    dedupe_key: object = None
    task: object = None


@dataclasses.dataclass
class Delivery:
    ok: bool = True
    handled: bool = True
    note: str = "stub"


def send(ev, *a, **k):
    with open(os.environ["STUB_OUT"], "a") as f:
        f.write(json.dumps(dataclasses.asdict(ev)) + "\\n")
    return Delivery(ok=os.environ.get("STUB_OK", "1") == "1", handled=os.environ.get("STUB_HANDLED", "1") == "1")
'''
HOOK_ENV = {"SMARTD_DEVICE": "/dev/sda", "SMARTD_DEVICETYPE": "sat", "SMARTD_FAILTYPE": "Health",
            "SMARTD_MESSAGE": "Device: /dev/sda, FAILED SMART self-check"}


def hook_tree(tmp_path: Path, *, broken=(), stub=True) -> tuple[Path, Path]:
    """A scratch copy of the package (the live one is never touched) with the named modules broken by a syntax error and, by
    default, a stub `notify` that records events instead of sending. Its SMART_LOG points into tmp_path. Returns (root, log)."""
    root, log = tmp_path / "pkg", tmp_path / "smart-alert.log"
    assert PKG not in root.parents and root.resolve() != PKG.parent        # never edit, break or run the live tree
    shutil.copytree(PKG, root / "homelab_maint", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"))
    hook = root / "homelab_maint" / "smart_hook.py"
    text = hook.read_text()
    assert 'SMART_LOG = "/var/log/smart-alert.log"' in text
    hook.write_text(text.replace('SMART_LOG = "/var/log/smart-alert.log"', f'SMART_LOG = "{log}"'))
    if stub:
        (root / "homelab_maint" / "notify.py").write_text(NOTIFY_STUB)
    for rel in broken:
        with open(root / "homelab_maint" / rel, "a") as f:
            f.write("\n)(\n")                                  # one syntax error: what a bad deploy of that module looks like
    return root, log


def hook_env(root: Path, extra: dict | None = None) -> dict:
    """Environment for a subprocess that imports the SCRATCH package. Three independent guards against reaching the real world:
    cwd is the scratch root (python -m puts cwd first on sys.path, so the live package can never shadow it), HOMELAB_MAINT_* point
    into tmp, and PYTEST_CURRENT_TEST makes even a live notify module refuse to run its real transport."""
    tmp = str(root.parent)
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "PYTEST_CURRENT_TEST": "hook-subprocess", "HOMELAB_MAINT_STATE": tmp, "HOMELAB_MAINT_LOG": tmp,
            "HOMELAB_MAINT_RUN": tmp, "HOMELAB_MAINT_CONF": tmp, "HOMELAB_MAINT_NO_SYSLOG": "1", **(extra or {})}   # last one: no `logger` line in the real journal


def py(root: Path, code_or_module: list[str], env: dict | None = None):
    return subprocess.run([sys.executable, *code_or_module], env=hook_env(root, env), cwd=root, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)


def test_hook_scenario_the_old_arrangement_really_broke(tmp_path):
    """REGRESSION evidence: with one syntax error in cleaners.py, importing tasks.native (where the hook used to live) raises, so
    the hook died before it could write its local record. The same tree with the hook module imported directly is fine."""
    root, log = hook_tree(tmp_path, broken=("tasks/cleaners.py",))
    r = py(root, ["-c", "import homelab_maint.tasks.native"])
    assert r.returncode != 0 and "SyntaxError" in r.stderr
    r = py(root, ["-c", "import homelab_maint.smart_hook"])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("broken", [("tasks/cleaners.py",), ("core.py",), ("tasks/gates.py", "tasks/native.py"),
                                    ("tasks/cleaners.py", "core.py", "cli.py", "tasks/native.py", "legacy.py", "routine.py")])
def test_smart_hook_pages_and_logs_whatever_other_module_is_broken(tmp_path, broken):
    """REGRESSION: a bad deploy of any other stream's file used to drop a disk-failure alert silently (no local line, no page)."""
    root, log = hook_tree(tmp_path, broken=broken)
    out = tmp_path / "events.jsonl"
    r = py(root, ["-m", "homelab_maint.smart_hook", "root", "subject", "msg"], {**HOOK_ENV, "STUB_OUT": str(out)})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")                  # smartd treats any output of a hook as a failure
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "device=/dev/sda type=sat failtype=Health Device: /dev/sda, FAILED SMART self-check" in lines[0]
    assert lines[1].endswith("alert sent for /dev/sda")
    ev = json.loads(out.read_text().splitlines()[0])
    assert (ev["kind"], ev["severity"], ev["task"]) == ("alert", "crit", "smart_event") and ev["dedupe_key"] == "smart:/dev/sda:Health"


def test_smart_hook_with_a_broken_notify_still_logs_first_prints_nothing_and_exits_nonzero(tmp_path):
    root, log = hook_tree(tmp_path, broken=("notify.py",), stub=False)
    r = py(root, ["-m", "homelab_maint.smart_hook"], HOOK_ENV)
    assert (r.returncode, r.stdout, r.stderr) == (1, "", "")                  # no traceback; 1 = not delivered
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "failtype=Health" in lines[0]
    assert lines[1].endswith("ALERT SEND FAILED rc=127 for /dev/sda: notify module unavailable")
    assert [(e[0], e[2]) for e in checks_health._smart_log_events(lines)] == [("fail", 127)]       # alert_path_health sees it


def test_smart_hook_with_everything_broken_still_leaves_the_local_record(tmp_path):
    root, log = hook_tree(tmp_path, broken=("notify.py", "core.py"), stub=False)
    r = py(root, ["-m", "homelab_maint.smart_hook"], HOOK_ENV)
    assert (r.returncode, r.stdout, r.stderr) == (1, "", "")
    assert "failtype=Health" in log.read_text().splitlines()[0]


def test_smart_hook_delivery_failure_exit_codes_through_the_process(tmp_path):
    """0 = delivered or held back by policy, 1 = the owner was not told (what the retirement stub keys its fallback on)."""
    root, log = hook_tree(tmp_path)
    out = {"STUB_OUT": str(tmp_path / "ev.jsonl")}
    assert py(root, ["-m", "homelab_maint.smart_hook"], {**HOOK_ENV, **out}).returncode == 0
    assert py(root, ["-m", "homelab_maint.smart_hook"], {**HOOK_ENV, **out, "STUB_OK": "0", "STUB_HANDLED": "1"}).returncode == 0
    assert py(root, ["-m", "homelab_maint.smart_hook"], {**HOOK_ENV, **out, "STUB_OK": "0", "STUB_HANDLED": "0"}).returncode == 1
    tail = [ln.split(" ", 1)[1] for ln in log.read_text().splitlines()]
    assert [t for t in tail if not t.startswith("device=")] == ["alert sent for /dev/sda", "alert not sent for /dev/sda: stub",
                                                                 "ALERT SEND FAILED rc=1 for /dev/sda: stub"]


def test_smart_hook_module_level_imports_are_stdlib_only_and_the_rest_is_guarded():
    """The structural guarantee behind the tests above: nothing from this package is imported at module level, and every
    (relative) import inside a function sits in a try that catches BaseException."""
    import ast
    tree = ast.parse((PKG / "smart_hook.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names), ast.dump(node)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and (node.module or "").split(".")[0] in sys.stdlib_module_names, ast.dump(node)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    lazy = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    assert lazy, "the hook is expected to import notify (and core, for the audit) lazily"
    for n in lazy:
        p = parents[n]
        while not isinstance(p, ast.Try):
            p = parents[p]                                                      # raises KeyError if the import is not inside a try
        assert any(isinstance(h.type, ast.Name) and h.type.id == "BaseException" for h in p.handlers), ast.dump(n)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3), RuntimeError("password=hunter2"), MemoryError()])
def test_smart_hook_swallows_even_base_exceptions_from_the_notification_path(tmp_path, monkeypatch, exc):
    def boom(ev):
        raise exc

    monkeypatch.setattr(sm, "_notify", boom)
    log = tmp_path / "x.log"
    assert sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW) == 1
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "failtype=CurrentPendingSector" in lines[0] and "ALERT SEND FAILED rc=1" in lines[1]
    assert "hunter2" not in log.read_text()


def test_smart_hook_notify_send_raising_base_exception_is_a_failed_send_not_a_crash(monkeypatch):
    from homelab_maint import notify

    def boom(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(notify, "send", boom)
    r = REAL_SM_NOTIFY({"kind": "alert", "title": "T"})
    assert r["ok"] is False and r["handled"] is False and r["rc"] == 1


def test_smart_hook_the_local_record_is_written_even_when_building_the_event_fails(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    monkeypatch.setattr(sm.socket, "gethostname", lambda: (_ for _ in ()).throw(OSError("no hostname")))
    assert sm.smart_event(SMART_ENV, [], log_path=str(log), now=NOW) == 1
    lines = log.read_text().splitlines()
    assert "failtype=CurrentPendingSector" in lines[0] and "ALERT SEND FAILED rc=1" in lines[1]


def _retirement_stub() -> str:
    import tomllib
    doc = tomllib.loads((Path(__file__).resolve().parent.parent / "etc" / "legacy-retirement.toml").read_text())
    item = next(i for i in doc["item"] if i["name"] == "smartd-alert-hook")
    return next(a["content"] for a in item["retire_actions"] if a.get("do") == "stub")


def test_retirement_stub_falls_back_to_the_legacy_script_exactly_when_the_hook_exits_nonzero(tmp_path):
    """The retire stream's forwarding stub (read from etc/legacy-retirement.toml, not copied) against the real hook: delivered ->
    the legacy script does NOT run; not delivered (notify broken) -> it does, so a SMART alert is never lost to a bad deploy."""
    stub = _retirement_stub()
    assert 'smart-event "$@" && exit 0' in stub and "exec {legacy_dir}/smart-alert.sh" in stub
    for broken, stub_notify, legacy_runs in ((False, True, False), (True, False, True)):
        root, log = hook_tree(tmp_path / f"b{broken}", broken=("notify.py",) if broken else (), stub=stub_notify)
        (tmp_path / f"b{broken}" / "legacy").mkdir()
        marker = tmp_path / f"b{broken}" / "legacy-ran"
        legacy_script = tmp_path / f"b{broken}" / "legacy" / "smart-alert.sh"
        legacy_script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\n')
        legacy_script.chmod(0o755)
        sh_stub = tmp_path / f"b{broken}" / "stub.sh"
        sh_stub.write_text(stub.replace("{legacy_dir}", str(legacy_script.parent))
                           .replace("/usr/local/sbin/homelab-maint smart-event", f"{sys.executable} -m homelab_maint.smart_hook"))
        events = tmp_path / f"b{broken}" / "ev.jsonl"
        r = subprocess.run(["/bin/sh", str(sh_stub), "root", "subject", "msg"], env=hook_env(root, {"STUB_OUT": str(events), **HOOK_ENV}),
                           cwd=root, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
        assert marker.exists() == legacy_runs, broken
        assert events.exists() == (not broken)                    # the scratch tree's stub notify saw the event: the live package was not used


# =========================================================================== mem-guard: retired, documented, never ported
def test_mem_guard_retirement_record_matches_the_real_units():
    r = nv.RETIRED_MEM_GUARD
    assert r["mode"] == "retire" and r["name"] == "mem-guard"
    svc, timer = unit_text("mem-guard.service"), unit_text("mem-guard.timer")
    assert re.search(r"^ExecStart=/usr/local/sbin/mem-guard\.py .*--dry-run", svc, re.M)      # it only ever reported
    assert re.search(r"^OnUnitActiveSec=3h\s*$", timer, re.M)
    assert r["script"] == "/usr/local/sbin/mem-guard.py" and set(r["units"]) == {"mem-guard.service", "mem-guard.timer"}
    assert any("disable --now mem-guard.timer" in a for a in r["retire_actions"])
    assert any("legacy/mem-guard" in a for a in r["retire_actions"])
    assert r["rollback_actions"][-1].startswith("systemctl enable --now mem-guard.timer")


def test_mem_guard_replacements_exist_in_the_runner():
    root = Path(__file__).resolve().parent.parent / "homelab_maint" / "tasks"
    sources = " ".join(p.read_text() for p in root.glob("*.py") if p.name != "native.py")
    missing = [n for n in nv.RETIRED_MEM_GUARD["replaced_by"] if n != "protected.toml" and f'"{n}"' not in sources]
    if missing and not (root / "pressure.py").exists():
        pytest.skip("pressure.py is not written yet")
    assert not missing, f"mem-guard is said to be replaced by tasks that do not exist: {missing}"


# =========================================================================== os_jobs
def timers_json(rows: dict) -> str:
    return json.dumps([{"unit": u, "activates": u.replace(".timer", ".service"), "next": int(n * 1e6) if n else None,
                        "last": int(last * 1e6) if last else 0} for u, (last, n) in rows.items()])


def show_text(units: dict) -> str:
    return "\n\n".join("\n".join(f"{k}={v}" for k, v in {"Id": u, **p}.items()) for u, p in units.items()) + "\n"


def tprops(**kw):
    return {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting", "UnitFileState": "enabled", "Result": "success",
            "ActiveEnterTimestampMonotonic": "12000000", **kw}


def sprops(**kw):
    return {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "static", "Result": "success",
            "ExecMainStatus": "0", "ActiveEnterTimestampMonotonic": "0", **kw}


def os_world(monkeypatch, jobs: list[dict], last: dict, *, timer=None, service=None, list_rc=0, show_rc=0, list_out=None):
    """Fake systemctl for `jobs` (each a timer job): last = {name: epoch|None}; timer/service = {name: props override}."""
    timer, service = timer or {}, service or {}
    rows, units = {}, {}
    for j in jobs:
        if "timer" in j:
            rows[j["timer"]] = (last.get(j["name"]), NOW + 3600)
            units[j["timer"]] = tprops(**timer.get(j["name"], {}))
            units[j["service"]] = sprops(**service.get(j["name"], {}))

    def show(cmd):
        wanted = [u for u in cmd.split() if u.endswith((".timer", ".service"))]
        return (show_rc, show_text({u: units[u] for u in wanted if u in units}), "")

    return use_sh(monkeypatch, ("systemctl list-timers", (list_rc, timers_json(rows) if list_out is None else list_out, "")),
                  ("systemctl show", show))


JOBS3 = [{"name": "alpha", "timer": "alpha.timer", "service": "alpha.service", "max_age_hours": 36},
         {"name": "beta", "timer": "beta.timer", "service": "beta.service", "max_age_hours": 200},
         {"name": "gamma", "timer": "gamma.timer", "service": "gamma.service", "max_age_hours": 2}]
MONO = 5 * DAY      # the host has been up 5 days


def table(monkeypatch, last, **kw):
    os_world(monkeypatch, JOBS3, last, **kw)
    return {r["name"]: r for r in nv.os_jobs_table(JOBS3, NOW, MONO)}


def test_os_jobs_states_table(monkeypatch):
    h = 3600
    t = table(monkeypatch, {"alpha": NOW - 3 * h, "beta": NOW - 190 * h, "gamma": NOW - 3 * h})
    assert (t["alpha"]["state"], t["beta"]["state"], t["gamma"]["state"]) == ("ok", "ok", "overdue")
    assert t["alpha"]["detail"] == "last ran 3.0 h ago" and "limit 2 h" in t["gamma"]["detail"] and t["alpha"]["next"] == NOW + 3600
    t = table(monkeypatch, {"alpha": NOW - 36.1 * h, "beta": NOW - 100, "gamma": NOW - 100})
    assert t["alpha"]["state"] == "overdue" and "36.1 h ago (limit 36 h)" in t["alpha"]["detail"]
    t = table(monkeypatch, {"alpha": None, "beta": None, "gamma": NOW - 100})        # up 5 d: alpha's 36 h limit is blown, beta's 200 h is not
    assert t["alpha"]["state"] == "overdue" and "never ran" in t["alpha"]["detail"] and t["beta"]["state"] == "waiting"
    t = table(monkeypatch, {"alpha": None, "beta": NOW - h, "gamma": NOW - h}, timer={"alpha": {"ActiveEnterTimestampMonotonic": str(int((MONO - 3600) * 1e6))}})
    assert t["alpha"]["state"] == "waiting" and "timer started recently" in t["alpha"]["detail"]


def test_os_jobs_the_services_result_is_the_verdict_not_the_exit_status(monkeypatch):
    last = {j["name"]: NOW - 3600 for j in JOBS3}
    t = table(monkeypatch, last, service={"alpha": {"Result": "success", "ExecMainStatus": "2"}})      # fwupd-refresh: SuccessExitStatus=2
    assert t["alpha"]["state"] == "ok"
    t = table(monkeypatch, last, service={"alpha": {"Result": "exit-code", "ExecMainStatus": "1"}})
    assert t["alpha"]["state"] == "failed" and "exit-code" in t["alpha"]["detail"] and "exit status 1" in t["alpha"]["detail"]
    t = table(monkeypatch, last, service={"alpha": {"ActiveState": "failed", "Result": "success"}})
    assert t["alpha"]["state"] == "failed"
    t = table(monkeypatch, last, service={"alpha": {"ActiveState": "activating", "SubState": "start"}})
    assert t["alpha"]["state"] == "running" and t["alpha"]["state"] in nv._OK_STATES


def test_os_jobs_inactive_and_absent_timers(monkeypatch):
    last = {j["name"]: NOW - 3600 for j in JOBS3}
    t = table(monkeypatch, last, timer={"alpha": {"ActiveState": "inactive", "UnitFileState": "disabled"}, "beta": {"LoadState": "not-found"}})
    assert t["alpha"]["state"] == "inactive" and "disabled" in t["alpha"]["detail"]
    assert t["beta"]["state"] == "absent" and "absent" in nv._OK_STATES


@pytest.mark.parametrize("kw", [dict(list_rc=1), dict(show_rc=1), dict(list_out="not json")])
def test_os_jobs_systemctl_trouble_is_unknown_not_ok(monkeypatch, kw):
    t = table(monkeypatch, {}, **kw)
    assert {r["state"] for r in t.values()} == {"unknown"} and "unknown" not in nv._OK_STATES


def test_os_jobs_timer_listing_parsing(monkeypatch):
    use_sh(monkeypatch, ("systemctl list-timers", ok(json.dumps([
        {"unit": "a.timer", "last": 1_790_000_000_000_000, "next": 1_790_000_100_000_000}, {"unit": "b.timer", "last": 0, "next": None},
        {"unit": "c.timer", "last": True, "next": "x"}, {"nounit": 1}, "junk"]))))
    assert nv._list_timers() == {"a.timer": (1_790_000_000.0, 1_790_000_100.0), "b.timer": (None, None), "c.timer": (None, None)}
    use_sh(monkeypatch, ("systemctl list-timers", ok("{}")))
    assert nv._list_timers() == {}


def test_os_jobs_show_parsing_handles_blocks_and_unknown_lines(monkeypatch):
    use_sh(monkeypatch, ("systemctl show", ok("Result=success\nId=a.timer\nLoadState=loaded\n\nId=b.service\nGarbageLine\nResult=failed\n\n")))
    out = nv._show(["a.timer", "b.service"])
    assert out["a.timer"]["LoadState"] == "loaded" and out["b.service"]["Result"] == "failed"
    assert nv._show([]) == {}


def write_ua_log(path: Path, lines):
    path.write_text("\n".join(lines) + "\n")


def ua_stamp(t: float, level="ERROR", text="boom") -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f",123 {level} {text}"


def daemon_job(log):
    return {"name": "unattended-upgrades", "kind": "daemon", "service": "unattended-upgrades.service", "log": str(log), "log_hours": 36}


def ua_world(monkeypatch, **svc):
    use_sh(monkeypatch, ("systemctl list-timers", ok("[]")),
           ("systemctl show", ok(show_text({"unattended-upgrades.service": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", **svc}}))))


def test_os_jobs_unattended_upgrades_daemon_and_log(tmp_path, monkeypatch):
    log = tmp_path / "ua.log"
    job = daemon_job(log)
    ua_world(monkeypatch)
    write_ua_log(log, [ua_stamp(NOW - 3600, "INFO", "all fine"), ua_stamp(NOW - 40 * 3600, "ERROR", "old problem")])
    row = nv.os_jobs_table([job], NOW, MONO)[0]
    assert row["state"] == "ok" and row["detail"] == "shutdown helper active"            # an old error is not news
    write_ua_log(log, [ua_stamp(NOW - 3600, "INFO", "all fine"), ua_stamp(NOW - 7200, "ERROR", "dpkg returned an error code (1)"),
                       ua_stamp(NOW - 3600, "CRITICAL", "broken password=hunter2")])
    row = nv.os_jobs_table([job], NOW, MONO)[0]
    assert row["state"] == "failed" and "2 error line(s)" in row["detail"] and "hunter2" not in row["detail"]
    assert nv._ua_errors(str(tmp_path / "missing"), NOW, 36) == (0, "")
    write_ua_log(log, ["garbage", "9999-99-99 99:99:99,000 ERROR bad date", ua_stamp(NOW + 7200, "ERROR", "from the future")])
    assert nv._ua_errors(str(log), NOW, 36) == (0, "")


def test_os_jobs_daemon_not_installed_or_not_active(tmp_path, monkeypatch):
    job = daemon_job(tmp_path / "ua.log")
    ua_world(monkeypatch, LoadState="not-found")
    assert nv.os_jobs_table([job], NOW, MONO)[0]["state"] == "absent"
    ua_world(monkeypatch, ActiveState="inactive")
    row = nv.os_jobs_table([job], NOW, MONO)[0]
    assert row["state"] == "inactive" and "is inactive" in row["detail"]


def test_ua_errors_reads_only_the_tail(tmp_path):
    log = tmp_path / "big.log"
    with open(log, "w") as f:
        f.write(ua_stamp(NOW - 60, "ERROR", "ancient but inside the window") + "\n")
        f.write(("x" * 99 + "\n") * 2000)                                       # 200 KB of noise pushes it out of the 64 KiB tail
    assert nv._ua_errors(str(log), NOW, 36) == (0, "")


@pytest.mark.parametrize("reply,uptime,want_state", [
    ({"result": {"refresh": {"last": "2026-10-01T22:00:00-04:00"}}}, 5 * DAY, "ok"),
    ({"result": {"refresh": {"last": "2026-09-01T00:00:00-04:00"}}}, 5 * DAY, "overdue"),
    ({"result": {"refresh": {"timer": "00:00~24:00/4"}}}, 5 * DAY, "overdue"),          # never refreshed, host up > 26 h
    ({"result": {"refresh": {"timer": "00:00~24:00/4"}}}, 3600, "waiting"),             # fresh boot: not late yet
    ({"result": {}}, 5 * DAY, "unknown"), ("junk", 5 * DAY, "unknown")])
def test_os_jobs_snapd_refresh(tmp_path, monkeypatch, reply, uptime, want_state):
    sock = tmp_path / "snapd.socket"
    sock.write_text("")
    monkeypatch.setattr(nv, "SNAPD_SOCKET", str(sock))
    use_sh(monkeypatch, ("systemctl list-timers", ok("[]")), ("systemctl show", ok("")),
           ("curl -fsS -m 5 --unix-socket", ok(json.dumps(reply) if not isinstance(reply, str) else reply)))
    now = time.mktime((2026, 10, 2, 2, 0, 0, 0, 0, -1))
    row = nv.os_jobs_table([{"name": "snapd-refresh", "kind": "snapd", "max_age_hours": 26}], now, uptime)[0]
    assert row["state"] == want_state


def test_os_jobs_snapd_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(nv, "SNAPD_SOCKET", str(tmp_path / "nope"))
    use_sh(monkeypatch, ("systemctl list-timers", ok("[]")), ("systemctl show", ok("")))
    assert nv.os_jobs_table([{"name": "snapd-refresh", "kind": "snapd", "max_age_hours": 26}], NOW, MONO)[0]["state"] == "absent"


def healthy_default_world(monkeypatch, tmp_path, drop=()):
    jobs = [j for j in nv.DEFAULT_OS_JOBS if "timer" in j]
    last = {j["name"]: NOW - 600 for j in jobs}
    use_sh(monkeypatch, ("systemctl list-timers", ok(timers_json({j["timer"]: (last[j["name"]], NOW + 600) for j in jobs}))),
           ("systemctl show", lambda cmd: ok(show_text({**{j["timer"]: tprops() for j in jobs}, **{j["service"]: sprops() for j in jobs},
                                                        "unattended-upgrades.service": {"LoadState": "loaded", "ActiveState": "active"}}))),
           ("curl", ok(json.dumps({"result": {"refresh": {"last": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600))}}}))))
    sock = tmp_path / "snapd.socket"
    sock.write_text("")
    monkeypatch.setattr(nv, "SNAPD_SOCKET", str(sock))
    monkeypatch.setattr(nv, "_mono_now", lambda: MONO)


def test_os_jobs_task_all_healthy(tmp_path, monkeypatch):
    healthy_default_world(monkeypatch, tmp_path)
    jobs = [{**j, "log": str(tmp_path / "ua.log")} if j["name"] == "unattended-upgrades" else j for j in nv.DEFAULT_OS_JOBS]
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=jobs)
    check_result(res)
    assert res.status == "ok" and res.summary == "OS jobs: 13/13 on schedule (0 not installed)"
    assert res.metrics["jobs"] == 13 and res.metrics["overdue"] == 0 and res.metrics["failed"] == 0 and res.metrics["oldest_run_h"] >= 0
    assert len(res.items) == 12


def test_os_jobs_default_job_list_is_the_spec_list():
    names = {j["name"] for j in nv.DEFAULT_OS_JOBS}
    for want in ("apt-daily", "apt-daily-upgrade", "unattended-upgrades", "logrotate", "systemd-tmpfiles-clean", "fstrim", "e2scrub_all",
                 "fwupd-refresh", "man-db", "sysstat-collect", "snapd-refresh"):
        assert want in names, want
    for j in nv.DEFAULT_OS_JOBS:
        if "timer" in j:
            assert j["timer"].endswith(".timer") and j["service"].endswith(".service") and j["max_age_hours"] > 0


def test_os_jobs_task_flags_problems_in_severity_order(tmp_path, monkeypatch):
    jobs = JOBS3
    last = {"alpha": NOW - 40 * 3600, "beta": NOW - 3600, "gamma": NOW - 3600}
    os_world(monkeypatch, jobs, last, service={"beta": {"Result": "exit-code", "ExecMainStatus": "1"}})
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=jobs)
    check_result(res)
    assert res.status == "warn" and res.summary.startswith("OS jobs: 2 of 3 need attention: beta failed (last run failed: exit-code)")
    assert "alpha overdue" in res.summary and [i["name"] for i in res.items][:2] == ["beta", "alpha"]
    assert res.metrics["failed"] == 1 and res.metrics["overdue"] == 1 and res.metrics["on_schedule"] == 1


def test_os_jobs_task_many_problems_summary_is_bounded(tmp_path, monkeypatch):
    jobs = [{"name": f"job{i}", "timer": f"job{i}.timer", "service": f"job{i}.service", "max_age_hours": 1} for i in range(9)]
    os_world(monkeypatch, jobs, {j["name"]: NOW - 99 * 3600 for j in jobs})
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=jobs)
    check_result(res)
    assert res.status == "warn" and "(+5)" in res.summary and res.metrics["overdue"] == 9 and len(res.items) == 9


def test_os_jobs_ignore_and_extra_jobs(monkeypatch):
    os_world(monkeypatch, JOBS3, {"alpha": NOW - 99 * 3600, "beta": NOW - 3600, "gamma": NOW - 3600})
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=JOBS3, ignore=["alpha"])
    assert res.status == "ok" and res.metrics["jobs"] == 2
    extra = [{"name": "delta", "timer": "delta.timer", "service": "delta.service", "max_age_hours": 1}]
    os_world(monkeypatch, JOBS3 + extra, {"alpha": NOW - 600, "beta": NOW - 600, "gamma": NOW - 600, "delta": NOW - 99 * 3600})
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=JOBS3, extra_jobs=extra)
    assert res.status == "warn" and "delta overdue" in res.summary


@pytest.mark.parametrize("jobs", ["x", [1], [{"timer": "a.timer"}], {"a": 1}])
def test_os_jobs_bad_config_does_nothing(monkeypatch, jobs):
    f = use_sh(monkeypatch)
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=jobs)
    assert res.status == "skipped" and f.calls == []


def test_os_jobs_is_read_only(monkeypatch):
    f = os_world(monkeypatch, JOBS3, {j["name"]: NOW - 600 for j in JOBS3})
    step(nv.os_jobs, "os_jobs", NOW, apply=True, jobs=JOBS3)
    assert all(c.startswith(("systemctl list-timers", "systemctl show", "curl")) for c in f.calls)


@pytest.mark.parametrize("sec,want", [(60, "1 min"), (3000, "50 min"), (5400, "1.5 h"), (3600 * 47, "47.0 h"), (3 * DAY, "3.0 d")])
def test_ago_formatting(sec, want):
    assert nv._ago(sec) == want


REAL_SH = nv.sh


def test_live_os_jobs_readonly_smoke(monkeypatch):
    """Reads this host's systemd (list-timers, show) and snapd's socket: nothing is changed. Skipped where systemd is absent."""
    if shutil.which("systemctl") is None:
        pytest.skip("no systemctl")
    monkeypatch.setattr(nv, "sh", REAL_SH)
    rows = nv.os_jobs_table()
    if all(r["state"] == "unknown" for r in rows):
        pytest.skip("systemctl is not usable here")
    assert {r["name"] for r in rows} == {j["name"] for j in nv.DEFAULT_OS_JOBS}
    assert all(r["state"] in {"ok", "running", "waiting", "absent", "overdue", "failed", "inactive", "unknown"} for r in rows)
    assert all(isinstance(r["detail"], str) for r in rows)


# =========================================================================== small helpers
def test_container_state_helper(monkeypatch):
    for resp, want in (((0, "Running\n", ""), "running"), ((0, "exited\n", ""), "exited"), ((1, "", "Error: No such object: x"), "absent"),
                       ((1, "", "Cannot connect to the Docker daemon"), None), ((0, "", ""), None), ((127, "", "not found"), None)):
        use_sh(monkeypatch, ("docker container inspect", resp))
        assert nv._container_state("x") == want, resp


def test_container_pids_helper_shapes(monkeypatch):
    use_sh(monkeypatch, ("docker container inspect", ok(f"{cid(5)}|running|123\n")))
    assert nv._container_pids("c", False) == ("running", {123})
    use_sh(monkeypatch, ("docker container inspect", ok(f"{cid(5)}|exited|0\n")))
    assert nv._container_pids("c", True) == ("exited", set())
    use_sh(monkeypatch, ("docker container inspect", ok("garbage\n")))
    assert nv._container_pids("c", True) is None
    use_sh(monkeypatch, ("docker container inspect", (1, "", "Error: No such container: c")))
    assert nv._container_pids("c", True) == ("absent", set())
    use_sh(monkeypatch, ("docker container inspect", (1, "", "daemon down")))
    assert nv._container_pids("c", True) is None


def test_surreal_read_tolerates_odd_docker_output(tmp_path, monkeypatch):
    rocks = tmp_path / "r"
    rocks.mkdir()
    use_sh(monkeypatch, ("docker container inspect", ok(f"{cid(9)}|running|not-a-number\n")))
    rd, answered = nv.surreal_read(str(rocks), str(tmp_path), "c")
    assert answered and rd.restarts == 0 and rd.state == "running" and rd.oom_kill == 0
    use_sh(monkeypatch, ("docker container inspect", ok("a|b\n")))
    rd, answered = nv.surreal_read(str(rocks), str(tmp_path), "c")
    assert not answered and rd.state == "unknown"


def test_every_result_meets_the_runner_contract_under_stress(tmp_path, monkeypatch):
    """Long/odd inputs must never break the 140-char ASCII summary or the 12-row item limit."""
    w = ComfyWorld(monkeypatch)
    w.restart_rc = 1
    comfy_run(0, apply=True, unprotect=["^comfyui$"])
    res, _ = comfy_run(1, apply=True, unprotect=["^comfyui$"])
    check_result(res)
    gate_cfg()
    ImmichWorld(monkeypatch, busy=(True, "café " * 100))
    res, _ = immich_run(0, apply=True)
    check_result(res)
    assert res.summary.isascii() and len(res.summary) == 140


# --------------------------------------------------------------------------- SPEC5 issue_key: ALL of what is exposed / needs attention
def native_fp(name, res):
    from homelab_maint import acks
    fp = acks.fingerprint(name, res, res.status)
    # `ackable` now folds in the [ack] severity policy too (a crit issue is never
    # ackable). These key tests exercise the task rule, not the severity policy, so
    # assert that policy alone.
    assert fp.mode == "explicit" and acks.policy_ok(name, fp.mode) and res.issue_key
    return str(fp)


def test_docker_prune_exposure_key_is_every_container_and_image_and_never_the_countdown_or_the_size(tmp_path, monkeypatch, sandbox):
    rows = comfy_rows() + [crow(3, "tunarr", created_d=20, finished_d=2, image="tunarr:1"), crow(4, "kometa", created_d=20, finished_d=2, image="kometa:1")]
    imgs = comfy_images(more=[irow("tunarr:1", age_d=50, size=2 * GIB), irow("kometa:1", age_d=50, size=1 * GIB)])
    a, _ = exp_run(tmp_path, monkeypatch, rows=rows, imgs=imgs, expected=("comfyui", "tunarr", "kometa"))
    assert a.issue_key == f"containers:comfyui,kometa,tunarr;images:{COMFY_IMG},kometa:1,tunarr:1"
    b, _ = exp_run(tmp_path, monkeypatch, now=NOW + 3 * 3600, rows=rows, imgs=[{**i, "size": i["size"] + GIB} for i in imgs],
                   expected=("comfyui", "tunarr", "kometa"), timer_next=NOW + 38 * 3600)                    # three hours later, bigger images, the countdown moved
    assert b.issue_key == a.issue_key and native_fp("docker_prune_exposure", a) == native_fp("docker_prune_exposure", b)
    c, _ = exp_run(tmp_path, monkeypatch, rows=rows[:3], imgs=imgs[:5], expected=("comfyui", "tunarr"))
    assert native_fp("docker_prune_exposure", c) != native_fp("docker_prune_exposure", a)                  # one container is no longer exposed
    swap = [rows[0], rows[1], rows[2], crow(5, "other", created_d=20, finished_d=2, image="other:1")]
    d, _ = exp_run(tmp_path, monkeypatch, rows=swap, imgs=comfy_images(more=[irow("tunarr:1", age_d=50, size=2 * GIB), irow("other:1", age_d=50, size=1 * GIB)]),
                   expected=("comfyui", "tunarr", "other"))
    assert native_fp("docker_prune_exposure", d) != native_fp("docker_prune_exposure", c) and "other" in d.issue_key


def test_docker_prune_exposure_has_no_key_when_nothing_is_exposed(tmp_path, monkeypatch, sandbox):
    res, _ = exp_run(tmp_path, monkeypatch, timer=("disabled", "inactive"))
    assert res.status == "ok" and res.issue_key is None


def os_key_run(monkeypatch, jobs, last, **kw):
    os_world(monkeypatch, jobs, last, **kw)
    res, _ = step(nv.os_jobs, "os_jobs", NOW, jobs=jobs)
    return res


def test_os_jobs_key_is_every_job_that_needs_attention_with_its_state_not_its_age(monkeypatch):
    jobs = [{"name": f"job{i}", "timer": f"job{i}.timer", "service": f"job{i}.service", "max_age_hours": 1} for i in range(6)]
    a = os_key_run(monkeypatch, jobs, {j["name"]: NOW - 99 * 3600 for j in jobs})
    assert a.issue_key == "jobs:" + ",".join(f"job{i}=overdue" for i in range(6)) and "(+2)" in a.summary          # six overdue, the summary shows four
    b = os_key_run(monkeypatch, jobs, {j["name"]: NOW - 500 * 3600 for j in jobs})
    assert b.issue_key == a.issue_key and native_fp("os_jobs", a) == native_fp("os_jobs", b)                       # the same jobs, a lot later
    c = os_key_run(monkeypatch, jobs, {j["name"]: NOW - 99 * 3600 for j in jobs}, service={"job5": {"Result": "exit-code", "ExecMainStatus": "1"}})
    assert c.issue_key.endswith("job5=failed") and native_fp("os_jobs", c) != native_fp("os_jobs", a)            # overdue -> failed is another error (status stays warn)
    d = os_key_run(monkeypatch, jobs, {**{j["name"]: NOW - 99 * 3600 for j in jobs[:5]}, "job5": NOW - 600})
    assert "job5" not in d.issue_key and native_fp("os_jobs", d) != native_fp("os_jobs", a)                        # a hidden one recovered: same count shown, other set
    assert os_key_run(monkeypatch, jobs, {j["name"]: NOW - 600 for j in jobs}).issue_key is None
