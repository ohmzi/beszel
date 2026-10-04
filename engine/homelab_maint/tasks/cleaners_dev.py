"""cleaners_dev: standing cleanup of developer leftovers, so what was cleaned by hand does not build up again.

tool_caches         C1 daily   npm / pip / uv / pnpm caches, thumbnails > 30 d, Gradle daemon logs > 14 d, idle browser caches
stale_build_output  C1 weekly  target/ dist/ build/ .next/ ... of projects nobody touched for 90 d (git-ignored, untracked)
unused_venvs        C2 weekly  plan: Python virtualenvs nothing uses; approved apply archives the package list first
large_cold_files    C2 weekly  plan: > 2 GiB files/dirs untouched for 90 d in StudioProjects/.config/.android

Every removal comes with an IN-USE PROOF, a short sentence recorded in the Result items ("proof"). The rule of the module
is fail closed: any doubt (probe error, unreadable process table, unknown docker mounts, git failing, a scan that ran out
of budget, an unparsable number, a clock nobody can vouch for) means the item is KEPT, never selected.

  * Process proofs: the shared probes in homelab_maint/inuse.py (one /proc pass: cwd, exe, fds, maps, absolute argv/env
    tokens) PLUS a local index of RELATIVE argv tokens resolved against each process's cwd (`cd proj && ./venv/bin/python`).
    A venv / build dir is also held by a process working anywhere in its project directory.
  * Reference proofs (`_RefSearch`, local because the shared one cannot exclude a single path and skips symlinks): units,
    cron, shell rc files, launchers in ~/.local/bin, ~/.config, nginx/caddy, project run files; absolute, ~, $HOME and
    systemd %h/%E/%S forms, relative forms resolved against the file / WorkingDirectory= / `cd`, one level of script
    indirection; symlinked files and roots are followed; plus a scan for symlinks that resolve into the candidate.
  * "Idle" is the newest of mtime, ctime and (when the mount keeps it) the atime of the files: use does not modify a tree.
  * Build output is only removed when it is provably TOOL OUTPUT (cargo CACHEDIR.TAG, .next BUILD_ID, .pyc only, the nearest
    package.json / gradle / cmake recipe produces that directory) and holds none of: *.bak/.orig/.patch, .env, databases,
    keys, signed apks, files of another owner. dist/ and build/ stay report-only until `apply_generic = true`.
  * C1 mutations go through `cleaners._Acts` -> `ctx.act` (caps, protected list, PAUSE, audit); report mode only audits
    "dry-run", so the dry-run list is exactly the list apply mode walks. Root deletes go through directory fds opened
    component by component with O_NOFOLLOW (no path swap between check and delete), never through a user-controlled path.
  * C2 plans hold only facts that are stable between `plan` and `approve` (coarse MiB sizes, dates, no in-use states);
    apply needs `ctx.apply`, an approval for the plan hash, root (the process table must be complete) and re-proves every
    item right before it acts. Removal only after the copy on the cold disk was written and read back. A venv that holds
    anything a package list cannot rebuild (src/ checkouts, custom scripts, .git, files no package owns) is a manual item.
  * Tools run as the owner of the data (runuser) under `timeout` (the whole process group dies), and a venv's python never
    runs as root. No tool is downloaded unpinned.
"""
from __future__ import annotations

import bisect
import csv
import glob
import io
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

from .. import core, inuse
from ..core import GIB, Ctx, Result, audit, human, plan_hash, sh, task
from . import cleaners as cl
from .cleaners import MIB, _Acts, _ascii, _num, _skipped

DAY = 86400
PROC = Path("/proc")            # tests point this (and inuse.PROC) at a fake tree
MOUNTINFO = "/proc/self/mountinfo"
_euid = os.geteuid
_mono = time.monotonic          # tests never sleep
HOME_DEFAULT = "/home/ohmz"
COLD_ARCHIVE = "/media/WD24to10TB/archive"
PROJ_KINDS = ("cwd", "exe", "fd", "map", "argv")       # how a process can hold a project (environ paths are noise: PWD)
_DIRFLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}

# Never-touch floor for these cleaners (SPEC rules: Docker volumes, Plex, Ollama/ComfyUI models, ai-stack, databases,
# backups, Immich/Nextcloud, libvirt images, Cursor state, ~/models, browser profiles, keys). protected.toml is honoured
# on top (ctx.is_protected); `never_touch = [regex, ...]` in [tasks.X] adds more. A broken extra pattern protects all.
_NEVER = re.compile("|".join(f"(?:{p})" for p in (
    r"^/var/lib/(docker|libvirt|containerd)(/|$)", r"/var/snap/plexmediaserver", r"/media/(Immich|nextcloud)(/|$)",
    r"/media/SandiskSSD/plex", r"^/mnt/backup(/|$)", r"/ai-stack(/|$)", r"(surreal|notebook)_data|pgdata",
    r"/\.config/Cursor(/|$)", r"^/home/[^/]+/(models|\.cursor|\.ollama|\.ssh|\.gnupg|\.mozilla)(/|$)", r"/usr/share/ollama",
    r"/comfyui/models", r"/\.config/(google-chrome|chromium|BraveSoftware|microsoft-edge|vivaldi)(/|$)",
    r"/snap/[^/]+/[^/]+/\.config(/|$)",
)), re.I)


def _never(ctx: Ctx, path: str) -> str:
    """Why `path` must not be touched ('' = allowed)."""
    if _NEVER.search(path):
        return "never-touch path"
    extra = ctx.opt("never_touch", [])
    for p in extra if isinstance(extra, list) else ["("]:
        try:
            if re.search(str(p), path):
                return "never_touch config"
        except re.error:
            return "bad never_touch pattern"
    return "protected.toml" if ctx.is_protected(path) else ""


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _age(now: float, ts: float) -> str:
    return f"{max(now - ts, 0) / DAY:.0f}d"


def _odd(path: str) -> bool:
    """Names git would read as pathspec magic or a glob: git answers about a different path, so they are not trusted."""
    return bool(re.search(r"[*?\[\\\n]", path)) or any(c.startswith(":") for c in path.split("/"))


def _stamp(st: os.stat_result) -> float:
    """When something last CHANGED: newest of mtime and ctime (a tree restored with cp -p / rsync -a / tar keeps its old
    mtimes but gets ctime = now). Tests that cannot fake a ctime replace this."""
    return max(st.st_mtime, st.st_ctime)


# =========================================================================== is the clock believable?
def _ntp_synced() -> bool:
    r = sh(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
    return r.returncode == 0 and r.stdout.strip() == "yes"


def _heartbeat() -> float:
    """Newest mtime of anything the tool itself wrote (history, status, audit, any task state): the last moment this
    install believed it was."""
    paths = [core.STATE_DIR / "history.jsonl", core.STATE_DIR / "status.json", core.LOG_DIR / "audit.jsonl"]
    try:
        paths += list((core.STATE_DIR / "tasks").glob("*.json"))
    except OSError:
        pass
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def _clock_problem(ctx: Ctx) -> str:
    """'' when every "N days old" decision below can be trusted. Every age is `ctx.now` minus a timestamp, so a wrong
    clock (dead RTC after a power event, VM resume, a timer that fired before NTP) would turn every tree "old". Trusted
    when the system says NTP is synchronized, else only when now is within `clock_tolerance_h` of the tool's own last
    write (history/status/audit/task state of the 15-minute check tier)."""
    tol = _num(ctx.opt("clock_tolerance_h", 6), 0.1, 24 * 365)
    if tol is None:
        return "bad clock_tolerance_h config"
    if not _ntp_synced():
        hb = _heartbeat()
        if hb <= 0:
            return "clock unverified: NTP not synchronized and no earlier write to compare with"
        if abs(ctx.now - hb) > tol * 3600:
            return f"clock suspect: NTP not synchronized and now is {abs(ctx.now - hb) / 3600:.0f} h from the last write"
    ctx.state["last_now"] = ctx.now
    return ""


# =========================================================================== in-use proofs (shared probes in inuse.py)
def _snapshot_problem() -> str:
    """'' when /proc was read completely and docker can list its mounts, else why no proof is possible."""
    inuse.reset_caches()
    snap = inuse.proc_snapshot()
    if not snap.ok:
        return f"process table unusable: {snap.err}"
    return "" if inuse.container_bind_mounts() is not None else "docker mounts unknown"


class _LaunchIdx:
    snap: object = None
    keys: list[str] = []
    who: dict[str, tuple[int, str]] = {}
    err: str = ""


_LAUNCH = _LaunchIdx()


def _launch_index() -> _LaunchIdx:
    """Every argv token of every process that is a RELATIVE path, resolved against that process's cwd. inuse.py only
    records absolute tokens, so `cd ~/tools/foo && nohup ./venv/bin/python server.py` was invisible. Cached per /proc
    snapshot (a new snapshot, i.e. any reset, rebuilds it)."""
    snap = inuse.proc_snapshot()
    if _LAUNCH.snap is snap:
        return _LAUNCH
    who: dict[str, tuple[int, str]] = {}
    err = ""
    try:
        names = os.listdir(PROC)
    except OSError as exc:
        names, err = [], f"/proc unreadable ({type(exc).__name__})"
    me = os.getpid()
    for n in names:
        if not n.isdigit() or int(n) == me:
            continue
        base = PROC / n
        try:
            cwd = os.readlink(base / "cwd")
            raw = (base / "cmdline").read_bytes()
        except PermissionError:
            err = err or "unreadable process entries"
            continue
        except OSError:
            continue                                   # exited, or a kernel thread
        cwd = cwd[:-10] if cwd.endswith(" (deleted)") else cwd
        if not cwd.startswith("/"):
            continue
        comm = _rd(base / "comm").strip()[:20] or "?"
        for arg in raw.decode("utf-8", "replace").split("\0"):
            for piece in {arg, *re.split(r"[=:,;]", arg)}:           # --config=venv/x, PATH-like lists
                if piece and not piece.startswith(("/", "-", "~", "$", "%")):
                    who.setdefault(os.path.normpath(os.path.join(cwd, piece)), (int(n), comm))
    _LAUNCH.snap, _LAUNCH.who, _LAUNCH.keys, _LAUNCH.err = snap, who, sorted(who), err
    return _LAUNCH


def _launched_under(path: str) -> inuse.Proof:
    idx = _launch_index()
    if idx.err:
        return inuse.Proof(True, False, _ascii("unknown: " + idx.err, 110))
    for p in {os.path.normpath(path), os.path.realpath(path)}:
        hit = idx.who.get(p)
        if hit is None:
            i = bisect.bisect_left(idx.keys, p + "/")
            if i < len(idx.keys) and idx.keys[i].startswith(p + "/"):
                hit = idx.who[idx.keys[i]]
        if hit:
            return inuse.Proof(True, True, _ascii(f"pid {hit[0]} ({hit[1]}) relative argv", 110))
    return inuse.Proof(False, True, "no relative-path launch")


def _proc_hold(path: str, kinds) -> inuse.Proof:
    """inuse.process_cwd_or_open_under, minus THIS process: the delete holds directory fds on the very parents it is about
    to delete from (fd-anchored removal), and that must not count as somebody using the project."""
    if not os.path.isabs(path):
        return inuse.Proof(True, False, "unknown: path is not absolute")
    snap = inuse.proc_snapshot()
    if not snap.ok:
        return inuse.Proof(True, False, _ascii(f"unknown: {snap.err}", 110))
    me, hits = os.getpid(), []
    for p in {os.path.normpath(path), os.path.realpath(path)}:
        hits += [h for h in snap.under(p, kinds) if h[0] != me]
    if hits:
        uniq = sorted({(pid, kind) for pid, kind, _ in hits})
        why = "; ".join(f"pid {pid} ({_rd(PROC / str(pid) / 'comm').strip()[:20] or '?'}) {kind}" for pid, kind in uniq[:3])
        return inuse.Proof(True, True, _ascii(why + (f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else ""), 110))
    return inuse.Proof(False, True, f"no cwd/exe/fd/map/argv/env under it in {snap.nproc} processes")


def _held(path: str, kinds=PROJ_KINDS) -> inuse.Proof:
    """Is `path` in use right now: bind-mounted by any container or held by any process (open, mapped, cwd, argv absolute
    or relative to the process's cwd)? `.unused` is the only go."""
    held = inuse.combine(inuse.mounted_by_container(path), _proc_hold(path, kinds))
    if kinds is None or "argv" in kinds:
        held = inuse.combine(held, _launched_under(path))
    return held


def _venv_held(v: str, root: str, home: str) -> inuse.Proof:
    """A venv is also in use when anything works in its PROJECT directory (the process may have launched it with a path
    relative to that directory); a loose venv directly under the scan root / $HOME has no project to ask."""
    held = _held(v, None)                                  # None: also environ (VIRTUAL_ENV, PATH of an activated shell)
    parent = os.path.dirname(v)
    if parent not in (root, home, "/") and cl._inside(parent, [root]):
        ph = _held(parent)
        held = inuse.Proof(False, True, held.why + "; nor its project dir") if held.unused and ph.unused else inuse.combine(held, ph)
    return held


class _Recheck:
    """Last-moment re-proof before each delete: fresh probes, but at most one full /proc + docker re-scan per `ttl` s."""

    def __init__(self, ttl: float = 20.0):
        self.ttl, self.at = ttl, -1e9

    def held(self, path: str, kinds=PROJ_KINDS) -> inuse.Proof:
        if _mono() - self.at > self.ttl:
            inuse.reset_caches()
            self.at = _mono()
        return _held(path, kinds)


def _project_top(path: str, root: str) -> str | None:
    """Nearest ancestor of `path` that is a git work tree, looking only strictly below the scan root."""
    p = os.path.dirname(path)
    while len(p) > len(root) and p.startswith(root + "/"):
        if os.path.lexists(os.path.join(p, ".git")):
            return p
        p = os.path.dirname(p)
    return None


def _below(repo: str, root: str) -> bool:
    """A repo that counts as 'the project': inside the scan root, never the root itself (a dotfiles repo in ~)."""
    return bool(repo) and repo != root and cl._inside(repo, [root])


def _single_device(path: str, limit: int = 2_000_000) -> bool:
    """True when everything below `path` lives on the same filesystem as `path` itself. rmtree would otherwise walk into
    a mount point and empty the mounted filesystem. Unreadable parts or a huge tree: cannot prove, so False."""
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return False
    stack, n = [path], 0
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for en in it:
                    n += 1
                    st = en.stat(follow_symlinks=False)
                    if n > limit or st.st_dev != dev:
                        return False
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(en.path)
        except OSError:
            return False
    return True


# =========================================================================== deleting / writing without a path race
def _open_dir_nofollow(path: str) -> int:
    """fd of the directory `path`, opened from "/" one component at a time with O_NOFOLLOW: a symlink anywhere in it (also
    one swapped in after the caller's own check) makes this fail instead of redirecting a root-privileged write."""
    if not os.path.isabs(path):
        raise cl._Changed("path is not absolute")
    fd = os.open("/", _DIRFLAGS)
    try:
        for part in [p for p in os.path.normpath(path).split("/") if p]:
            nfd = os.open(part, _DIRFLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError as exc:
        os.close(fd)
        raise cl._Changed(f"directory chain changed or unsafe ({exc.strerror})") from None
    return fd


def _rm_anchored(root: str, path: str, st: os.stat_result, before: Callable[[str], None] | None = None) -> None:
    """Remove `path` (a file or a tree) below `root`: the parents are opened with O_NOFOLLOW directory fds, the entry must
    still be the very inode (and mtime) that was proved, `before(fdpath)` may veto (mount point, in use), and the delete
    is made relative to the parent fd. Nobody can redirect it by swapping a parent directory for a symlink."""
    if path == root or not cl._inside(path, [root]):
        raise RuntimeError("path is outside its root: refusing")
    parts = os.path.relpath(path, root).split(os.sep)
    fd = _open_dir_nofollow(root)
    try:
        try:
            for p in parts[:-1]:
                nfd = os.open(p, _DIRFLAGS, dir_fd=fd)
                os.close(fd)
                fd = nfd
            name = parts[-1]
            now = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            raise cl._Changed(f"path changed ({exc.strerror})") from None
        if (now.st_ino, now.st_dev, now.st_mtime_ns, stat.S_IFMT(now.st_mode)) != \
                (st.st_ino, st.st_dev, st.st_mtime_ns, stat.S_IFMT(st.st_mode)):
            raise cl._Changed("changed since the proof")
        if before:
            before(f"/proc/self/fd/{fd}/{name}")
        if stat.S_ISDIR(now.st_mode):
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("platform rmtree is not symlink-safe")
            shutil.rmtree(name, dir_fd=fd)
        elif stat.S_ISLNK(now.st_mode):
            raise RuntimeError("refusing to remove a symlink candidate")
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


# =========================================================================== atime / tree facts
def _atime_kept(path: str) -> bool:
    """True when the mount holding `path` records access times (relatime / strictatime). noatime, or any doubt: False,
    and then "last read" is unknowable."""
    try:
        text = Path(MOUNTINFO).read_text()
    except OSError:
        return False
    rp, best, opts = os.path.realpath(path), -1, None
    for ln in text.splitlines():
        f = ln.split(" ")
        try:
            dash = f.index("-", 6)
        except ValueError:
            continue
        mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[4])
        if (rp == mp or rp.startswith(mp.rstrip("/") + "/")) and len(mp) >= best:
            best = len(mp)
            opts = set(f[5].split(",")) | (set(f[dash + 3].split(",")) if len(f) > dash + 3 else set())
    return opts is not None and "noatime" not in opts


def _deny_reason(name: str) -> str:
    """Files that a build never produces from nothing or that hold something irreplaceable."""
    n = name.lower()
    if ".bak" in n or n.endswith(("~", ".orig", ".rej", ".patch", ".diff", ".swp")):
        return "backup/patch file"
    if n.startswith(".env") or n.endswith(".env"):
        return "env file"
    if n.endswith((".db", ".sqlite", ".sqlite3", ".sql", ".db-wal", ".db-shm")):
        return "database"
    if n.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")) or n.startswith(("id_rsa", "id_ed25519")):
        return "key material"
    if n.endswith((".apk", ".aab")):
        return "signed app bundle"
    return ""


@dataclass
class _Tree:
    size: int = 0
    newest: float = 0.0          # newest mtime/ctime of anything in it (the directory itself included)
    atime: float = 0.0           # newest atime of a regular file: what a reader leaves behind
    complete: bool = True
    files: int = 0
    top: set = field(default_factory=set)            # names directly inside
    deny: dict = field(default_factory=dict)         # reason -> first example name
    unlisted: list = field(default_factory=list)     # venv: files no package's RECORD names
    changed: list = field(default_factory=list)      # venv: files whose size differs from what their package's RECORD says
    non_pyc: bool = False
    subdirs: bool = False


def _venv_ignorable(v: str, path: str) -> bool:
    rel = os.path.relpath(path, v)
    base, d = os.path.basename(rel), os.path.dirname(rel)
    return (base.endswith((".pyc", ".pyo")) or "__pycache__" in rel.split(os.sep) or rel in ("pyvenv.cfg", ".gitignore", "CACHEDIR.TAG", "lib64")
            or (d == "bin" and re.fullmatch(r"python[\d.]*|activate[\w.]*|Activate\.ps1|deactivate\.nu|pydoc\.bat", base) is not None)
            or (d.endswith(".dist-info") and base in ("INSTALLER", "REQUESTED", "RECORD", "direct_url.json"))
            or base in ("_virtualenv.py", "_virtualenv.pth", "distutils-precedence.pth"))


def _inspect(path: str, *, owner: int | None = None, deny: bool = False, recorded: dict | None = None,
             limit: int = 2_000_000, budget_s: float = 120.0) -> _Tree:
    """One lstat walk (nothing is opened, so nothing's atime changes): size (like cleaners._tree_stats), newest
    mtime/ctime, newest file atime, top-level names and, on request, content that must not be deleted as output."""
    t = _Tree()
    try:
        t.newest = _stamp(os.lstat(path))
    except OSError:
        t.complete = False
        return t
    stack, n, t0 = [path], 0, _mono()
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            t.complete = False                       # an unreadable part cannot be proved harmless
            continue
        with it:
            for e in it:
                n += 1
                if n > limit or _mono() - t0 > budget_s:
                    t.complete = False
                    return t
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    t.complete = False
                    continue
                t.size += st.st_size
                t.newest = max(t.newest, _stamp(st))
                if d == path:
                    t.top.add(e.name)
                if stat.S_ISDIR(st.st_mode):
                    stack.append(e.path)
                    t.subdirs = True
                    continue
                t.files += 1
                if stat.S_ISREG(st.st_mode):
                    t.atime = max(t.atime, st.st_atime)
                if not e.name.endswith((".pyc", ".pyo")):
                    t.non_pyc = True
                if deny:
                    why = _deny_reason(e.name)
                    if why:
                        t.deny.setdefault(why, e.name)
                    if owner is not None and st.st_uid != owner:
                        t.deny.setdefault("file of another owner", e.name)
                if recorded is not None and not _venv_ignorable(path, e.path):
                    exp = recorded.get(e.path, -1)
                    if exp == -1:
                        if len(t.unlisted) < 20:
                            t.unlisted.append(os.path.relpath(e.path, path))
                    elif exp is not None and stat.S_ISREG(st.st_mode) and st.st_size != exp and len(t.changed) < 20:
                        t.changed.append(os.path.relpath(e.path, path))
    return t


_HEAVY = {".git", "node_modules", ".venv", "venv", ".gradle", ".idea", ".cache", ".tox", ".nox", "site-packages", ".dart_tool",
          "__pycache__", ".pytest_cache", ".mypy_cache", "target", "dist", "build", ".next", ".turbo", ".vite"}


def _project_newest(top: str, exclude: str = "", limit: int = 400_000, budget_s: float = 60.0) -> tuple[float, bool]:
    """Newest mtime/ctime of the WORKING files of a project dir: everything but dependencies, VCS data, virtualenvs and
    build output. Unlike git status this sees ignored files too (.env, local config) and files restored with their old
    mtimes. (newest, complete); incomplete means unknown."""
    try:
        newest = _stamp(os.lstat(top))
    except OSError:
        return 0.0, False
    stack, n, t0 = [top], 0, _mono()
    while stack:
        d = stack.pop()
        try:
            ents = list(os.scandir(d))
        except OSError:
            return newest, False
        if any(x.name == "pyvenv.cfg" for x in ents):
            continue                                    # a virtualenv of some name
        for e in ents:
            n += 1
            if n > limit or _mono() - t0 > budget_s:
                return newest, False
            if e.name in _HEAVY or e.name.startswith("node_modules") or e.path == exclude:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                return newest, False
            newest = max(newest, _stamp(st))
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
    return newest, True


# =========================================================================== git: every enclosing repo and worktree
def _git_cmd(workdir: str, args: list[str], timeout: int = 60):
    try:
        uid = os.stat(workdir).st_uid
    except OSError:
        return None
    cmd = _as_owner(uid, ["git", "-C", workdir, "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", *args])
    return sh(cmd, timeout=timeout, env=GIT_ENV) if cmd else None


def _git_extra(repo: str, now: float) -> tuple[float, str]:
    """(newest ref commit date or linked-worktree activity, why-unknown). A commit made in a linked worktree moves the
    shared branch ref and the worktree's own HEAD, not the main repo's HEAD reflog."""
    r = _git_cmd(repo, ["for-each-ref", "--sort=-committerdate", "--count=1", "--format=%(committerdate:unix)"])
    if r is None or r.returncode != 0:
        return 0.0, "unknown: git for-each-ref failed"
    out = r.stdout.strip()
    if out and not out.isdigit():
        return 0.0, "unknown: unparsable git for-each-ref"
    newest = float(out or 0)
    w = _git_cmd(repo, ["worktree", "list", "--porcelain"])
    if w is None or w.returncode != 0:
        return newest, "unknown: git worktree list failed"
    for ln in w.stdout.splitlines():
        if not ln.startswith("worktree "):
            continue
        wp = ln[9:].strip()
        if os.path.realpath(wp) == os.path.realpath(repo) or not os.path.isdir(wp):
            continue
        gs = inuse.git_state(wp, now)
        if not gs.known:
            return newest, f"unknown: worktree {os.path.basename(wp)}: {gs.why}"[:100]
        newest = max(newest, gs.last_activity)
    return newest, ""


def _project_activity(ctx: Ctx, root: str, path: str, gs0: inuse.GitState, memo: dict) -> tuple[str, float, str]:
    """(why-not-known, newest activity, project dir). `gs0` is the git state of the nearest repo. The project is the
    top-level directory under the scan root; EVERY repo between `path` and it (nested repos, submodules, a superproject)
    must be quiet, as must be its linked worktrees, and so must its working files (ignored ones and ctime included)."""
    outer = os.path.join(root, os.path.relpath(path, root).split(os.sep)[0])
    newest, cur, seen = 0.0, gs0, set()
    while cur.repo not in seen:
        seen.add(cur.repo)
        if cur.repo not in memo:
            memo[cur.repo] = _git_extra(cur.repo, ctx.now)
        extra, why = memo[cur.repo]
        if why:
            return why, 0.0, outer
        newest = max(newest, cur.last_activity, extra)
        parent = os.path.dirname(cur.repo)
        if len(parent) <= len(root) or not parent.startswith(root + "/"):
            break
        up = inuse.git_state(parent, ctx.now)
        if not up.known:
            return up.why, 0.0, outer
        if not (up.in_repo and _below(up.repo, root)):
            break
        cur = up
    if ("walk", outer) not in memo:
        memo[("walk", outer)] = _project_newest(outer)
    wn, complete = memo[("walk", outer)]
    if not complete:
        return "project activity unmeasurable", 0.0, outer
    return "", max(newest, wn), outer


# =========================================================================== reference search (units, cron, scripts, configs)
_NAMECH = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_@+-")
_STOP = frozenset(" \t\r\n\f\v'\"`=:;,()<>|&[]")            # a path token starts and ends at these
_VENV_SUFFIX = ("/bin", "/lib", "/activate", "/Scripts", "/include")
_BIN_EXT = frozenset(".png .jpg .jpeg .gif .webp .ico .svg .mp4 .mkv .mp3 .wav .flac .so .o .a .class .jar .apk .zip .gz .zst .xz "
                     ".bz2 .tar .tgz .pyc .pyo .sqlite .db .bin .gguf .safetensors .onnx .pt .pth .woff .woff2 .ttf .pdf .lock "
                     ".log .whl .iso .img .qcow2".split())
_EXEC_NAME = re.compile(r".*\.(?:service|timer|socket|path|sh|bash|zsh|fish|cron|container)$|(?:Makefile|makefile|justfile|Procfile"
                        r"|Caddyfile|Dockerfile.*|.*\.dockerfile|(?:docker-)?compose.*\.ya?ml|crontab.*|supervisord.*|\.bashrc"
                        r"|\.bash_profile|\.bash_aliases|\.profile|\.zshrc|\.zprofile|\.zshenv|ecosystem\.config\..*)$")
_REF_SKIP = {".git", "node_modules", "__pycache__", "site-packages", ".gradle", ".cache", ".mypy_cache", ".pytest_cache", "pgdata",
             "surreal_data", "notebook_data", "volumes", "models", "backups", "Trash", "target", "dist", "build", ".next",
             ".turbo", ".tox", ".npm", ".cargo", ".rustup", ".venv", "venv"}
_CFG_SKIP = {"Cache", "Code Cache", "GPUCache", "CachedData", "CachedExtensions", "CachedExtensionVSIXs", "Service Worker",
             "IndexedDB", "Local Storage", "Session Storage", "blob_storage", "logs", "Crashpad", "workspaceStorage", "History",
             "google-chrome", "chromium", "BraveSoftware", "microsoft-edge", "vivaldi", "Slack", "discord", "pulse", "dconf",
             "libreoffice", "JetBrains", "Google", "snap"}
_GROUPS = {"unit": (True, True), "bin": (True, False), "proj": (False, False), "config": (False, False)}   # (every file runs, $HOME base)
_MAKE_NOISE = re.compile(r"\b(?:rm|rmdir|clean|mkdir|cp|mv|touch|tsc|cargo|build|webpack|rollup|vite|esbuild|gcc|cc|go|npm|pnpm|yarn|install)\b", re.I)
_PATH_TOK = re.compile(r"(?<![\w.~$%{}/-])(?:(?:~|\$HOME|\$\{HOME\}|%h)/|/)[^\s'\"`=:;,()<>|&\[\]]{2,400}")


class _Target(NamedTuple):
    key: str
    path: str
    role: str            # "exact": the candidate itself; "scope": the project directory around it (a script that `cd`s there)
    rel: str             # "venv": a relative mention only counts when followed by /bin /lib ...; "any"
    forms: tuple
    own: str = ""        # files below this directory are the candidate's own project: a Makefile there is not a user


class _RefSearch:
    """One walk of the roots for every target. A reference is a path token that ENDS at a component named like the target
    and resolves to it: absolute, ~ / $HOME / ${HOME} / systemd %h %E %S %C %t, or relative (resolved against the file's
    own directory, WorkingDirectory=, `cd X`, and $HOME for cron / user units). The candidate's own tree is never read
    (exclude, by inode); symlinked files and directories are followed (loops are cut by inode); one level of script
    indirection is followed. An unreadable root or file or an exhausted budget makes "no reference" UNKNOWN."""

    def __init__(self, targets: dict[str, list[tuple]], home: str, *, budget_s: float = 120.0,
                 max_files: int = 2_000_000, exclude: tuple | list = (), skip: set | None = None):
        self.home, self.budget, self.max_files, self.skip = home, budget_s, max_files, set(skip if skip is not None else _REF_SKIP)
        self.uid = os.stat(home).st_uid if os.path.isdir(home) else os.getuid()
        self.user = os.path.basename(home.rstrip("/"))
        self.by_base: dict[str, list[_Target]] = {}
        self.keys = list(targets)
        for key, lst in targets.items():
            for path, role, rel, *own in lst:
                forms = tuple(dict.fromkeys((os.path.normpath(path), os.path.realpath(path))))
                self.by_base.setdefault(os.path.basename(path.rstrip("/")), []).append(_Target(key, path, role, rel, forms, own[0] if own else ""))
        self.found: dict[str, str] = {}
        self.incomplete, self.nfiles, self.t0 = "", 0, _mono()
        self.excl: set = set()
        self.excl_real: list[str] = []
        for p in exclude:
            try:
                st = os.lstat(p)
                self.excl.add((st.st_dev, st.st_ino))
                self.excl_real.append(os.path.realpath(p))
            except OSError:
                pass
        self.seen: set = set()
        self.queued: list[str] = []
        self.q_seen: set = set()

    # -- paths ---------------------------------------------------------------------------------------------
    def _in_excl(self, path: str) -> bool:
        rp = os.path.realpath(path)
        return any(rp == e or rp.startswith(e + "/") for e in self.excl_real)

    def _expand(self, tok: str) -> tuple[str, str]:
        """("abs", path) | ("rel", "") | ("skip", "") for a token whose variable cannot be resolved."""
        h = self.home
        for pre, rep in (("~/", h + "/"), ("$HOME/", h + "/"), ("${HOME}/", h + "/"), ("%h/", h + "/"), ("%E/", h + "/.config/"),
                         ("%S/", h + "/.local/state/"), ("%C/", h + "/.cache/"), ("%t/", f"/run/user/{self.uid}/")):
            if tok.startswith(pre):
                return "abs", rep + tok[len(pre):]
        m = re.match(r"~([A-Za-z0-9_.-]+)/", tok)
        if m:
            return ("abs", h + "/" + tok[m.end():]) if m.group(1) == self.user else ("skip", "")
        if tok.startswith("/"):
            return "abs", tok
        return ("skip", "") if tok[:1] in "~$%" else ("rel", "")

    def _bases(self, fp: str, text: str, home_base: bool) -> list[str]:
        bases = [os.path.dirname(fp)] + ([self.home] if home_base else [])
        for m in re.finditer(r"(?:WorkingDirectory\s*=\s*-?|\bcd\s+(?:--\s+)?|\bpushd\s+)([^\s;&|'\"`)]+)", text):
            kind, p = self._expand(m.group(1))
            if kind == "abs":
                bases.append(os.path.normpath(p))
            elif kind == "rel":
                bases += [os.path.normpath(os.path.join(b, m.group(1))) for b in bases[:2]]
            if len(bases) > 40:
                break
        return bases

    # -- one file ------------------------------------------------------------------------------------------
    def _inc(self, why: str) -> None:
        self.incomplete = self.incomplete or why

    def _file(self, fp: str, st: os.stat_result, grp: str, explicit: bool = False, level: int = 0) -> None:
        self.nfiles += 1
        if self.nfiles > self.max_files or _mono() - self.t0 > self.budget:
            self._inc("grep budget exhausted")
            return
        if not explicit and os.path.splitext(fp)[1].lower() in _BIN_EXT:
            return
        limit = (32 if explicit else 1) * MIB
        if st.st_size > limit:
            if explicit:
                self._inc(f"file too large to search: {os.path.basename(fp)}")
            return
        try:
            with open(fp, "rb") as f:
                raw = f.read(limit)
        except PermissionError:
            self._inc(f"unreadable file {os.path.basename(fp)}")
            return
        except OSError:
            return
        if b"\0" in raw[:4096]:
            return
        text = raw.decode("utf-8", "replace")
        if os.path.basename(fp) in ("Makefile", "makefile", "GNUmakefile", "justfile"):
            text = "\n".join(ln for ln in text.splitlines() if not _MAKE_NOISE.search(ln))    # `rm -rf dist`, `tsc -o dist`: not a use
        all_exec, home_base = _GROUPS[grp]
        exec_ctx = all_exec or text.startswith("#!") or bool(st.st_mode & 0o111) or bool(_EXEC_NAME.match(os.path.basename(fp)))
        self._match(fp, text, exec_ctx, home_base)
        if level == 0 and exec_ctx:
            self._queue_scripts(text)

    def _match(self, fp: str, text: str, exec_ctx: bool, home_base: bool) -> None:
        bases: list[str] | None = None
        for b, tl in self.by_base.items():
            if b not in text or all(t.key in self.found for t in tl):
                continue
            for k, m in enumerate(re.finditer(re.escape(b), text)):
                if k > 4000:
                    self._inc(f"too many mentions in {os.path.basename(fp)}")
                    break
                s, e = m.span()
                if s and text[s - 1] != "/" and text[s - 1] not in _STOP:
                    continue
                if e < len(text) and (text[e] in _NAMECH or (text[e] == "." and e + 1 < len(text) and text[e + 1] in _NAMECH)):
                    continue
                j = s
                while j > 0 and text[j - 1] not in _STOP and s - j < 1024:
                    j -= 1
                tok = text[j:e]
                kind, ab = self._expand(tok)
                if kind == "skip":
                    continue
                if kind == "abs":
                    cands, relative = [os.path.normpath(ab)], False
                else:
                    if not exec_ctx:
                        continue
                    bases = bases if bases is not None else self._bases(fp, text, home_base)
                    cands, relative = [os.path.normpath(os.path.join(bd, tok)) for bd in bases], True
                for t in tl:
                    if t.key in self.found or not any(c in t.forms for c in cands):
                        continue
                    if t.own and (fp == t.own or fp.startswith(t.own + "/")):
                        continue                        # the project's own README / Makefile / run.sh: the project-activity proofs judge those
                    if t.role == "scope" and (not exec_ctx or fp == t.path or fp.startswith(t.path + "/")):
                        continue                        # only OTHER running things count, and only code that runs
                    if relative and t.rel == "venv" and not text.startswith(_VENV_SUFFIX, e):
                        continue                        # `venv` in a comment is not `venv/bin/python`
                    self.found[t.key] = fp

    def _queue_scripts(self, text: str) -> None:
        """Remember files a unit / launcher / rc file points at: they may be the ones that name the venv."""
        for m in _PATH_TOK.finditer(text):
            if len(self.queued) >= 4000:
                return
            kind, p = self._expand(m.group(0))
            if kind != "abs":
                continue
            p = os.path.normpath(p)
            if p in self.q_seen or not p.startswith((self.home + "/", "/opt/", "/srv/", "/usr/local/", "/etc/")):
                continue
            self.q_seen.add(p)
            if os.path.splitext(p)[1].lower() not in _BIN_EXT and os.path.isfile(p) and not self._in_excl(p):
                self.queued.append(p)

    # -- the walk ------------------------------------------------------------------------------------------
    def _walk(self, root: str, grp: str) -> None:
        skip = self.skip | (_CFG_SKIP if grp == "config" else set())
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                ents = list(os.scandir(d))
            except FileNotFoundError:
                continue
            except OSError:
                self._inc(f"cannot list {os.path.basename(d)}")
                continue
            if any(x.name == "pyvenv.cfg" for x in ents):
                continue                                # a virtualenv: its insides are not config
            for x in ents:
                if self.incomplete == "grep budget exhausted":
                    return
                try:
                    st = x.stat()                       # follows symlinks: a symlinked unit or directory is searched through
                except OSError:
                    continue                            # dangling link: no content
                if stat.S_ISDIR(st.st_mode):
                    key = (st.st_dev, st.st_ino)
                    if x.name in skip or x.name.startswith("node_modules") or key in self.seen or key in self.excl:
                        continue
                    self.seen.add(key)
                    stack.append(x.path)
                elif stat.S_ISREG(st.st_mode):
                    if x.is_symlink() and self._in_excl(x.path):
                        continue
                    self._file(x.path, st, grp)

    def run(self, roots: list[tuple[str, str]]) -> dict[str, inuse.Proof]:
        for path, grp in roots:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            except OSError:
                self._inc(f"cannot stat {os.path.basename(path)}")
                continue
            if stat.S_ISREG(st.st_mode):
                self._file(path, st, grp, explicit=True)
            elif stat.S_ISDIR(st.st_mode):
                self._walk(path, grp)
        for fp in self.queued:                           # one level: files the primary files point at
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size <= MIB:
                self._file(fp, st, "bin", level=1)
        res: dict[str, inuse.Proof] = {}
        for k in self.keys:
            if k in self.found:
                res[k] = inuse.Proof(True, True, _ascii("referenced by " + self.found[k][-70:], 110))
            elif self.incomplete:
                res[k] = inuse.Proof(True, False, _ascii(f"unknown: reference search incomplete ({self.incomplete})", 110))
            elif self.nfiles == 0:
                res[k] = inuse.Proof(True, False, "unknown: no files were searched")      # an empty search proves nothing
            else:
                res[k] = inuse.Proof(False, True, f"no reference in {self.nfiles} unit/cron/script/config files")
        return res


def _ref_roots(ctx: Ctx, home: str, projects: bool = True) -> list[tuple[str, str]]:
    """(path, group) where a host script, unit, cron entry, launcher or config could name a path. `ref_paths` replaces the
    list ("unit:/dir" etc. picks the group; a bare path is a project-like root)."""
    opt = ctx.opt("ref_paths", None)
    if isinstance(opt, list):
        out = []
        for s in opt:
            if isinstance(s, str):
                g, sep, p = s.partition(":")
                out.append((p, g) if sep and g in _GROUPS and p.startswith("/") else (s, "proj"))
        return out
    roots = [(p, "unit") for p in ("/etc/systemd/system", "/etc/systemd/user", "/etc/cron.d", "/etc/cron.daily", "/etc/cron.weekly",
                                   "/etc/cron.hourly", "/etc/cron.monthly", "/etc/crontab", "/var/spool/cron/crontabs",
                                   "/etc/anacrontab", "/etc/rc.local", "/etc/supervisor", "/etc/nginx", "/etc/caddy")]
    roots += [(p, "bin") for p in ("/usr/local/sbin", "/usr/local/bin", "/etc/profile", "/etc/profile.d", "/etc/bash.bashrc")]
    roots += [("/etc/environment", "config")]
    homes = {home}
    try:
        homes |= {e.path for e in os.scandir("/home") if e.is_dir()}
    except OSError:
        pass
    for h in sorted(homes):
        roots += [(f"{h}/.config/systemd/user", "unit"), (f"{h}/.config/autostart", "unit"), (f"{h}/.local/bin", "bin")]
        roots += [(f"{h}/{f}", "bin") for f in (".profile", ".bashrc", ".bash_profile", ".bash_login", ".bash_aliases", ".zshrc",
                                                 ".zprofile", ".zshenv", ".config/fish")]
        roots += [(f"{h}/.claude.json", "config"), (f"{h}/.claude/settings.json", "config"), (f"{h}/.claude/settings.local.json", "config"),
                  (f"{h}/.config", "config")]
    if projects:
        roots += [(f"{home}/ai-stack", "proj"), (f"{home}/StudioProjects", "proj")]
    extra = ctx.opt("ref_extra", [])
    return roots + [(p, "proj") for p in (extra if isinstance(extra, list) else []) if isinstance(p, str)]


def _ref_check(ctx: Ctx, targets: dict[str, list[tuple]], home: str, extra_roots: list[tuple[str, str]] = (),
               projects: bool = True) -> dict[str, inuse.Proof]:
    s = _RefSearch(targets, home, budget_s=float(_num(ctx.opt("ref_timeout_s", 120), 5, 3600) or 120),
                   max_files=int(_num(ctx.opt("ref_max_files", 2_000_000), 1000, 50_000_000) or 2_000_000),
                   exclude=[t[0] for lst in targets.values() for t in lst if t[1] == "exact"])
    return s.run(_ref_roots(ctx, home, projects) + list(extra_roots))


_LINK_SKIP = {".git", "node_modules", ".cache", ".npm", ".cargo", ".rustup", ".gradle", "Trash", "site-packages", "__pycache__",
              ".mozilla", "snap", ".var", "Android", "target", "dist", "build", ".next"}


def _links_into(items: dict[str, str], roots: list[str], budget_s: float = 120.0,
                limit: int = 3_000_000) -> tuple[dict[str, str], str]:
    """({key: a symlink that resolves to or into items[key]}, why-incomplete). `find -lname` for several paths at once;
    the candidates' own trees are not searched. A symlink into a candidate is a use no text search can see."""
    reals = {k: os.path.realpath(p) for k, p in items.items()}
    ids = set()
    for p in items.values():
        try:
            st = os.lstat(p)
            ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    hits: dict[str, str] = {}
    why, n, t0 = "", 0, _mono()
    for root in roots:
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                ents = list(os.scandir(d))
            except FileNotFoundError:
                continue
            except OSError:
                why = why or f"cannot list {os.path.basename(d) or d}"
                continue
            for x in ents:
                n += 1
                if n > limit or _mono() - t0 > budget_s:
                    return hits, "link scan budget exhausted"
                try:
                    if x.is_symlink():
                        tgt = os.path.realpath(x.path)
                        for k, rp in reals.items():
                            if k not in hits and (tgt == rp or tgt.startswith(rp + "/")):
                                hits[k] = x.path
                    elif x.is_dir(follow_symlinks=False) and x.name not in _LINK_SKIP and not x.name.startswith("node_modules"):
                        st = x.stat(follow_symlinks=False)
                        if (st.st_dev, st.st_ino) not in ids:
                            stack.append(x.path)
                except OSError:
                    continue
    return hits, why


def _link_roots(ctx: Ctx, home: str) -> list[str]:
    opt = ctx.opt("link_roots", None)
    return [p for p in opt if isinstance(p, str)] if isinstance(opt, list) else [home, "/var/www", "/usr/local", "/etc", "/opt"]


# =========================================================================== running things as the data's owner
def _user_of(uid: int) -> tuple[str, str] | None:
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return None
    return pw.pw_name, pw.pw_dir


def _as_owner(uid: int, cmd: list[str], env: list[str] | None = None) -> list[str] | None:
    """Wrap `cmd` so it runs as `uid`: runuser when root, plain when already that user, None when impossible."""
    env = env or []
    me = _euid()
    if me == uid:
        return ["env", *env, *cmd] if env else cmd
    u = _user_of(uid)
    if me != 0 or not u:
        return None
    home = [] if any(e.startswith("HOME=") for e in env) else [f"HOME={u[1]}"]
    return ["runuser", "-u", u[0], "--", "env", *home, *env, *cmd]


def _finish(acts: _Acts, kept: list[tuple[str, int, str]], proofs: dict[str, str], noun: str, extra: dict,
            note: str = "") -> Result:
    """acts.result + the in-use proof on every selected row + the biggest KEPT rows (with the reason) so a skip is visible."""
    res = acts.result(noun, extra, note)
    rows = [dict(i, proof=proofs[i["name"]]) if i["name"] in proofs else i for i in res.items]
    k = [{"name": lab[:60], "size": human(sz) if sz else "-", "state": "kept", "proof": why[:150]}
         for lab, sz, why in sorted(kept, key=lambda t: (-t[1], t[0]))]
    res.items = (rows[:max(8, 12 - len(k))] + k)[:12]
    return res


# =========================================================================== tool_caches
_PM = {"npm": "npm", "npm-cli.js": "npm", "npx": "npm", "npx-cli.js": "npm", "pnpm": "pnpm", "pnpm.cjs": "pnpm",
       "pnpx": "pnpm", "corepack": "pnpm", "yarn": "yarn", "yarn.js": "yarn", "uv": "uv", "uvx": "uv"}
_USER_SCRIPT_VERBS = {"run", "run-script", "start", "dev", "test", "serve", "watch"}   # a dev server is not an install (npm/pnpm only)
_LAUNCHERS = ("npx", "pnpx", "uvx")                  # fetch into the cache, then often live on as a server (MCP servers)
_BROWSERS = {                                       # name -> (process names, cache dirs below the home directory)
    "firefox": ({"firefox", "firefox-bin"}, [".cache/mozilla/firefox/*/cache2",
                                             "snap/firefox/common/.cache/mozilla/firefox/*/cache2"]),
    "chrome": ({"chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"},
               [".cache/google-chrome/*/Cache", ".cache/google-chrome/*/Code Cache", ".cache/chromium/*/Cache",
                ".cache/chromium/*/Code Cache"]),
}
Procs = list[tuple[int, str, list[str]]]


def _rd(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def _cmdlines() -> Procs | None:
    """(pid, comm, argv) of every process (world readable); None when /proc cannot be listed."""
    try:
        names = os.listdir(PROC)
    except OSError:
        return None
    out = []
    for n in names:
        if n.isdigit():
            comm = _rd(PROC / n / "comm").strip()
            if comm:                                  # vanished mid-scan: nothing to record
                out.append((int(n), comm, [a for a in _rd(PROC / n / "cmdline").split("\0") if a]))
    return out


def _proc_age(pid: int) -> float | None:
    """Seconds since the process started (/proc/<pid>/stat starttime, /proc/uptime); None when unknown."""
    st, up = _rd(PROC / str(pid) / "stat"), _rd(PROC / "uptime").split()
    try:
        return float(up[0]) - float(st[st.rfind(")") + 2:].split()[19]) / cl.CLK_TCK
    except (ValueError, IndexError):
        return None


def _pm_users(procs: Procs, long_lived_s: float = 1800.0) -> dict[str, str]:
    """{tool: 'pid N cmd'} for package managers that are installing/fetching right now (their cache is in use). Any uv
    command counts (`uv run` resolves into the cache); `npm run dev` does not. The launchers npx / pnpx / uvx / `uv run` /
    `uv tool run` count while young: an MCP server or uvicorn started by one half an hour ago no longer touches the
    cache (unknown age: counts). Installs (npm install, pip install, uv sync) count however long they run."""
    busy: dict[str, str] = {}
    for pid, comm, argv in procs:
        base = [os.path.basename(a) for a in argv[:4]]
        for i, b in enumerate(base):
            tool = _PM.get(b) or ("pip" if re.fullmatch(r"pip[0-9.]*", b) else None)
            if not tool:
                continue
            verbs = [a for a in argv[i + 1:i + 4] if not a.startswith("-")]
            if b in _LAUNCHERS or (tool == "uv" and verbs[:1] in (["run"], ["tool"])):     # `uv run uvicorn ...` lives for days
                age = _proc_age(pid)
                running = age is None or age < long_lived_s
            else:
                running = tool == "uv" or not verbs or verbs[0] not in _USER_SCRIPT_VERBS
            if running:
                busy.setdefault(tool, f"pid {pid} {comm}")
            break
    return busy


def _running_now(exes: set) -> str:
    """Fresh /proc read: '' when none of the named processes runs, else who. Unreadable: say so (= treat as running)."""
    ps = _cmdlines()
    if ps is None:
        return "process table unreadable"
    for pid, comm, argv in ps:
        if comm in exes or (argv and os.path.basename(argv[0]) in exes):
            return f"pid {pid} {comm}"
    return ""


@dataclass
class _Env:
    user: str
    uid: int
    home: str
    path: str

    def run_cmd(self, argv: list[str]) -> list[str] | None:
        return _as_owner(self.uid, argv, [f"HOME={self.home}", f"PATH={self.path}"])

    def timed(self, argv: list[str], seconds: int) -> list[str] | None:
        """`timeout` runs the tool in its own process group and kills the WHOLE group (npx -> node -> pnpm) at the limit;
        a plain subprocess timeout would only kill the direct child and leave the grandchildren running unaudited."""
        return self.run_cmd(["timeout", "-k", "10", str(seconds), *argv])

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.path)


def _tool_dir(e: _Env, argv: list[str]) -> str | None:
    """Cache directory as the tool itself reports it (as the user); must lie below the user's home."""
    cmd = e.run_cmd(argv)
    r = sh(cmd, timeout=60) if cmd else None
    out = r.stdout.strip().splitlines() if r is not None and r.returncode == 0 else []
    real = os.path.realpath(out[-1]) if out and out[-1].startswith("/") else ""
    return real if real and real != e.home and cl._inside(real, [e.home]) else None


def _dir_size(path: str, budget_s: float = 120.0) -> int | None:
    size, _, complete = cl._tree_stats(path, 2_000_000, budget_s)
    return size if complete else None


def _pnpm_versions(root: str) -> dict[int, str]:
    """{major: highest exact version} from `packageManager: pnpm@x.y.z` in package.json files of the projects."""
    best: dict[int, tuple[int, ...]] = {}
    for pj in glob.glob(os.path.join(root, "*", "package.json")) + glob.glob(os.path.join(root, "*", "*", "package.json")):
        m = re.search(r'"packageManager"\s*:\s*"pnpm@(\d+)\.(\d+)\.(\d+)', _rd(Path(pj))[:200_000])
        if m:
            v = tuple(int(x) for x in m.groups())
            best[v[0]] = max(best.get(v[0], v), v)
    return {k: ".".join(map(str, v)) for k, v in best.items()}


def _orphan_bytes(store: str) -> int | None:
    """Bytes of store files nothing links to (st_nlink == 1): what `pnpm store prune` drops. None when unmeasurable."""
    total, n, t0 = 0, 0, _mono()
    stack = [os.path.join(store, "files")]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for en in it:
                    n += 1
                    if n > 1_500_000 or _mono() - t0 > 90:
                        return None
                    st = en.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(en.path)
                    elif st.st_nlink <= 1:
                        total += st.st_size
        except OSError:
            continue
    return total


def _purge_files(root: str, ents: list[cl._Ent], skip: Callable[[cl._Ent], bool] | None = None) -> int:
    """Delete scanned entries (symlink-safe, inode-checked); returns bytes really removed. Changed ones are skipped."""
    freed = 0
    for e in ents:
        if skip is not None and skip(e):
            continue
        try:
            cl._remove_entry(root, e)
            freed += e.size
        except (FileNotFoundError, cl._Changed):
            continue
    return freed


@task("tool_caches", klass="C1", tier="daily", title="Tool and app caches", timeout=3600, needs_root=True)
def tool_caches(ctx: Ctx) -> Result:
    """Regenerable caches of the owner's tools. Each cache is only touched when its tool is not running (/proc, read
    again right before every clean) and the cache is large enough to matter. npm `cache clean --force`, `pip cache
    purge`, `uv cache prune` (never --force), `pnpm store prune` per store version (only when files nothing links to
    exist, with a pnpm pinned by packageManager, never an unpinned download), freedesktop thumbnails > thumbs_days, Gradle
    daemon logs > gradle_log_days whose daemon pid is gone (never ~/.gradle/caches), browser cache files > browser_days
    while that browser is not running (never bookmarks, history, cookies, profiles). Tools run under `timeout`."""
    user = str(ctx.opt("user", "ohmz"))
    thumbs = _num(ctx.opt("thumbs_days", 30), 1, 3650)
    glogs = _num(ctx.opt("gradle_log_days", 14), 1, 3650)
    bdays = _num(ctx.opt("browser_days", 7), 1, 3650)
    long_min = _num(ctx.opt("long_lived_min", 30), 1, 100000)
    min_b = (_num(ctx.opt("min_mib", 64), 0, 1_000_000) or 0) * MIB
    if thumbs is None or glogs is None or bdays is None or long_min is None:
        return _skipped("bad thumbs_days/gradle_log_days/browser_days/long_lived_min config: nothing done")
    bad = _clock_problem(ctx)
    if bad:
        return _skipped(f"{bad}: nothing done")
    hw = cl._home_of(user)
    if not hw:
        return _skipped(f"user {user} unknown: nothing done")
    home = os.path.realpath(hw[0])
    e = _Env(user, hw[1], home, f"{home}/.local/bin:{home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin")
    if e.run_cmd(["true"]) is None:
        return _skipped(f"cannot run as {user} (not root): nothing done")
    procs = _cmdlines()
    if procs is None:
        return _skipped("/proc unreadable: nothing selected")
    ll = long_min * 60

    def busy_now(tool: str) -> str:
        """The tool's state AT THE MOMENT of the clean: a fresh process table, not the one read at task start."""
        ps = _cmdlines()
        return "process table unreadable" if ps is None else _pm_users(ps, ll).get(tool, "")

    users = _pm_users(procs, ll)
    acts, kept, proofs, unsized = _Acts(ctx), [], {}, []
    on = {k: ctx.opt(k, True) is not False for k in ("npm", "pip", "uv", "pnpm", "thumbnails", "gradle_logs", "browsers")}

    def tool_cache(tool: str, label: str, argv_dir: list[str], sub: str, clean: list[str], timeout: int, *, estimate=None):
        """One tool cache: located by the tool, size-gated, skipped while the tool runs, cleaned by the tool itself."""
        if not on[tool] or not e.which(argv_dir[0]):
            return
        if tool in users:
            kept.append((label, 0, f"{tool} running ({users[tool]})"))
            return
        d = _tool_dir(e, argv_dir)
        if d is None:
            kept.append((label, 0, "cache dir unknown or outside home"))
            return
        target = os.path.join(d, sub) if sub else d
        size = _dir_size(target) if os.path.isdir(target) else 0
        if size is None:
            kept.append((label, 0, "cache too large to measure"))
            return
        if size < min_b:
            return
        cmd = e.timed(clean, timeout)

        def run(target=target, size=size, cmd=cmd, tool=tool) -> int:
            b = busy_now(tool)
            if b:
                raise cl._Changed(f"{tool} started: {b}")
            cl._run_ok(cmd, timeout + 30)
            after = _dir_size(target) if os.path.isdir(target) else 0
            return max(size - after, 0) if after is not None else size

        # `estimate` is what dry-run claims it would free: exact for a purge, unknown (0) for prune verbs
        if estimate is not None:
            unsized.append(label)
        lab = f"{label} {human(size)}"
        proofs[lab[:60]] = f"{tool} not running; regenerable cache"
        acts.run(f"{tool}-cache", target, size if estimate is None else estimate, run, label=lab)

    tool_cache("npm", "npm cache", ["npm", "config", "get", "cache"], "_cacache", ["npm", "cache", "clean", "--force"], 300)
    tool_cache("pip", "pip cache", ["pip", "cache", "dir"], "", ["pip", "cache", "purge"], 120)
    tool_cache("uv", "uv cache", ["uv", "cache", "dir"], "", ["uv", "cache", "prune"], 900, estimate=0)
    if on["pnpm"]:
        _pnpm(ctx, e, users, acts, kept, proofs, min_b, str(ctx.opt("projects_root", f"{home}/StudioProjects")), busy_now)
    if on["thumbnails"]:
        _thumbnails(ctx, acts, kept, proofs, home, thumbs)
    if on["gradle_logs"]:
        _gradle_logs(ctx, acts, kept, proofs, home, glogs, procs)
    if on["browsers"]:
        _browsers(ctx, acts, kept, proofs, home, bdays, procs)
    res = _finish(acts, kept, proofs, "caches", {"kept": len(kept), "pm_running": len(users)})
    note = ", ".join(sorted({why.split(" (")[0] for _, _, why in kept}))[:60]       # e.g. "chrome running, firefox running"
    if not acts.rows:
        res.summary = _ascii("no tool cache needs cleaning" + (f" (kept: {note})" if note else ""))
        return res
    extra = (f"; size unknown: {', '.join(unsized)}" if unsized and not ctx.apply else "") + (f"; kept: {note}" if note else "")
    res.summary = _ascii(res.summary + extra)
    return res


def _pnpm(ctx: Ctx, e: _Env, users: dict, acts: _Acts, kept: list, proofs: dict, min_b: int, projects: str,
          busy_now: Callable[[str], str]) -> None:
    stores = ctx.opt("pnpm_stores", [f"{e.home}/.local/share/pnpm/store", f"{e.home}/.pnpm-store"])
    stores = [os.path.realpath(s) for s in stores if isinstance(s, str) and os.path.isabs(s)] if isinstance(stores, list) else []
    versions = _pnpm_versions(projects)
    for base in stores:
        if not cl._inside(base, [e.home]) or base == e.home or not os.path.isdir(base):
            continue
        for vdir in sorted(os.listdir(base)):
            m = re.fullmatch(r"v(\d+)", vdir)
            store = os.path.join(base, vdir)
            if not m or int(m.group(1)) < 10 or not os.path.isdir(os.path.join(store, "files")):
                continue                                  # older layouts need their own pnpm: leave them alone
            major, label = int(m.group(1)), f"pnpm store {vdir}"
            if "pnpm" in users:
                kept.append((label, 0, f"pnpm running ({users['pnpm']})"))
                continue
            orphans = _orphan_bytes(store)
            if orphans is None:
                kept.append((label, 0, "store too large to assess"))
                continue
            if orphans < min_b:
                continue
            ver, argv = versions.get(major), None
            if e.which("pnpm"):                           # a pnpm of the right major on PATH wins
                r = sh(e.run_cmd(["pnpm", "--version"]) or ["false"], timeout=60)
                argv = ["pnpm"] if r.returncode == 0 and r.stdout.strip().split(".")[0] == str(major) else None
            if argv is None:
                if ver is None:                           # never download and run an unpinned registry package as the user
                    kept.append((label, orphans, f"no pnpm {major}.x on PATH and none pinned by packageManager"))
                    continue
                if not e.which("npx"):
                    kept.append((label, orphans, "no pnpm and no npx to run it"))
                    continue
                argv = ["npx", "--yes", f"pnpm@{ver}"]
            cmd = e.timed([*argv, "store", "prune", "--store-dir", base], 900)

            def run(store=store, cmd=cmd) -> int:
                b = busy_now("pnpm")
                if b:
                    raise cl._Changed(f"pnpm started: {b}")
                before = _dir_size(store)
                cl._run_ok(cmd, 930)
                after = _dir_size(store)
                return max(before - after, 0) if before is not None and after is not None else 0

            lab = f"{label} ~{human(orphans)} unlinked"
            proofs[lab[:60]] = "pnpm not running; unlinked store files only"
            acts.run("pnpm-store-prune", store, orphans, run, label=lab)


def _thumbnails(ctx: Ctx, acts: _Acts, kept: list, proofs: dict, home: str, days: float) -> None:
    for top in (f"{home}/.cache/thumbnails", f"{home}/.thumbnails"):
        if os.path.realpath(top) != top or not os.path.isdir(top):
            continue
        for sub in sorted(os.listdir(top)):
            root = os.path.join(top, sub)
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                continue
            ents, _links, complete = cl._scan(root, ["**", "*.png"])
            if not complete:
                kept.append((f"thumbnails {sub}", 0, "scan limit reached"))
                continue
            sel, _recent = cl._select([x for x in ents if x.kind == "file"], ctx.now, days * DAY, None)
            if sel:
                lab = f"thumbnails {sub} {len(sel)} files"
                proofs[lab[:60]] = f"freedesktop thumbnails, mtime > {days:g} d; regenerated on demand"
                acts.run("thumbnail-purge", root, sum(x.size for x in sel), lambda r=root, s=sel: _purge_files(r, s), label=lab)


def _gradle_logs(ctx: Ctx, acts: _Acts, kept: list, proofs: dict, home: str, days: float, procs: Procs) -> None:
    root = f"{home}/.gradle/daemon"
    if os.path.realpath(root) != root or not os.path.isdir(root):
        return
    alive = {p[0] for p in procs}
    ents, _links, complete = cl._scan(root, ["*", "daemon-*.out.log"])
    if not complete:
        kept.append(("gradle daemon logs", 0, "scan limit reached"))
        return
    sel, _recent = cl._select([x for x in ents if x.kind == "file"], ctx.now, days * DAY, None)
    by_ver: dict[str, list[cl._Ent]] = {}
    for x in sel:
        m = re.fullmatch(r"daemon-(\d+)\.out\.log", os.path.basename(x.path))
        if m and int(m.group(1)) not in alive:          # the daemon that wrote it is gone: nothing appends to it
            by_ver.setdefault(os.path.dirname(x.path), []).append(x)

    def daemon_alive_now(ent: cl._Ent) -> bool:        # a pid that came back (or an unreadable table) keeps its log
        ps = _cmdlines()
        m = re.fullmatch(r"daemon-(\d+)\.out\.log", os.path.basename(ent.path))
        return ps is None or (m is not None and int(m.group(1)) in {p[0] for p in ps})

    for d, group in sorted(by_ver.items()):
        lab = f"gradle logs {os.path.basename(d)} {len(group)} files"
        proofs[lab[:60]] = f"daemon-N.out.log > {days:g} d, daemon pid not running"
        acts.run("gradle-log-purge", d, sum(x.size for x in group),
                 lambda g=group: _purge_files(root, g, daemon_alive_now), label=lab)


def _browsers(ctx: Ctx, acts: _Acts, kept: list, proofs: dict, home: str, days: float, procs: Procs) -> None:
    for name, (exes, globs) in sorted(_BROWSERS.items()):
        runs = [p for p in procs if p[1] in exes or (p[2] and os.path.basename(p[2][0]) in exes)]
        dirs = sorted({d for g in globs for d in glob.glob(os.path.join(home, g)) if os.path.realpath(d) == d})
        if runs and dirs:
            kept.append((f"{name} cache", 0, f"{name} running (pid {runs[0][0]})"))
            continue
        for d in dirs:
            ents, _links, complete = cl._scan(d, ["**", "*"])
            if not complete:
                kept.append((f"{name} cache", 0, "scan limit reached"))
                continue
            sel, _recent = cl._select([x for x in ents if x.kind == "file"], ctx.now, days * DAY, None)
            if sel:
                lab = f"{name} cache {os.path.basename(os.path.dirname(d))}/{os.path.basename(d)} {len(sel)} files"
                proofs[lab[:60]] = f"{name} not running; cache files > {days:g} d"

                def purge(r=d, s=sel, exes=exes, name=name) -> int:
                    now_running = _running_now(exes)            # the browser may have been started since the task began
                    if now_running:
                        raise cl._Changed(f"{name} started: {now_running}")
                    return _purge_files(r, s)

                acts.run("browser-cache-purge", d, sum(x.size for x in sel), purge, label=lab)


# =========================================================================== stale_build_output
_BUILD_NAMES = {"target", "dist", "build", ".next", ".turbo", "__pycache__", ".pytest_cache", ".mypy_cache"}
_GENERIC = {"dist", "build"}          # names every kind of project uses for hand-made things too: report-only unless apply_generic
_MANIFESTS = ("package.json", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
              "settings.gradle.kts", "pyproject.toml", "setup.py", "CMakeLists.txt", "Makefile", "go.mod", "pubspec.yaml")
_PRUNE = {".git", ".venv", "venv", "site-packages", ".tox", ".nox", ".gradle", ".idea"}
_RUN_FILES = re.compile(r"(?:(?:docker-)?compose.*\.ya?ml|Dockerfile.*|.*\.dockerfile|Procfile|ecosystem\.config\..*|.*\.service|.*\.timer"
                        r"|Caddyfile|nginx.*\.conf|(?:start|run|serve|deploy|up)[\w.-]*\.sh|[Mm]akefile|GNUmakefile|justfile)$")
_SRC_DIRS = {"src", "lib", "app", "pages", "source", "packages", "apps", "components", "server", "client"}
_SRC_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".py", ".rs", ".java", ".kt", ".go", ".c", ".cc", ".cpp", ".h")


def _find_build_dirs(root: str, depth: int, budget_s: float) -> tuple[list[str], bool]:
    """Directories named like build output below `root` (never inside node_modules*, .git, virtualenvs, other
    filesystems, or other build dirs). (paths, complete)."""
    found: list[str] = []
    try:
        dev = os.lstat(root).st_dev
    except OSError:
        return [], False
    stack, t0, n = [(root, 0)], _mono(), 0
    while stack:
        d, lvl = stack.pop()
        try:
            ents = list(os.scandir(d))
        except OSError:
            continue
        n += len(ents)
        if _mono() - t0 > budget_s or n > 3_000_000:
            return sorted(found), False
        if any(x.name == "pyvenv.cfg" for x in ents):
            continue                                             # a virtualenv: its insides are not build output
        for x in ents:
            try:
                if not x.is_dir(follow_symlinks=False) or x.stat(follow_symlinks=False).st_dev != dev:
                    continue
            except OSError:
                continue
            if x.name in _BUILD_NAMES:
                found.append(x.path)
            elif x.name not in _PRUNE and not x.name.startswith("node_modules") and lvl + 1 < depth:
                stack.append((x.path, lvl + 1))
    return sorted(found), True


def _owner(path: str, outer: str) -> tuple[str, set] | None:
    """(dir, manifest names) of the NEAREST ancestor of `path` (up to the project dir) that holds a build manifest."""
    p = os.path.dirname(path)
    while len(p) >= len(outer) and p.startswith(outer):
        try:
            have = {m for m in _MANIFESTS if os.path.exists(os.path.join(p, m))}
        except OSError:
            have = set()
        if have:
            return p, have
        p = os.path.dirname(p)
    return None


def _has_sources(d: str) -> bool:
    try:
        for i, e in enumerate(os.scandir(d)):
            if i > 400:
                break
            if (e.name in _SRC_DIRS and e.is_dir(follow_symlinks=False)) or e.name.endswith(_SRC_EXT):
                return True
    except OSError:
        pass
    return False


def _recipe_outputs(text: str, d: str, name: str) -> bool:
    """Does a package.json script (all of them joined in `text`) produce directory `name`? Named in a script, a bundler's
    default output, or tsc's outDir."""
    leaf = name.split("/")[-1]
    if re.search(rf"(?<![\w.-]){re.escape(leaf)}(?![\w-])", text):
        return True
    if name == ".vitepress/dist":
        return bool(re.search(r"\bvitepress\b", text))
    if name == "dist" and re.search(r"\b(vite|webpack|rollup|tsup|parcel|microbundle|vue-cli-service|esbuild|unbuild|tsdown|ng build)\b", text):
        return True
    if name == "build" and re.search(r"\b(react-scripts|craco|react-app-rewired)\b", text):
        return True
    if re.search(r"\btsc\b", text):
        for cfg in glob.glob(os.path.join(d, "tsconfig*.json")):
            m = re.search(r'"outDir"\s*:\s*"(?:\./)?([^"/]+)', _rd(Path(cfg))[:100_000])
            if m and m.group(1) == name:
                return True
    return False


def _owner_builds(path: str, outer: str, tree: _Tree) -> str:
    """'' when the nearest manifest owner has a build recipe whose output is this directory and its sources exist; else
    why not (a hand-made or hand-patched dist/ cannot be tied to a recipe)."""
    name = ".vitepress/dist" if os.path.basename(os.path.dirname(path)) == ".vitepress" and os.path.basename(path) == "dist" \
        else os.path.basename(path)
    own = _owner(path, outer)
    if own is None:
        return "no build manifest above it"
    d, have = own
    if not _has_sources(d):
        return "no sources next to the build recipe"
    if "package.json" in have:
        try:
            pj = json.loads(_rd(Path(d, "package.json"))[:2_000_000])
            scripts = pj.get("scripts") if isinstance(pj, dict) else None
        except ValueError:
            scripts = None
        if isinstance(scripts, dict) and isinstance(scripts.get("build"), str) \
                and _recipe_outputs(" ".join(str(v) for v in scripts.values()), d, name):
            return ""
    if have & {"build.gradle", "build.gradle.kts"} and name == "build":
        return ""
    if "CMakeLists.txt" in have and name == "build" and "CMakeCache.txt" in tree.top:
        return ""
    if have & {"setup.py", "pyproject.toml"} and ((name == "build" and any(t.startswith(("lib", "bdist", "temp")) for t in tree.top))
                                                  or (name == "dist" and tree.top and all(t.endswith((".whl", ".tar.gz", ".zip")) for t in tree.top))):
        return ""
    if "Makefile" in have and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", _rd(Path(d, "Makefile"))[:500_000]):
        return ""
    return "no build recipe of its project produces it"


def _signature_problem(path: str, outer: str, tree: _Tree) -> str:
    """'' when the directory carries the signature of the tool that makes it (so it is output, not somebody's work)."""
    name = os.path.basename(path)
    top = tree.top
    if name == "target":
        own = _owner(path, outer)
        mvn = own is not None and "pom.xml" in own[1] and bool(top & {"classes", "maven-status", "maven-archiver", "test-classes", "surefire-reports"})
        return "" if "CACHEDIR.TAG" in top or mvn else "no cargo CACHEDIR.TAG or maven layout"
    if name == ".next":
        return "" if top & {"BUILD_ID", "build-manifest.json"} else "no BUILD_ID/build-manifest.json"
    if name == ".turbo":
        return "" if all(t in ("cache", "daemon", "cookies") or re.fullmatch(r"turbo-.*\.log|.*\.log", t) for t in top) else "unexpected content"
    if name == "__pycache__":
        return "" if not tree.non_pyc and not tree.subdirs else "holds more than .pyc files"
    if name in (".pytest_cache", ".mypy_cache"):
        return "" if "CACHEDIR.TAG" in top else "no CACHEDIR.TAG"
    return ""                                               # dist/build: tied to a recipe by _owner_builds


def _pkg_runs_output(path: str, outer: str) -> str:
    """'' unless the nearest package.json runs something FROM this directory (main, bin, module, exports, start/serve)."""
    own = _owner(path, outer)
    if own is None or "package.json" not in own[1]:
        return ""
    try:
        pj = json.loads(_rd(Path(own[0], "package.json"))[:2_000_000])
    except ValueError:
        return ""
    if not isinstance(pj, dict):
        return ""
    rel = os.path.relpath(path, own[0])
    scripts = pj.get("scripts") if isinstance(pj.get("scripts"), dict) else {}
    fields = [pj.get(k) for k in ("main", "bin", "module", "exports")] + [scripts.get(k) for k in ("start", "serve", "prestart", "preview")]
    text = json.dumps([f for f in fields if f])
    return f"package.json runs from {rel}" if re.search(rf"(?<![\w.-])(?:\./)?{re.escape(rel)}(?![\w-])", text) else ""


def _run_files(path: str, outer: str) -> list[tuple[str, str]]:
    """Run-type files (compose, Dockerfile, units, nginx, start scripts) next to the build dir or in any directory up to
    the project dir: the places that say `./dist` or `COPY dist` without naming the project."""
    out, p = [], os.path.dirname(path)
    while len(p) >= len(outer) and p.startswith(outer):
        try:
            out += [(e.path, "proj") for e in os.scandir(p) if _RUN_FILES.match(e.name) and e.is_file()]
        except OSError:
            pass
        p = os.path.dirname(p)
    return out


@task("stale_build_output", klass="C1", tier="weekly", title="Stale build output", timeout=3600, needs_root=True)
def stale_build_output(ctx: Ctx) -> Result:
    """Remove build output (target, dist, build, .next, .turbo, __pycache__, .pytest_cache, .mypy_cache, .vitepress/dist)
    of projects nobody touched for `idle_days`. A directory is selected ONLY when ALL hold: git-ignored; nothing in it
    tracked or staged; every repo around it, its linked worktrees and its working files (ignored ones, ctime included) are
    idle for idle_days; the output itself is older than output_days; it carries its tool's signature and none of: backup
    or patch files, .env, databases, keys, signed apks, files of another owner; dist/build also need the nearest build
    recipe to produce them and `apply_generic = true`; no container (running or stopped) bind-mounts the project; no
    process has its cwd, an open or mapped file or an argv path (absolute or relative to its cwd) in the project; no unit,
    cron entry, launcher, rc file, nginx/caddy config, run file or symlink points at it; not under a never-touch path; no
    mount point inside. Probes that fail keep the directory."""
    root = os.path.realpath(str(ctx.opt("projects_root", f"{HOME_DEFAULT}/StudioProjects")))
    idle = _num(ctx.opt("idle_days", 90), 7, 3650)
    out_days = _num(ctx.opt("output_days", 30), 0, 3650)
    depth = _num(ctx.opt("max_depth", 7), 1, 20)
    budget = _num(ctx.opt("scan_budget_s", 900), 10, 7200)
    if None in (idle, out_days, depth, budget):
        return _skipped("bad idle_days/output_days/max_depth/scan_budget_s config: nothing done")
    if not os.path.isdir(root) or root == "/":
        return _skipped("projects_root missing: nothing done")
    bad = _clock_problem(ctx)
    if bad:
        return _skipped(f"{bad}: nothing done")
    for gate in ("docker_build", "gradle"):
        busy, why = cl._busy(gate)
        if busy:
            return _skipped(f"{gate} active, cleanup deferred ({why})")
    bad = _snapshot_problem()
    if bad:
        return _skipped(f"{bad}: nothing selected")
    apply_generic = ctx.opt("apply_generic", False) is True
    home = os.path.realpath(str(ctx.opt("home", HOME_DEFAULT)))
    dirs, complete = _find_build_dirs(root, int(depth), budget)
    acts, kept, proofs, reasons = _Acts(ctx), [], {}, {}
    memo: dict = {}
    pending: list[tuple] = []

    def keep(label: str, size: int, why: str) -> None:
        kept.append((label, size, why))
        key = re.sub(r"\W+", "_", why.split(" (")[0].split(":")[0])[:24]
        reasons[key] = reasons.get(key, 0) + 1

    for path in dirs:
        label = os.path.relpath(path, root)
        why = _never(ctx, path)
        if why:
            keep(label, 0, why)
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or os.path.realpath(path) != path:
            keep(label, 0, "symlink in path")
            continue
        if _odd(path):
            keep(label, 0, "odd path name: git would not answer for it")
            continue
        gs = inuse.git_state(path, ctx.now)
        if not gs.known:
            keep(label, 0, gs.why)
            continue
        if not gs.in_repo or not _below(gs.repo, root):
            keep(label, 0, "not inside a git project")
            continue
        outer = os.path.join(root, os.path.relpath(path, root).split(os.sep)[0])
        why = _never(ctx, gs.repo) or _never(ctx, outer)
        if why:
            keep(label, 0, f"project {why}")
            continue
        tree = _inspect(path, owner=_owner_uid(outer), deny=True, limit=1_000_000, budget_s=20.0)
        if not tree.complete:
            keep(label, 0, "size unmeasurable")
            continue
        size = tree.size
        if gs.tracked:
            keep(label, size, "tracked or staged in git")
            continue
        if not gs.ignored:
            keep(label, size, "not git-ignored")
            continue
        why, newest_act, outer = _project_activity(ctx, root, path, gs, memo)
        if why:
            keep(label, size, why)
            continue
        idle_d = max(ctx.now - newest_act, 0) / DAY if newest_act > 0 else float("inf")
        if idle_d < idle:
            keep(label, size, f"project active ({idle_d:.0f}d ago)")
            continue
        if out_days and ctx.now - tree.newest < out_days * DAY:
            keep(label, size, f"output touched {_age(ctx.now, tree.newest)} ago")
            continue
        if tree.deny:
            why, ex = next(iter(tree.deny.items()))
            keep(label, size, f"manual: holds {why} ({ex[:30]})")
            continue
        why = _signature_problem(path, outer, tree)
        if not why and os.path.basename(path) in _GENERIC:
            why = _owner_builds(path, outer, tree)
        if why:
            keep(label, size, f"manual: not provably tool output ({why})")
            continue
        why = _pkg_runs_output(path, outer)
        if why:
            keep(label, size, f"in use: {why}")
            continue
        if not _single_device(path):
            keep(label, size, "contains a mount point or unreadable part")
            continue
        held = _held(outer)
        if not held.unused:
            keep(label, size, f"in use: {held.why}")
            continue
        if os.path.basename(path) in _GENERIC and not apply_generic:
            keep(label, size, "generic dist/build name: report-only (apply_generic = false)")
            continue
        pending.append((size, path, outer, label, st, _ascii(f"{gs.why}; {held.why}", 300)))
    # in use by something that is not running right now: units, cron, launchers, rc files, nginx, run files, symlinks
    chosen = []
    if pending:
        targets = {p: [(p, "exact", "any"), (o, "scope", "any")] for _, p, o, _, _, _ in pending}
        run_files = sorted({f for _, p, o, _, _, _ in pending for f in _run_files(p, o)})
        refs = _ref_check(ctx, targets, home, run_files, projects=False)
        links, links_why = _links_into({p: p for _, p, _, _, _, _ in pending}, _link_roots(ctx, home))
        for size, path, outer, label, st, proof in pending:
            if not refs[path].unused:
                keep(label, size, refs[path].why)
            elif path in links:
                keep(label, size, f"in use: symlink {links[path][-60:]}")
            elif links_why:
                keep(label, size, f"symlink scan incomplete ({links_why})")
            else:
                chosen.append((size, path, outer, label, st, _ascii(f"{proof}; {refs[path].why}", 300)))
    recheck = _Recheck()
    for size, path, outer, label, st, proof in sorted(chosen, key=lambda c: (-c[0], c[1])):
        proofs[label[:60]] = proof
        acts.run("build-output-rm", path, size, lambda p=path, o=outer, s=st: _rm_build_dir(root, p, o, s, recheck), label=label)
    res = _finish(acts, kept, proofs, "build dirs", {"found": len(dirs), "scan_complete": complete,
                                                     **{f"kept_{k}": v for k, v in sorted(reasons.items())}})
    if not complete:
        res.summary = _ascii(res.summary + "; scan incomplete")
    elif not acts.rows:
        res.summary = _ascii(f"no stale build output ({len(dirs)} dirs checked, {len(kept)} kept)")
    return res


def _owner_uid(path: str) -> int | None:
    try:
        return os.lstat(path).st_uid
    except OSError:
        return None


def _rm_build_dir(root: str, path: str, outer: str, st: os.stat_result, recheck: _Recheck) -> None:
    """Final guard right before the delete: same directory (inode, mtime), through a no-symlink fd chain, nothing holds the
    project now, one filesystem. The delete itself is relative to the opened parent directory."""
    def before(fdpath: str) -> None:
        held = recheck.held(outer)
        if not held.unused:
            raise cl._Changed(f"in use now: {held.why}")
        if not _single_device(fdpath):
            raise cl._Changed("a mount point appeared inside")

    _rm_anchored(root, path, st, before)


# =========================================================================== shared C2 plumbing
def _c2_gate(ctx: Ctx, res: Result, h: str, has_items: bool) -> Result | None:
    """Apply preconditions shared by both C2 tasks: a Result to hand back, or None = go ahead."""
    if not (ctx.apply and has_items):
        return res
    if not cl._approved(ctx.name, h, float(_num(ctx.opt("approval_ttl_hours", 48), 1) or 48)):
        res.summary = _ascii(f"{res.summary}; awaiting approval: homelab-maint approve {ctx.name} {h}")
        return res
    if _euid() != 0:
        audit(ctx.name, "c2-apply", h, 0, "refused-not-root")
        res.status = "warn"
        res.summary = _ascii(f"{res.summary}; apply refused: needs root (complete process table)")
        return res
    busy, why = cl._busy("backup")
    if busy:
        res.summary = _ascii(f"{res.summary}; apply deferred: backup running ({why})")
        return res
    return None


def _c2_done(ctx: Ctx, acts: _Acts, res: Result, noun: str, extra: dict) -> Result:
    out = acts.result(noun, extra)
    out.plan, out.alert = res.plan, False
    if acts.n["done"]:
        for p in (core.STATE_DIR / "approvals").glob(f"{ctx.name}.*"):
            try:
                p.unlink()                                     # approvals are single use
            except OSError:
                pass
    return out


def _slug(path: str, home: str) -> str:
    rel = path[len(home) + 1:] if path.startswith(home + "/") else path
    return re.sub(r"[^A-Za-z0-9._-]+", "_", "-".join(p.lstrip(".") or "_" for p in rel.strip("/").split("/")))[:80]


# =========================================================================== unused_venvs
_VENV_PRUNE = {".cache", ".local", ".git", "node_modules", "snap", ".npm", ".nvm", ".cargo", ".rustup", ".gradle", ".var",
               ".mozilla", ".vscode-server", ".cursor-server", "Android", "site-packages"}
_VENV_TOP_OK = {"bin", "include", "lib", "lib64", "share", "pyvenv.cfg", ".gitignore", "CACHEDIR.TAG"}


class _V(NamedTuple):
    why: str                 # '' only when every proof of "unused" holds
    size: int = 0
    newest: float = 0.0
    proof: str = ""
    manual: str = ""         # non-empty: unused, but a person must look first (never applied unless allow_manual_check_items)


def _find_venvs(root: str, depth: int) -> list[str]:
    """Directories holding pyvenv.cfg, at most `depth` levels below root (a venv is never searched inside)."""
    found, stack = [], [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            ents = list(os.scandir(d))
        except OSError:
            continue
        if lvl and any(x.name == "pyvenv.cfg" for x in ents):
            found.append(d)
            continue
        for x in ents:
            try:
                if x.is_dir(follow_symlinks=False) and x.name not in _VENV_PRUNE and lvl + 1 <= depth:
                    stack.append((x.path, lvl + 1))
            except OSError:
                continue
    return sorted(found)


def _read_noatime(path: str, limit: int = 8 * MIB) -> str | None:
    """Read a file inside a candidate WITHOUT touching its atime (O_NOATIME; owner or root only): reading it normally
    would stamp 'used now' on the very venv we are judging. None when that is not possible."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOATIME", 0) | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _venv_site_dirs(v: str) -> list[str]:
    return sorted({os.path.join(v, d, p, "site-packages") for d in ("lib", "lib64") for p in
                   (os.listdir(os.path.join(v, d)) if os.path.isdir(os.path.join(v, d)) and not os.path.islink(os.path.join(v, d)) else [])
                   if p.startswith("python")})


def _venv_records(v: str) -> dict | None:
    """{absolute path: size or None} of every file some installed package's RECORD names; None when a RECORD cannot be read."""
    rec: dict = {}
    try:
        sites = _venv_site_dirs(v)
        for sp in sites:
            for di in glob.glob(os.path.join(sp, "*.dist-info", "RECORD")):
                txt = _read_noatime(di)
                if txt is None:
                    return None
                for row in csv.reader(io.StringIO(txt)):
                    if row and row[0]:
                        rec[os.path.normpath(row[0] if row[0].startswith("/") else os.path.join(sp, row[0]))] = \
                            int(row[2]) if len(row) > 2 and row[2].isdigit() else None
    except OSError:
        return None
    return rec


def _venv_manual(v: str, tree: _Tree, records_ok: bool) -> str:
    """Why a person must look before this venv is removed: it holds what `pip freeze` cannot rebuild."""
    try:
        names = os.listdir(v)
    except OSError:
        return "unreadable"
    odd = sorted(n for n in names if n not in _VENV_TOP_OK)
    if odd:
        return "holds " + ",".join(odd[:4]) + " (not rebuildable from a package list)"
    for sp in _venv_site_dirs(v):
        for n in (".git", "src"):
            if os.path.lexists(os.path.join(sp, n)):
                return f"{n} inside site-packages"
    if not records_ok:
        return "package records unreadable: cannot tell what is hand-made"
    if tree.unlisted:
        return f"{len(tree.unlisted)}+ files no package owns (e.g. {tree.unlisted[0][:40]})"
    if tree.changed:
        return f"{len(tree.changed)}+ files differ from their package (hand-patched? e.g. {tree.changed[0][:40]})"
    return ""


def _assess_venvs(ctx: Ctx, root: str, home: str, paths: list[str]) -> dict[str, _V]:
    """{venv: _V}; `why` is '' only when every proof of 'unused' holds (`manual` may still ask for a human)."""
    out: dict[str, _V] = {}
    survivors: list[tuple[str, os.stat_result, str]] = []
    active = _num(ctx.opt("active_days", 90), 1, 3650) or 90
    idle = _num(ctx.opt("idle_days", 60), 1, 3650) or 60
    for v in paths:
        why = _never(ctx, v)
        if why:
            out[v] = _V(why)
            continue
        try:
            st = os.lstat(v)
        except OSError:
            out[v] = _V("gone")
            continue
        if not stat.S_ISDIR(st.st_mode) or os.path.realpath(v) != v:
            out[v] = _V("symlink in path")
            continue
        gs = inuse.git_state(v, ctx.now)                       # a project-local venv of an ACTIVE project stays
        if not gs.known:
            out[v] = _V(f"project activity unknown ({gs.why})")
            continue
        if gs.in_repo and _below(gs.repo, root) and gs.idle_days(ctx.now) < active:
            out[v] = _V(f"active project ({gs.idle_days(ctx.now):.0f}d ago)")
            continue
        parent = os.path.dirname(v)
        if parent not in (root, home, "/") and cl._inside(parent, [root]):
            newest, ok = _project_newest(parent, exclude=v)      # git or not: the files next to the venv say whether it is worked on
            if not ok:
                out[v] = _V("project activity unmeasurable")
                continue
            if ctx.now - newest < idle * DAY:
                out[v] = _V(f"project files changed {_age(ctx.now, newest)} ago")
                continue
        held = _venv_held(v, root, home)
        if not held.unused:
            out[v] = _V(f"in use: {held.why}")
            continue
        survivors.append((v, st, held.why))
    if not survivors:
        return out
    targets = {}
    for v, _, _ in survivors:
        par = os.path.dirname(v)
        proj = par not in (root, home, "/") and cl._inside(par, [root])
        targets[v] = [(v, "exact", "venv", par if proj else "")] + ([(par, "scope", "any")] if proj else [])
    refs = _ref_check(ctx, targets, home)
    links, links_why = _links_into({v: v for v, _, _ in survivors}, _link_roots(ctx, home))
    for v, st, held_why in survivors:
        pr = refs[v]
        if not pr.unused:
            out[v] = _V(pr.why)
            continue
        if v in links:
            out[v] = _V(f"in use: symlink {links[v][-60:]}")
            continue
        if links_why:
            out[v] = _V(f"symlink scan incomplete ({links_why})")
            continue
        records = _venv_records(v)
        tree = _inspect(v, recorded=records if records is not None else {})
        if not tree.complete or not _single_device(v):
            out[v] = _V("size unmeasurable or mount point inside")
            continue
        kept_atime = _atime_kept(v)
        last = max(tree.newest, tree.atime if kept_atime else 0.0)
        if ctx.now - last < idle * DAY:
            out[v] = _V(f"{'read' if kept_atime and tree.atime > tree.newest else 'modified'} {_age(ctx.now, last)} ago", tree.size, last)
            continue
        manual = _venv_manual(v, tree, records is not None)
        if not kept_atime:
            manual = manual or "atime not kept on this mount (noatime): last use cannot be known"
        read = f"last read {_day(tree.atime)}" if kept_atime else "atime unknown"
        # the plan carries the date of the last CHANGE (stable); a mere read must not alter the plan hash
        out[v] = _V("", tree.size, tree.newest, _ascii(f"idle {_age(ctx.now, last)} ({read}); {held_why}; {pr.why}", 300), manual)
    return out


@task("unused_venvs", klass="C2", tier="weekly", title="Unused Python virtualenvs", timeout=3600, needs_root=True)
def unused_venvs(ctx: Ctx) -> Result:
    """Plan (never auto-delete) for virtualenvs under `roots` (depth <= 3) that nothing uses: no process maps/opens/runs
    it, has it activated, or works in its project directory (also via relative paths), no container mounts it, no unit /
    cron / launcher / rc file / ai-stack script / project config names it (or a script that cds into its project), no
    symlink points into it, nothing in it was modified, changed or READ for idle_days (atime, when the mount keeps it),
    the project's files are idle too, and it is not the venv of an active git project. A venv that holds what a package
    list cannot rebuild (src/ checkouts, custom scripts, .git, files no package owns, noatime mount) is planned as
    needs_manual_check and refused on apply unless allow_manual_check_items. An approved apply writes `pip freeze`,
    pyvenv.cfg, a bin listing and install metadata to the cold archive (read back) BEFORE the venv is removed."""
    roots = ctx.opt("roots", [HOME_DEFAULT])
    depth = _num(ctx.opt("max_depth", 3), 1, 6)
    arch = str(ctx.opt("archive_dir", f"{COLD_ARCHIVE}/dev-leftovers"))
    if not isinstance(roots, list) or depth is None or not os.path.isabs(arch):
        return _skipped("bad roots/max_depth/archive_dir config: nothing done")
    bad = _clock_problem(ctx)
    if bad:
        return _skipped(f"{bad}: nothing done")
    home = os.path.realpath(str(ctx.opt("home", HOME_DEFAULT)))        # `~/x` and `$HOME/x` spellings are searched for
    boot = _snapshot_problem()
    items, plan_items = [], []
    for r in sorted({os.path.realpath(x) for x in roots if isinstance(x, str) and os.path.isabs(x)}):
        if not os.path.isdir(r) or r == "/":
            continue
        found = _find_venvs(r, int(depth))
        verdicts = _assess_venvs(ctx, r, home, found) if found and not boot else {}
        for v in found:
            label = os.path.relpath(v, r)
            vd = verdicts.get(v, _V(boot))
            if vd.why:
                items.append({"name": label[:60], "size": human(vd.size) if vd.size else "-", "state": "kept", "proof": vd.why[:150]})
                continue
            stem = f"venv-{_slug(v, home)}"
            q = shlex.quote
            cmd = (f"{q(v + '/bin/python')} -m pip freeze > {q(arch + '/' + stem)}-requirements-$(date +%F).txt && "
                   f"cp {q(v + '/pyvenv.cfg')} {q(arch + '/' + stem)}-pyvenv-$(date +%F).cfg && rm -rf -- {q(v)}")
            plan_items.append({"name": label, "path": v, "root": r, "stem": stem, "bytes": vd.size // MIB * MIB,   # coarse: stable hash
                               "mtime": _day(vd.newest), "archive_dir": arch, "command": cmd, "needs_manual_check": bool(vd.manual),
                               "why": vd.manual or "unused", "archive_files": [f"{stem}-{k}-<date>.{x}" for k, x in
                                                                               (("requirements", "txt"), ("pyvenv", "cfg"), ("bin-listing", "txt"), ("meta", "txt"))]})
            items.append({"name": label[:60], "size": human(vd.size), "state": "manual check" if vd.manual else "unused",
                          "proof": _ascii(vd.manual or vd.proof, 300)})
    plan_items.sort(key=lambda i: i["path"])
    plan = {"items": plan_items, "total_bytes": sum(i["bytes"] for i in plan_items)}
    h = plan_hash(plan)
    metrics = {"mode": "apply" if ctx.apply else "report", "venvs": len(items), "unused": len(plan_items),
               "manual": sum(1 for i in plan_items if i["needs_manual_check"]), "total_h": human(plan["total_bytes"]), "plan_hash": h}
    summary = (f"{len(plan_items)} unused venvs, {human(plan['total_bytes'])}; plan {h}" if plan_items
               else f"no unused virtualenvs ({len(items)} checked)") + (f"; {boot}" if boot else "")
    res = Result("info" if plan_items else "ok", _ascii(summary), metrics,
                 sorted(items, key=lambda i: (i["state"] == "kept", i["state"] == "manual check", i["name"]))[:12], plan=plan, alert=False)
    go = _c2_gate(ctx, res, h, bool(plan_items))
    if go is not None:
        return go
    acts = _Acts(ctx)
    allow_manual = ctx.opt("allow_manual_check_items") is True
    for it in plan_items:                                  # re-prove every item from scratch right before acting
        if it["needs_manual_check"] and not allow_manual:
            acts._note("refused", f"{it['name'][:34]} (manual check)", it["bytes"])
            continue
        vd = None if _snapshot_problem() else _assess_venvs(ctx, it["root"], home, [it["path"]])[it["path"]]
        why = "process table unusable or docker mounts unknown" if vd is None else vd.why
        if not why and vd.manual and not allow_manual:
            why = f"manual check: {vd.manual}"
        if why:
            acts._note("refused", f"{it['name'][:30]}: {why}"[:60], it["bytes"])
            continue
        acts.run("venv-archive-remove", it["path"], it["bytes"], lambda it=it: _archive_venv(ctx, it, home),
                 protect=(it["name"],), label=it["name"])
    return _c2_done(ctx, acts, res, "venvs", {"plan_hash": h})


def _freeze(path: str) -> tuple[list[str], str]:
    """(requirement lines, how). `pip freeze` run as the venv's owner; without a working pip, the dist-info names."""
    py = os.path.join(path, "bin", "python")
    try:
        uid = os.lstat(path).st_uid
    except OSError:
        uid = 0
    # the venv's own python runs as its owner, never as root (sitecustomize / .pth files are code)
    cmd = _as_owner(uid, [py, "-m", "pip", "freeze", "--disable-pip-version-check"]) \
        if os.access(py, os.X_OK) and not (uid == 0 and _euid() == 0) else None
    r = sh(cmd, timeout=180) if cmd else None
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return sorted(r.stdout.strip().splitlines()), "pip freeze"
    lines = []
    for di in glob.glob(os.path.join(path, "lib", "python3*", "site-packages", "*.dist-info")):
        name, _, ver = os.path.basename(di)[:-len(".dist-info")].rpartition("-")
        if name and ver:
            lines.append(f"{name}=={ver}")
    return sorted(lines), "dist-info names (pip freeze unavailable)"


def _venv_meta(path: str) -> str:
    """What a bare requirements list loses: where non-index packages came from (direct_url.json), index settings of the
    owner's pip, and uv's own freeze. Informational: every part may be 'n/a'."""
    lines = []
    for sp in _venv_site_dirs(path):
        for du in sorted(glob.glob(os.path.join(sp, "*.dist-info", "direct_url.json"))):
            txt = _read_noatime(du)
            try:
                lines.append(f"direct_url {os.path.basename(os.path.dirname(du))[:-10]}: {json.loads(txt or '{}').get('url', '?')}")
            except ValueError:
                lines.append(f"direct_url {os.path.basename(os.path.dirname(du))[:-10]}: unreadable")
    try:
        uid = os.lstat(path).st_uid
    except OSError:
        uid = 0
    py = os.path.join(path, "bin", "python")
    for title, argv in (("pip config", [py, "-m", "pip", "config", "list"]),
                        ("uv pip freeze", ["uv", "pip", "freeze", "--python", py])):
        cmd = _as_owner(uid, argv) if not (uid == 0 and _euid() == 0) else None
        r = sh(cmd, timeout=120) if cmd else None
        lines.append(f"[{title}]\n" + (r.stdout.strip() if r is not None and r.returncode == 0 and r.stdout.strip() else "n/a"))
    lines.append("note: local versions like +cu121 need their index URL (see pip config / the project's docs)")
    return "\n".join(lines) + "\n"


def _write_verified(dest: str, text: str, uid: int, gid: int) -> None:
    """Create `dest` with `text`, flush it, read it back. An existing identical file is fine, a different one is refused.
    Done relative to a no-symlink directory fd (the archive tree is writable by the data's owner, root writes here)."""
    dfd = _open_dir_nofollow(os.path.dirname(dest))
    name = os.path.basename(dest)

    def read(n: str) -> str:
        fd = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        with os.fdopen(fd, "rb") as f:
            return f.read().decode("utf-8", "replace")

    try:
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
            exists = True
        except FileNotFoundError:
            exists = False
        if exists:
            if read(name) != text:
                raise RuntimeError(f"archive file exists with different content: {name}")
            return
        tmp = name + ".part"
        try:
            os.unlink(tmp, dir_fd=dfd)                     # left by a crashed run
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            try:
                os.fchown(f.fileno(), uid, gid)            # on the fd: no path, no symlink to follow
            except OSError:
                pass
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        if read(name) != text:
            raise RuntimeError("archive file read-back mismatch")
    finally:
        os.close(dfd)


def _archive_venv(ctx: Ctx, it: dict, home: str) -> None:
    """Write the rebuild information to the cold archive and verify it, then remove the venv. Raises before removing."""
    path, arch, root = it["path"], it["archive_dir"], it["root"]
    problem = cl._archive_target_problem(arch, path)
    if problem:
        raise RuntimeError(f"archive target refused: {problem}")
    if ctx.is_protected(arch):
        raise RuntimeError("archive dir protected")
    st = os.lstat(path)
    stem, date = it["stem"], _day(ctx.now)
    reqs, how = _freeze(path)
    if not reqs:
        raise RuntimeError("no package list could be produced: refusing to remove")
    try:
        with os.scandir(os.path.join(path, "bin")) as it_:
            listing = sorted(en.name + (f" -> {os.readlink(en.path)}" if en.is_symlink() else "") for en in it_)
    except OSError:
        listing = []
    files = {f"{stem}-requirements-{date}.txt": "\n".join([f"# {how}; venv {path}", *reqs]) + "\n",
             f"{stem}-pyvenv-{date}.cfg": _rd(Path(path, "pyvenv.cfg")),
             f"{stem}-bin-listing-{date}.txt": "\n".join(listing) + "\n",
             f"{stem}-meta-{date}.txt": _venv_meta(path)}
    for name, text in files.items():
        _write_verified(os.path.join(arch, name), text, st.st_uid, st.st_gid)

    def before(fdpath: str) -> None:                       # the archive took a while: prove it is still unused
        bad = _snapshot_problem()
        held = None if bad else _venv_held(path, root, home)
        if bad or not held.unused:
            raise cl._Changed(f"in use after archiving: {bad or held.why}")
        if not _single_device(fdpath):
            raise RuntimeError("a mount point inside the venv: refusing to remove")

    _rm_anchored(root, path, st, before)


# =========================================================================== large_cold_files
class _Stop(Exception):
    """Scan budget exhausted."""


def _cold_scan(ctx: Ctx, root: str, min_b: int, cutoff: float, budget_s: float, limit: int,
               use_atime: bool = False) -> tuple[list[dict], bool]:
    """Maximal files/dirs of >= min_b bytes whose NEWEST change is <= cutoff: newest of mtime, ctime and (use_atime: the
    mount keeps atime) the file's last read. Post-order: a directory replaces the candidates found below it when the whole
    tree qualifies. Never a candidate: anything inside .git, node_modules*, site-packages or a virtualenv (only the whole
    tree can be), a directory with a never-touch, unreadable or foreign-filesystem part inside it. (candidates, complete)."""
    cands: list[dict] = []
    root_st = os.lstat(root)
    dev, t0, n = root_st.st_dev, _mono(), [0]

    def walk(path: str, depth: int, blocked: bool, own: float) -> tuple[int, float, bool, bool]:
        """(bytes, newest incl. the directory's own, has a .git inside, tainted)"""
        n[0] += 1
        if n[0] > limit or (n[0] % 4096 == 0 and _mono() - t0 > budget_s):
            raise _Stop()
        mark = len(cands)
        size, newest, git, taint = 0, own, False, depth > 200
        try:
            ents = [] if taint else list(os.scandir(path))
        except OSError:
            return 0, 0.0, False, True
        blocked_kids = blocked or any(x.name == "pyvenv.cfg" for x in ents)
        for x in ents:
            n[0] += 1
            try:
                st = x.stat(follow_symlinks=False)
            except OSError:
                taint = True
                continue
            newest = max(newest, _stamp(st))
            if x.name == ".git":
                git = True
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev or _never(ctx, x.path):
                    taint = True                            # a mount or a never-touch part: the parent may not go
                    continue
                kid_blocked = blocked_kids or x.name in (".git", "site-packages") or x.name.startswith("node_modules")
                s, nw, g, t = walk(x.path, depth + 1, kid_blocked, _stamp(st))
                size, newest, git, taint = size + s, max(newest, nw), git or g, taint or t
            else:
                size += st.st_size
                fnew = max(_stamp(st), st.st_atime if use_atime and stat.S_ISREG(st.st_mode) else 0.0)
                newest = max(newest, fnew)
                if not blocked_kids and st.st_size >= min_b and fnew <= cutoff and stat.S_ISREG(st.st_mode):
                    cands.append({"path": x.path, "is_dir": False, "bytes": st.st_size, "newest": fnew, "git": False})
        if depth and not blocked and not taint and size >= min_b and newest <= cutoff:
            del cands[mark:]                                # the whole tree is cold: it replaces what was found inside
            cands.append({"path": path, "is_dir": True, "bytes": size, "newest": newest, "git": git})
        return size, newest, git, taint

    try:
        walk(root, 0, False, _stamp(root_st))
    except _Stop:
        return cands, False
    return cands, True


def _newest_change(p: str, is_dir: bool, use_atime: bool, limit: int = 6_000_000, budget_s: float = 600.0) -> tuple[float, bool]:
    """(newest mtime/ctime/(atime of files), complete) of an item, same measure as _cold_scan."""
    if not is_dir:
        try:
            st = os.lstat(p)
        except OSError:
            return 0.0, False
        return max(_stamp(st), st.st_atime if use_atime else 0.0), True
    t = _inspect(p, limit=limit, budget_s=budget_s)
    return max(t.newest, t.atime if use_atime else 0.0), t.complete


@task("large_cold_files", klass="C2", tier="weekly", title="Large cold files", timeout=7200, needs_root=True)
def large_cold_files(ctx: Ctx) -> Result:
    """Plan (never auto) for files/dirs larger than `min_gib` whose newest change (mtime, ctime, and the last READ where
    the mount keeps atime) is older than `cold_days` under `roots` (StudioProjects, .config, .android). Candidates must be
    unused now (no mounting container, no process, no unit/script/config naming them, no symlink pointing at them); anything
    in or containing a git repo, one file of a directory that is not cold, or on a mount without atime, is flagged
    needs_manual_check. An approved apply copies to `archive_root` (rsync -aHSAX into a no-symlink directory fd), verifies by
    checksum, re-proves the item is unused and cold, and only then removes the original."""
    roots = ctx.opt("roots", [f"{HOME_DEFAULT}/StudioProjects", f"{HOME_DEFAULT}/.config", f"{HOME_DEFAULT}/.android"])
    gib, days = _num(ctx.opt("min_gib", 2), 0.001, 100000), _num(ctx.opt("cold_days", 90), 1, 3650)
    budget, limit = _num(ctx.opt("scan_budget_s", 900), 10, 7200), _num(ctx.opt("scan_limit", 6_000_000), 1000, 50_000_000)
    arch = str(ctx.opt("archive_root", COLD_ARCHIVE))
    if not isinstance(roots, list) or None in (gib, days, budget, limit) or not os.path.isabs(arch):
        return _skipped("bad roots/min_gib/cold_days/archive_root config: nothing done")
    bad = _clock_problem(ctx)
    if bad:
        return _skipped(f"{bad}: nothing done")
    home = os.path.realpath(str(ctx.opt("home", HOME_DEFAULT)))
    min_b, cutoff = int(gib * GIB), ctx.now - days * DAY
    boot = _snapshot_problem()
    plan_items, items, complete_all, found = [], [], True, []
    for r in sorted({os.path.realpath(x) for x in roots if isinstance(x, str) and os.path.isabs(x)}):
        if not os.path.isdir(r) or r == "/" or _never(ctx, r):
            continue
        kept_atime = _atime_kept(r)
        cands, complete = _cold_scan(ctx, r, min_b, cutoff, float(budget), int(limit), kept_atime)
        complete_all &= complete
        bucket = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(r).lstrip(".")).lower() or "root"
        for c in sorted(cands, key=lambda c: (-c["bytes"], c["path"]))[:60]:
            p = c["path"]
            label = f"{os.path.basename(r)}/{os.path.relpath(p, r)}"
            if os.path.realpath(p) != p:
                continue
            held = None if boot else _held(p, None)
            who = _never(ctx, p) or boot or (f"in use: {held.why}" if not held.unused else "")
            if who:
                items.append({"name": label[:60], "size": human(c["bytes"]), "state": "kept", "proof": who[:150]})
                continue
            found.append((r, bucket, kept_atime, c, p, label, held))
    refs: dict[str, inuse.Proof] = {}
    links, links_why = {}, ""
    if found and not boot:
        paths = [p for *_, p, _l, _h in found]
        refs = _ref_check(ctx, {p: [(p, "exact", "any")] for p in paths}, home)
        links, links_why = _links_into({p: p for p in paths}, _link_roots(ctx, home))
    for r, bucket, kept_atime, c, p, label, held in found:
        pr = refs.get(p)
        if pr is not None and pr.known and pr.used or p in links:
            items.append({"name": label[:60], "size": human(c["bytes"]), "state": "kept",
                          "proof": (pr.why if pr is not None and pr.used and pr.known else f"in use: symlink {links[p][-60:]}")[:150]})
            continue
        top = _project_top(p, r)
        loose = not c["is_dir"] and os.path.dirname(p) != r      # one file of a directory that is otherwise in use
        notes = []
        if c["git"]:
            notes.append("contains a git repo: check unpushed commits and stashes")
        elif top:
            notes.append(f"inside git project {os.path.basename(top)}: may be tracked")
        elif loose:
            notes.append("a single file of a directory that is not cold: check what needs it")
        if not kept_atime:
            notes.append("atime not kept on this mount: last read unknown")
        if pr is None or not pr.unused:
            notes.append(f"reference search inconclusive ({(pr.why if pr else 'not run')[:50]})")
        if links_why:
            notes.append(f"symlink scan incomplete ({links_why})")
        manual = bool(notes)
        note = "; ".join(notes)
        dest_root = os.path.normpath(os.path.join(arch, bucket, os.path.relpath(os.path.dirname(p), r)))
        q, dest, rs = shlex.quote, os.path.join(dest_root, os.path.basename(p)), " ".join(cl._RSYNC)
        cmd = (f"{rs} -- {q(p + '/')} {q(dest + '/')} && rm -rf -- {q(p)}" if c["is_dir"]
               else f"{rs} -- {q(p)} {q(dest)} && rm -f -- {q(p)}")
        plan_items.append({"name": label, "path": p, "root": r, "kind": "dir" if c["is_dir"] else "file",
                           "bytes": c["bytes"] // MIB * MIB, "mtime": _day(c["newest"]), "archive_to": dest_root,
                           "needs_manual_check": manual, "command": cmd,
                           "why": f"untouched since {_day(c['newest'])}" + (f"; {note}" if note else "")})
        items.append({"name": label[:60], "size": human(c["bytes"]), "state": "manual check" if manual else "candidate",
                      "proof": _ascii(f"newest {_age(ctx.now, c['newest'])}; {held.why}; {pr.why if pr else ''}", 300)})
    plan_items.sort(key=lambda i: i["path"])
    plan = {"items": plan_items, "total_bytes": sum(i["bytes"] for i in plan_items)}
    h = plan_hash(plan)
    metrics = {"mode": "apply" if ctx.apply else "report", "candidates": len(plan_items), "scan_complete": complete_all,
               "total_h": human(plan["total_bytes"]), "plan_hash": h}
    summary = (f"{len(plan_items)} cold items, {human(plan['total_bytes'])}; plan {h}" if plan_items else "no large cold files") \
        + (f"; {boot}" if boot else "") + ("" if complete_all else "; scan incomplete")
    res = Result("info" if plan_items else "ok", _ascii(summary), metrics,
                 sorted(items, key=lambda i: (i["state"] == "kept", i["name"]))[:12], plan=plan, alert=False)
    go = _c2_gate(ctx, res, h, bool(plan_items))
    if go is not None:
        return go
    acts = _Acts(ctx)
    allow_manual = ctx.opt("allow_manual_check_items") is True
    for it in plan_items:
        label, p = it["name"], it["path"]
        if it["needs_manual_check"] and not allow_manual:
            acts._note("refused", f"{label[:40]} (manual check)", it["bytes"])
            continue
        bad = _cold_recheck(ctx, it, cutoff, arch)
        if bad:
            acts._note("refused", f"{label[:34]}: {bad}"[:60], it["bytes"])
            continue
        acts.run("c2-archive-remove", p, it["bytes"], lambda it=it: _archive_cold(it, arch), protect=(label,), label=label)
    return _c2_done(ctx, acts, res, "items", {"plan_hash": h})


def _cold_recheck(ctx: Ctx, it: dict, cutoff: float, arch: str) -> str:
    """'' when the item is still unused and still cold and the cold disk is a usable target; else why not."""
    p = it["path"]
    try:
        st = os.lstat(p)
    except OSError:
        return "gone"
    if stat.S_ISLNK(st.st_mode) or os.path.realpath(p) != p:
        return "symlink"
    why = _never(ctx, p) or _snapshot_problem()
    held = None if why else _held(p, None)
    if why or not held.unused:
        return (why or f"in use: {held.why}")[:40]
    newest, complete = _newest_change(p, it["kind"] == "dir", _atime_kept(p))
    if not complete:
        return "tree unmeasurable"
    if newest > cutoff:
        return "touched since the plan"
    bad = cl._archive_target_problem(arch, p)
    if bad or ctx.is_protected(arch) or not cl._inside(it["archive_to"], [arch]):
        return "archive target refused"
    if cl._free_bytes(arch) < it["bytes"] * 1.05:
        return "archive disk too small"
    return ""


def _archive_cold(it: dict, arch: str) -> None:
    """Copy to the cold disk, verify by checksum, re-prove, remove the original. Every step on the archive side goes
    through directory fds opened with O_NOFOLLOW from `arch`, and rsync is handed `/proc/<pid>/fd/N/...` of those
    directories: the user-writable archive tree cannot be swapped for a symlink under the root-privileged copy."""
    dest_root, p, root = it["archive_to"], it["path"], it["root"]
    st = os.lstat(p)
    rel = os.path.relpath(dest_root, arch)
    if rel.startswith("..") or not cl._inside(p, [root]):
        raise RuntimeError("archive path outside its root: refusing")
    problem = cl._archive_target_problem(arch, p)
    if problem:
        raise RuntimeError(f"archive target refused: {problem}")
    fd = _open_dir_nofollow(arch)
    owned = [fd]
    try:
        for part in [x for x in rel.split(os.sep) if x and x != "."]:
            try:
                nfd = os.open(part, _DIRFLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, _DIRFLAGS, dir_fd=fd)
                try:
                    os.fchown(nfd, st.st_uid, st.st_gid)
                except OSError:
                    pass
            except OSError as exc:
                raise cl._Changed(f"archive path unsafe ({exc.strerror})") from None
            owned.append(nfd)
            fd = nfd
        base, fdp = os.path.basename(p.rstrip("/")), f"/proc/{os.getpid()}/fd"
        if it["kind"] == "dir":
            try:
                os.mkdir(base, 0o700, dir_fd=fd)       # exclusive: never merges into an existing copy, nothing to swap
            except FileExistsError:
                raise RuntimeError("archive target exists: not overwriting") from None
            bfd = os.open(base, _DIRFLAGS, dir_fd=fd)
            owned.append(bfd)
            a, b = p.rstrip("/") + "/", f"{fdp}/{bfd}/"
        else:
            if os.path.lexists(f"{fdp}/{fd}/{base}"):
                raise RuntimeError("archive target exists: not overwriting")
            a, b = p, f"{fdp}/{fd}/{base}"
        cl._run_ok([*cl._RSYNC, "--", a, b], 3600)
        chk = sh([*cl._RSYNC, "-n", "-c", "--itemize-changes", "--", a, b], timeout=3600)
        if chk.returncode != 0 or chk.stdout.strip():
            raise RuntimeError("archive verification failed; source kept")

        def before(fdpath: str) -> None:               # the copy took minutes: something may have opened the source
            bad = _snapshot_problem()
            held = None if bad else _held(p, None)
            if bad or not held.unused:
                raise cl._Changed(f"in use after copy: {bad or held.why}")

        _rm_anchored(root, p, st, before)
    finally:
        for o in owned:
            os.close(o)
