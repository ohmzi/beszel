"""inuse: shared, READ-ONLY "is this still in use?" proofs for the cleanup tasks.

Every cleaner that deletes something must be able to say WHY it is safe. This module holds the probes; they never
mutate anything and they all fail closed: when a probe cannot run to completion (not root, /proc unreadable, docker
down, git error, grep budget exhausted) the answer is "in use / unknown", never "unused".

PUBLIC API (stdlib only; import as `from .. import inuse` inside tasks/)
------------------------------------------------------------------------
Proof(used, known, why)                NamedTuple. `used` is True for "in use" AND for "could not tell" (fail closed);
                                       `known` is False when the probe failed; `.unused` == known and not used (the ONLY
                                       state that allows deletion); `why` is a short ASCII proof string for Result items.
combine(*proofs) -> Proof              unknown if any unknown, else used if any used, else unused ("; "-joined why).

proc_snapshot(max_age_s=30, refresh=False) -> ProcSnap
                                       ONE pass over /proc/*/{maps,cwd,exe,fd,cmdline,environ}, cached for max_age_s.
                                       `.ok` is False unless EVERY process could be read (non-root => not ok => unknown) AND the scan
                                       covered the whole host: pid 1 read, same pid namespace as pid 1, and >= 90 % of the
                                       kernel's thread count (/proc/loadavg) visible (a pid namespace, hidepid or
                                       ProtectProc=invisible would otherwise show a handful of processes and call every
                                       file unused). Only enforced on the real /proc (FULL_VIEW_CHECK None = auto).
                                       Decide with the cached snapshot; re-prove with refresh=True right before acting.
files_in_use(paths, kinds=("map","exe","fd","cwd")) -> {path: Proof}
                                       Exact-file lookups (path, realpath, dev:inode) against the snapshot: thousands of
                                       files cost milliseconds. lib_mapped_by_processes(paths) is the kinds=("map",) case.
                                       ALL_KINDS adds "argv"/"env": an interpreter-run script (`python3 /usr/bin/x`) leaves
                                       its path ONLY on the command line. An EMPTY `paths` on an unusable snapshot returns
                                       {"": unknown} so a caller that looks at `.values()` still fails closed.
lib_mapped_by_processes(paths) -> {path: Proof}
                                       Is the file (or its realpath, or the same dev:inode) mapped by a running process?
                                       "(deleted)" mappings still count. unused => "not mapped by any of N processes".
process_cwd_or_open_under(path, kinds=None) -> Proof
                                       Any process whose cwd, exe, open fd, mapped file, argv or environ path token is
                                       `path` or under it (kinds: subset of cwd/exe/fd/map/argv/env). Container processes
                                       show container paths, so pair this with container_bind_mounts().
container_bind_mounts(running_only=False) -> {container: [host source paths]} | None
                                       `docker inspect` of all (default) or only running containers, batched; None when
                                       docker cannot say. Sources are listed raw and realpath-resolved. Cached 30 s.
mounted_by_container(path, mounts=None, running_only=False) -> Proof
                                       A mount that equals, contains or lies inside `path` counts.
referenced_by(path, search_roots=None) -> Proof
referenced_by_many(paths, search_roots=None) -> {path: Proof}
                                       Boundary-aware text search (also ~/x, $HOME/x forms) of systemd units, cron, scripts,
                                       configs under `search_roots` (default: default_ref_roots()). Binary/huge files are
                                       skipped; an unreadable root/file or an exhausted budget makes "no reference" unknown.
                                       Pass MANY paths in one call: the tree is walked once.
default_ref_roots(extra=()) -> list[str]
git_state(path, now=None) -> GitState  in_repo / ignored (git check-ignore -q) / tracked (git ls-files, i.e. tracked or
                                       staged) / last_commit / last_change (newest mtime of modified or untracked, not
                                       ignored, working-tree files) / last_activity / idle_days(now). Runs git as the repo
                                       owner (runuser) when root, GIT_OPTIONAL_LOCKS=0, core.fsmonitor=false; per-repo
                                       results cached 60 s. `known` False => caller must not delete.
tree_newest(path, cap=20000, budget_s=10) -> (newest_mtime, complete)   lstat walk, never follows symlinks.
loaded_nvidia_driver() -> Driver(version, branch, ondisk, why)
                                       The kernel module that is LOADED (/sys/module/nvidia/version cross-checked with
                                       /proc/driver/nvidia/version). version == "" means unknown/not loaded: never act.
reset_caches()                         for tests.

ProcSnap fields: ok, err, nproc, threads, unreadable, maps {path: [pids]}, inodes {(maj:min, inode): [pids]},
held {path: [(pid, kind)]}, under(prefix, kinds=None) -> [(pid, kind, path)]. argv/environ are reduced to absolute-path tokens in
memory (the runner's own argv/environ and its parents' are skipped); nothing from them is logged, `why` strings only name pid,
command name and kind.

Conventions: PROC (a Path), FULL_VIEW_CHECK and the module-level `sh`, `_euid`, `_mono`, `_now` are patch points for tests; nothing here
imports the other task modules, so any stream can import it without cycles. Run as root for real answers: as a normal user
/proc/<pid>/maps of other users is unreadable, the snapshot is "not ok" and every /proc based proof is unknown (= keep).
"""
from __future__ import annotations

import bisect
import json
import os
import pwd
import re
import stat
import time
from pathlib import Path
from typing import Iterable, NamedTuple

from .core import sh

PROC = Path("/proc")
SYS_NVIDIA_VERSION = "/sys/module/nvidia/version"
PROC_NVIDIA_VERSION = "/proc/driver/nvidia/version"
SNAP_TTL_S = 30.0
MOUNTS_TTL_S = 30.0
GIT_TTL_S = 60.0
VIEW_MIN_FRACTION = 0.90          # visible threads / kernel thread count (/proc/loadavg field 4 denominator)
FULL_VIEW_CHECK: bool | None = None   # None = enforce the whole-host checks only on the real /proc; tests force True/False
_mono = time.monotonic
_now = time.time
_euid = os.geteuid


# =========================================================================== Proof
class Proof(NamedTuple):
    used: bool       # True = in use OR unknown (fail closed): never delete
    known: bool      # False = the probe could not run to completion
    why: str         # short ASCII proof for Result items

    @property
    def unused(self) -> bool:
        return self.known and not self.used


def _a(s: object, n: int = 110) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def _unused(why: str) -> Proof:
    return Proof(False, True, _a(why))


def _used(why: str) -> Proof:
    return Proof(True, True, _a(why))


def _unknown(why: str) -> Proof:
    return Proof(True, False, _a("unknown: " + why))


def combine(*proofs: Proof) -> Proof:
    """unknown beats used beats unused; the why strings are joined (an unused result keeps every proof)."""
    if not proofs:
        return _unknown("no proof given")
    bad = [p for p in proofs if not p.known] or [p for p in proofs if p.used]
    if bad:
        return Proof(True, all(p.known for p in bad), _a("; ".join(p.why for p in bad), 140))
    return Proof(False, True, _a("; ".join(p.why for p in proofs), 140))


def _clean(p: str) -> str:
    """Normalise a path from /proc: drop the kernel's ' (deleted)' suffix."""
    return p[:-10] if p.endswith(" (deleted)") else p


def _comm(pid: int) -> str:
    try:
        return (PROC / str(pid) / "comm").read_text(errors="replace").strip()[:20] or "?"
    except OSError:
        return "?"


# =========================================================================== one pass over /proc
_TOKEN = re.compile(r"/[^\s:='\"<>|&;,()\[\]{}]+")
_KINDS = ("cwd", "exe", "fd", "map", "argv", "env")
ALL_KINDS = ("map", "exe", "fd", "cwd", "argv", "env")


class ProcSnap:
    """Point-in-time view of every process. `.ok` is only True when no process was unreadable."""

    def __init__(self) -> None:
        self.ok = False
        self.err = ""
        self.nproc = 0
        self.threads = 0                                         # scheduling entities seen (sum of /proc/<pid>/task)
        self.unreadable = 0
        self.taken = 0.0
        self.maps: dict[str, list[int]] = {}                     # mapped file path -> pids
        self.inodes: dict[tuple[str, int], list[int]] = {}       # ("maj:min" hex, inode) -> pids
        self.held: dict[str, list[tuple[int, str]]] = {}         # cwd/exe/fd/argv/env path -> [(pid, kind)]
        self._keys: list[str] | None = None

    def under(self, prefix: str, kinds: Iterable[str] | None = None) -> list[tuple[int, str, str]]:
        """[(pid, kind, path)] for every recorded path that is `prefix` or below it (binary search, no scan)."""
        want = set(kinds) if kinds is not None else set(_KINDS)
        prefix = prefix.rstrip("/")
        if self._keys is None:
            self._keys = sorted(set(self.maps) | set(self.held))
        if not prefix:                                          # "/" : everything is under it
            keys = self._keys
        else:
            i = bisect.bisect_left(self._keys, prefix + "/")
            keys = ([prefix] if prefix in self.maps or prefix in self.held else [])
            while i < len(self._keys) and self._keys[i].startswith(prefix + "/"):
                keys.append(self._keys[i])
                i += 1
        out: list[tuple[int, str, str]] = []
        for k in keys:
            if "map" in want:
                out += [(pid, "map", k) for pid in self.maps.get(k, ())]
            out += [(pid, kind, k) for pid, kind in self.held.get(k, ()) if kind in want]
            if len(out) > 50:
                break
        return out


def _read_text(p: str, limit: int = -1) -> str:
    """Whole file by default (a truncated maps file would hide libraries: false "unused"); cap argv/environ."""
    with open(p, encoding="utf-8", errors="surrogateescape") as f:
        return f.read(limit)


def _add(d: dict, key, val, cap: int = 6) -> None:
    lst = d.setdefault(key, [])
    if len(lst) < cap and val not in lst:
        lst.append(val)


def _ancestors(pid: int) -> set[int]:
    """pid and its parent chain (best effort; a failure just means fewer exclusions, i.e. more caution)."""
    out = {pid}
    for _ in range(32):
        try:
            st = (PROC / str(pid) / "stat").read_text()
            pid = int(st[st.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 0 or pid in out:
            break
        out.add(pid)
    return out


def _scan_proc() -> ProcSnap:
    snap = ProcSnap()
    snap.taken = _mono()
    try:
        names = os.listdir(PROC)
    except OSError as exc:
        snap.err = f"/proc unreadable ({type(exc).__name__})"
        return snap
    me = os.getpid()
    seen_me = seen_init = False
    skip_text = _ancestors(me)                                 # our own argv/environ (and our callers') name paths, but use none
    for n in names:
        if not n.isdigit():
            continue
        pid, base = int(n), f"{PROC}/{n}"
        try:
            text = _read_text(base + "/maps")
        except PermissionError:
            snap.unreadable += 1
            continue
        except OSError:
            continue                                           # exited while we scanned: nothing to prove about it
        snap.nproc += 1
        seen_me = seen_me or pid == me
        seen_init = seen_init or pid == 1
        try:
            snap.threads += max(len(os.listdir(base + "/task")), 1)
        except OSError:
            snap.threads += 1
        done: set[str] = set()
        for ln in text.splitlines():
            f = ln.split(None, 5)
            if len(f) == 6 and f[5].startswith("/"):
                p = _clean(f[5])
                if p not in done:
                    done.add(p)
                    _add(snap.maps, p, pid, 8)
                    if f[4].isdigit() and f[4] != "0":
                        _add(snap.inodes, (f[3].lower(), int(f[4])), pid, 8)
        for kind in ("cwd", "exe"):
            try:
                _add(snap.held, _clean(os.readlink(f"{base}/{kind}")), (pid, kind))
            except PermissionError:
                snap.unreadable += 1
            except OSError:
                pass                                           # kernel thread / zombie: no cwd or exe
        try:
            with os.scandir(base + "/fd") as it:
                for e in it:
                    try:
                        t = os.readlink(e.path)
                    except OSError:
                        continue
                    if t.startswith("/"):
                        _add(snap.held, _clean(t), (pid, "fd"))
        except PermissionError:
            snap.unreadable += 1
        except OSError:
            pass
        for kind, fname in (("argv", "cmdline"), ("env", "environ")):
            try:
                raw = _read_text(f"{base}/{fname}", 1 << 20).replace("\0", " ")
            except PermissionError:
                snap.unreadable += 1
                continue
            except OSError:
                continue
            if pid in skip_text:
                continue
            for tok in set(_TOKEN.findall(raw)):
                _add(snap.held, tok.rstrip("/.") or "/", (pid, kind))
    snap.ok = snap.unreadable == 0 and snap.nproc > 0 and seen_me
    if not snap.ok:
        snap.err = (f"{snap.unreadable} unreadable process entries (not root?)" if snap.unreadable
                    else "no processes visible" if not snap.nproc else "own process not visible (hidepid?)")
    elif _whole_host_check():
        bad = _partial_view(snap, seen_init, me)               # a PARTIAL view is the one thing "no process uses it" cannot survive
        if bad:
            snap.ok, snap.err = False, "partial /proc view: " + bad
    return snap


def _whole_host_check() -> bool:
    return (str(PROC) == "/proc") if FULL_VIEW_CHECK is None else FULL_VIEW_CHECK


def _partial_view(snap: ProcSnap, seen_init: bool, me: int) -> str:
    """'' when the scan covered the whole host, else why not. Three independent signals, all required: pid 1 was read,
    we share its pid namespace, and the threads we saw are (almost) all the threads the kernel counts (a `unshare --pid
    --mount-proc`, hidepid=2, ProtectProc=invisible or a container shows a few processes and every file looks unused)."""
    if not seen_init:
        return "pid 1 not visible"
    try:
        if os.readlink(f"{PROC}/1/ns/pid") != os.readlink(f"{PROC}/{me}/ns/pid"):
            return "different pid namespace than pid 1"
    except OSError as exc:
        return f"pid namespace unreadable ({type(exc).__name__})"
    try:
        total = int((PROC / "loadavg").read_text().split()[3].split("/")[1])
    except (OSError, ValueError, IndexError):
        return "/proc/loadavg unreadable"
    if total <= 0 or snap.threads < VIEW_MIN_FRACTION * total:
        return f"saw {snap.threads} of {total} threads"
    return ""


_SNAP: ProcSnap | None = None
_MOUNTS: dict[bool, tuple[float, dict[str, list[str]]]] = {}
_GIT: dict[str, tuple[float, tuple[float, float]]] = {}


def reset_caches() -> None:
    global _SNAP
    _SNAP = None
    _MOUNTS.clear()
    _GIT.clear()


def proc_snapshot(max_age_s: float = SNAP_TTL_S, refresh: bool = False) -> ProcSnap:
    global _SNAP
    if refresh or _SNAP is None or _mono() - _SNAP.taken > max_age_s:
        _SNAP = _scan_proc()
    return _SNAP


def _forms(path: str) -> list[str]:
    """The path as given plus its realpath (symlinked dirs, /lib -> /usr/lib), normalised, no trailing slash."""
    out = []
    for p in (path, os.path.realpath(path)):
        p = os.path.normpath(p)
        if p not in out:
            out.append(p)
    return out


def files_in_use(paths: Iterable[str], kinds: Iterable[str] = ("map", "exe", "fd", "cwd")) -> dict[str, Proof]:
    """{path: Proof}: exact-file lookups (path, realpath, or device:inode) in the snapshot. Cheap for thousands of files."""
    snap = proc_snapshot()
    want, res = set(kinds), {}
    paths = list(paths)
    if not paths and not snap.ok:
        return {"": _unknown(snap.err)}                        # "nothing to check" on an unusable /proc must not read as "unused"
    for path in paths:
        if not os.path.isabs(path):
            res[path] = _unknown("path is not absolute")
            continue
        if not snap.ok:
            res[path] = _unknown(snap.err)
            continue
        hits: list[tuple[int, str]] = []
        for p in _forms(path):
            if "map" in want:
                hits += [(pid, "map") for pid in snap.maps.get(p, ())]
            hits += [(pid, k) for pid, k in snap.held.get(p, ()) if k in want]
        if "map" in want:
            try:
                st = os.stat(path)
                key = (f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}", st.st_ino)
                hits += [(pid, "map") for pid in snap.inodes.get(key, ())]
            except OSError:
                pass                                           # file absent: the path forms above still count
        uniq = sorted(set(hits))
        res[path] = (_used("; ".join(f"pid {pid} ({_comm(pid)}) {k}" for pid, k in uniq[:3]))
                     if uniq else _unused(f"not in use by any of {snap.nproc} processes"))
    return res


def lib_mapped_by_processes(paths: Iterable[str]) -> dict[str, Proof]:
    """{path: Proof}. used = some process maps the file (matched by path, realpath, or device:inode)."""
    return {p: (Proof(r.used, r.known, r.why.replace("not in use by", "not mapped by")) if r.unused else r)
            for p, r in files_in_use(paths, ("map",)).items()}


def process_cwd_or_open_under(path: str, kinds: Iterable[str] | None = None) -> Proof:
    if not os.path.isabs(path):
        return _unknown("path is not absolute")
    snap = proc_snapshot()
    if not snap.ok:
        return _unknown(snap.err)
    hits: list[tuple[int, str, str]] = []
    for p in _forms(path):
        hits += snap.under(p, kinds)
    if hits:
        uniq = sorted({(pid, kind) for pid, kind, _ in hits})
        s = "; ".join(f"pid {pid} ({_comm(pid)}) {kind}" for pid, kind in uniq[:3])
        return _used(s + (f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else ""))
    return _unused(f"no cwd/exe/fd/map/argv/env under it in {snap.nproc} processes")


# =========================================================================== docker bind mounts
def container_bind_mounts(running_only: bool = False) -> dict[str, list[str]] | None:
    """{container name: [host source paths]}; every source appears raw and realpath-resolved. None = unknown."""
    hit = _MOUNTS.get(running_only)
    if hit and _mono() - hit[0] < MOUNTS_TTL_S:
        return hit[1]
    ps = sh(["docker", "ps", *([] if running_only else ["-a"]), "-q", "--no-trunc"], timeout=30)
    if ps.returncode != 0:
        return None
    ids = ps.stdout.split()
    out: dict[str, list[str]] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = sh(["docker", "inspect", "--format", "{{.Name}}\t{{json .Mounts}}", *chunk], timeout=90)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode != 0 or len(lines) != len(chunk):
            return None
        for ln in lines:
            name, _, js = ln.partition("\t")
            try:
                mounts = json.loads(js) if js not in ("", "null") else []
            except ValueError:
                return None
            if not isinstance(mounts, list):
                return None
            raw = {m["Source"] for m in mounts if isinstance(m, dict) and str(m.get("Source", "")).startswith("/")}
            out[name.lstrip("/")] = sorted(raw | {os.path.realpath(s) for s in raw})
    _MOUNTS[running_only] = (_mono(), out)
    return out


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def mounted_by_container(path: str, mounts: dict[str, list[str]] | None = None,
                         running_only: bool = False) -> Proof:
    if not os.path.isabs(path):
        return _unknown("path is not absolute")
    if mounts is None:
        mounts = container_bind_mounts(running_only)
    if mounts is None:
        return _unknown("docker inspect failed")
    forms = _forms(path)
    hit = sorted(n for n, srcs in mounts.items()
                 if any(s != "/" and _overlap(s, f) for s in srcs for f in forms))
    if hit:
        return _used("mounted by container " + ",".join(hit[:3]) + (f" (+{len(hit) - 3})" if len(hit) > 3 else ""))
    return _unused(f"no {'running ' if running_only else ''}container mounts it ({len(mounts)} checked)")


# =========================================================================== references in units / cron / scripts
REF_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages", "target", "dist", "build",
                 ".gradle", ".cache", ".mypy_cache", ".pytest_cache", "pgdata", "surreal_data", "notebook_data",
                 "volumes", "models", "backups", "Trash", ".next", ".turbo"}


def default_ref_roots(extra: Iterable[str] = ()) -> list[str]:
    """Where a host script, unit or cron entry could name a path (system + the owner's user units)."""
    roots = ["/etc/systemd/system", "/etc/systemd/user", "/etc/cron.d", "/etc/cron.daily", "/etc/cron.weekly",
             "/etc/cron.hourly", "/etc/cron.monthly", "/etc/crontab", "/var/spool/cron/crontabs",
             "/usr/local/sbin", "/usr/local/bin", "/etc/profile.d", "/etc/environment"]
    # /etc/homelab-maint is NOT searched: maint.toml lists the very paths the cleaners consider (a mention there is no use)
    try:
        for e in os.scandir("/home"):
            roots += [f"{e.path}/.config/systemd/user", f"{e.path}/.config/autostart", f"{e.path}/.profile",
                      f"{e.path}/.bashrc", f"{e.path}/.bash_aliases", f"{e.path}/.zshrc", f"{e.path}/.config/fish"]
    except OSError:
        pass
    return roots + [str(x) for x in extra]


def _variants(path: str) -> list[str]:
    out = []
    for p in _forms(path):
        out.append(p)
        m = re.fullmatch(r"/home/([^/]+)(/.*)?", p)
        if m:
            rest = m.group(2) or ""
            out += [f"~{rest}", f"$HOME{rest}", f"${{HOME}}{rest}", f"~{m.group(1)}{rest}"]
    return sorted(set(v for v in out if len(v) > 1))


def referenced_by_many(paths: Iterable[str], search_roots: Iterable[str] | None = None, *,
                       budget_s: float = 25.0, max_files: int = 80_000, max_bytes: int = 1 << 20) -> dict[str, Proof]:
    """One walk of the roots for all paths. A reference is the path (or ~/ $HOME/ form) NOT followed by a name
    character, so /x/.venv matches /x/.venv/bin/python but not /x/.venv2. Parent-directory references do not count."""
    paths = list(dict.fromkeys(paths))
    roots = list(search_roots) if search_roots is not None else default_ref_roots()
    pats = {}
    for p in paths:
        if not p or not os.path.isabs(p):
            continue
        vs = _variants(p)
        pats[p] = (os.path.basename(p.rstrip("/")),
                   re.compile("(?:" + "|".join(re.escape(v) for v in vs) + r")(?![A-Za-z0-9_-])"))
    found: dict[str, str] = {}
    incomplete, nfiles, t0 = "", 0, _mono()

    def scan(fp: str, size: int) -> None:
        nonlocal incomplete
        try:
            with open(fp, "rb") as f:
                raw = f.read(max_bytes)
        except PermissionError:
            incomplete = incomplete or f"unreadable file {os.path.basename(fp)}"
            return
        except OSError:
            return
        if b"\0" in raw[:4096]:
            return
        text = raw.decode("utf-8", "replace")
        for p, (base, rx) in pats.items():
            if p not in found and base in text and rx.search(text):
                found[p] = fp

    for root in roots:
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = incomplete or f"cannot stat {os.path.basename(root)}"
            continue
        if stat.S_ISREG(st.st_mode):
            nfiles += 1
            scan(root, st.st_size)
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue

        def onerr(exc: OSError) -> None:
            nonlocal incomplete
            if not isinstance(exc, FileNotFoundError):
                incomplete = incomplete or f"cannot list {os.path.basename(str(exc.filename))}"

        for d, dirs, files in os.walk(root, onerror=onerr):
            dirs[:] = [x for x in dirs if x not in REF_SKIP_DIRS]
            for fn in files:
                nfiles += 1
                if nfiles > max_files or _mono() - t0 > budget_s:
                    incomplete = incomplete or "grep budget exhausted"
                    break
                fp = os.path.join(d, fn)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_size <= max_bytes:
                    scan(fp, st.st_size)
            if incomplete == "grep budget exhausted":
                break
        if incomplete == "grep budget exhausted":
            break
    res: dict[str, Proof] = {}
    for p in paths:
        if p not in pats:
            res[p] = _unknown("path is not absolute")
        elif p in found:
            res[p] = _used("referenced by " + found[p][-70:])
        elif incomplete:
            res[p] = _unknown(f"reference search incomplete ({incomplete})")
        elif nfiles == 0:
            res[p] = _unknown("no files were searched")             # an empty search proves nothing
        else:
            res[p] = _unused(f"no reference in {nfiles} unit/cron/script/config files")
    return res


def referenced_by(path: str, search_roots: Iterable[str] | None = None, **kw) -> Proof:
    return referenced_by_many([path], search_roots, **kw)[path]


# =========================================================================== git
class GitState(NamedTuple):
    known: bool          # False: git could not answer, the caller must not delete
    in_repo: bool
    repo: str
    ignored: bool        # `git check-ignore -q` exit 0
    tracked: bool        # `git ls-files -- path` lists something (tracked or staged)
    last_commit: float   # newest of: HEAD commit time, .git/logs/HEAD and .git/HEAD mtime (any ref movement); 0.0 = none
    last_change: float   # newest mtime among modified/untracked (not ignored) files, 0.0 = clean tree
    why: str

    @property
    def last_activity(self) -> float:
        return max(self.last_commit, self.last_change)

    def idle_days(self, now: float | None = None) -> float:
        """Days since the last commit / reflog entry / working-tree change; inf when there never was any."""
        t = self.last_activity
        return float("inf") if t <= 0 else max((_now() if now is None else now) - t, 0.0) / 86400


def tree_newest(path: str, cap: int = 20_000, budget_s: float = 10.0) -> tuple[float, bool]:
    """(newest mtime of path and everything below it, complete). lstat only; incomplete => treat as 'now'."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0.0, True
    newest, n, t0, stack = st.st_mtime, 0, _mono(), []
    if stat.S_ISDIR(st.st_mode):
        stack.append(path)
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    n += 1
                    if n > cap or _mono() - t0 > budget_s:
                        return newest, False
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, s.st_mtime)
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(e.path)
        except OSError:
            continue
    return newest, True


def _git(workdir: str, args: list[str], timeout: int = 60):
    """git as the owner of `workdir` (root must not run git inside a user-owned repo: hooks/fsmonitor config)."""
    cmd = ["git", "-C", workdir, "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", *args]
    try:
        uid = os.stat(workdir).st_uid
        if _euid() == 0 and uid != 0:
            cmd = ["runuser", "-u", pwd.getpwuid(uid).pw_name, "--", *cmd]
    except (OSError, KeyError):
        pass
    return sh(cmd, timeout=timeout, env={"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})


def _nearest_dir(path: str) -> str:
    d = path
    while d and d != "/" and not os.path.isdir(d):
        d = os.path.dirname(d)
    return d or "/"


def _repo_activity(top: str, gitdir: str) -> tuple[float, float] | None:
    """(last_commit-or-reflog, last working-tree change) for a repo, cached; None when git cannot say."""
    hit = _GIT.get(top)
    if hit and _mono() - hit[0] < GIT_TTL_S:
        return hit[1]
    r = _git(top, ["log", "-1", "--format=%ct"])
    if r.returncode == 0 and r.stdout.strip().isdigit():
        commit = float(r.stdout.strip())
    elif r.returncode != 0 and "does not have any commits" in (r.stderr or ""):
        commit = 0.0
    else:
        return None
    for f in (("logs", "HEAD"), ("HEAD",)):                    # any ref movement (checkout, pull, reset, `git init`) is activity
        try:
            commit = max(commit, os.stat(os.path.join(gitdir, *f)).st_mtime)
        except OSError:
            pass
    st = _git(top, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], timeout=120)
    if st.returncode != 0:
        return None
    recs = st.stdout.split("\0")
    change, i, count = 0.0, 0, 0
    while i < len(recs):
        rec = recs[i]
        i += 1
        if len(rec) < 4:
            continue
        count += 1
        if rec[0] in "RC" or rec[1] in "RC":
            i += 1                                             # rename/copy: the next record is the origin path
        if count > 5000:
            change = _now()                                    # too many changes to measure: call it active
            break
        full = os.path.join(top, rec[3:])
        try:
            if stat.S_ISDIR(os.lstat(full).st_mode):
                m, complete = tree_newest(full)
                change = max(change, m if complete else _now())
            else:
                change = max(change, os.lstat(full).st_mtime)
        except OSError:
            change = max(change, _now())                       # deleted tracked file: unknown when, so "now"
    _GIT[top] = (_mono(), (commit, change))
    return commit, change


def git_state(path: str, now: float | None = None) -> GitState:
    unknown = lambda why, repo="": GitState(False, bool(repo), repo, False, False, 0.0, 0.0, _a("unknown: " + why))  # noqa: E731
    if not os.path.isabs(path):
        return unknown("path is not absolute")
    real = os.path.realpath(path)
    base = _nearest_dir(real)
    r = _git(base, ["rev-parse", "--show-toplevel", "--absolute-git-dir"])
    if r.returncode != 0:
        if "not a git repository" in (r.stderr or ""):
            return GitState(True, False, "", False, False, 0.0, 0.0, "not in a git repository")
        return unknown(f"git rev-parse rc={r.returncode} {(r.stderr or '').strip()[:60]}")
    lines = r.stdout.splitlines()
    if len(lines) != 2 or not lines[0].startswith("/"):
        return unknown("unparsable rev-parse output")
    top, gitdir = os.path.realpath(lines[0]), lines[1]
    rel = os.path.relpath(real, top)
    if rel.startswith(".."):
        return unknown("path outside the work tree", top)
    ign = _git(top, ["check-ignore", "-q", "--", rel])
    if ign.returncode not in (0, 1):
        return unknown(f"check-ignore rc={ign.returncode}", top)
    ls = _git(top, ["ls-files", "-z", "--", rel])
    if ls.returncode != 0:
        return unknown(f"ls-files rc={ls.returncode}", top)
    act = _repo_activity(top, gitdir)
    if act is None:
        return unknown("git log/status failed", top)
    tracked, ignored = bool(ls.stdout.strip("\0")), ign.returncode == 0
    gs = GitState(True, True, top, ignored, tracked, act[0], act[1], "")
    idle = gs.idle_days(now)
    why = (f"{'ignored' if ignored else 'NOT ignored'}, {'tracked/staged' if tracked else 'untracked'}, "
           f"repo idle {'forever' if idle == float('inf') else f'{idle:.0f} d'}")
    return gs._replace(why=_a(why))


# =========================================================================== NVIDIA driver
class Driver(NamedTuple):
    version: str     # "580.173.02" or "" when unknown / not loaded
    branch: str      # "580"
    ondisk: str      # version of the module on disk for the running kernel ("" when modinfo cannot say)
    why: str


_VER = re.compile(r"\b(\d{3,4}\.\d+(?:\.\d+)?)\b")


def loaded_nvidia_driver() -> Driver:
    """The driver version of the LOADED kernel module. /sys and /proc must agree; anything else is unknown."""
    seen = {}
    try:
        seen["sys"] = Path(SYS_NVIDIA_VERSION).read_text().strip()
    except OSError:
        pass
    try:
        first = Path(PROC_NVIDIA_VERSION).read_text().splitlines()[0]
        m = re.search(r"Module(?: for \S+)?\s+(\d{3,4}\.\d+(?:\.\d+)?)", first) or _VER.search(first)
        if m:
            seen["proc"] = m.group(1)
    except (OSError, IndexError):
        pass
    vers = set(seen.values())
    if not vers:
        return Driver("", "", "", "nvidia kernel module not loaded or unreadable")
    if len(vers) != 1 or not _VER.fullmatch(next(iter(vers))):
        return Driver("", "", "", f"loaded-driver sources disagree: {seen}")
    v = vers.pop()
    r = sh(["modinfo", "-F", "version", "nvidia"], timeout=15)
    ondisk = r.stdout.strip() if r.returncode == 0 and _VER.fullmatch(r.stdout.strip()) else ""
    return Driver(v, v.split(".")[0], ondisk, f"loaded {v} (" + ("+".join(sorted(seen)) or "?") + ")")
