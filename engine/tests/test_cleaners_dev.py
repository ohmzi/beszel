"""Tests for tasks/cleaners_dev.py. Nothing here touches the host: every path lives under tmp_path, /proc is a fake
tree (shared with inuse.py), docker and the package managers are mocked (`sh` is replaced; an unmocked command
returns rc 127), git runs for real but only on tmp repos. Every decision is tested both ways: provably unused =>
selected, any doubt or probe error => kept. Today's hand decisions are replayed as fixtures (Afsaane staged target,
Seerr bind-mounted, active vs idle project, gitignored vs tracked, venv named by a systemd unit). The probes themselves
(inuse.py) have their own tests in test_inuse.py; here they are used through their public API."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import core, inuse
from homelab_maint.tasks import cleaners as cl
from homelab_maint.tasks import cleaners_dev as cd

REAL_STAMP = cd._stamp               # the sandbox replaces it by mtime-only (utime() cannot fake a ctime); ctime tests restore it
REAL_NTP = cd._ntp_synced            # the sandbox says "synchronized"; the clock-guard tests restore the real probe
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git needed for the project fixtures")

REAL_SH = core.sh
ME = os.getpid()
NOW = time.time()
DAY = 86400
MIB = 1024 ** 2
PROTECTED = {"patterns": ["immich", "plexmediaserver", "postgres", "tunarr", "kometa", "/mnt/backup", "buildkitd"]}
MUTATING = ("npm cache clean", "pip cache purge", "uv cache prune", "store prune", "rsync -aHSAX --", "rm ", "kill",
            "docker rm", "docker update")


# --------------------------------------------------------------------------- harness
class FakeSh:
    """`sh` stand-in. rows: (substring, response); the first row whose substring is in the command wins.
    response = (rc, out, err) or callable(cmd_str) -> that. Plain `git -C` commands run for real (on tmp repos only)."""

    def __init__(self, *rows, real=("git -C",)):
        self.rows, self.calls, self.real, self.timeouts = list(rows), [], real, {}

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        self.timeouts[key] = timeout
        for sub, resp in self.rows:
            if sub in key:
                rc, out, err = resp(key) if callable(resp) else resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        if key.startswith(self.real):
            env = {**(kw.get("env") or {}), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
            return REAL_SH(cmd, timeout=timeout, env=env)
        return subprocess.CompletedProcess(cmd, 127, "", "unmocked: " + key)

    def mutating(self):
        return [c for c in self.calls if any(m in c for m in MUTATING)]


def ok(out=""):
    return (0, out, "")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    inuse.reset_caches()
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(core, "CONF_DIR", tmp_path / "conf")
    (tmp_path / "conf").mkdir()
    monkeypatch.setattr(core, "sh", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))   # logger
    monkeypatch.setattr(cl, "_busy", lambda name: (False, "idle"))
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "")
    monkeypatch.setattr(cd, "_ntp_synced", lambda: True)                  # the clock guard has its own tests
    monkeypatch.setattr(cd, "_stamp", lambda st: st.st_mtime)             # os.utime cannot fake a ctime
    (tmp_path / "quiet").mkdir()
    (tmp_path / "quiet" / "base.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    QUIET[:] = [str(tmp_path / "quiet")]
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n")
    monkeypatch.setattr(cd, "MOUNTINFO", str(tmp_path / "mountinfo"))
    use_sh(monkeypatch)
    mounts(monkeypatch, {})                                              # docker: no container mounts anything
    proc = tmp_path / "proc"
    monkeypatch.setattr(cd, "PROC", proc)
    monkeypatch.setattr(inuse, "PROC", proc)
    make_proc(proc, [dict(pid=1, comm="systemd", cwd="/")])              # a complete, quiet process table
    clock = [1000.0]
    for mod in (cd, cl, inuse):
        monkeypatch.setattr(mod, "_mono", lambda: clock[0])
    yield clock
    inuse.reset_caches()


def use_sh(monkeypatch, *rows, **kw) -> FakeSh:
    f = FakeSh(*rows, **kw)
    for mod in (cd, cl, inuse):
        monkeypatch.setattr(mod, "sh", f)
    return f


def mounts(monkeypatch, value):
    """What `docker inspect` says: {container: [host sources]} or None (docker cannot say)."""
    monkeypatch.setattr(inuse, "container_bind_mounts", lambda running_only=False: value)


QUIET = ["/nonexistent"]             # replaced by the sandbox: a directory with one harmless file (an empty search proves nothing)
HERMETIC = {"stale_build_output": True, "unused_venvs": True, "large_cold_files": True}


def mk(name, *, apply=False, now=NOW, protected=None, **opts):
    if name in HERMETIC:                                    # never read the real /etc, ~/.config or walk the real home
        opts = {"ref_paths": QUIET, "link_roots": [], **opts}
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED if protected is None else protected,
           "tasks": {name: {"mode": "apply" if apply else "report", **opts}}}
    return core.Ctx(cfg, name, apply, now)


def audit_rows(tmp_path):
    p = tmp_path / "log" / "audit.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def outcomes(tmp_path):
    return [r["outcome"] for r in audit_rows(tmp_path)]


def ascii_ok(res):
    assert len(res.summary) <= 140 and res.summary.isascii(), res.summary
    assert len(res.items) <= 12
    json.dumps(res.metrics)
    assert all(i["proof"].isascii() for i in res.items if "proof" in i)


def make_proc(proc: Path, procs, reset=True):
    """A fake /proc. Each proc: pid, comm, cmdline=[...], cwd, exe, fds=[paths], maps=[paths], environ={}.
    Our own pid is always there (inuse refuses a snapshot that cannot see its own process)."""
    shutil.rmtree(proc, ignore_errors=True)
    proc.mkdir(parents=True)
    procs = list(procs)
    if not any(p["pid"] == ME for p in procs):
        procs.append(dict(pid=ME, comm="pytest", cwd="/"))
    for p in procs:
        d = proc / str(p["pid"])
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(p.get("comm", "x") + "\n")
        if "age_s" in p:                                              # field 22 of /proc/<pid>/stat is the start time in ticks
            ticks = int((100000 - p["age_s"]) * cl.CLK_TCK)
            (d / "stat").write_text(f"{p['pid']} ({p.get('comm', 'x')}) S 1 {'0 ' * 13}20 0 1 0 {ticks} 0\n")
        else:
            (d / "stat").write_text(f"{p['pid']} ({p.get('comm', 'x')}) S 1 0 0 0\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in p.get("cmdline", [])) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in p.get("environ", {}).items()))
        (d / "maps").write_text("".join(f"7f00-7f01 r-xp 00000000 08:01 12 {m}\n" for m in p.get("maps", [])))
        os.symlink(p.get("cwd", "/"), d / "cwd")
        if "exe" in p:
            os.symlink(p["exe"], d / "exe")
        for i, f in enumerate(p.get("fds", [])):
            os.symlink(f, d / "fd" / str(3 + i))
    (proc / "uptime").write_text("100000.00 0.00\n")
    if reset:
        inuse.reset_caches()


REAL_READ = inuse._read_text


def deny(monkeypatch, pid):
    """Process `pid` cannot be read (what a non-root run sees for somebody else's process)."""
    def read(p, limit=-1):
        if f"/{pid}/maps" in p:
            raise PermissionError(13, "denied")
        return REAL_READ(p, limit)

    monkeypatch.setattr(inuse, "_read_text", read)
    inuse.reset_caches()


def undeny(monkeypatch):
    monkeypatch.setattr(inuse, "_read_text", REAL_READ)
    inuse.reset_caches()


def age(path, days, now=NOW):
    os.utime(path, (now - days * DAY, now - days * DAY), follow_symlinks=False)


def age_tree(root, days, skip_git=True):
    for dp, dn, fn in os.walk(root):
        if skip_git and ".git" in dn:
            dn.remove(".git")
        for n in dn + fn:
            age(os.path.join(dp, n), days)
        age(dp, days)


def git(cwd, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{int(when)} +0000"
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


CACHEDIR = "Signature: 8a477f597d28d172789f06886806bc55\n# created by cargo / pytest / mypy\n"


def make_output(d, name):
    """Build output as the tool leaves it: with the signature file that proves which tool made it."""
    d.mkdir(parents=True, exist_ok=True)
    if name == "__pycache__":
        (d / "a.cpython-312.pyc").write_bytes(b"o" * 4096)
        (d / "b.cpython-312.pyc").write_bytes(b"m" * 4096)
        return
    if name == ".turbo":
        (d / "cache").mkdir(exist_ok=True)
        (d / "cache" / "out.bin").write_bytes(b"o" * 4096)
        return
    (d / "out.bin").write_bytes(b"o" * 4096)
    (d / "sub").mkdir(exist_ok=True)
    (d / "sub" / "more.bin").write_bytes(b"m" * 4096)
    if name in ("target", ".pytest_cache", ".mypy_cache"):
        (d / "CACHEDIR.TAG").write_text(CACHEDIR)
    if name == ".next":
        (d / "BUILD_ID").write_text("abc123")


def project(root, name, *, commit_days=200, files_days=200, ignore=("target/", "dist/", "build/", "__pycache__/", ".venv/"),
            manifest="Cargo.toml", builds=("target",), build_days=200, tracked_build=False):
    """A git project with the given build dirs. Commit, working tree (and the HEAD reflog) are aged to `commit_days` /
    `files_days`, the build output to `build_days`. A package.json carries a build script that produces those dirs."""
    p = Path(root) / name
    p.mkdir(parents=True)
    git(p, "init", "-q")
    (p / ".gitignore").write_text("\n".join(ignore) + "\n")
    if manifest == "package.json":
        (p / manifest).write_text(json.dumps({"name": name, "scripts": {"build": "bundler --out " + " ".join(builds), "lint": "eslint ."}}))
    elif manifest:
        (p / manifest).write_text("x\n")
    (p / "src").mkdir()
    (p / "src" / "main.rs").write_text("fn main(){}\n")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init", when=NOW - commit_days * DAY)
    for ref in (("HEAD",), ("logs", "HEAD")):                        # inuse counts ref movement (checkout, pull) as activity
        age(p / ".git" / Path(*ref), commit_days)
    for b in builds:
        d = p / b
        make_output(d, Path(b).name)
        if tracked_build:
            git(p, "add", "-f", b)
        age_tree(d, build_days)
    for dp, dn, fn in os.walk(p):
        if ".git" in dn:
            dn.remove(".git")
        if os.path.relpath(dp, p).split(os.sep)[0] in builds:
            continue
        for n in fn:
            age(os.path.join(dp, n), files_days)
        age(dp, files_days)
    inuse.reset_caches()
    return p


def edit(path, text, days):
    """A real content change (git status sees it) with the mtime `days` ago."""
    Path(path).write_text(text)
    age(path, days)
    inuse.reset_caches()


def rows(res):
    return {i["name"]: i for i in res.items}


def tree_bytes(path):
    """What the tasks measure (apparent size incl. directory entries; differs per filesystem)."""
    return cl._tree_stats(str(path))[0]


def approve(tmp_path, task, plan, age_s=0):
    ap = tmp_path / "state" / "approvals"
    ap.mkdir(parents=True, exist_ok=True)
    f = ap / f"{task}.{core.plan_hash(plan)}"
    f.write_text("1")
    os.utime(f, (time.time() - age_s,) * 2)
    return f


# =========================================================================== registry
def test_tasks_registered_with_the_specified_classes():
    R = core.REGISTRY
    assert (R["tool_caches"].klass, R["tool_caches"].tier) == ("C1", "daily")
    assert (R["stale_build_output"].klass, R["stale_build_output"].tier) == ("C1", "weekly")
    assert (R["unused_venvs"].klass, R["unused_venvs"].tier) == ("C2", "weekly")
    assert (R["large_cold_files"].klass, R["large_cold_files"].tier) == ("C2", "weekly")
    for n in ("tool_caches", "stale_build_output", "unused_venvs", "large_cold_files"):
        assert R[n].title and R[n].timeout >= 600 and R[n].needs_root, n


# =========================================================================== in-use glue (the probes live in inuse.py)
def test_snapshot_problem_names_why_no_proof_is_possible(tmp_path, monkeypatch):
    assert cd._snapshot_problem() == ""
    deny(monkeypatch, 1)                                                      # not root: another user's process is unreadable
    assert "process table unusable" in cd._snapshot_problem() and "unreadable" in cd._snapshot_problem()
    undeny(monkeypatch)
    assert cd._snapshot_problem() == ""
    monkeypatch.setattr(inuse, "PROC", tmp_path / "nowhere")
    assert "unusable" in cd._snapshot_problem()


def test_docker_unable_to_list_mounts_is_a_problem_too(monkeypatch):
    mounts(monkeypatch, None)
    assert cd._snapshot_problem() == "docker mounts unknown"


def test_held_is_unused_only_when_no_container_mounts_it_and_no_process_holds_it(tmp_path, monkeypatch):
    p = str(tmp_path / "proj")
    assert cd._held(p).unused and "no container mounts it" in cd._held(p).why and "processes" in cd._held(p).why
    mounts(monkeypatch, {"seerr": [p]})                                        # a (running or stopped) container bind-mounts it
    assert cd._held(p).used and cd._held(p).known and "mounted by container seerr" in cd._held(p).why
    mounts(monkeypatch, {"other": ["/elsewhere"], "everything": ["/"]})        # a bind of "/" is ignored, not everything
    assert cd._held(p).unused
    mounts(monkeypatch, None)                                                   # docker cannot say: unknown beats unused
    pr = cd._held(p)
    assert pr.used and not pr.known and not pr.unused
    mounts(monkeypatch, {})
    make_proc(cd.PROC, [dict(pid=7, comm="cargo", cwd=p + "/src")])
    assert "pid 7 (cargo) cwd" in cd._held(p).why and cd._held(p).used
    deny(monkeypatch, 7)
    assert not cd._held(p).known                                                # an unreadable process makes everything unknown


def test_held_kinds_project_ignores_environ_but_venvs_count_it(tmp_path):
    p = str(tmp_path / "venv")
    make_proc(cd.PROC, [dict(pid=7, comm="bash", environ={"VIRTUAL_ENV": p, "PWD": p})])
    assert cd._held(p).unused                                                   # PROJ_KINDS: environ paths are noise (PWD)
    assert cd._held(p, None).used and "env" in cd._held(p, None).why           # a venv activated in a shell is in use


def test_recheck_rescans_at_most_once_per_ttl(tmp_path, sandbox):
    p = str(tmp_path / "proj")
    rc = cd._Recheck(ttl=20)
    assert rc.held(p).unused                                                    # first call: fresh probes
    make_proc(cd.PROC, [dict(pid=7, comm="cargo", cwd=p)], reset=False)         # a build starts in the project ...
    assert rc.held(p).unused                                                    # ... within the ttl the cached table is reused
    sandbox[0] += 25
    assert rc.held(p).used and "pid 7 (cargo)" in rc.held(p).why                # stale: one full re-scan sees it


def test_odd_names_below_and_single_device(tmp_path):
    for bad in ("/p/app[id]/dist", "/p/a*/dist", "/p/:(top)/dist", "/p/a\nb/dist", "/p/a?/dist", "/p/a\\b/dist"):
        assert cd._odd(bad), bad
    assert not cd._odd("/p/app/dist") and not cd._odd("/p/my-app.v2/target")
    assert cd._below("/r/p", "/r") and not cd._below("/r", "/r") and not cd._below("/x/p", "/r") and not cd._below("", "/r")
    (tmp_path / "t" / "a").mkdir(parents=True)
    assert cd._single_device(str(tmp_path / "t")) and not cd._single_device(str(tmp_path / "missing"))
    assert not cd._single_device("/")                                           # /proc, /sys, /dev are other filesystems
    assert not cd._single_device(str(tmp_path / "t"), limit=0)                  # too big to prove


def test_as_owner_runs_plain_as_the_owner_through_runuser_as_root_and_never_otherwise(monkeypatch):
    me = os.geteuid()
    assert cd._as_owner(me, ["x", "1"]) == ["x", "1"]
    assert cd._as_owner(me, ["x"], ["A=1"]) == ["env", "A=1", "x"]
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    monkeypatch.setattr(cd, "_user_of", lambda uid: ("alice", "/home/alice"))
    assert cd._as_owner(4242, ["git", "log"], ["A=1"]) == ["runuser", "-u", "alice", "--", "env", "HOME=/home/alice", "A=1", "git", "log"]
    assert cd._as_owner(4242, ["x"], ["HOME=/h"]) == ["runuser", "-u", "alice", "--", "env", "HOME=/h", "x"]    # no second HOME
    monkeypatch.setattr(cd, "_user_of", lambda uid: None)                       # unknown owner: never fall back to root
    assert cd._as_owner(4242, ["x"]) is None
    monkeypatch.setattr(cd, "_euid", lambda: 1000)
    monkeypatch.setattr(cd, "_user_of", lambda uid: ("alice", "/home/alice"))
    assert cd._as_owner(4242, ["x"]) is None                                    # neither root nor the owner


def test_never_touch_floor_and_extra_patterns(tmp_path):
    ctx = mk("x", never_touch=["secret-proj"], protected={"patterns": []})
    for p in ("/home/u/ai-stack/a/target", "/home/u/models/x", "/home/u/.config/Cursor/User", "/home/u/.cursor/worktrees",
              "/var/lib/docker/volumes/v", "/media/Immich/lib", "/media/nextcloud/data", "/mnt/backup/system",
              "/home/u/.config/google-chrome/Default", "/x/surreal_data/y", "/x/pgdata", "/var/lib/libvirt/images",
              "/home/u/StudioProjects/secret-proj/dist"):
        assert cd._never(ctx, p), p
    for p in ("/home/u/StudioProjects/app/target", "/home/u/StudioProjects/models-app/dist", "/home/u/.venv"):
        assert cd._never(ctx, p) == "", p
    assert cd._never(mk("x", never_touch=["("], protected={"patterns": []}), "/a/b") == "bad never_touch pattern"
    assert cd._never(mk("x", never_touch="oops", protected={"patterns": []}), "/a/b")        # malformed config protects
    assert cd._never(mk("x"), "/h/tunarr/target") == "protected.toml"


# =========================================================================== stale_build_output
def build_ctx(root, apply=False, **kw):
    return mk("stale_build_output", apply=apply, projects_root=str(root), **{"apply_generic": True, **kw})


def run_build(tmp_path, apply=False, **kw):
    return cd.stale_build_output(build_ctx(tmp_path / "proj", apply, **kw))


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_idle_ignored_untracked_unmounted_unused_build_dir_is_selected_with_its_proof(tmp_path, proj):
    p = project(proj, "oldapp", builds=("target",))
    res = run_build(tmp_path)
    ascii_ok(res)
    assert res.status == "info" and res.metrics["selected"] == 1 and "would free" in res.summary
    row = rows(res)["oldapp/target"]
    assert row["state"] == "would" and row["size"] == core.human(tree_bytes(p / "target"))
    proof = row["proof"]
    assert "ignored" in proof and "untracked" in proof and "repo idle 200 d" in proof        # git state
    assert "no container mounts it" in proof and "processes" in proof                        # docker + /proc
    assert (p / "target" / "out.bin").exists()                              # dry-run deleted nothing
    assert set(outcomes(tmp_path)) == {"dry-run"}


def test_apply_deletes_exactly_what_the_dry_run_listed_and_reports_the_bytes(tmp_path, proj):
    a = project(proj, "a", builds=("target", "dist"), manifest="package.json")
    b = project(proj, "b", builds=("target",))
    total = sum(tree_bytes(d) for d in (a / "target", a / "dist", b / "target"))
    dry = run_build(tmp_path)
    listed = {r["target"] for r in audit_rows(tmp_path)}
    assert listed == {str(a / "target"), str(a / "dist"), str(b / "target")}
    res = run_build(tmp_path, apply=True)
    assert not (a / "target").exists() and not (a / "dist").exists() and not (b / "target").exists()
    assert (a / "src" / "main.rs").exists() and (a / ".gitignore").exists()      # only the build dirs went
    assert {r["target"] for r in audit_rows(tmp_path) if r["outcome"] == "done"} == listed
    assert res.metrics["selected"] == 3 and res.reclaimed_bytes == total and res.status == "ok"
    again = run_build(tmp_path, apply=True)                                    # idempotent
    assert again.metrics["selected"] == 0 and again.reclaimed_bytes == 0 and "no stale build output" in again.summary
    assert dry.metrics["selected"] == 3


def test_afsaane_like_staged_target_is_kept_even_though_ignored_and_idle(tmp_path, proj):
    p = project(proj, "Afsaane", builds=("server-rs/target",), manifest="Cargo.toml")
    (p / "server-rs").mkdir(exist_ok=True)
    (p / "server-rs" / "Cargo.toml").write_text("x")
    git(p, "add", "-f", "server-rs/target")                                   # staged, never committed
    res = run_build(tmp_path, apply=True)
    assert (p / "server-rs" / "target" / "out.bin").exists()
    r = rows(res)["Afsaane/server-rs/target"]
    assert r["state"] == "kept" and "tracked or staged" in r["proof"]
    assert res.metrics["kept_tracked_or_staged_in_git"] == 1 and res.metrics["selected"] == 0


def test_a_committed_build_dir_and_a_dir_that_is_not_ignored_are_kept(tmp_path, proj):
    t = project(proj, "tracked", builds=("dist",), tracked_build=True, manifest="package.json")
    git(t, "commit", "-q", "-m", "build", when=NOW - 200 * DAY)
    for ref in (("HEAD",), ("logs", "HEAD")):
        age(t / ".git" / Path(*ref), 200)
    n = project(proj, "noignore", builds=("target",), ignore=("nothing/",))
    res = run_build(tmp_path, apply=True)
    assert (t / "dist" / "out.bin").exists() and (n / "target" / "out.bin").exists()
    r = rows(res)
    assert "tracked" in r["tracked/dist"]["proof"] and "not git-ignored" in r["noignore/target"]["proof"]


def test_active_project_is_kept_by_recent_commit_modified_or_new_files_or_a_checkout_but_not_by_ignored_output(tmp_path, proj):
    project(proj, "fresh-commit", commit_days=5, files_days=200)
    fe = project(proj, "fresh-edit", commit_days=300, files_days=300)
    edit(fe / "src" / "main.rs", "fn main(){ changed }\n", 10)                  # edited 10 days ago, never committed
    fn = project(proj, "fresh-new", commit_days=300, files_days=300)
    (fn / "notes.txt").write_text("hi")                                          # new untracked file today
    fc = project(proj, "fresh-checkout", commit_days=300, files_days=300)
    os.utime(fc / ".git" / "logs" / "HEAD", (NOW - 2 * DAY,) * 2)               # HEAD moved 2 days ago (checkout/pull)
    idle = project(proj, "idle", commit_days=300, files_days=300, build_days=0)  # only the ignored build output is fresh
    res = run_build(tmp_path, apply=True, output_days=0)
    for n in ("fresh-commit", "fresh-edit", "fresh-new", "fresh-checkout"):
        assert (proj / n / "target" / "out.bin").exists(), n
        r = rows(res)[f"{n}/target"]
        assert r["state"] == "kept" and "project active" in r["proof"], (n, r)
    assert not (idle / "target").exists()                                       # idle project: build output fresh, project not
    assert res.metrics["kept_project_active"] == 4


def test_a_deleted_tracked_file_counts_as_a_change_now(tmp_path, proj):
    p = project(proj, "app", commit_days=300, files_days=300)
    (p / "src" / "main.rs").unlink()                                            # uncommitted deletion: unknown when
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "project active" in rows(res)["app/target"]["proof"]


def test_recently_built_output_of_an_idle_project_is_kept_unless_output_days_is_zero(tmp_path, proj):
    p = project(proj, "built-lately", commit_days=300, files_days=300, build_days=3)
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "output touched" in rows(res)["built-lately/target"]["proof"]
    run_build(tmp_path, apply=True, output_days=0)
    assert not (p / "target").exists()


def test_seerr_like_bind_mounted_project_is_kept_running_or_stopped_and_unrelated_mounts_are_not(tmp_path, proj, monkeypatch):
    seerr = project(proj, "seerr", builds=("dist",), manifest="package.json")
    other = project(proj, "other", builds=("dist",), manifest="package.json")
    third = project(proj, "third", builds=("dist",), manifest="package.json")
    mounts(monkeypatch, {"Seerr": [str(seerr)], "stopped-dev": [str(third / "dist")],
                         "unrelated": ["/var/lib/foo", str(proj / "unrelated-dir")]})
    res = run_build(tmp_path, apply=True)
    assert (seerr / "dist").exists() and (third / "dist").exists() and not (other / "dist").exists()
    assert rows(res)["seerr/dist"]["proof"].startswith("in use: mounted by container Seerr")
    assert "mounted by container stopped-dev" in rows(res)["third/dist"]["proof"]


def test_process_cwd_open_file_mapped_file_or_argv_under_the_project_keeps_it_but_environ_does_not(tmp_path, proj):
    cases = {"by-cwd": dict(cwd="{p}/src"), "by-fd": dict(fds=["{p}/target/sub/more.bin"]),
             "by-map": dict(maps=["{p}/target/out.bin"]), "by-argv": dict(cmdline=["node", "{p}/server.js"]),
             "by-env": dict(environ={"PWD": "{p}"}), "free": dict(cwd="/tmp")}
    projects = {n: project(proj, n) for n in cases}

    def fmt(p, v):
        return [x.format(p=p) for x in v] if isinstance(v, list) else {k: x.format(p=p) for k, x in v.items()} \
            if isinstance(v, dict) else v.format(p=p)

    make_proc(cd.PROC, [dict(pid=100 + i, comm="worker", **{k: fmt(projects[n], v) for k, v in spec.items()})
                        for i, (n, spec) in enumerate(cases.items())])
    res = run_build(tmp_path, apply=True)
    for n in cases:
        assert (projects[n] / "target").exists() == (n not in ("free", "by-env")), n     # PWD in an environ is noise
    assert rows(res)["by-cwd/target"]["proof"].startswith("in use: pid 100 (worker) cwd")


def test_unreadable_process_table_or_unknown_docker_mounts_select_nothing(tmp_path, proj, monkeypatch):
    p = project(proj, "idle")
    deny(monkeypatch, 1)                                                          # not root: cannot see everything
    res = run_build(tmp_path, apply=True)
    assert res.status == "skipped" and "unreadable" in res.summary and (p / "target").exists()
    undeny(monkeypatch)
    mounts(monkeypatch, None)
    res = run_build(tmp_path, apply=True)
    assert res.status == "skipped" and "mounts unknown" in res.summary and (p / "target").exists()
    mounts(monkeypatch, {})
    monkeypatch.setattr(inuse, "PROC", tmp_path / "gone")
    assert run_build(tmp_path, apply=True).status == "skipped" and (p / "target").exists()


@pytest.mark.parametrize("failing", ["rev-parse", "check-ignore", "ls-files", "log -1", "status --porcelain"])
def test_git_failures_keep_the_directory_fail_closed(tmp_path, proj, monkeypatch, failing):
    p = project(proj, "idle")
    f = use_sh(monkeypatch, (failing, (128, "", "fatal: boom")))
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and res.metrics["selected"] == 0 and not f.mutating()
    assert "unknown" in rows(res)["idle/target"]["proof"]


def test_protected_and_never_touch_projects_are_kept_even_when_idle(tmp_path, proj):
    t = project(proj, "tunarr-clone")                                           # matches the protected.toml pattern "tunarr"
    c = project(proj, "plain")
    res = run_build(tmp_path, apply=True)
    assert (t / "target").exists() and not (c / "target").exists()
    assert rows(res)["tunarr-clone/target"]["proof"] == "protected.toml"
    # an explicit per-task unprotect (the owner's decision, like retention's tunarr-subtitles) lifts it for that project
    cd.stale_build_output(mk("stale_build_output", apply=True, projects_root=str(proj), unprotect=["/tunarr-clone(/|$)"]))
    assert not (t / "target").exists()
    ai = project(proj / "ai-stack", "x")                                       # the never-touch floor has no unprotect
    cd.stale_build_output(mk("stale_build_output", apply=True, projects_root=str(proj), unprotect=["ai-stack"]))
    assert (ai / "target").exists()


def test_directories_that_are_not_build_output_candidates_are_never_even_looked_at(tmp_path, proj):
    p = project(proj, "app", builds=("target",), manifest="package.json")
    nm = p / "node_modules_old" / "pkg" / "dist"                                # a package's own dist
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module")
    venv = p / "tools" / "env"                                                  # a venv with a build/ inside
    (venv / "build").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin")
    nested = p / "target" / "debug" / "build"                                    # build dir inside a build dir
    nested.mkdir(parents=True)
    ignored_venv = p / ".venv" / "lib" / "build"
    ignored_venv.mkdir(parents=True)
    age_tree(p, 200)                                                             # the project as a whole is idle
    found, complete = cd._find_build_dirs(str(proj), 7, 100)
    assert complete and found == [str(p / "target")]
    res = run_build(tmp_path, apply=True)
    assert nm.exists() and (venv / "build").exists() and ignored_venv.exists() and not (p / "target").exists()
    assert res.metrics["found"] == 1


def test_symlinked_build_dir_is_not_followed_or_removed(tmp_path, proj):
    p = project(proj, "app", builds=())
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "data").write_text("keep me")
    os.symlink(outside, p / "target")
    res = run_build(tmp_path, apply=True)
    assert (outside / "data").read_text() == "keep me" and os.path.islink(p / "target")
    assert res.metrics["found"] == 0


def test_a_directory_without_a_build_manifest_is_not_provably_output_but_pycache_is(tmp_path, proj):
    a = project(proj, "nomanifest", builds=("dist", "__pycache__"), manifest=None)
    res = run_build(tmp_path, apply=True)
    assert (a / "dist").exists() and not (a / "__pycache__").exists()
    assert "no build manifest" in rows(res)["nomanifest/dist"]["proof"]


def test_project_outside_git_the_scan_root_repo_and_odd_names_are_kept(tmp_path, proj):
    loose = proj / "plain-dir"
    (loose / "target").mkdir(parents=True)
    (loose / "target" / "f").write_text("x")
    age_tree(loose, 400)
    real = project(tmp_path, "realproj")
    os.symlink(real, proj / "linkproj")                                          # a symlinked project dir is not walked
    odd = project(proj, "next-app", builds=("apps/[id]/dist",), manifest="package.json")    # git would glob "[id]"
    res = run_build(tmp_path, apply=True)
    assert (loose / "target" / "f").exists() and (real / "target").exists() and (odd / "apps/[id]/dist").exists()
    assert "not inside a git project" in rows(res)["plain-dir/target"]["proof"]
    assert "odd path name" in rows(res)["next-app/apps/[id]/dist"]["proof"]
    git(proj, "init", "-q")                                                      # a dotfiles-style repo AT the scan root
    res = run_build(tmp_path, apply=True)
    assert (loose / "target" / "f").exists() and "not inside a git project" in rows(res)["plain-dir/target"]["proof"]


def test_a_mount_point_inside_the_build_dir_keeps_it_and_stops_a_late_delete(tmp_path, proj, monkeypatch):
    p = project(proj, "app")
    monkeypatch.setattr(cd, "_single_device", lambda path, limit=0: False)       # like a bind mount below target/
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "mount point" in rows(res)["app/target"]["proof"]
    calls = []
    monkeypatch.setattr(cd, "_single_device", lambda path, limit=0: calls.append(1) or len(calls) == 1)   # appears after the scan
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and res.metrics["gone"] == 1


def test_race_between_scan_and_delete_a_process_appearing_keeps_the_directory(tmp_path, proj, monkeypatch):
    p = project(proj, "app")
    real_act = core.Ctx.act

    def act_after_a_process_appeared(self, what, target, size, fn, protect_names=()):
        if self.apply:
            make_proc(cd.PROC, [dict(pid=7, comm="cargo", cwd=str(p / "src"))])      # a build started in the project
        return real_act(self, what, target, size, fn, protect_names)

    monkeypatch.setattr(core.Ctx, "act", act_after_a_process_appeared)
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and res.metrics["gone"] == 1 and res.metrics["selected"] == 0
    assert "failed" not in outcomes(tmp_path) and "done" not in outcomes(tmp_path)


def test_directory_replaced_or_touched_after_the_scan_is_not_deleted(tmp_path, proj, monkeypatch):
    p = project(proj, "app")
    real_act = core.Ctx.act

    def swap_then_act(self, what, target, size, fn, protect_names=()):
        if self.apply:
            shutil.rmtree(target)
            os.mkdir(target)                                                       # a new directory at the same path
            (Path(target) / "new").write_text("fresh work")
        return real_act(self, what, target, size, fn, protect_names)

    monkeypatch.setattr(core.Ctx, "act", swap_then_act)
    res = run_build(tmp_path, apply=True)
    assert (p / "target" / "new").read_text() == "fresh work" and res.metrics["gone"] == 1


def test_report_mode_pause_and_busy_gates_never_delete(tmp_path, proj, monkeypatch):
    p = project(proj, "app")
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED,
           "tasks": {"stale_build_output": {"mode": "report", "projects_root": str(proj), "ref_paths": QUIET, "link_roots": [],
                                            "apply_generic": True}}}
    cd.stale_build_output(core.Ctx(cfg, "stale_build_output", True, NOW))          # --apply but mode=report
    assert (p / "target").exists()
    cfg["tasks"]["stale_build_output"]["mode"] = "apply"
    (tmp_path / "conf" / "PAUSE.stale_build_output").write_text("x")
    cd.stale_build_output(core.Ctx(cfg, "stale_build_output", True, NOW))
    assert (p / "target").exists()
    (tmp_path / "conf" / "PAUSE.stale_build_output").unlink()
    monkeypatch.setattr(cl, "_busy", lambda name: (name == "gradle", "gradle build running"))
    res = cd.stale_build_output(core.Ctx(cfg, "stale_build_output", True, NOW))
    assert res.status == "skipped" and "gradle" in res.summary and (p / "target").exists()


def test_caps_stop_the_run_and_oversize_items_are_skipped(tmp_path, proj):
    for n in ("a", "b", "c"):
        project(proj, n)
    res = run_build(tmp_path, apply=True, max_items_per_run=2)
    assert res.metrics["selected"] == 2 and res.metrics["deferred"] >= 1 and res.status == "info"
    assert sum((proj / n / "target").exists() for n in "abc") == 1
    res = run_build(tmp_path, apply=True, max_gib_per_run=0.000001)                 # every item is over the byte cap
    assert res.metrics["oversize"] == 1 and sum((proj / n / "target").exists() for n in "abc") == 1


def test_bad_config_selects_nothing(tmp_path, proj):
    project(proj, "a")
    for bad in ({"idle_days": "x"}, {"idle_days": -1}, {"output_days": True}, {"max_depth": 0}, {"scan_budget_s": None}):
        assert run_build(tmp_path, apply=True, **bad).status == "skipped", bad
    assert (proj / "a" / "target").exists()
    assert cd.stale_build_output(mk("stale_build_output", projects_root=str(tmp_path / "missing"))).status == "skipped"


def test_every_directory_name_of_the_spec_is_build_output_and_other_names_are_not(tmp_path, proj):
    names = ("target", "dist", "build", ".next", ".turbo", "__pycache__", ".pytest_cache", ".mypy_cache", "docs/.vitepress/dist")
    others = ("out", "coverage", "node_modules", "cache", ".cache", "bin", "obj")
    p = project(proj, "everything", builds=names + others, manifest="package.json",
                ignore=("target/", "dist/", "build/", ".next/", ".turbo/", "__pycache__/", ".pytest_cache/", ".mypy_cache/",
                        ".vitepress/", "out/", "coverage/", "node_modules/", "cache/", ".cache/", "bin/", "obj/"))
    res = run_build(tmp_path, apply=True)
    for n in names:
        assert not (p / n).exists(), n
    for n in others:
        assert (p / n / "out.bin").exists(), n
    assert res.metrics["selected"] == len(names) and (p / "docs").exists() and (p / "docs" / ".vitepress").exists()


def test_items_show_the_biggest_skips_next_to_the_selected_rows_and_stay_within_twelve(tmp_path, proj):
    for i in range(13):
        project(proj, f"idle{i:02d}")
    for i in range(3):
        project(proj, f"tracked{i}", builds=("dist",), tracked_build=True, manifest="package.json")
    res = run_build(tmp_path)
    ascii_ok(res)
    assert len(res.items) == 12 and sum(1 for i in res.items if i["state"] == "kept") == 3
    assert sum(1 for i in res.items if i["state"] == "would") == 9 and res.metrics["selected"] == 13
    assert all(i["proof"] for i in res.items)


def test_the_nearest_manifest_owner_decides_whether_dist_is_the_output_of_a_build_recipe(tmp_path):
    top = tmp_path / "proj"
    d = top / "apps" / "web" / "dist"
    d.mkdir(parents=True)
    (top / "apps" / "web" / "src").mkdir()
    tree = cd._inspect(str(d))
    assert cd._owner_builds(str(d), str(top), tree) == "no build manifest above it"
    (tmp_path / "package.json").write_text("{}")                                      # above the project: does not count
    assert cd._owner_builds(str(d), str(top), tree) == "no build manifest above it"
    (top / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))   # a manifest higher up, but not the nearest
    (top / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint ."}}))   # the nearest has no build script
    assert "no build recipe" in cd._owner_builds(str(d), str(top), tree)
    (top / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))  # vite builds into dist
    assert cd._owner_builds(str(d), str(top), tree) == ""
    (top / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {"build": "electron-forge make --platform=darwin"}}))
    assert "no build recipe" in cd._owner_builds(str(d), str(top), tree)             # the codex-app shape: output is out/, not dist/
    (top / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    (top / "apps" / "web" / "tsconfig.json").write_text('{"compilerOptions": {"outDir": "./dist"}}')
    assert cd._owner_builds(str(d), str(top), tree) == ""
    shutil.rmtree(top / "apps" / "web" / "src")
    assert cd._owner_builds(str(d), str(top), tree) == "no sources next to the build recipe"


# =========================================================================== tool_caches
@pytest.fixture
def tc(tmp_path, monkeypatch):
    """Home with fake tool binaries; docker/pm commands mocked; the process table has no package manager running."""
    home = tmp_path / "home" / "ohmz"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    for n in ("npm", "npx", "pip", "uv", "pnpm"):
        (bindir / n).write_text("#!/bin/sh\n")
        (bindir / n).chmod(0o755)
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(home), os.geteuid()))
    return home


def fill(path, n=3, size=4096, age_days=0):
    for i in range(n):
        f = Path(path) / f"f{i}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"c" * size)
        age(f, age_days)


def tc_ctx(apply=False, **kw):
    return mk("tool_caches", apply=apply, min_mib=0.001, **kw)


def pm_mock(home, monkeypatch, **extra):
    """Answers the cache-dir probes and records/executes the clean commands (simulated by deleting the cache)."""
    cache = {"npm": home / ".npm", "pip": home / ".cache" / "pip", "uv": home / ".cache" / "uv"}
    rows = [("npm config get cache", ok(f"{cache['npm']}\n")), ("pip cache dir", ok(f"{cache['pip']}\n")),
            ("uv cache dir", ok(f"{cache['uv']}\n")), ("pnpm --version", ok("10.9.0\n"))]
    wipe = lambda d: (lambda cmd: (shutil.rmtree(d, ignore_errors=True), ok("done"))[1])
    rows += [("npm cache clean --force", wipe(cache["npm"] / "_cacache")), ("pip cache purge", wipe(cache["pip"])),
             ("uv cache prune", lambda cmd: (shutil.rmtree(cache["uv"] / "archive-v0" / "dead", ignore_errors=True), ok())[1])]
    return use_sh(monkeypatch, *extra.get("rows", []), *rows), cache


def test_dry_run_lists_every_cache_with_proof_and_changes_nothing(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache", age_days=1)
    fill(cache["pip"])
    fill(cache["uv"] / "archive-v0" / "dead", n=4)
    res = cd.tool_caches(tc_ctx())
    ascii_ok(res)
    r = rows(res)
    assert {k.split()[0] for k in r} == {"npm", "pip", "uv"} and all(v["state"] == "would" for v in r.values())
    assert all("not running" in v["proof"] for v in r.values())
    assert res.metrics["selected"] == 3 and not f.mutating()
    assert (cache["npm"] / "_cacache" / "f0").exists() and (cache["uv"] / "archive-v0" / "dead" / "f0").exists()
    assert set(outcomes(tmp_path)) == {"dry-run"}
    assert "size unknown: uv cache" in res.summary                          # a prune verb: the dry-run cannot know the bytes


def test_apply_runs_each_tool_as_the_owner_and_measures_freed_bytes(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache", n=3, size=4096)
    fill(cache["pip"], n=2, size=4096)
    res = cd.tool_caches(tc_ctx(apply=True))
    assert [c for c in f.mutating() if "npm cache clean" in c][0].endswith("npm cache clean --force")
    assert any(c.endswith("pip cache purge") for c in f.mutating())
    assert not (cache["npm"] / "_cacache").exists() and not cache["pip"].exists()
    assert res.reclaimed_bytes == 3 * 4096 + 2 * 4096 and res.status == "ok"
    assert "uv cache prune" not in " ".join(f.mutating()) or "--force" not in " ".join(f.mutating())
    assert all("--force" not in c for c in f.calls if "uv " in c)           # never uv --force (ignores the in-use check)
    assert tc_ctx(apply=True).freed == 0
    again = cd.tool_caches(tc_ctx(apply=True))                               # idempotent: nothing left above the floor
    assert again.metrics["selected"] == 0 and again.summary.startswith("no tool cache needs cleaning")


def test_root_runs_tools_via_runuser_with_the_owners_home_and_path(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(tc), 4242))          # a uid that is not ours
    monkeypatch.setattr(cd, "_user_of", lambda uid: ("ohmz", str(tc)))
    cd.tool_caches(tc_ctx(apply=True))
    cmd = [c for c in f.calls if "npm cache clean" in c][0]
    assert cmd.startswith(f"runuser -u ohmz -- env HOME={tc} PATH={tc}/.local/bin:")
    probe = [c for c in f.calls if "npm config get cache" in c][0]
    assert probe.startswith("runuser -u ohmz --")


def test_neither_root_nor_the_owner_degrades_to_doing_nothing(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(tc), os.geteuid() + 1))
    res = cd.tool_caches(tc_ctx(apply=True))
    assert res.status == "skipped" and "not root" in res.summary and not f.calls
    monkeypatch.setattr(cl, "_home_of", lambda user: None)
    assert cd.tool_caches(tc_ctx(apply=True)).status == "skipped"


@pytest.mark.parametrize("argv,blocked", [
    (["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "install"], "npm"),
    (["npm", "ci"], "npm"), (["npx", "vite"], "npm"), (["npx", "--yes", "pnpm@10", "store", "prune"], "npm"),
    (["python3", "-m", "pip", "install", "x"], "pip"), (["/usr/bin/pip3", "download", "y"], "pip"),
    (["/home/u/.local/bin/uv", "sync"], "uv"), (["uv", "pip", "install", "z"], "uv"),
    (["npm", "run", "dev"], None), (["npm", "start"], None), (["uv", "run", "server.py"], "uv"),     # uv run resolves into the cache
    (["python3", "app.py"], None), (["vim", "package.json"], None)])
def test_a_running_package_manager_blocks_only_its_own_cache(tmp_path, tc, monkeypatch, argv, blocked):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    fill(cache["pip"])
    fill(cache["uv"])
    make_proc(cd.PROC, [dict(pid=500, comm=os.path.basename(argv[0])[:15], cmdline=argv, cwd="/")])
    res = cd.tool_caches(tc_ctx(apply=True))
    ran = {t: any(f"{t} cache" in c for c in f.mutating()) for t in ("npm", "pip", "uv")}
    assert ran == {t: t != blocked for t in ran}, (argv, ran)                       # only the busy tool's cache is left alone
    if blocked:
        assert any(f"{blocked} running" in r["proof"] for r in res.items if r["state"] == "kept")


def test_cache_dir_the_tool_reports_must_lie_below_the_home_and_a_big_cache_that_cannot_be_measured_is_kept(tmp_path, tc, monkeypatch):
    f = use_sh(monkeypatch, ("npm config get cache", ok("/etc\n")), ("pip cache dir", ok(f"{tc}\n")),
               ("uv cache dir", (1, "", "boom")))
    res = cd.tool_caches(tc_ctx(apply=True))
    assert not f.mutating() and res.metrics["selected"] == 0
    assert res.metrics["kept"] == 3 and "cache dir unknown or outside home" in res.items[0]["proof"]
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache", n=5)
    monkeypatch.setattr(cd, "_dir_size", lambda path, budget_s=120.0: None)
    res = cd.tool_caches(tc_ctx(apply=True))
    assert not f.mutating() and any("too large to measure" in r["proof"] for r in res.items)


def test_small_caches_are_left_alone_and_a_failing_tool_is_a_failure_not_a_crash(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache", n=1, size=100)
    res = cd.tool_caches(mk("tool_caches", apply=True, min_mib=64))
    assert res.metrics["selected"] == 0 and not f.mutating()
    f, cache = pm_mock(tc, monkeypatch, rows=[("pip cache purge", (1, "", "pip exploded"))])
    f.rows.insert(0, ("pip cache purge", (1, "", "pip exploded")))
    fill(cache["pip"], n=3)
    res = cd.tool_caches(tc_ctx(apply=True))
    assert res.status == "warn" and res.metrics["failed"] == 1 and "pip" in res.summary
    assert "failed" in " ".join(outcomes(tmp_path))
    assert cache["pip"].exists()


def make_pnpm_store(home, version="v10", orphan=(2, 4096), linked=(3, 4096)):
    store = home / ".local" / "share" / "pnpm" / "store" / version
    files = store / "files" / "ab"
    files.mkdir(parents=True)
    for i in range(orphan[0]):
        (files / f"orphan{i}").write_bytes(b"o" * orphan[1])                    # nlink == 1: nothing links to it
    nm = home / "proj" / f"node_modules_{version}"
    nm.mkdir(parents=True, exist_ok=True)
    for i in range(linked[0]):
        (files / f"used{i}").write_bytes(b"u" * linked[1])
        os.link(files / f"used{i}", nm / f"used{i}")                            # a project hardlinks it: in use
    return store


def test_pnpm_store_prune_only_when_unlinked_files_exist_via_npx_with_the_version_from_package_manager(tmp_path, tc, monkeypatch):
    for n in ("pnpm",):
        (tc / ".local" / "bin" / n).unlink()                                     # pnpm is not on PATH, like on the real host
    store = make_pnpm_store(tc, "v10")
    make_pnpm_store(tc, "v3")                                                    # older layout: not ours to prune
    (tmp_path / "projects" / "app").mkdir(parents=True)
    (tmp_path / "projects" / "app" / "package.json").write_text('{"packageManager": "pnpm@10.9.3+sha512.abc"}')
    (tmp_path / "projects" / "old").mkdir()
    (tmp_path / "projects" / "old" / "package.json").write_text('{"packageManager": "pnpm@10.2.0"}')
    f, _ = pm_mock(tc, monkeypatch, rows=[("store prune", lambda cmd: (
        [os.unlink(p) for p in (store / "files" / "ab").glob("orphan*")], ok())[1])])
    opts = dict(projects_root=str(tmp_path / "projects"))
    dry = cd.tool_caches(tc_ctx(**opts))
    row = [r for r in dry.items if r["name"].startswith("pnpm")][0]
    assert row["state"] == "would" and "v10" in row["name"] and "8.0 KiB" in row["name"] and not f.mutating()
    res = cd.tool_caches(tc_ctx(apply=True, **opts))
    cmd = [c for c in f.mutating() if "store prune" in c]
    assert len(cmd) == 1 and f"npx --yes pnpm@10.9.3 store prune --store-dir {tc}/.local/share/pnpm/store" in cmd[0]
    assert res.reclaimed_bytes >= 8192 and not list((store / "files" / "ab").glob("orphan*"))
    assert len(list((store / "files" / "ab").glob("used*"))) == 3
    f2 = use_sh(monkeypatch, *f.rows)
    again = cd.tool_caches(tc_ctx(apply=True, **opts))                           # nothing unlinked any more: no npx download
    assert not [c for c in f2.mutating() if "store prune" in c] and again.metrics["selected"] == 0


def test_pnpm_on_path_with_matching_major_is_used_directly_and_a_running_pnpm_blocks(tmp_path, tc, monkeypatch):
    make_pnpm_store(tc, "v10")
    f, _ = pm_mock(tc, monkeypatch, rows=[("store prune", ok())])
    cd.tool_caches(tc_ctx(apply=True))
    assert any(c.endswith("pnpm store prune --store-dir " + str(tc / ".local/share/pnpm/store")) for c in f.mutating())
    pins = tmp_path / "pins" / "app"
    pins.mkdir(parents=True)
    (pins / "package.json").write_text('{"packageManager": "pnpm@10.9.3+sha512.abc"}')
    f, _ = pm_mock(tc, monkeypatch, rows=[("pnpm --version", ok("9.1.0\n")), ("store prune", ok())])
    cd.tool_caches(tc_ctx(apply=True, projects_root=str(tmp_path / "pins")))
    assert any("npx --yes pnpm@10.9.3 store prune" in c for c in f.mutating())  # wrong major on PATH: the PINNED version of the right one
    make_proc(cd.PROC, [dict(pid=9, comm="pnpm", cmdline=["pnpm", "install"], cwd="/")])
    f, _ = pm_mock(tc, monkeypatch, rows=[("store prune", ok())])
    res = cd.tool_caches(tc_ctx(apply=True))
    assert not [c for c in f.mutating() if "store prune" in c]
    assert any("pnpm running" in r["proof"] for r in res.items)


def test_thumbnails_older_than_30_days_are_deleted_by_mtime_and_nothing_else(tmp_path, tc, monkeypatch):
    f, _ = pm_mock(tc, monkeypatch)
    th = tc / ".cache" / "thumbnails"
    old, new = th / "normal" / "a.png", th / "normal" / "b.png"
    for p, d in ((old, 45), (new, 5), (th / "large" / "c.png", 100), (th / "normal" / "keep.txt", 100),
                 (th / "fail" / "gnome" / "d.png", 60)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"p" * 1000)
        age(p, d)
    os.symlink("/etc/hostname", th / "normal" / "link.png")
    age(th / "normal" / "link.png", 90)
    dry = cd.tool_caches(tc_ctx())
    assert old.exists() and sum(1 for r in dry.items if r["name"].startswith("thumbnails")) == 3
    assert "mtime > 30 d" in [r for r in dry.items if r["name"].startswith("thumbnails")][0]["proof"]
    cd.tool_caches(tc_ctx(apply=True))
    assert not old.exists() and new.exists() and (th / "normal" / "keep.txt").exists()
    assert not (th / "large" / "c.png").exists() and not (th / "fail" / "gnome" / "d.png").exists()
    assert os.path.islink(th / "normal" / "link.png") and Path("/etc/hostname").exists()      # symlinks are never followed/removed
    assert cd.tool_caches(tc_ctx(apply=True, thumbs_days=500)).metrics["selected"] == 0


def test_gradle_daemon_logs_older_than_14_days_whose_daemon_is_gone(tmp_path, tc, monkeypatch):
    f, _ = pm_mock(tc, monkeypatch)
    d = tc / ".gradle" / "daemon" / "9.4.1"
    logs = {"daemon-111.out.log": 30, "daemon-222.out.log": 30, "daemon-333.out.log": 3, "registry.bin": 90, "daemon-444.out.log.lock": 90}
    for n, days in logs.items():
        (d / n).parent.mkdir(parents=True, exist_ok=True)
        (d / n).write_bytes(b"l" * 500)
        age(d / n, days)
    caches = tc / ".gradle" / "caches" / "x.bin"
    caches.parent.mkdir(parents=True)
    caches.write_bytes(b"c")
    age(caches, 400)
    make_proc(cd.PROC, [dict(pid=222, comm="java", cmdline=["java", "GradleDaemon"], cwd="/")])     # daemon 222 is alive
    res = cd.tool_caches(tc_ctx(apply=True))
    assert not (d / "daemon-111.out.log").exists()
    assert (d / "daemon-222.out.log").exists() and (d / "daemon-333.out.log").exists()                 # alive / recent
    assert (d / "registry.bin").exists() and (d / "daemon-444.out.log.lock").exists() and caches.exists()
    assert "daemon pid not running" in [r for r in res.items if r["name"].startswith("gradle")][0]["proof"]
    cd.PROC = tmp_path / "noproc"                                                                    # cannot tell who is alive
    (d / "daemon-555.out.log").write_bytes(b"x")
    age(d / "daemon-555.out.log", 99)
    res = cd.tool_caches(tc_ctx(apply=True))
    assert (d / "daemon-555.out.log").exists() and res.status in ("ok", "skipped")


def test_browser_caches_are_skipped_while_the_browser_runs_and_only_cache_files_go_when_it_does_not(tmp_path, tc, monkeypatch):
    f, _ = pm_mock(tc, monkeypatch)
    prof = tc / "snap" / "firefox" / "common" / ".cache" / "mozilla" / "firefox" / "abc.default"
    chrome = tc / ".cache" / "google-chrome" / "Default" / "Cache"
    for p, d in ((prof / "cache2" / "entries" / "e1", 20), (prof / "cache2" / "entries" / "e2", 1),
                 (chrome / "Cache_Data" / "f_1", 30)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"b" * 1000)
        age(p, d)
    bookmarks = tc / "snap" / "firefox" / "common" / ".mozilla" / "places.sqlite"      # a profile: never touched
    bookmarks.parent.mkdir(parents=True)
    bookmarks.write_bytes(b"bookmarks")
    age(bookmarks, 500)
    make_proc(cd.PROC, [dict(pid=7, comm="firefox", cmdline=["/snap/firefox/1/usr/lib/firefox/firefox"], cwd="/")])
    res = cd.tool_caches(tc_ctx(apply=True))
    assert (prof / "cache2" / "entries" / "e1").exists()                                  # firefox running: untouched
    assert not (chrome / "Cache_Data" / "f_1").exists()                                    # chrome is not running
    assert any("firefox running" in r["proof"] for r in res.items)
    make_proc(cd.PROC, [dict(pid=1, comm="systemd", cwd="/")])
    cd.tool_caches(tc_ctx(apply=True))
    assert not (prof / "cache2" / "entries" / "e1").exists() and (prof / "cache2" / "entries" / "e2").exists()   # > 7 d only
    assert bookmarks.read_bytes() == b"bookmarks"


def test_report_mode_and_pause_run_no_mutating_command_and_options_can_switch_a_target_off(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    fill(cache["pip"])
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED, "tasks": {"tool_caches": {"mode": "apply", "min_mib": 0.001}}}
    (tmp_path / "conf" / "PAUSE").write_text("x")
    cd.tool_caches(core.Ctx(cfg, "tool_caches", True, NOW))
    assert not f.mutating()
    (tmp_path / "conf" / "PAUSE").unlink()
    cfg["tasks"]["tool_caches"].update(npm=False, uv=False)
    cd.tool_caches(core.Ctx(cfg, "tool_caches", True, NOW))
    assert any("pip cache purge" in c for c in f.mutating()) and not any("npm cache clean" in c for c in f.mutating())
    for bad in ({"thumbs_days": "x"}, {"gradle_log_days": 0}, {"browser_days": True}):
        assert cd.tool_caches(tc_ctx(**bad)).status == "skipped", bad


def test_pnpm_versions_and_orphan_bytes_helpers(tmp_path, sandbox):
    (tmp_path / "p" / "a").mkdir(parents=True)
    (tmp_path / "p" / "b").mkdir()
    (tmp_path / "p" / "c").mkdir()
    (tmp_path / "p" / "a" / "package.json").write_text('{"packageManager":"pnpm@10.2.1"}')
    (tmp_path / "p" / "b" / "package.json").write_text('{"packageManager": "pnpm@10.9.3+sha512.x"}')
    (tmp_path / "p" / "c" / "package.json").write_text('{"packageManager": "yarn@4.0.0", "name": "x"}')
    (tmp_path / "p" / "d.json").write_text("{")
    assert cd._pnpm_versions(str(tmp_path / "p")) == {10: "10.9.3"}
    store = make_pnpm_store(tmp_path, "v10", orphan=(3, 1000), linked=(2, 5000))
    assert cd._orphan_bytes(str(store)) == 3000                                       # only files nothing links to
    ticks = iter(range(0, 10_000, 100))
    cd._mono = lambda: next(ticks)                                                    # the walk runs out of time: unmeasurable
    assert cd._orphan_bytes(str(store)) is None


def test_slug_is_stable_ascii_and_relative_to_the_home():
    assert cd._slug("/home/u/.venv", "/home/u") == "venv"
    assert cd._slug("/home/u/.hermes/hermes-agent/venv", "/home/u") == "hermes-hermes-agent-venv"
    assert cd._slug("/srv/x y/venv", "/home/u") == "srv-x_y-venv"
    assert cd._slug("/home/u/a b/ü", "/home/u").isascii()


def test_write_verified_is_idempotent_refuses_different_content_and_replaces_a_crashed_part_file(tmp_path):
    d = tmp_path / "req.txt"
    cd._write_verified(str(d), "a==1\n", os.getuid(), os.getgid())
    assert d.read_text() == "a==1\n"
    cd._write_verified(str(d), "a==1\n", os.getuid(), os.getgid())                     # same content: fine
    with pytest.raises(RuntimeError, match="different content"):
        cd._write_verified(str(d), "b==2\n", os.getuid(), os.getgid())
    e = tmp_path / "e.txt"
    (tmp_path / "e.txt.part").write_text("half a write")
    cd._write_verified(str(e), "x\n", os.getuid(), os.getgid())
    assert e.read_text() == "x\n" and not (tmp_path / "e.txt.part").exists()


# =========================================================================== unused_venvs
def make_venv(path, days=200, pkgs=("torch-2.1.0", "numpy-1.26.4"), pip=True, so_size=20000):
    """A virtualenv as pip leaves it: every file but pyvenv.cfg / bin/python* / bin/activate is named by some package's RECORD.
    The project directory around it is as idle as the venv itself."""
    path = Path(path)
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12\n")
    (path / "bin" / "python").symlink_to("/usr/bin/python3")
    (path / "bin" / "activate").write_text(f'VIRTUAL_ENV="{path}"\n')           # a venv names itself in its own scripts
    if pip:
        (path / "bin" / "pip").write_text("#!/x")
    sp = path / "lib" / "python3.12" / "site-packages"
    (sp / "torch").mkdir(parents=True, exist_ok=True)
    (sp / "torch" / "libtorch.so").write_bytes(b"s" * so_size)
    for i, p in enumerate(pkgs):
        di = sp / f"{p}.dist-info"
        di.mkdir(parents=True)
        (di / "METADATA").write_text("m")
        rec = [f"{p}.dist-info/METADATA,,"]
        if i == 0:                                                              # the first package owns the .so and bin/pip
            rec += [f"torch/libtorch.so,sha256=x,{so_size}"] + (["../../../bin/pip,sha256=y,4"] if pip else [])
        (di / "RECORD").write_text("\n".join(rec + [f"{p}.dist-info/RECORD,,"]) + "\n")
    age_tree(path, days)
    age(path.parent, days)
    return path


@pytest.fixture
def vh(tmp_path):
    """A fake home with the usual places a unit, cron entry, script or config could name a venv."""
    home = tmp_path / "home"
    for d in (".config/systemd/user", "ai-stack/scripts", "StudioProjects", "etc-systemd"):
        (home / d).mkdir(parents=True)
    (home / "etc-systemd" / "base.service").write_text("[Service]\nExecStart=/usr/bin/true\n")    # an empty search proves nothing
    return home


def venv_ctx(home, apply=False, **kw):
    refs = [str(home / ".config/systemd/user"), str(home / "ai-stack"), str(home / "StudioProjects"), str(home / "etc-systemd"),
            str(home / ".bashrc")]
    opts = dict(roots=[str(home)], home=str(home), ref_paths=refs, archive_dir=str(home / "cold"))
    return mk("unused_venvs", apply=apply, **{**opts, **kw})


def test_unreferenced_idle_unused_venv_is_planned_and_a_unit_that_names_it_keeps_it(tmp_path, vh):
    flight = make_venv(vh / "flightclaw" / "venv")
    unused = make_venv(vh / ".venv", so_size=3 * MIB + 100)
    (vh / ".config/systemd/user/flightclaw.service").write_text(f"[Service]\nExecStart={flight}/bin/python main.py\n")
    res = cd.unused_venvs(venv_ctx(vh))
    ascii_ok(res)
    assert [i["path"] for i in res.plan["items"]] == [str(unused)]
    it = res.plan["items"][0]
    assert it["bytes"] == 3 * MIB                                                   # coarse MiB: the plan hash stays stable
    assert it["archive_dir"] == str(vh / "cold") and "pip freeze" in it["command"] and it["stem"] == "venv-venv"
    assert it["command"].endswith(f"rm -rf -- {unused}") and f"{vh}/cold/venv-venv-requirements-$(date +%F).txt" in it["command"]
    r = rows(res)
    assert r["flightclaw/venv"]["state"] == "kept" and r["flightclaw/venv"]["proof"].startswith("referenced by")
    assert r[".venv"]["state"] == "unused" and "no reference" in r[".venv"]["proof"] and "processes" in r[".venv"]["proof"]
    assert res.status == "info" and res.alert is False and unused.exists() and flight.exists()
    assert res.metrics["unused"] == 1 and res.metrics["venvs"] == 2


@pytest.mark.parametrize("place", [".config/systemd/user/x.service", "ai-stack/scripts/run.sh", "StudioProjects/Makefile",
                                   "etc-systemd/y.service", ".bashrc"])
def test_a_unit_script_project_config_or_shell_rc_naming_the_venv_keeps_it(tmp_path, vh, place):
    v = make_venv(vh / "pyenv")
    (vh / place).write_text(f"x={v}/bin/python\n")
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and rows(res)["pyenv"]["proof"].startswith("referenced by")
    (vh / place).write_text(f"x={v}2/bin/python {v}-old\n")                           # a longer name is another path
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]


def test_a_failed_or_unfinished_reference_search_keeps_the_venv(tmp_path, vh, monkeypatch):
    v = make_venv(vh / "pyenv")
    monkeypatch.setattr(cd, "_ref_check", lambda ctx, targets, home, *a, **k: {p: inuse._unknown("grep budget exhausted") for p in targets})
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "grep budget exhausted" in rows(res)["pyenv"]["proof"] and v.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_file_or_a_search_that_ran_out_of_budget_makes_no_reference_unknown_not_unused(tmp_path, vh):
    v = make_venv(vh / "pyenv")
    locked = vh / "ai-stack" / "scripts" / "secret.sh"
    locked.write_text("echo hi\n")
    locked.chmod(0)
    try:
        res = cd.unused_venvs(venv_ctx(vh))
        assert res.plan["items"] == [] and "unreadable file secret.sh" in rows(res)["pyenv"]["proof"]
    finally:
        locked.chmod(0o644)
    s = cd._RefSearch({"k": [(str(v), "exact", "venv")]}, str(vh), max_files=1)
    pr = s.run([(str(vh / "ai-stack"), "proj"), (str(vh / "StudioProjects"), "proj"), (str(vh / "etc-systemd"), "proj")])["k"]
    assert pr.used and not pr.known and "budget" in pr.why                                    # more files than the cap: unknown
    empty = cd._RefSearch({"k": [(str(v), "exact", "venv")]}, str(vh)).run([(str(vh / "nowhere"), "unit")])["k"]
    assert empty.used and not empty.known and "no files were searched" in empty.why           # an empty search proves nothing


def test_default_reference_roots_cover_units_launchers_rc_files_configs_nginx_and_the_projects_of_the_home(tmp_path, vh):
    ctx = core.Ctx({"global": {}, "caps": {}, "protected": PROTECTED, "tasks": {"unused_venvs": {}}}, "unused_venvs", False, NOW)
    roots = dict(cd._ref_roots(ctx, str(vh)))
    for r in ("/etc/systemd/system", "/var/spool/cron/crontabs", "/etc/nginx", "/etc/caddy", "/etc/supervisor", "/etc/profile",
              "/etc/bash.bashrc", "/etc/rc.local", "/etc/anacrontab", "/usr/local/bin", str(vh / "ai-stack"), str(vh / "StudioProjects"),
              str(vh / ".local/bin"), str(vh / ".config"), str(vh / ".claude.json"), str(vh / ".bash_profile"), str(vh / ".zprofile"),
              str(vh / ".zshenv"), str(vh / ".config/systemd/user")):
        assert r in roots, r
    assert roots[str(vh / ".local/bin")] == "bin" and roots[str(vh / ".config/systemd/user")] == "unit" and roots[str(vh / ".config")] == "config"
    assert str(vh / "StudioProjects") not in dict(cd._ref_roots(ctx, str(vh), projects=False))
    make_venv(vh / "pyenv")
    seen, real_run = {}, cd._RefSearch.run
    mp = pytest.MonkeyPatch()
    mp.setattr(cd._RefSearch, "run", lambda self, roots: seen.update(budget=self.budget, max_files=self.max_files) or real_run(self, []))
    try:
        cd.unused_venvs(venv_ctx(vh, ref_timeout_s=33))
    finally:
        mp.undo()
    assert seen["budget"] == 33.0 and seen["max_files"] >= 1_000_000


def test_a_venv_inside_a_searched_root_is_not_kept_by_its_own_scripts(tmp_path, vh):
    v = make_venv(vh / "StudioProjects" / "tools" / "pyenv")                         # bin/activate names it, root is searched
    other = make_venv(vh / "etc-systemd" / "x" / "venv2")
    res = cd.unused_venvs(venv_ctx(vh))
    assert {i["path"] for i in res.plan["items"]} == {str(v), str(other)}
    (vh / "StudioProjects" / "README").write_text(f"run {v}/bin/python\n")            # but a real reference elsewhere still counts
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(other)]


def test_a_running_process_keeps_the_venv_however_it_uses_it(tmp_path, vh):
    cases = {"maps": dict(maps=["{v}/lib/python3.12/site-packages/torch/libtorch.so"]), "argv": dict(cmdline=["{v}/bin/python", "x.py"]),
             "environ": dict(environ={"VIRTUAL_ENV": "{v}"}), "path-env": dict(environ={"PATH": "{v}/bin:/usr/bin"}),
             "cwd": dict(cwd="{v}/lib"), "fd": dict(fds=["{v}/pyvenv.cfg"])}
    for i, (how, spec) in enumerate(cases.items()):
        v = make_venv(vh / f"v{i}" / "pyenv")
        fmt = lambda x: x.format(v=v) if isinstance(x, str) else [y.format(v=v) for y in x] if isinstance(x, list) \
            else {k: z.format(v=v) for k, z in x.items()}
        make_proc(cd.PROC, [dict(pid=50, comm="python3", **{k: fmt(val) for k, val in spec.items()})])
        res = cd.unused_venvs(venv_ctx(vh))
        assert str(v) not in [p["path"] for p in res.plan["items"]], how
        assert "in use: pid 50 (python3)" in rows(res)[f"v{i}/pyenv"]["proof"], how
        shutil.rmtree(v.parent)


def test_a_container_mounting_the_venv_keeps_it_and_unknown_mounts_plan_nothing(tmp_path, vh, monkeypatch):
    v = make_venv(vh / "tools" / "pyenv")
    mounts(monkeypatch, {"trainer": [str(v)]})
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "mounted by container trainer" in rows(res)["tools/pyenv"]["proof"]
    mounts(monkeypatch, None)
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "mounts unknown" in res.summary and v.exists()


def test_recently_modified_venv_and_active_project_local_venv_are_kept_but_an_idle_projects_venv_is_planned(tmp_path, vh):
    make_venv(vh / "recent" / "pyenv", days=10)
    age(vh / "recent", 200)                                                       # the project around it is idle, the venv itself is not
    proj_active = project(vh / "StudioProjects", "active", commit_days=5)
    make_venv(proj_active / ".venv")
    proj_idle = project(vh / "StudioProjects", "idle", commit_days=300, files_days=300)
    make_venv(proj_idle / ".venv")
    res = cd.unused_venvs(venv_ctx(vh))
    r = rows(res)
    assert "modified" in r["recent/pyenv"]["proof"]
    assert "active project" in r["StudioProjects/active/.venv"]["proof"]
    assert r["StudioProjects/idle/.venv"]["state"] == "unused"
    assert [i["name"] for i in res.plan["items"]] == ["StudioProjects/idle/.venv"]


def test_a_venv_whose_project_activity_git_cannot_tell_is_kept(tmp_path, vh, monkeypatch):
    p = project(vh / "StudioProjects", "idle", commit_days=300, files_days=300)
    make_venv(p / ".venv")
    use_sh(monkeypatch, ("status --porcelain", (128, "", "fatal")))
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "project activity unknown" in rows(res)["StudioProjects/idle/.venv"]["proof"]


def test_depth_limit_pruned_dirs_never_touch_and_protected_venvs(tmp_path, vh):
    make_venv(vh / "a" / "b" / "c" / "venv")                                      # depth 4: out of range
    make_venv(vh / ".cache" / "pre-commit" / "pyenv")                              # tool-managed: pruned
    make_venv(vh / "ai-stack" / ".venv")                                           # never touch
    make_venv(vh / "StudioProjects" / "tunarr-x" / ".venv")                         # protected.toml
    make_venv(vh / "ok" / "pyenv")
    res = cd.unused_venvs(venv_ctx(vh))
    names = set(rows(res))
    assert "a/b/c/venv" not in names and ".cache/pre-commit/pyenv" not in names
    assert rows(res)["ai-stack/.venv"]["proof"] == "never-touch path"
    assert rows(res)["StudioProjects/tunarr-x/.venv"]["proof"] == "protected.toml"
    assert [i["name"] for i in res.plan["items"]] == ["ok/pyenv"]


def test_plan_is_stable_sorted_and_changes_with_the_venv_set(tmp_path, vh):
    make_venv(vh / "z" / "pyenv")
    make_venv(vh / "a" / "pyenv")
    p1 = cd.unused_venvs(venv_ctx(vh)).plan
    p2 = cd.unused_venvs(venv_ctx(vh)).plan
    assert p1 == p2 and core.plan_hash(p1) == core.plan_hash(p2)
    assert [i["name"] for i in p1["items"]] == ["a/pyenv", "z/pyenv"]
    make_venv(vh / "m" / "pyenv")
    assert core.plan_hash(cd.unused_venvs(venv_ctx(vh)).plan) != core.plan_hash(p1)
    assert all("<date>" in f for i in p1["items"] for f in i["archive_files"])           # no timestamps inside the plan


def venv_apply_env(tmp_path, vh, monkeypatch, freeze="torch==2.1.0\nnumpy==1.26.4\n"):
    (vh / "cold").mkdir(exist_ok=True)
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    monkeypatch.setattr(cd, "_user_of", lambda uid: ("ohmz", str(vh)))
    return use_sh(monkeypatch, ("-m pip freeze", (0, freeze, "") if freeze is not None else (1, "", "no pip")))


def test_c2_apply_needs_the_approval_for_the_exact_plan_hash_and_root(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and "awaiting approval: homelab-maint approve unused_venvs " + core.plan_hash(plan) in res.summary
    (tmp_path / "state" / "approvals").mkdir(parents=True)
    (tmp_path / "state" / "approvals" / "unused_venvs.deadbeef0000").write_text("1")          # some other plan
    cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists()
    approve(tmp_path, "unused_venvs", plan, age_s=3 * DAY)                                      # expired approval
    cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists()
    approve(tmp_path, "unused_venvs", plan)
    cd.unused_venvs(venv_ctx(vh, apply=False))                                                  # dry-run ignores a valid approval
    assert v.exists() and not list((vh / "cold").iterdir())
    monkeypatch.setattr(cd, "_euid", lambda: 1000)
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and res.status == "warn" and "needs root" in res.summary and "refused-not-root" in outcomes(tmp_path)


def test_c2_apply_archives_the_package_list_first_then_removes_and_consumes_the_approval(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    f = venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    ap = approve(tmp_path, "unused_venvs", plan)
    seen = {}
    real_remove = cd._rm_anchored

    def checked_remove(root, path, st, before=None):           # the archive must be complete and verified BEFORE removal
        seen["files"] = sorted(p.name for p in (vh / "cold").iterdir())
        seen["req"] = next((vh / "cold").glob("venv-venv-requirements-*.txt")).read_text()
        real_remove(root, path, st, before)

    monkeypatch.setattr(cd, "_rm_anchored", checked_remove)
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    day = time.strftime("%Y-%m-%d", time.localtime(NOW))
    assert not v.exists() and res.status == "ok" and res.metrics["selected"] == 1
    assert seen["files"] == [f"venv-venv-bin-listing-{day}.txt", f"venv-venv-meta-{day}.txt", f"venv-venv-pyvenv-{day}.cfg",
                             f"venv-venv-requirements-{day}.txt"]
    assert "numpy==1.26.4" in seen["req"] and "# pip freeze" in seen["req"]
    assert (vh / "cold" / f"venv-venv-pyvenv-{day}.cfg").read_text().startswith("home = /usr/bin")
    assert "python -> /usr/bin/python3" in (vh / "cold" / f"venv-venv-bin-listing-{day}.txt").read_text()
    assert not ap.exists()                                                                          # approvals are single use
    assert audit_rows(tmp_path)[-1]["outcome"] == "done"
    assert any("-m pip freeze" in c and c.startswith("runuser -u ohmz --") for c in f.calls)       # freeze ran as the owner, not root


def test_c2_apply_falls_back_to_dist_info_names_and_refuses_when_no_package_list_can_be_made(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch, freeze=None)                         # pip freeze fails
    approve(tmp_path, "unused_venvs", cd.unused_venvs(venv_ctx(vh)).plan)
    cd.unused_venvs(venv_ctx(vh, apply=True))
    req = next((vh / "cold").glob("venv-venv-requirements-*.txt")).read_text()
    assert "dist-info names" in req and "torch==2.1.0" in req and "numpy==1.26.4" in req and not v.exists()
    shutil.rmtree(vh / "cold")
    e = make_venv(vh / "empty" / "pyenv", pkgs=(), pip=False)
    shutil.rmtree(e / "lib" / "python3.12" / "site-packages" / "torch")
    age_tree(e, 200)
    venv_apply_env(tmp_path, vh, monkeypatch, freeze=None)
    approve(tmp_path, "unused_venvs", cd.unused_venvs(venv_ctx(vh)).plan)
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert e.exists() and res.metrics["failed"] == 1 and "no package list" in res.summary


def test_a_root_owned_venv_never_has_its_python_run_as_root(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    f = venv_apply_env(tmp_path, vh, monkeypatch)
    real = os.lstat

    class RootOwned:
        def __init__(self, st):
            self.st_uid, self.st_mode = 0, st.st_mode

    with monkeypatch.context() as m:
        m.setattr(os, "lstat", lambda p, *a, **k: RootOwned(real(p, *a, **k)) if str(p) == str(v) else real(p, *a, **k))
        lines, how = cd._freeze(str(v))
    assert how.startswith("dist-info") and "torch==2.1.0" in lines and not [c for c in f.calls if "-m pip freeze" in c]


def test_c2_apply_keeps_the_venv_when_the_archive_step_fails(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    approve(tmp_path, "unused_venvs", plan)
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "on the root filesystem (cold disk not mounted?)")
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and res.metrics["failed"] == 1 and "archive target refused" in res.summary
    assert not list((vh / "cold").iterdir())
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "")
    day = time.strftime("%Y-%m-%d", time.localtime(NOW))
    (vh / "cold" / f"venv-venv-requirements-{day}.txt").write_text("someone else's list\n")      # never overwrite a different archive
    approve(tmp_path, "unused_venvs", plan)
    res = cd.unused_venvs(venv_ctx(vh, apply=True, retry_after_days=0))
    assert v.exists() and "different content" in res.summary
    assert (vh / "cold" / f"venv-venv-requirements-{day}.txt").read_text() == "someone else's list\n"


def test_a_reference_or_process_that_appears_after_the_plan_changes_the_hash_so_the_approval_no_longer_applies(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    approve(tmp_path, "unused_venvs", plan)
    (vh / ".config/systemd/user/new.service").write_text(f"ExecStart={v}/bin/python\n")        # a unit appeared after the plan
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and res.plan["items"] == [] and not any(r["outcome"] == "done" for r in audit_rows(tmp_path))
    (vh / ".config/systemd/user/new.service").unlink()
    approve(tmp_path, "unused_venvs", plan)
    make_proc(cd.PROC, [dict(pid=5, comm="python3", cwd=str(v))])                               # a process started in it
    cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and not any(r["outcome"] == "done" for r in audit_rows(tmp_path))


def test_c2_apply_reproves_each_item_right_before_acting(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    approve(tmp_path, "unused_venvs", plan)
    real, calls = cd._assess_venvs, []

    def assess(*a, **k):                                        # quiet when planning, busy by the time apply re-proves
        calls.append(1)
        return real(*a, **k) if len(calls) == 1 else {a[3][0]: cd._V("in use: pid 9 (python3) cwd")}

    monkeypatch.setattr(cd, "_assess_venvs", assess)
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and len(calls) == 2 and res.metrics["selected"] == 0
    assert "in use: pid 9" in " ".join(r["name"] for r in res.items) and not list((vh / "cold").iterdir())


def test_a_process_that_starts_while_the_archive_is_written_stops_the_removal(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    approve(tmp_path, "unused_venvs", cd.unused_venvs(venv_ctx(vh)).plan)
    real = cd._write_verified

    def write_then_start_a_process(*a, **k):
        real(*a, **k)
        make_proc(cd.PROC, [dict(pid=5, comm="python3", cwd=str(v))])
    monkeypatch.setattr(cd, "_write_verified", write_then_start_a_process)
    res = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and res.metrics["gone"] == 1 and list((vh / "cold").iterdir())     # the archive stays, the venv too


def test_unreadable_process_table_means_no_venv_is_ever_planned(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv")
    deny(monkeypatch, 1)
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "unusable" in res.summary and v.exists()
    assert "process table unusable" in rows(res)[".venv"]["proof"]


def test_bad_venv_config_does_nothing(tmp_path, vh):
    assert cd.unused_venvs(mk("unused_venvs", roots="x")).status == "skipped"
    assert cd.unused_venvs(mk("unused_venvs", max_depth="deep")).status == "skipped"
    assert cd.unused_venvs(mk("unused_venvs", archive_dir="relative")).status == "skipped"


# =========================================================================== large_cold_files
def big(path, size=2 * MIB, days=200):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    age(path, days)
    return path


def cold_ctx(home, apply=False, **kw):
    opts = dict(roots=[str(home / "StudioProjects"), str(home / ".config"), str(home / ".android")], min_gib=0.001, cold_days=90,
                archive_root=str(home / "cold"))
    return mk("large_cold_files", apply=apply, **{**opts, **kw})


@pytest.fixture
def ch(tmp_path):
    home = tmp_path / "home"
    for d in ("StudioProjects", ".config", ".android", "cold"):
        (home / d).mkdir(parents=True)
    return home


def test_cold_directory_replaces_its_files_and_a_recent_file_keeps_the_parent_from_going(tmp_path, ch):
    big(ch / "StudioProjects" / "oldtool" / "data" / "a.bin")
    big(ch / "StudioProjects" / "oldtool" / "data" / "b.bin")
    age_tree(ch / "StudioProjects" / "oldtool", 200)
    big(ch / "StudioProjects" / "livetool" / "cold.bin")                               # cold file next to a fresh one
    big(ch / "StudioProjects" / "livetool" / "fresh.bin", days=2)
    big(ch / ".android" / "avd" / "Pixel.avd" / "userdata.img", size=3 * MIB)
    age_tree(ch / ".android" / "avd", 190)
    res = cd.large_cold_files(cold_ctx(ch))
    ascii_ok(res)
    items = {i["name"]: i for i in res.plan["items"]}
    assert set(items) == {"StudioProjects/oldtool", "StudioProjects/livetool/cold.bin", ".android/avd"}
    assert items["StudioProjects/oldtool"]["kind"] == "dir" and items["StudioProjects/oldtool"]["bytes"] == 4 * MIB
    assert items["StudioProjects/oldtool"]["needs_manual_check"] is False
    loose = items["StudioProjects/livetool/cold.bin"]
    assert loose["kind"] == "file" and loose["needs_manual_check"] is True and "not cold" in loose["why"]     # siblings are in use
    assert res.status == "info" and res.alert is False and res.plan["total_bytes"] == sum(i["bytes"] for i in items.values())
    assert set(outcomes(tmp_path)) <= {"dry-run"}


def test_threshold_and_age_are_both_required(tmp_path, ch):
    big(ch / "StudioProjects" / "small" / "x.bin", size=100 * 1024)                    # cold but small
    big(ch / "StudioProjects" / "young" / "y.bin", days=10)                              # big but young
    big(ch / "StudioProjects" / "cold" / "z.bin")                                         # big and cold
    age_tree(ch / "StudioProjects" / "cold", 200)
    names = lambda **kw: {i["name"] for i in cd.large_cold_files(cold_ctx(ch, **kw)).plan["items"]}
    assert names() == {"StudioProjects/cold"}
    assert names(min_gib=0.003) == set()                                                  # 2 MiB < 3 MiB
    assert names(cold_days=5) == {"StudioProjects/cold", "StudioProjects/young/y.bin"}      # the young dir itself was just created
    assert names(cold_days=500) == set()


def test_git_repos_never_candidates_inside_git_node_modules_or_venvs_and_flagged_manual(tmp_path, ch):
    p = project(ch / "StudioProjects", "coldrepo", commit_days=300, files_days=300)
    big(p / "data.bin", days=300)
    age_tree(p, 300, skip_git=False)
    assert (p / ".git").exists()
    big(ch / "StudioProjects" / "live" / ".git" / "objects" / "pack" / "p.pack")        # a cold pack of an ACTIVE repo
    (ch / "StudioProjects" / "live" / "src.txt").write_text("fresh")
    big(ch / "StudioProjects" / "live2" / "node_modules" / "big" / "x.bin")
    (ch / "StudioProjects" / "live2" / "src.txt").write_text("fresh")
    make_venv(ch / "StudioProjects" / "live3" / "env")
    big(ch / "StudioProjects" / "live3" / "env" / "lib" / "torch.so")
    (ch / "StudioProjects" / "live3" / "src.txt").write_text("fresh")
    res = cd.large_cold_files(cold_ctx(ch))
    items = {i["name"]: i for i in res.plan["items"]}
    assert "StudioProjects/coldrepo" in items and items["StudioProjects/coldrepo"]["needs_manual_check"] is True
    assert "git repo" in items["StudioProjects/coldrepo"]["why"]
    assert not any(n.startswith(("StudioProjects/live/", "StudioProjects/live2/", "StudioProjects/live3/")) for n in items), items


def test_never_touch_parts_taint_the_parent_and_protected_paths_are_not_candidates(tmp_path, ch):
    big(ch / ".config" / "Cursor" / "User" / "state.vscdb")                            # never-touch app state
    big(ch / ".config" / "someapp" / "cache.bin")
    big(ch / ".config" / "someapp" / "models" / "m.bin")                               # not a never-touch (only ~/models is)
    big(ch / "StudioProjects" / "tunarr-old" / "x.bin")                                # protected.toml
    big(ch / "StudioProjects" / "mixed" / "a.bin")
    big(ch / "StudioProjects" / "mixed" / "ai-stack" / "b.bin")                        # a never-touch part inside
    age_tree(ch / ".config", 200)
    age_tree(ch / "StudioProjects", 200)
    res = cd.large_cold_files(cold_ctx(ch))
    names = {i["name"] for i in res.plan["items"]}
    assert not any("Cursor" in n or "tunarr" in n for n in names), names
    assert ".config/someapp" in names
    assert "StudioProjects/mixed" not in names and "StudioProjects/mixed/a.bin" in names


def test_candidates_in_use_by_a_container_or_a_process_are_kept_out_of_the_plan(tmp_path, ch, monkeypatch):
    a = big(ch / "StudioProjects" / "mounted" / "d.bin")
    b = big(ch / "StudioProjects" / "held" / "d.bin")
    c = big(ch / "StudioProjects" / "free" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    mounts(monkeypatch, {"dev": [str(a.parent)]})
    make_proc(cd.PROC, [dict(pid=3, comm="vlc", fds=[str(b)])])
    res = cd.large_cold_files(cold_ctx(ch))
    assert {i["name"] for i in res.plan["items"]} == {"StudioProjects/free"}
    r = rows(res)
    assert r["StudioProjects/mounted"]["proof"] == "in use: mounted by container dev"
    assert r["StudioProjects/held"]["proof"].startswith("in use: pid 3 (vlc) fd")
    deny(monkeypatch, 3)
    res = cd.large_cold_files(cold_ctx(ch))
    assert res.plan["items"] == [] and "unusable" in res.summary


def test_plan_is_stable_and_scan_budget_exhaustion_is_reported(tmp_path, ch, sandbox):
    for n in ("b", "a", "c"):
        big(ch / "StudioProjects" / n / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    p1, p2 = cd.large_cold_files(cold_ctx(ch)).plan, cd.large_cold_files(cold_ctx(ch)).plan
    assert p1 == p2 and [i["name"] for i in p1["items"]] == ["StudioProjects/a", "StudioProjects/b", "StudioProjects/c"]
    assert all(i["archive_to"] == str(ch / "cold" / "studioprojects") for i in p1["items"])
    assert "rsync -aHSAX" in p1["items"][0]["command"] and "rm -rf" in p1["items"][0]["command"]
    res = cd.large_cold_files(cold_ctx(ch, scan_limit=1000))
    assert res.metrics["scan_complete"] is True
    tiny = cd.large_cold_files(cold_ctx(ch, scan_limit=1000))
    import homelab_maint.tasks.cleaners_dev as m
    c, complete = m._cold_scan(cold_ctx(ch), str(ch / "StudioProjects"), 1, NOW, 100.0, 3)
    assert complete is False and tiny.metrics["candidates"] == 3


def rsync_sim(monkeypatch, fail_verify=False):
    def run(cmd):
        parts = cmd.split(" -- ")[1].split(" ")
        src, dst = parts[0], parts[1]
        if " -n -c " in cmd:
            return (0, "<fc.... x\n", "") if fail_verify else ok()
        s, d = src.rstrip("/"), dst.rstrip("/")
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
        return ok()
    return ("rsync -aHSAX", run)


def cold_apply_env(monkeypatch, **kw):
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    return use_sh(monkeypatch, rsync_sim(monkeypatch, **kw))


def test_c2_apply_copies_verifies_then_removes_only_with_the_approval_and_root(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects" / "oldtool", 200)
    f = cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and "awaiting approval" in res.summary and not f.mutating()
    approve(tmp_path, "large_cold_files", plan)
    monkeypatch.setattr(cd, "_euid", lambda: 1000)
    assert cd.large_cold_files(cold_ctx(ch, apply=True)).status == "warn" and src.exists()
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert not src.exists() and (ch / "cold" / "studioprojects" / "oldtool" / "d.bin").stat().st_size == 2 * MIB
    assert res.reclaimed_bytes == 2 * MIB and res.metrics["selected"] == 1
    assert any("rsync -aHSAX" in c and " -c " in c for c in f.calls)                  # checksum verification pass
    assert not list((tmp_path / "state" / "approvals").glob("large_cold_files.*"))


def test_c2_apply_keeps_the_original_when_the_checksum_verification_fails(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    cold_apply_env(monkeypatch, fail_verify=True)
    approve(tmp_path, "large_cold_files", cd.large_cold_files(cold_ctx(ch)).plan)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and res.metrics["failed"] == 1 and "verification failed" in res.summary
    assert "failed" in " ".join(outcomes(tmp_path))


def test_an_item_that_became_busy_or_touched_after_the_plan_drops_out_of_it_and_the_recheck_refuses_it(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    approve(tmp_path, "large_cold_files", plan)
    make_proc(cd.PROC, [dict(pid=8, comm="player", fds=[str(src)])])                    # opened after the plan
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and res.plan["items"] == [] and not (ch / "cold" / "studioprojects" / "oldtool").exists()
    make_proc(cd.PROC, [dict(pid=1, comm="ok", cwd="/")])
    ctx, it, cutoff, arch = cold_ctx(ch, apply=True), plan["items"][0], NOW - 90 * DAY, str(ch / "cold")
    assert cd._cold_recheck(ctx, it, cutoff, arch) == ""
    make_proc(cd.PROC, [dict(pid=8, comm="player", fds=[str(src)])])
    assert cd._cold_recheck(ctx, it, cutoff, arch).startswith("in use: pid 8")
    make_proc(cd.PROC, [dict(pid=1, comm="ok", cwd="/")])
    os.utime(src.parent, (NOW - DAY, NOW - DAY))                                       # touched after the plan
    assert cd._cold_recheck(ctx, it, cutoff, arch) == "touched since the plan"
    monkeypatch.setattr(cl, "_free_bytes", lambda p: 10)
    os.utime(src.parent, (NOW - 200 * DAY,) * 2)
    assert cd._cold_recheck(ctx, it, cutoff, arch) == "archive disk too small"
    assert cd._cold_recheck(ctx, {**it, "path": str(ch / "gone")}, cutoff, arch) == "gone"
    deny(monkeypatch, 1)
    assert "process table unusable" in cd._cold_recheck(ctx, it, cutoff, arch)


def test_c2_apply_refuses_manual_check_items_unless_allowed_and_a_bad_archive_target(tmp_path, ch, monkeypatch):
    big(ch / "StudioProjects" / "live" / "cold.bin")
    big(ch / "StudioProjects" / "live" / "fresh.bin", days=1)
    age(ch / "StudioProjects" / "live" / "cold.bin", 200)
    cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    assert plan["items"][0]["needs_manual_check"] is True
    approve(tmp_path, "large_cold_files", plan)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert (ch / "StudioProjects" / "live" / "cold.bin").exists() and "manual check" in " ".join(r["name"] for r in res.items)
    approve(tmp_path, "large_cold_files", plan)
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "same filesystem as the source")
    res = cd.large_cold_files(cold_ctx(ch, apply=True, allow_manual_check_items=True))
    assert (ch / "StudioProjects" / "live" / "cold.bin").exists() and res.metrics["selected"] == 0
    monkeypatch.setattr(cl, "_archive_target_problem", lambda arch, src: "")
    approve(tmp_path, "large_cold_files", plan)
    cd.large_cold_files(cold_ctx(ch, apply=True, allow_manual_check_items=True))
    assert not (ch / "StudioProjects" / "live" / "cold.bin").exists() and (ch / "StudioProjects" / "live" / "fresh.bin").exists()


def test_bad_cold_config_does_nothing_and_a_busy_backup_defers_the_apply(tmp_path, ch, monkeypatch):
    for bad in ({"min_gib": "x"}, {"cold_days": -1}, {"archive_root": "rel"}, {"roots": "x"}):
        assert cd.large_cold_files(cold_ctx(ch, **bad)).status == "skipped", bad
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    approve(tmp_path, "large_cold_files", plan)
    monkeypatch.setattr(cl, "_busy", lambda name: (name == "backup", "backup-system running"))
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and "backup running" in res.summary


# =========================================================================== REVIEW REGRESSIONS (each would have deleted something in use)
def put(path, text, days=200):
    """Write a file and age it (and its directory) so it does not make its project look active."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    age(path, days)
    age(path.parent, days)
    inuse.reset_caches()
    return path


# ---- 1 (critical): a process that uses a venv / output through a path RELATIVE to its cwd ----------------------------------------
def test_a_server_started_as_dot_slash_venv_from_the_project_dir_keeps_the_venv(tmp_path, vh):
    """cd ~/tools/foo && nohup ./venv/bin/python server.py &   -> cwd is the PARENT, exe is /usr/bin/python3, nothing else names the venv."""
    v = make_venv(vh / "tools" / "foo" / "venv")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]          # idle and unreferenced: planned
    make_proc(cd.PROC, [dict(pid=7, comm="python", cwd=str(v.parent), exe="/usr/bin/python3.12", cmdline=["./venv/bin/python", "server.py"])])
    assert inuse.process_cwd_or_open_under(str(v), None).unused                     # the shared probe alone sees nothing (the review's finding)
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and v.exists()
    assert "pid 7 (python)" in rows(res)["tools/foo/venv"]["proof"] and rows(res)["tools/foo/venv"]["state"] == "kept"


@pytest.mark.parametrize("cwd,argv", [("{p}", ["venv/bin/python", "x.py"]),                          # no leading ./
                                      ("{p}/..", ["foo/venv/bin/python"]),                          # from the parent of the project
                                      ("{p}", ["python", "--config=venv/etc/x.ini"]),             # inside an option value
                                      ("{p}", ["env", "PATH=venv/bin:/usr/bin", "python"])])       # inside a PATH-like list
def test_every_relative_spelling_of_the_venv_path_is_resolved_against_the_cwd(tmp_path, vh, cwd, argv):
    v = make_venv(vh / "tools" / "foo" / "venv")
    make_proc(cd.PROC, [dict(pid=7, comm="python", cwd=cwd.format(p=v.parent), cmdline=argv)])
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "pid 7" in rows(res)["tools/foo/venv"]["proof"]


def test_any_process_working_in_the_venvs_project_dir_holds_the_venv_but_one_elsewhere_does_not(tmp_path, vh):
    v = make_venv(vh / "tools" / "foo" / "venv")
    make_proc(cd.PROC, [dict(pid=7, comm="python", cwd=str(v.parent), cmdline=["python", "server.py"])])    # a venv reached through PATH / a shim
    assert cd.unused_venvs(venv_ctx(vh)).plan["items"] == []
    make_proc(cd.PROC, [dict(pid=8, comm="python", cwd="/tmp", cmdline=["./venv/bin/python", "x.py"]),    # /tmp/venv/... is another path
                        dict(pid=9, comm="python", cwd=str(vh / "tools"), cmdline=["bar/venv/bin/python"])])   # another project's venv
    assert [i["name"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == ["tools/foo/venv"]


def test_a_relative_launch_of_build_output_from_above_the_project_keeps_it(tmp_path, proj):
    """cd ~/StudioProjects && node app/dist/server.js : the cwd is the scan root, not under the project."""
    p = project(proj, "app", builds=("dist",), manifest="package.json")
    make_proc(cd.PROC, [dict(pid=7, comm="node", cwd=str(proj), cmdline=["node", "app/dist/server.js"])])
    res = run_build(tmp_path, apply=True)
    assert (p / "dist").exists() and "in use: pid 7 (node)" in rows(res)["app/dist"]["proof"]
    make_proc(cd.PROC, [dict(pid=7, comm="node", cwd=str(proj), cmdline=["node", "other-app/dist/server.js"])])
    run_build(tmp_path, apply=True)
    assert not (p / "dist").exists()                                                  # a different project's dist is no reason


def test_relative_launch_index_fails_closed_and_is_rebuilt_with_every_new_snapshot(tmp_path, vh, monkeypatch):
    v = str(vh / "tools" / "foo" / "venv")
    assert cd._launched_under(v).unused
    make_proc(cd.PROC, [dict(pid=7, comm="python", cwd=str(vh / "tools" / "foo"), cmdline=["./venv/bin/python"])])      # make_proc resets caches
    assert cd._launched_under(v).used and "relative argv" in cd._launched_under(v).why
    real_readlink = os.readlink
    monkeypatch.setattr(os, "readlink", lambda p, *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")) if str(p).endswith("/7/cwd") else real_readlink(p, *a, **k))
    inuse.reset_caches()
    pr = cd._launched_under(v)
    assert not pr.known and pr.used                                                     # an unreadable process table is "unknown"


# ---- 2 (high): things that run or serve the output on demand ---------------------------------------------------------------------
def idle_app(proj, **kw):
    return project(proj, "app", builds=("dist",), manifest="package.json", **kw)


def served(tmp_path, kind, p):
    """Make `kind` of consumer for app/dist; returns the extra task options."""
    d, home = p / "dist", tmp_path
    opts = {"home": str(home)}
    if kind == "launcher":                                                               # ~/.local/bin/codex-desktop shape
        put(tmp_path / "localbin" / "app-server", f"#!/bin/sh\nexec node {d}/server.js\n")
        opts["ref_paths"] = [f"bin:{tmp_path / 'localbin'}", *QUIET]
    elif kind == "user timer via %h":
        put(tmp_path / "units" / "job.service", "[Service]\nExecStart=/usr/bin/node %h/proj/app/dist/job.js\n")
        opts["ref_paths"] = [f"unit:{tmp_path / 'units'}", *QUIET]
    elif kind == "workingdirectory":
        put(tmp_path / "units" / "job.service", "[Service]\nWorkingDirectory=%h/proj/app\nExecStart=/usr/bin/node dist/job.js\n")
        opts["ref_paths"] = [f"unit:{tmp_path / 'units'}", *QUIET]
    elif kind == "cron cd":
        put(tmp_path / "units" / "crontab", "0 3 * * * cd ~/proj/app && node dist/job.js\n")
        opts["ref_paths"] = [f"unit:{tmp_path / 'units'}", *QUIET]
    elif kind == "nginx root":
        put(tmp_path / "nginx" / "site.conf", f"server {{ listen 80; root {d}; }}\n")
        opts["ref_paths"] = [f"unit:{tmp_path / 'nginx'}", *QUIET]
    elif kind == "compose bind mount of a removed container":
        put(p / "docker-compose.yml", "services:\n  web:\n    image: nginx\n    volumes:\n      - ./dist:/usr/share/nginx/html:ro\n")
    elif kind == "Dockerfile COPY":
        put(p / "Dockerfile", "FROM nginx\nCOPY dist /usr/share/nginx/html\n")
    elif kind == "symlink":
        (tmp_path / "www").mkdir()
        os.symlink(d, tmp_path / "www" / "html")
        opts["link_roots"] = [str(tmp_path / "www")]
    elif kind == "package.json main":
        put(p / "package.json", json.dumps({"main": "dist/index.js", "scripts": {"build": "tsc --outDir dist"}}))
    elif kind == "mcp config":
        put(tmp_path / "cfg" / "claude.json", json.dumps({"mcpServers": {"x": {"command": "node", "args": [f"{d}/mcp.js"]}}}))
        opts["ref_paths"] = [f"config:{tmp_path / 'cfg'}", *QUIET]
    return opts


@pytest.mark.parametrize("kind", ["launcher", "user timer via %h", "workingdirectory", "cron cd", "nginx root",
                                  "compose bind mount of a removed container", "Dockerfile COPY", "symlink", "package.json main", "mcp config"])
def test_things_that_run_or_serve_the_output_on_demand_keep_it_though_nothing_runs_now(tmp_path, proj, kind):
    p = idle_app(proj)
    assert run_build(tmp_path).metrics["selected"] == 1                                  # idle, ignored, untracked, unused: it WOULD go
    opts = served(tmp_path, kind, p)
    res = run_build(tmp_path, apply=True, **opts)
    assert (p / "dist" / "out.bin").exists() and res.metrics["selected"] == 0, kind
    proof = rows(res)["app/dist"]["proof"]
    assert rows(res)["app/dist"]["state"] == "kept" and any(k in proof for k in ("referenced by", "symlink", "package.json runs")), proof


def test_a_reference_to_another_project_or_a_longer_name_is_no_reason(tmp_path, proj):
    p = idle_app(proj)
    put(tmp_path / "units" / "other.service", f"ExecStart=/usr/bin/node {proj}/app-two/dist/x.js\nWorkingDirectory=%h/proj/application\n")
    res = run_build(tmp_path, apply=True, home=str(tmp_path), ref_paths=[f"unit:{tmp_path / 'units'}", *QUIET])
    assert not (p / "dist").exists() and res.metrics["selected"] == 1


def test_an_incomplete_reference_or_symlink_search_keeps_the_output(tmp_path, proj, monkeypatch):
    p = idle_app(proj)
    with monkeypatch.context() as m:
        m.setattr(cd, "_ref_check", lambda ctx, targets, home, *a, **k: {t: inuse._unknown("grep budget exhausted") for t in targets})
        res = run_build(tmp_path, apply=True)
        assert (p / "dist").exists() and "budget" in rows(res)["app/dist"]["proof"]
    with monkeypatch.context() as m:
        m.setattr(cd, "_links_into", lambda items, roots, **k: ({}, "cannot list etc"))
        res = run_build(tmp_path, apply=True)
        assert (p / "dist").exists() and "symlink scan incomplete" in rows(res)["app/dist"]["proof"]


# ---- 3 (high): "ignored + idle + a manifest somewhere above" does not prove the directory is regenerable ---------------------------
def test_the_hand_patched_bundle_next_to_its_bak_files_is_never_deleted(tmp_path, proj):
    """codex-desktop-linux/codex-workspace/codex-app/.vite/build: main.js differs from main.js.bak, `build` cannot run on Linux."""
    p = project(proj, "codex", builds=(), manifest=None)
    (p / "codex-workspace").mkdir()
    put(p / "codex-workspace" / "package.json", json.dumps({"scripts": {"build": "electron-forge make --platform=darwin"}}))
    b = p / "codex-workspace" / "codex-app" / ".vite" / "build"
    for n, t in (("main.js", "patched"), ("main.js.bak", "orig"), ("main.js.bak2", "orig2")):
        put(b / n, t)
    age_tree(p / "codex-workspace", 200)
    age(p, 200)
    inuse.reset_caches()
    res = run_build(tmp_path, apply=True)
    assert (b / "main.js").read_text() == "patched" and (b / "main.js.bak").exists()
    assert res.metrics["selected"] == 0 and "backup/patch file" in rows(res)["codex/codex-workspace/codex-app/.vite/build"]["proof"]
    os.unlink(b / "main.js.bak"), os.unlink(b / "main.js.bak2")                          # without the .bak files the recipe still says no
    age(b, 200), age(b.parent, 200)
    inuse.reset_caches()
    res = run_build(tmp_path, apply=True)
    assert (b / "main.js").exists() and "not provably tool output" in rows(res)["codex/codex-workspace/codex-app/.vite/build"]["proof"]   # no src in that tree


@pytest.mark.parametrize("name,why", [("main.js.bak", "backup/patch file"), ("fix.patch", "backup/patch file"), (".env.production", "env file"),
                                      ("cache.sqlite", "database"), ("data.db", "database"), ("deploy.pem", "key material"),
                                      ("release.jks", "key material"), ("app-release.apk", "signed app bundle"), ("app.aab", "signed app bundle")])
def test_build_output_holding_irreplaceable_files_is_kept_as_manual(tmp_path, proj, name, why):
    p = idle_app(proj)
    put(p / "dist" / name, "x")
    age_tree(p / "dist", 200)
    res = run_build(tmp_path, apply=True)
    assert (p / "dist" / name).exists() and res.metrics["selected"] == 0 and why in rows(res)["app/dist"]["proof"]


def test_files_of_another_owner_in_the_output_keep_it(tmp_path, proj, monkeypatch):
    p = idle_app(proj)
    monkeypatch.setattr(cd, "_owner_uid", lambda path: os.getuid() + 1)                   # the project belongs to somebody else
    res = run_build(tmp_path, apply=True)
    assert (p / "dist").exists() and "file of another owner" in rows(res)["app/dist"]["proof"]


@pytest.mark.parametrize("build,tamper,why", [("target", lambda d: (d / "CACHEDIR.TAG").unlink(), "cargo CACHEDIR.TAG"),
                                              (".next", lambda d: (d / "BUILD_ID").unlink(), "BUILD_ID"),
                                              (".pytest_cache", lambda d: (d / "CACHEDIR.TAG").unlink(), "CACHEDIR.TAG"),
                                              ("__pycache__", lambda d: (d / "notes.txt").write_text("hand made"), "more than .pyc"),
                                              (".turbo", lambda d: (d / "my-data").mkdir(), "unexpected content")])
def test_a_directory_without_the_signature_of_its_tool_is_not_output(tmp_path, proj, build, tamper, why):
    p = project(proj, "app", builds=(build,), manifest="package.json", ignore=(build + "/",))
    tamper(p / build)
    age_tree(p / build, 200)
    res = run_build(tmp_path, apply=True)
    assert (p / build).exists() and why in rows(res)[f"app/{build}"]["proof"]


def test_dist_and_build_need_a_recipe_that_produces_them_and_stay_report_only_unless_apply_generic(tmp_path, proj):
    a = project(proj, "has-recipe", builds=("dist",), manifest="package.json")
    b = project(proj, "no-recipe", builds=("dist",), manifest="package.json")
    put(b / "package.json", json.dumps({"scripts": {"test": "jest"}}))                   # a package.json without a build script
    c = project(proj, "no-manifest", builds=("build",), manifest=None)
    res = run_build(tmp_path, apply=True, apply_generic=False)                           # the default
    assert (a / "dist").exists() and "report-only" in rows(res)["has-recipe/dist"]["proof"] and res.metrics["selected"] == 0
    res = run_build(tmp_path, apply=True, apply_generic=True)
    assert not (a / "dist").exists()                                                      # opted in: provable output goes
    assert (b / "dist").exists() and "no build recipe" in rows(res)["no-recipe/dist"]["proof"]
    assert (c / "build").exists() and "no build manifest" in rows(res)["no-manifest/build"]["proof"]
    cfg = {"global": {}, "caps": {}, "protected": PROTECTED, "tasks": {}}
    assert core.Ctx(cfg, "stale_build_output", True, NOW).opt("apply_generic", False) is False       # report-only is the default


def test_maven_target_and_gradle_build_are_recognised_by_their_layout(tmp_path, proj):
    m = project(proj, "mvn", builds=("target",), manifest="pom.xml")
    (m / "target" / "CACHEDIR.TAG").unlink()
    (m / "target" / "classes").mkdir()
    (m / "target" / "classes" / "A.class").write_bytes(b"c")
    age_tree(m / "target", 200)
    g = project(proj, "gradle", builds=("build",), manifest="build.gradle")
    res = run_build(tmp_path, apply=True)
    assert not (m / "target").exists() and not (g / "build").exists() and res.metrics["selected"] == 2


# ---- 4 (high): reference search blind spots --------------------------------------------------------------------------------------
def groups(vh, **grp):
    """ref_paths with explicit groups: groups(vh, unit=[dir], bin=[dir]) -> ["unit:...", ...]  (+ a quiet file so the search is never empty)"""
    return [f"{g}:{p}" for g, ps in grp.items() for p in ps] + QUIET


def venv_kept(tmp_path, vh, v, **kw):
    res = cd.unused_venvs(venv_ctx(vh, **kw))
    return res.plan["items"] == [] and v.exists(), rows(res)[os.path.relpath(v, vh)]["proof"]


@pytest.mark.parametrize("text", [
    "[Service]\nExecStart=%h/tools/foo/venv/bin/python app.py\n",                                      # systemd specifier, absolute
    "[Service]\nExecStart=${HOME}/tools/foo/venv/bin/python app.py\n",
    "[Service]\nWorkingDirectory=%h/tools/foo\nExecStart=./venv/bin/python app.py\n",                  # relative to WorkingDirectory
    "[Service]\nWorkingDirectory=tools/foo\nExecStart=venv/bin/python app.py\n",                       # relative WorkingDirectory: user units start in $HOME
    "[Service]\nExecStart=/bin/sh -c 'cd tools/foo && ./venv/bin/python app.py'\n",                    # cron-style cd
    "[Service]\nExecStart=/usr/bin/env bash -lc 'cd ~/tools/foo && exec venv/bin/python app.py'\n",
    "[Service]\nWorkingDirectory=%h/tools/foo\nExecStart=/usr/bin/python3 app.py\n",                   # only the PROJECT dir is named
])
def test_user_units_naming_the_venv_by_specifier_or_relative_path_or_only_its_project_keep_it(tmp_path, vh, text):
    v = make_venv(vh / "tools" / "foo" / "venv")
    put(vh / "units" / "foo.service", text)
    kept, proof = venv_kept(tmp_path, vh, v, ref_paths=groups(vh, unit=[vh / "units"]))
    assert kept and proof.startswith("referenced by"), (text, proof)
    (vh / "units" / "foo.service").write_text("[Service]\nExecStart=/usr/bin/true\n")                # control: nothing names it
    inuse.reset_caches()
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh, ref_paths=groups(vh, unit=[vh / "units"]))).plan["items"]] == [str(v)]


def test_a_comment_or_a_readme_that_merely_says_venv_or_names_the_project_is_not_a_use(tmp_path, vh):
    v = make_venv(vh / "tools" / "foo" / "venv")
    put(vh / "units" / "x.service", "# create the venv first\n[Service]\nExecStart=/usr/bin/true\n")
    put(vh / "notes" / "README.md", f"the project lives in {v.parent}\nsource venv/bin/activate\n")           # prose, not code
    put(vh / "proj" / "run.sh", "source .venv/bin/activate\n")                                           # another project's own .venv
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh, ref_paths=groups(vh, unit=[vh / "units"], config=[vh / "notes"], proj=[vh / "proj"]))
                                                ).plan["items"]] == [str(v)]


def test_a_symlinked_unit_file_and_a_symlinked_root_directory_are_searched_through(tmp_path, vh):
    """~/.config/systemd/user/qwen36-vllm.service -> ~/Documents/Codex/... on this very host."""
    v = make_venv(vh / "vllm" / "venv")
    real = put(vh / "Documents" / "Codex" / "qwen" / "qwen.service", f"[Service]\nExecStart={v}/bin/python -m vllm\n")
    udir = vh / ".config" / "systemd" / "user"
    os.symlink(real, udir / "qwen.service")
    kept, proof = venv_kept(tmp_path, vh, v)
    assert kept and proof.startswith("referenced by"), proof
    os.unlink(udir / "qwen.service")
    shutil.rmtree(udir)
    os.symlink(real.parent, udir)                                                                   # the ROOT itself is a symlink
    kept, proof = venv_kept(tmp_path, vh, v)
    assert kept and proof.startswith("referenced by"), proof
    os.unlink(udir)
    udir.mkdir()
    os.symlink(vh / "nowhere.service", udir / "dangling.service")                                    # a dangling link has no content: not an error
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]


def test_symlink_loops_do_not_hang_the_reference_search(tmp_path, vh):
    v = make_venv(vh / "vllm" / "venv")
    os.symlink(vh / "ai-stack", vh / "ai-stack" / "loop")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]


@pytest.mark.parametrize("where,grp,text", [
    ("localbin/hermes", "bin", "#!/bin/sh\nexec {v}/bin/python -m hermes \"$@\"\n"),                      # ~/.local/bin/hermes
    ("cfg/claude.json", "config", '{"mcpServers": {"x": {"command": "{v}/bin/python"}}}'),               # ~/.claude.json, MCP configs
    ("cfg/settings.json", "config", '{"python.defaultInterpreterPath": "{v}/bin/python"}'),             # Code / Cursor settings
    ("etc/profile", "bin", "export PATH={v}/bin:$PATH\n"),                                                 # /etc/profile, ~/.bash_profile, ~/.zprofile
    ("etc/nginx/site.conf", "unit", "location / { proxy_pass http://x; } # {v}/bin/gunicorn\n"),
    ("etc/supervisor/x.conf", "unit", "command={v}/bin/uvicorn app:app\n"),
])
def test_launchers_configs_profiles_nginx_and_supervisor_that_name_the_venv_keep_it(tmp_path, vh, where, grp, text):
    v = make_venv(vh / "vllm" / "venv")
    put(vh / where, text.replace("{v}", str(v)))
    kept, proof = venv_kept(tmp_path, vh, v, ref_paths=groups(vh, **{grp: [(vh / where).parent]}))
    assert kept and proof.startswith("referenced by"), proof


def test_a_unit_that_runs_a_script_that_names_the_venv_is_followed_one_level(tmp_path, vh):
    """unit -> ~/.config/opencode/start-x.sh -> venv: the script is in no searched root."""
    v = make_venv(vh / "vllm" / "venv")
    put(vh / "units" / "x.service", "[Service]\nExecStart=%h/oc/start-x.sh\n")
    put(vh / "oc" / "start-x.sh", f"#!/bin/sh\nexec {v}/bin/python -m x\n")
    kept, proof = venv_kept(tmp_path, vh, v, ref_paths=groups(vh, unit=[vh / "units"]))
    assert kept and "start-x.sh" in proof
    put(vh / "oc" / "start-x.sh", f"#!/bin/sh\nexec {vh}/other/bin/python -m x\n")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh, ref_paths=groups(vh, unit=[vh / "units"]))).plan["items"]] == [str(v)]


def test_a_same_named_project_elsewhere_no_longer_blinds_the_search(tmp_path, vh):
    """The old trick skipped every directory named like the venv: ~/envs/foo was not found as referenced by ~/StudioProjects/foo/run.sh."""
    v = make_venv(vh / "envs" / "foo")
    put(vh / "StudioProjects" / "foo" / "run.sh", f"#!/bin/sh\nexec {v}/bin/python main.py\n")
    kept, proof = venv_kept(tmp_path, vh, v)
    assert kept and proof.startswith("referenced by") and "foo/run.sh" in proof


def test_relative_mentions_resolve_against_the_script_that_makes_them_and_the_projects_own_files_are_no_user(tmp_path, vh):
    v = make_venv(vh / "StudioProjects" / "proj" / "venv")
    put(vh / "StudioProjects" / "proj" / "run.sh", "#!/bin/sh\nsource venv/bin/activate\n")           # the project's OWN script: not a user of its venv
    put(vh / "StudioProjects" / "proj" / "Makefile", "test:\n\tvenv/bin/pytest\n")
    put(vh / "StudioProjects" / "proj" / "README.md", f"python -m venv {v}\n")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]
    put(vh / "StudioProjects" / "other" / "run.sh", "#!/bin/sh\nsource venv/bin/activate\n")            # next to ANOTHER venv: not this one
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]
    put(vh / "StudioProjects" / "other" / "run.sh", "#!/bin/sh\ncd ../proj && source venv/bin/activate\n")   # cd's to it: that one
    kept, proof = venv_kept(tmp_path, vh, v)
    assert kept and proof.startswith("referenced by") and "other/run.sh" in proof


def test_a_symlink_pointing_into_the_venv_keeps_it(tmp_path, vh):
    """~/.local/bin/hermes -> ~/.hermes/hermes-agent/venv/bin/hermes (pipx style): no text names the venv."""
    v = make_venv(vh / "agent" / "venv")
    (vh / ".local" / "bin").mkdir(parents=True)
    (v / "bin" / "hermes").write_text("#!/x")
    os.symlink(v / "bin" / "hermes", vh / ".local" / "bin" / "hermes")
    age_tree(v, 200)
    res = cd.unused_venvs(venv_ctx(vh, link_roots=[str(vh)]))
    assert res.plan["items"] == [] and "in use: symlink" in rows(res)["agent/venv"]["proof"] and v.exists()
    os.unlink(vh / ".local" / "bin" / "hermes")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh, link_roots=[str(vh)])).plan["items"]] == [str(v)]
    with pytest.MonkeyPatch.context() as m:                                                          # an unfinished link scan keeps it too
        m.setattr(cd, "_links_into", lambda items, roots, **k: ({}, "link scan budget exhausted"))
        assert cd.unused_venvs(venv_ctx(vh)).plan["items"] == []


def test_links_into_finds_chains_relative_links_and_skips_the_candidates_own_tree(tmp_path):
    t = tmp_path / "t"
    (t / "venv" / "bin").mkdir(parents=True)
    (t / "venv" / "bin" / "x").write_text("x")
    os.symlink("python", t / "venv" / "bin" / "py")                                                  # inside the candidate: ignored
    (tmp_path / "bin").mkdir()
    os.symlink("../t/venv/bin/x", tmp_path / "bin" / "rel")                                          # relative link
    os.symlink(tmp_path / "bin" / "rel", tmp_path / "bin" / "chain")                                 # link to a link
    hits, why = cd._links_into({"v": str(t / "venv")}, [str(tmp_path)])
    assert hits == {"v": str(tmp_path / "bin" / "chain")} or hits["v"] in (str(tmp_path / "bin" / "rel"), str(tmp_path / "bin" / "chain"))
    assert why == "" and cd._links_into({"v": str(t / "venv")}, [str(tmp_path / "bin" / "nothing")]) == ({}, "")
    ticks = iter(range(0, 10_000, 1000))
    with pytest.MonkeyPatch.context() as m:
        m.setattr(cd, "_mono", lambda: next(ticks))
        assert "budget" in cd._links_into({"v": str(t / "venv")}, [str(tmp_path)], budget_s=1)[1]


# ---- 5 (high): "idle" must include use, not just modification --------------------------------------------------------------------
def set_atime(path, days, now=NOW):
    st = os.lstat(path)
    os.utime(path, (now - days * DAY, st.st_mtime), follow_symlinks=False)


def test_a_venv_read_recently_is_not_idle_even_though_nothing_in_it_was_modified(tmp_path, vh):
    """pyvenv.cfg is read at every python start: its atime was 2026-10-02 while the tree mtime was 54 days old."""
    v = make_venv(vh / "pyenv")
    set_atime(v / "pyvenv.cfg", 2)
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and rows(res)["pyenv"]["proof"] == "read 2d ago"
    set_atime(v / "pyvenv.cfg", 200)
    set_atime(v / "lib" / "python3.12" / "site-packages" / "torch" / "libtorch.so", 5)                  # an import reads a module
    assert cd.unused_venvs(venv_ctx(vh)).plan["items"] == [] and "read 5d ago" in rows(cd.unused_venvs(venv_ctx(vh)))["pyenv"]["proof"]
    set_atime(v / "lib" / "python3.12" / "site-packages" / "torch" / "libtorch.so", 200)
    res = cd.unused_venvs(venv_ctx(vh))
    assert [i["path"] for i in res.plan["items"]] == [str(v)]
    assert f"last read {time.strftime('%Y-%m-%d', time.localtime(NOW - 200 * DAY))}" in rows(res)["pyenv"]["proof"]     # the date a human approves on


def test_atime_not_kept_by_the_mount_makes_every_venv_a_manual_item_never_applied_by_default(tmp_path, vh, monkeypatch):
    v = make_venv(vh / "pyenv")
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,noatime shared:1 - ext4 /dev/nvme0n1p2 rw\n")
    res = cd.unused_venvs(venv_ctx(vh))
    it = res.plan["items"][0]
    assert it["needs_manual_check"] is True and "atime not kept" in it["why"] and rows(res)["pyenv"]["state"] == "manual check"
    venv_apply_env(tmp_path, vh, monkeypatch)
    approve(tmp_path, "unused_venvs", res.plan)
    out = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and "manual check" in " ".join(r["name"] for r in out.items)
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n/media/x\n")
    assert cd._atime_kept(str(v)) is True
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/x rw\n31 30 8:1 / " + str(vh) + " rw,noatime - ext4 /dev/y rw\n")
    assert cd._atime_kept(str(v)) is False and cd._atime_kept("/etc") is True                          # the longest mount point decides
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/x rw,noatime\n")
    assert cd._atime_kept(str(v)) is False                                                              # also as a superblock option
    (tmp_path / "mountinfo").write_text("garbage\n")
    assert cd._atime_kept(str(v)) is False                                                              # unparsable: unknown
    monkeypatch.setattr(cd, "MOUNTINFO", str(tmp_path / "missing"))
    assert cd._atime_kept(str(v)) is False


def test_a_venv_outside_any_git_project_is_not_idle_while_the_files_next_to_it_change(tmp_path, vh):
    """non-git tools/foo/venv + foo.py edited today: no repo, so project activity used to be examined by nobody."""
    v = make_venv(vh / "tools" / "foo" / "venv")
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]
    (v.parent / "foo.py").write_text("print('hi')\n")                                                  # edited today
    res = cd.unused_venvs(venv_ctx(vh))
    assert res.plan["items"] == [] and "project files changed 0d ago" in rows(res)["tools/foo/venv"]["proof"]
    age(v.parent / "foo.py", 3)
    age(v.parent, 3)
    assert "changed 3d ago" in rows(cd.unused_venvs(venv_ctx(vh)))["tools/foo/venv"]["proof"]
    age(v.parent / "foo.py", 200)
    age(v.parent, 200)
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]
    ticks = iter(range(0, 10_000, 100))                                                                # an unmeasurable neighbourhood: unknown
    with pytest.MonkeyPatch.context() as m:
        m.setattr(cd, "_mono", lambda: next(ticks))
        assert cd.unused_venvs(venv_ctx(vh)).plan["items"] == []


def test_the_files_of_the_same_project_restored_with_old_mtimes_still_count_as_changed_by_their_ctime(tmp_path, vh, monkeypatch):
    v = make_venv(vh / "tools" / "foo" / "venv")
    put(v.parent / "app.py", "x\n", days=300)                                                          # utime() gave it ctime = now
    monkeypatch.setattr(cd, "_stamp", REAL_STAMP)
    assert cd.unused_venvs(venv_ctx(vh)).plan["items"] == []                                           # `cp -p` / rsync -a / tar looks like this
    monkeypatch.setattr(cd, "_stamp", lambda st: st.st_mtime)
    assert [i["path"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == [str(v)]


def test_reading_the_records_of_a_candidate_does_not_touch_its_atime(tmp_path, vh):
    """else every weekly scan would stamp 'used now' on every venv and nothing would ever be idle."""
    v = make_venv(vh / "pyenv")
    rec = v / "lib" / "python3.12" / "site-packages" / "torch-2.1.0.dist-info" / "RECORD"
    os.utime(rec, (NOW - 200 * DAY, NOW - 200 * DAY))
    before = os.stat(rec).st_atime
    assert cd._read_noatime(str(rec)) is not None and cd._venv_records(str(v)) is not None
    assert os.stat(rec).st_atime == before
    assert cd._read_noatime(str(vh / "nowhere")) is None


# ---- 6 (high): a venv holds more than a package list can rebuild ----------------------------------------------------------------
def planned(vh, v, **kw):
    res = cd.unused_venvs(venv_ctx(vh, **kw))
    return [i for i in res.plan["items"] if i["path"] == str(v)], res


@pytest.mark.parametrize("build,why", [
    (lambda v: (v / "src" / "mypkg" / ".git").mkdir(parents=True), "holds src"),                          # pip -e checkout with unpushed commits
    (lambda v: (v / "etc").mkdir() or (v / "etc" / "config.ini").write_text("x"), "holds etc"),
    (lambda v: (v / "bin" / "my-custom-launcher").write_text("#!/bin/sh\n"), "no package owns"),            # hand-made script
    (lambda v: (v / "lib" / "python3.12" / "site-packages" / "patched.py").write_text("# hand patch"), "no package owns"),
    (lambda v: (v / "lib" / "python3.12" / "site-packages" / ".git").mkdir(), ".git inside site-packages"),
    (lambda v: (v / "lib" / "python3.12" / "site-packages" / "torch" / "libtorch.so").write_bytes(b"patched binary"), "differ from their package"),
])
def test_a_venv_with_hand_made_content_is_a_manual_item_that_apply_refuses(tmp_path, vh, monkeypatch, build, why):
    v = make_venv(vh / "pyenv")
    build(v)
    age_tree(v, 200, skip_git=False)
    items, res = planned(vh, v)
    assert items and items[0]["needs_manual_check"] is True and why in items[0]["why"], items
    assert rows(res)["pyenv"]["state"] == "manual check"
    venv_apply_env(tmp_path, vh, monkeypatch)
    approve(tmp_path, "unused_venvs", res.plan)
    out = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and out.metrics["selected"] == 0 and not list((vh / "cold").iterdir())           # nothing archived, nothing removed
    approve(tmp_path, "unused_venvs", res.plan)
    out = cd.unused_venvs(venv_ctx(vh, apply=True, allow_manual_check_items=True))                      # the owner looked and said yes
    assert not v.exists() and out.metrics["selected"] == 1 and list((vh / "cold").iterdir())


def test_a_plain_pip_venv_is_not_manual_and_unreadable_package_records_are(tmp_path, vh):
    v = make_venv(vh / "pyenv")
    items, _ = planned(vh, v)
    assert items[0]["needs_manual_check"] is False and items[0]["why"] == "unused"
    with pytest.MonkeyPatch.context() as m:
        m.setattr(cd, "_venv_records", lambda path: None)                                              # e.g. O_NOATIME refused
        items, _ = planned(vh, v)
        assert items[0]["needs_manual_check"] is True and "records unreadable" in items[0]["why"]


def test_apply_reproves_the_manual_state_even_if_the_plan_said_plain(tmp_path, vh, monkeypatch):
    v = make_venv(vh / "pyenv")
    venv_apply_env(tmp_path, vh, monkeypatch)
    plan = cd.unused_venvs(venv_ctx(vh)).plan
    assert plan["items"][0]["needs_manual_check"] is False
    approve(tmp_path, "unused_venvs", plan)
    real, calls = cd._assess_venvs, []

    def assess(*a, **k):                                        # plain when planning, somebody put a checkout in by the time apply re-proves
        calls.append(1)
        r = real(*a, **k)
        return r if len(calls) == 1 else {p: x._replace(manual="src inside") for p, x in r.items()}

    monkeypatch.setattr(cd, "_assess_venvs", assess)
    out = cd.unused_venvs(venv_ctx(vh, apply=True))
    assert v.exists() and "manual check: src inside" in " ".join(r["name"] for r in out.items) and not list((vh / "cold").iterdir())


def test_the_archive_records_where_non_index_packages_came_from_and_the_index_settings(tmp_path, vh, monkeypatch):
    v = make_venv(vh / ".venv", pkgs=("torch-2.1.0+cu121", "mylib-0.1"))
    di = v / "lib" / "python3.12" / "site-packages" / "mylib-0.1.dist-info"
    (di / "direct_url.json").write_text('{"url": "git+https://example.org/me/mylib.git", "vcs_info": {}}')
    (di / "RECORD").write_text("mylib-0.1.dist-info/METADATA,,\nmylib-0.1.dist-info/RECORD,,\n")
    age_tree(v, 200)
    f = venv_apply_env(tmp_path, vh, monkeypatch)
    f.rows.insert(0, ("pip config list", (0, "global.index-url='https://download.pytorch.org/whl/cu121'\n", "")))
    f.rows.insert(0, ("uv pip freeze", (0, "mylib @ git+https://example.org/me/mylib.git\n", "")))
    approve(tmp_path, "unused_venvs", cd.unused_venvs(venv_ctx(vh)).plan)
    cd.unused_venvs(venv_ctx(vh, apply=True))
    meta = next((vh / "cold").glob("venv-venv-meta-*.txt")).read_text()
    assert "direct_url mylib-0.1: git+https://example.org/me/mylib.git" in meta
    assert "download.pytorch.org/whl/cu121" in meta and "mylib @ git+" in meta and "+cu121" in meta
    assert not v.exists()


# ---- 7 (high): large_cold_files decided "cold" from mtime alone ------------------------------------------------------------------
def test_a_big_file_that_is_read_regularly_is_not_cold_however_old_its_mtime(tmp_path, ch):
    """~/StudioProjects/ml/data/x.gguf read by llama.cpp weekly: mtime 300 d, atime yesterday."""
    m = big(ch / "StudioProjects" / "ml" / "data" / "x.gguf", size=3 * MIB, days=300)
    big(ch / "StudioProjects" / "ml" / "fresh.txt", size=10, days=1)                                  # keeps the directory itself from going
    age_tree(ch / "StudioProjects" / "ml", 300)
    big(ch / "StudioProjects" / "ml" / "fresh.txt", size=10, days=1)
    names = lambda: {i["name"] for i in cd.large_cold_files(cold_ctx(ch)).plan["items"]}
    assert "StudioProjects/ml/data" in names()                                                          # cold by mtime and atime: planned
    set_atime(m, 1)
    assert names() == set()                                                                              # read yesterday: not cold
    set_atime(m, 200)
    assert "StudioProjects/ml/data" in names()
    (tmp_path / "mountinfo").write_text("30 1 259:2 / / rw,noatime - ext4 /dev/x rw\n")
    set_atime(m, 1)                                                                                      # atime is not kept: the read is invisible ...
    res = cd.large_cold_files(cold_ctx(ch))
    it = [i for i in res.plan["items"] if i["name"] == "StudioProjects/ml/data"][0]
    assert it["needs_manual_check"] is True and "atime not kept" in it["why"]                            # ... so a human must look


def test_a_cold_directory_with_one_recently_read_file_is_not_cold(tmp_path, ch):
    for n in ("a.bin", "b.bin"):
        big(ch / "StudioProjects" / "dataset" / n)
    age_tree(ch / "StudioProjects" / "dataset", 300)
    assert {i["name"] for i in cd.large_cold_files(cold_ctx(ch)).plan["items"]} == {"StudioProjects/dataset"}
    set_atime(ch / "StudioProjects" / "dataset" / "b.bin", 2)
    items = cd.large_cold_files(cold_ctx(ch)).plan["items"]
    assert [i["name"] for i in items] == ["StudioProjects/dataset/a.bin"] and items[0]["needs_manual_check"] is True   # not the directory, not b.bin


def test_a_tree_restored_or_moved_in_with_old_mtimes_has_ctime_now_and_is_not_cold(tmp_path, ch, monkeypatch):
    """a 5 GiB dump restored yesterday with rsync -a / cp -p / tar / mv across filesystems"""
    big(ch / "StudioProjects" / "restored" / "dump.sql.gz", size=3 * MIB, days=300)
    age_tree(ch / "StudioProjects" / "restored", 300)                                                   # utime(): mtime old, ctime = now
    assert {i["name"] for i in cd.large_cold_files(cold_ctx(ch)).plan["items"]} == {"StudioProjects/restored"}   # mtime alone: "cold"
    monkeypatch.setattr(cd, "_stamp", REAL_STAMP)
    assert cd.large_cold_files(cold_ctx(ch)).plan["items"] == []                                         # with ctime: not


def test_cold_candidates_named_by_a_config_a_unit_or_a_symlink_are_kept(tmp_path, ch):
    a = big(ch / "StudioProjects" / "ml" / "models" / "x.gguf", size=3 * MIB, days=300)
    b = big(ch / "StudioProjects" / "viz" / "scene.blend", size=3 * MIB, days=300)
    c = big(ch / "StudioProjects" / "old" / "unused.bin", size=3 * MIB, days=300)
    big(ch / "StudioProjects" / "ml" / "keep.txt", size=10, days=1)
    big(ch / "StudioProjects" / "viz" / "keep.txt", size=10, days=1)
    age_tree(ch / "StudioProjects" / "old", 300)
    put(ch / ".config" / "llama" / "settings.json", f'{{"model": "{a}"}}')
    os.makedirs(ch / "links")
    os.symlink(b, ch / "links" / "scene")
    opts = dict(ref_paths=[f"config:{ch / '.config'}", *QUIET], link_roots=[str(ch / "links")], roots=[str(ch / "StudioProjects")])
    res = cd.large_cold_files(cold_ctx(ch, **opts))
    got = {i["name"] for i in res.plan["items"]}
    assert got == {"StudioProjects/old"}, got
    r = rows(res)
    assert r["StudioProjects/ml/models/x.gguf"]["state"] == "kept" and r["StudioProjects/ml/models/x.gguf"]["proof"].startswith("referenced by")
    assert "symlink" in r["StudioProjects/viz/scene.blend"]["proof"]
    with pytest.MonkeyPatch.context() as m:                                                               # an unfinished search is manual, not "unused"
        m.setattr(cd, "_ref_check", lambda ctx, targets, home, *a, **k: {t: inuse._unknown("grep budget exhausted") for t in targets})
        m.setattr(cd, "_links_into", lambda items, roots, **k: ({}, "link scan budget exhausted"))
        it = cd.large_cold_files(cold_ctx(ch, **opts)).plan["items"][0]
        assert it["needs_manual_check"] is True and "inconclusive" in it["why"] and "symlink scan incomplete" in it["why"]
    assert c.exists()


def test_the_recheck_uses_the_same_measure_so_a_read_since_the_plan_stops_the_move(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin", size=3 * MIB)
    age_tree(ch / "StudioProjects" / "oldtool", 200)
    cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    ctx, it, cutoff, arch = cold_ctx(ch, apply=True), plan["items"][0], NOW - 90 * DAY, str(ch / "cold")
    assert cd._cold_recheck(ctx, it, cutoff, arch) == ""
    set_atime(src, 1)                                                                                    # somebody read it after the plan
    assert cd._cold_recheck(ctx, it, cutoff, arch) == "touched since the plan"
    approve(tmp_path, "large_cold_files", plan)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and not (ch / "cold" / "studioprojects").exists() and res.plan["items"] == []


# ---- 8 (medium): activity signals ------------------------------------------------------------------------------------------------
def test_a_nested_repo_is_not_idle_while_its_superproject_was_committed_yesterday(tmp_path, proj):
    mono = project(proj, "mono", commit_days=1, files_days=300, builds=(), ignore=("vendor/",))
    libx = project(mono / "vendor", "libx", commit_days=300, files_days=300)                              # idle on its own
    age_tree(mono, 300)
    age(mono / ".git" / "HEAD", 1), age(mono / ".git" / "logs" / "HEAD", 1)
    inuse.reset_caches()
    res = run_build(tmp_path, apply=True)
    assert (libx / "target").exists() and "project active (1d ago)" in rows(res)["mono/vendor/libx/target"]["proof"]
    mono2 = project(proj, "mono2", commit_days=300, files_days=300, builds=(), ignore=("vendor/",))        # the same layout, all of it idle
    libx2 = project(mono2 / "vendor", "libx", commit_days=300, files_days=300)
    age_tree(mono2, 300)
    inuse.reset_caches()
    run_build(tmp_path, apply=True)
    assert not (libx2 / "target").exists() and (libx / "target").exists()


def test_a_process_in_the_superproject_root_holds_the_nested_repos_output(tmp_path, proj):
    mono = project(proj, "mono", commit_days=300, files_days=300, builds=(), ignore=("vendor/",))
    libx = project(mono / "vendor", "libx", commit_days=300, files_days=300)
    age_tree(mono, 300)
    make_proc(cd.PROC, [dict(pid=7, comm="cargo", cwd=str(mono))])                                          # cargo build in mono/: not "under" libx
    res = run_build(tmp_path, apply=True)
    assert (libx / "target").exists() and "in use: pid 7 (cargo) cwd" in rows(res)["mono/vendor/libx/target"]["proof"]


def test_a_commit_in_a_linked_worktree_is_activity_of_the_main_repo(tmp_path, proj):
    p = project(proj, "app")
    git(p, "worktree", "add", "-q", "-b", "feature", str(proj / "app-wt"))
    git(proj / "app-wt", "commit", "-q", "--allow-empty", "-m", "work in the worktree", when=NOW - 3600)   # main's HEAD / reflog never move
    inuse.reset_caches()
    assert inuse.git_state(str(p / "target"), NOW).idle_days(NOW) > 150                                    # the shared probe still says "idle"
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "project active (0d ago)" in rows(res)["app/target"]["proof"]


def test_a_nested_worktree_and_its_own_changes_count_too(tmp_path, proj):
    p = project(proj, "app", ignore=("target/", "wt/"))
    git(p, "worktree", "add", "-q", "-b", "feature", str(p / "wt"))
    age_tree(p, 300)
    age(p, 300)
    for ref in (("HEAD",), ("logs", "HEAD")):
        age(p / ".git" / Path(*ref), 300)
    git(p / "wt", "commit", "-q", "--allow-empty", "-m", "x", when=NOW - 7200)
    inuse.reset_caches()
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "project active" in rows(res)["app/target"]["proof"]


@pytest.mark.parametrize("failing", ["for-each-ref", "worktree list"])
def test_worktree_and_ref_probes_that_fail_keep_the_output(tmp_path, proj, monkeypatch, failing):
    p = project(proj, "idle")
    use_sh(monkeypatch, (failing, (128, "", "fatal: boom")))
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "unknown" in rows(res)["idle/target"]["proof"]


def test_an_edit_to_an_ignored_file_like_dot_env_is_activity_though_git_never_sees_it(tmp_path, proj):
    p = project(proj, "app", ignore=("target/", ".env", "config.local"))
    assert run_build(tmp_path).metrics["selected"] == 1
    (p / ".env").write_text("TOKEN=1\n")                                                                  # edited today, ignored by git
    inuse.reset_caches()
    assert inuse.git_state(str(p / "target"), NOW).idle_days(NOW) > 150
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "project active (0d ago)" in rows(res)["app/target"]["proof"]
    os.unlink(p / ".env")
    (p / "config.local").write_text("x")
    age(p / "config.local", 200), age(p, 200)
    inuse.reset_caches()
    run_build(tmp_path, apply=True)
    assert not (p / "target").exists()


def test_a_project_restored_with_old_mtimes_is_active_by_ctime(tmp_path, proj, monkeypatch):
    p = project(proj, "restored")                                                                         # cp -p / rsync -a / tar x: mtimes old, ctime now
    monkeypatch.setattr(cd, "_stamp", REAL_STAMP)
    res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and "project active" in rows(res)["restored/target"]["proof"]
    monkeypatch.setattr(cd, "_stamp", lambda st: st.st_mtime)
    run_build(tmp_path, apply=True)
    assert not (p / "target").exists()


def test_a_project_whose_walk_cannot_finish_is_unknown_not_idle(tmp_path, proj):
    p = project(proj, "app")
    ticks = iter(range(0, 10_000, 100))
    with pytest.MonkeyPatch.context() as m:
        m.setattr(cd, "_mono", lambda: next(ticks))
        res = run_build(tmp_path, apply=True)
    assert (p / "target").exists() and res.metrics["selected"] == 0


# ---- 9 (medium): the clock ------------------------------------------------------------------------------------------------------
def heartbeat(tmp_path, at=NOW):
    f = tmp_path / "state" / "history.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}\n")
    os.utime(f, (at, at))


def test_a_clock_far_in_the_future_turns_nothing_old_when_nobody_vouches_for_it(tmp_path, proj, monkeypatch):
    """now = +200 d made an active project's target/ look idle: a dead RTC, a VM resume, a timer that fired before NTP."""
    p = project(proj, "active", commit_days=1, files_days=1)
    monkeypatch.setattr(cd, "_ntp_synced", lambda: False)
    assert "clock unverified" in run_build(tmp_path, apply=True).summary and (p / "target").exists()       # no earlier write to compare with
    heartbeat(tmp_path)
    res = run_build(tmp_path, apply=True, now=NOW + 200 * DAY)
    assert res.status == "skipped" and "clock suspect" in res.summary and (p / "target").exists()
    res = run_build(tmp_path, apply=True, now=NOW - 200 * DAY)                                              # backwards is as wrong
    assert res.status == "skipped" and "clock suspect" in res.summary
    heartbeat(tmp_path, at=NOW - 3600)
    idle = project(proj, "idle")
    res = run_build(tmp_path, apply=True)                                                                   # within the tolerance of the last write: fine
    assert res.status != "skipped" and not (idle / "target").exists() and (p / "target").exists()


def test_the_system_saying_ntp_is_synchronized_is_enough_and_is_asked_through_timedatectl(tmp_path, proj, monkeypatch):
    p = project(proj, "idle")
    monkeypatch.setattr(cd, "_ntp_synced", REAL_NTP)
    f = use_sh(monkeypatch, ("timedatectl show -p NTPSynchronized --value", ok("no\n")))
    assert run_build(tmp_path, apply=True).status == "skipped" and (p / "target").exists()
    f = use_sh(monkeypatch, ("timedatectl show -p NTPSynchronized --value", ok("yes\n")))
    assert run_build(tmp_path, apply=True).status != "skipped" and not (p / "target").exists()
    assert any(c.startswith("timedatectl") for c in f.calls)
    use_sh(monkeypatch, ("timedatectl", (127, "", "not found")))                                           # no timedatectl: not vouched for
    assert cd._ntp_synced() is False


@pytest.mark.parametrize("task,opts", [("stale_build_output", {}), ("unused_venvs", {}), ("large_cold_files", {}), ("tool_caches", {})])
def test_every_task_of_the_module_refuses_to_decide_on_a_clock_nobody_vouches_for(tmp_path, tc, task, opts, monkeypatch):
    monkeypatch.setattr(cd, "_ntp_synced", lambda: False)
    res = getattr(cd, task)(mk(task, apply=True, projects_root=str(tmp_path), roots=[str(tmp_path)], archive_dir=str(tmp_path / "c"),
                               archive_root=str(tmp_path / "c")))
    assert res.status == "skipped" and "clock" in res.summary and not [c for c in outcomes(tmp_path) if c != "refused-paused"]
    ctx = mk("stale_build_output", apply=True, projects_root=str(tmp_path), clock_tolerance_h="x")
    assert cd.stale_build_output(ctx).status == "skipped" and "clock_tolerance_h" in cd.stale_build_output(ctx).summary


def test_a_trusted_run_records_when_it_ran(tmp_path, proj):
    ctx = build_ctx(proj)
    project(proj, "idle")
    cd.stale_build_output(ctx)
    assert ctx.state["last_now"] == NOW


# ---- 10 (medium): root deletes and writes below paths an unprivileged process controls --------------------------------------------
def make_victim(tmp_path):
    victim = tmp_path / "victim"
    (victim / "target").mkdir(parents=True)
    (victim / "target" / "precious").write_text("do not delete")
    return victim


def test_a_parent_directory_swapped_for_a_symlink_while_the_proofs_run_cannot_redirect_the_delete(tmp_path, proj, monkeypatch):
    """The old code checked realpath(path) == path, spent seconds in the recheck and _single_device, then rmtree(path)."""
    p = project(proj, "app")
    victim = make_victim(tmp_path)
    real, calls = cd._single_device, []

    def swap_during_the_checks(path, limit=2_000_000):
        calls.append(path)
        if len(calls) == 2:                                                       # the pre-delete check (the first is the scan's)
            os.rename(p, proj / "app.moved")
            os.symlink(victim, p)                                                  # ~/StudioProjects/app is now a symlink to somebody else's tree
        return real(path, limit)

    monkeypatch.setattr(cd, "_single_device", swap_during_the_checks)
    run_build(tmp_path, apply=True)
    assert (victim / "target" / "precious").read_text() == "do not delete"
    assert not (proj / "app.moved" / "target").exists()                           # what was proved is what was deleted: the original directory


def test_a_parent_swapped_for_a_symlink_before_the_delete_starts_is_refused(tmp_path, proj, monkeypatch):
    p = project(proj, "app")
    victim = make_victim(tmp_path)
    real_act = core.Ctx.act

    def swap_then_act(self, what, target, size, fn, protect_names=()):
        if self.apply:
            os.rename(p, proj / "app.moved")
            os.symlink(victim, p)
        return real_act(self, what, target, size, fn, protect_names)

    monkeypatch.setattr(core.Ctx, "act", swap_then_act)
    res = run_build(tmp_path, apply=True)
    assert (victim / "target" / "precious").exists() and (proj / "app.moved" / "target").exists() and res.metrics["gone"] == 1


def test_rm_anchored_refuses_other_roots_changed_inodes_and_symlink_candidates(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "t").mkdir(parents=True)
    (root / "a" / "t" / "f").write_text("x")
    os.utime(root / "a" / "t", (1_000_000_000, 1_000_000_000))                                  # inode numbers get reused: the mtime tells them apart
    st = os.lstat(root / "a" / "t")
    with pytest.raises(RuntimeError):
        cd._rm_anchored(str(root), str(tmp_path / "elsewhere" / "t"), st)                 # not below the root
    with pytest.raises(RuntimeError):
        cd._rm_anchored(str(root), str(root), st)                                           # never the root itself
    with pytest.raises(cl._Changed):
        cd._rm_anchored(str(root), str(root / "a" / "nothing"), st)                         # vanished
    shutil.rmtree(root / "a" / "t")
    (root / "a" / "t").mkdir()                                                               # replaced: another inode
    with pytest.raises(cl._Changed, match="changed since"):
        cd._rm_anchored(str(root), str(root / "a" / "t"), st)
    os.symlink(tmp_path, root / "a" / "link")
    with pytest.raises(RuntimeError, match="symlink"):
        cd._rm_anchored(str(root), str(root / "a" / "link"), os.lstat(root / "a" / "link"))
    assert tmp_path.exists()
    vetoed = []
    with pytest.raises(cl._Changed):
        cd._rm_anchored(str(root), str(root / "a" / "t"), os.lstat(root / "a" / "t"), lambda fdpath: vetoed.append(fdpath) or (_ for _ in ()).throw(cl._Changed("busy")))
    assert (root / "a" / "t").exists() and vetoed[0].startswith("/proc/self/fd/")                  # the veto sees an fd-anchored path
    cd._rm_anchored(str(root), str(root / "a" / "t"), os.lstat(root / "a" / "t"))
    assert not (root / "a" / "t").exists()
    f = root / "a" / "file"
    f.write_text("x")
    cd._rm_anchored(str(root), str(f), os.lstat(f))                                           # a single file works too
    assert not f.exists()


def test_archive_files_are_written_below_a_no_symlink_directory_fd_and_never_through_planted_links(tmp_path):
    uid, gid = os.getuid(), os.getgid()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret").write_text("keep")
    os.symlink(victim, tmp_path / "arch")                                                      # the archive dir was swapped for a symlink
    with pytest.raises(cl._Changed):
        cd._write_verified(str(tmp_path / "arch" / "f.txt"), "x", uid, gid)
    assert [p.name for p in victim.iterdir()] == ["secret"]
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(victim / "secret", real / "f.txt")                                                 # a planted symlink where the file goes
    with pytest.raises(OSError):
        cd._write_verified(str(real / "f.txt"), "x", uid, gid)
    os.symlink(victim / "secret", real / "g.txt.part")                                            # ... or where the temp file goes
    cd._write_verified(str(real / "g.txt"), "y\n", uid, gid)
    assert (victim / "secret").read_text() == "keep" and (real / "g.txt").read_text() == "y\n" and not os.path.islink(real / "g.txt")
    os.symlink(tmp_path, tmp_path / "up")
    with pytest.raises(cl._Changed):
        cd._write_verified(str(tmp_path / "up" / "real" / "h.txt"), "z", uid, gid)               # a symlink ANYWHERE in the directory chain


def test_the_cold_archive_is_never_written_through_a_bucket_swapped_for_a_symlink(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    f = cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    approve(tmp_path, "large_cold_files", plan)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(victim, ch / "cold" / "studioprojects")                                           # planted between the plan and the copy (root writes here)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and not list(victim.iterdir()) and res.metrics["gone"] == 1 and not f.mutating()


def test_the_cold_copy_is_made_into_directories_we_created_through_fds_and_owned_like_the_source(tmp_path, ch, monkeypatch):
    big(ch / "StudioProjects" / "tools" / "old" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    f = cold_apply_env(monkeypatch)
    approve(tmp_path, "large_cold_files", cd.large_cold_files(cold_ctx(ch)).plan)
    cd.large_cold_files(cold_ctx(ch, apply=True))
    assert (ch / "cold" / "studioprojects" / "tools" / "old" / "d.bin").exists()
    assert os.stat(ch / "cold" / "studioprojects" / "tools").st_uid == os.getuid()
    rs = [c for c in f.calls if c.startswith("rsync")]
    assert rs and all("/proc/" in c and "/fd/" in c for c in rs)                                  # rsync is handed fd paths, not the user-writable path
    again = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert again.plan["items"] == []                                                                  # idempotent: the original is gone


def test_a_cold_destination_that_already_exists_is_never_merged_into(tmp_path, ch, monkeypatch):
    src = big(ch / "StudioProjects" / "oldtool" / "d.bin")
    age_tree(ch / "StudioProjects", 200)
    cold_apply_env(monkeypatch)
    plan = cd.large_cold_files(cold_ctx(ch)).plan
    (ch / "cold" / "studioprojects" / "oldtool").mkdir(parents=True)
    (ch / "cold" / "studioprojects" / "oldtool" / "older.bin").write_text("an earlier archive")
    approve(tmp_path, "large_cold_files", plan)
    res = cd.large_cold_files(cold_ctx(ch, apply=True))
    assert src.exists() and "exists" in res.summary and (ch / "cold" / "studioprojects" / "oldtool" / "older.bin").exists()


# ---- 11 (medium): tool_caches ------------------------------------------------------------------------------------------------------
def starts_before_act(monkeypatch, per_what):
    """Wrap ctx.act: a process (per_what[action] = proc dict) shows up AFTER the task's snapshot, right before that action."""
    real_act = core.Ctx.act

    def act(self, what, target, size, fn, protect_names=()):
        if self.apply and what in per_what:
            make_proc(cd.PROC, [per_what[what]])
        return real_act(self, what, target, size, fn, protect_names)

    monkeypatch.setattr(core.Ctx, "act", act)


def test_a_package_manager_started_after_the_task_snapshot_keeps_its_cache(tmp_path, tc, monkeypatch):
    """npm install / pip install / uv sync starts two minutes into the run: the snapshot is taken once, at task start."""
    f, cache = pm_mock(tc, monkeypatch)
    for d in (cache["npm"] / "_cacache", cache["pip"], cache["uv"]):
        fill(d)
    starts_before_act(monkeypatch, {"npm-cache": dict(pid=501, comm="npm", cmdline=["npm", "install"], cwd="/"),
                                    "pip-cache": dict(pid=502, comm="pip", cmdline=["pip", "install", "x"], cwd="/"),
                                    "uv-cache": dict(pid=503, comm="uv", cmdline=["uv", "sync"], cwd="/")})
    res = cd.tool_caches(tc_ctx(apply=True))
    assert not f.mutating() and res.metrics["gone"] == 3 and res.reclaimed_bytes == 0
    assert (cache["npm"] / "_cacache" / "f0").exists() and (cache["pip"] / "f0").exists() and (cache["uv"] / "f0").exists()
    assert "failed" not in outcomes(tmp_path)


def test_a_pnpm_started_after_the_snapshot_keeps_the_store(tmp_path, tc, monkeypatch):
    store = make_pnpm_store(tc, "v10")
    pins = tmp_path / "pins" / "app"
    pins.mkdir(parents=True)
    (pins / "package.json").write_text('{"packageManager": "pnpm@10.9.3"}')
    f, _ = pm_mock(tc, monkeypatch, rows=[("store prune", ok())])
    starts_before_act(monkeypatch, {"pnpm-store-prune": dict(pid=504, comm="pnpm", cmdline=["pnpm", "install"], cwd="/")})
    res = cd.tool_caches(tc_ctx(apply=True, projects_root=str(tmp_path / "pins")))
    assert not [c for c in f.mutating() if "store prune" in c] and res.metrics["gone"] == 1
    assert len(list((store / "files" / "ab").glob("orphan*"))) == 2


def test_a_browser_started_after_the_snapshot_keeps_its_cache_files(tmp_path, tc, monkeypatch):
    pm_mock(tc, monkeypatch)
    chrome = tc / ".cache" / "google-chrome" / "Default" / "Cache" / "Cache_Data" / "f_1"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"b" * 1000)
    age(chrome, 30)
    starts_before_act(monkeypatch, {"browser-cache-purge": dict(pid=505, comm="chrome", cmdline=["/opt/google/chrome/chrome"], cwd="/")})
    res = cd.tool_caches(tc_ctx(apply=True))
    assert chrome.exists() and res.metrics["gone"] == 1


def test_a_gradle_daemon_pid_that_is_alive_again_keeps_its_log(tmp_path, tc, monkeypatch):
    pm_mock(tc, monkeypatch)
    d = tc / ".gradle" / "daemon" / "9.4.1"
    d.mkdir(parents=True)
    for n in ("daemon-111.out.log", "daemon-222.out.log"):
        (d / n).write_bytes(b"l" * 500)
        age(d / n, 30)
    starts_before_act(monkeypatch, {"gradle-log-purge": dict(pid=111, comm="java", cmdline=["java", "GradleDaemon"], cwd="/")})
    cd.tool_caches(tc_ctx(apply=True))
    assert (d / "daemon-111.out.log").exists() and not (d / "daemon-222.out.log").exists()


def test_tools_run_under_timeout_so_the_whole_process_group_dies_and_the_runner_waits_longer(tmp_path, tc, monkeypatch):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    fill(cache["uv"])
    monkeypatch.setattr(cd, "_euid", lambda: 0)
    monkeypatch.setattr(cl, "_home_of", lambda user: (str(tc), 4242))
    monkeypatch.setattr(cd, "_user_of", lambda uid: ("ohmz", str(tc)))
    cd.tool_caches(tc_ctx(apply=True))
    npm = [c for c in f.calls if "npm cache clean" in c][0]
    assert f" timeout -k 10 300 npm cache clean --force" in npm and npm.startswith("runuser -u ohmz -- env HOME=")
    assert f.timeouts[npm] == 330                                                              # the runner's own limit is only a backstop
    uv = [c for c in f.calls if "uv cache prune" in c][0]
    assert " timeout -k 10 900 uv cache prune" in uv and f.timeouts[uv] == 930


def test_gnu_timeout_really_kills_the_grandchildren_too(tmp_path):
    """The assumption behind the wrapper, checked for real with a harmless sleep: `sh -c 'sleep 30 & wait'` under timeout."""
    if not shutil.which("timeout") or not shutil.which("sleep"):
        pytest.skip("coreutils timeout/sleep missing")
    pidfile = tmp_path / "pid"
    r = subprocess.run(["timeout", "-k", "1", "1", "sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], capture_output=True, timeout=20)
    assert r.returncode in (124, 143, 137)
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild survived the timeout")


def test_pnpm_is_never_downloaded_unpinned(tmp_path, tc, monkeypatch):
    (tc / ".local" / "bin" / "pnpm").unlink()                                                  # not on PATH
    store = make_pnpm_store(tc, "v10")
    f, _ = pm_mock(tc, monkeypatch, rows=[("store prune", ok())])
    res = cd.tool_caches(tc_ctx(apply=True, projects_root=str(tmp_path / "no-pins")))
    assert not [c for c in f.calls if "npx" in c or "store prune" in c]                         # nothing fetched from the registry, nothing run
    assert any("none pinned by packageManager" in r["proof"] for r in res.items) and len(list((store / "files" / "ab").glob("orphan*"))) == 2
    other = tmp_path / "pins" / "app"
    other.mkdir(parents=True)
    (other / "package.json").write_text('{"packageManager": "pnpm@9.15.0"}')                   # a pin of ANOTHER major does not count
    res = cd.tool_caches(tc_ctx(apply=True, projects_root=str(tmp_path / "pins")))
    assert not [c for c in f.calls if "store prune" in c]


def test_proc_age_reads_the_start_time_and_is_unknown_when_it_cannot(tmp_path):
    make_proc(cd.PROC, [dict(pid=600, comm="npx", age_s=3600), dict(pid=601, comm="npx")])
    assert abs(cd._proc_age(600) - 3600) < 1
    assert cd._proc_age(601) is None and cd._proc_age(99999) is None                           # no starttime field / no such process


@pytest.mark.parametrize("age_s,blocks", [(60, True), (1700, True), (3600, False), (None, True)])
def test_a_long_lived_npx_server_stops_blocking_the_npm_cache_but_a_young_or_unknown_one_still_does(tmp_path, tc, monkeypatch, age_s, blocks):
    """A long-lived npx MCP server kept the npm cache 'busy' for ever."""
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["npm"] / "_cacache")
    proc = dict(pid=600, comm="node", cmdline=["npx", "-y", "some-mcp-server"], cwd="/")
    if age_s is not None:
        proc["age_s"] = age_s
    make_proc(cd.PROC, [proc])
    res = cd.tool_caches(tc_ctx(apply=True))
    assert any("npm cache clean" in c for c in f.mutating()) != blocks, (age_s, res.summary)
    assert any("npm running" in r["proof"] for r in res.items if r["state"] == "kept") == blocks


def test_an_npm_install_is_never_long_lived_and_uvx_follows_the_npx_rule(tmp_path):
    make_proc(cd.PROC, [dict(pid=700, comm="npm", cmdline=["npm", "install"], age_s=99999),
                        dict(pid=701, comm="uvx", cmdline=["uvx", "ruff"], age_s=99999),
                        dict(pid=702, comm="uvx", cmdline=["uvx", "black"], age_s=5)])
    procs = cd._cmdlines()
    assert cd._pm_users([p for p in procs if p[0] == 700]) == {"npm": "pid 700 npm"}
    assert cd._pm_users([p for p in procs if p[0] == 701]) == {}
    assert cd._pm_users([p for p in procs if p[0] == 702]) == {"uv": "pid 702 uvx"}
    assert cd._pm_users([p for p in procs if p[0] == 701], long_lived_s=10**9) == {"uv": "pid 701 uvx"}


@pytest.mark.parametrize("argv,age_s,blocks", [(["uv", "run", "--no-sync", "python", "-m", "uvicorn", "api:app"], 8 * 86400, False),      # this host: 8 days old
                                               (["uv", "run", "server.py"], 30, True), (["uv", "tool", "run", "ruff"], 7200, False),
                                               (["uv", "sync"], 8 * 86400, True), (["uv", "pip", "install", "x"], 8 * 86400, True)])
def test_a_long_lived_uv_run_server_no_longer_pins_the_uv_cache_but_installs_always_do(tmp_path, tc, monkeypatch, argv, age_s, blocks):
    f, cache = pm_mock(tc, monkeypatch)
    fill(cache["uv"])
    make_proc(cd.PROC, [dict(pid=610, comm="uv", cmdline=argv, cwd="/", age_s=age_s)])
    res = cd.tool_caches(tc_ctx(apply=True))
    assert any("uv cache prune" in c for c in f.mutating()) != blocks, (argv, res.summary)


@pytest.mark.parametrize("line,keeps", [("serve:\n\tpython3 -m http.server --directory dist 8080\n", True),
                                        ("up:\n\tdocker run -d -v ./dist:/usr/share/nginx/html:ro nginx\n", True),
                                        ("clean:\n\trm -rf dist\n", False), ("all:\n\ttsc --outDir dist\n", False),
                                        ("pack:\n\tmkdir -p dist && cp app dist/\n", False)])
def test_a_makefile_that_serves_the_output_keeps_it_but_its_build_and_clean_steps_do_not(tmp_path, proj, line, keeps):
    p = idle_app(proj)
    put(p / "Makefile", line)
    res = run_build(tmp_path, apply=True)
    assert (p / "dist").exists() == keeps and (rows(res)["app/dist"]["proof"].startswith("referenced by") if keeps else res.metrics["selected"] == 1)


def test_a_read_of_the_venv_changes_the_verdict_but_not_the_plan_hash_of_other_venvs(tmp_path, vh):
    a = make_venv(vh / "a" / "pyenv")
    b = make_venv(vh / "b" / "pyenv")
    h1 = core.plan_hash(cd.unused_venvs(venv_ctx(vh)).plan)
    set_atime(a / "pyvenv.cfg", 100)                                   # a read long before the idle limit: still idle, plan unchanged
    assert core.plan_hash(cd.unused_venvs(venv_ctx(vh)).plan) == h1
    set_atime(b / "pyvenv.cfg", 2)                                     # a recent read: b leaves the plan
    assert [i["name"] for i in cd.unused_venvs(venv_ctx(vh)).plan["items"]] == ["a/pyenv"]


def test_the_runners_own_directory_fds_do_not_count_as_a_user_of_what_it_is_deleting(tmp_path, proj):
    """Found by a real run as root: the fd-anchored delete holds the project directory open while it re-proves 'unused'."""
    p = project(proj, "app")
    make_proc(cd.PROC, [dict(pid=ME, comm="homelab-maint", cwd="/", fds=[str(p), str(p / "target")], cmdline=["homelab-maint", str(p)])])
    assert cd._held(str(p)).unused and cd._held(str(p / "target")).unused
    res = run_build(tmp_path, apply=True)
    assert not (p / "target").exists() and res.metrics["selected"] == 1
    q = project(proj, "app2")
    make_proc(cd.PROC, [dict(pid=ME, comm="homelab-maint", cwd="/", fds=[str(q)]), dict(pid=9, comm="cargo", cwd=str(q / "src"))])
    assert cd._held(str(q)).used and "pid 9 (cargo) cwd" in cd._held(str(q)).why             # somebody ELSE still holds it
