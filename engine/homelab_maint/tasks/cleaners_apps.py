"""cleaners_apps: C1 (daily) housekeeping for the places that were cleaned by hand and must not fill up again.

app_cache_trim, log_compress, dangling_images, crash_dumps, apt_cache      (all C1 / daily / default mode = report)

Same safety model as tasks/cleaners.py, whose helpers are reused (`_Acts`, `_num`, `_busy`, ledger and container
readers) and not edited:
  * every mutation goes through ctx.act (via cleaners._Acts): caps, protected.toml, PAUSE and the kill switch apply, and
    the dry-run walks exactly the list that apply mode walks;
  * every deletion is justified by an IN-USE PROOF that is recorded in the Result items and unit-tested both ways:
    something that is provably unused is selected, any doubt or probe error (docker, /proc, gzip, ledger) selects NOTHING;
  * files are removed fd-relative: the directory is opened component by component with O_NOFOLLOW (a symlink swapped in
    after the scan makes the open fail instead of redirecting a root-run delete), the inode scanned is re-checked, and
    a file that changed, vanished or became open since the scan is skipped, never an error;
  * "is it open" is answered from /proc/<pid>/{fd,cwd,root,maps} by (st_dev, st_ino), not by path, so a file held open
    by a container (whose mount namespace shows a different path) is seen too. Without root other users' processes are
    unreadable: the proof is then incomplete, apply degrades to report and the dry-run says so.

Never-touch paths (NEVER_TOUCH) are refused for the configurable roots on top of protected.toml, and cannot be unprotected.
Every configurable path must already be canonical (absolute, no `.`/`..`/`//`/trailing `/`, no symlink in it): the checks
(allowed roots, protected.toml, never-touch, `unprotect`) all run on the string, so a `..` could make them lie.
"""
from __future__ import annotations

import errno
import fnmatch
import gzip
import json
import os
import re
import stat
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import core
from ..core import Ctx, Result, human, sh, task
from . import cleaners as cl

try:                                            # shared proofs (homelab_maint/inuse.py, pkgs stream)
    from .. import inuse as _inuse
except Exception:                               # noqa: BLE001 - a missing or broken module must not break this one
    _inuse = None

PROC = "/proc"                                  # tests point this at a fake tree
HOLD_REFRESH_S = 60.0                           # a /proc snapshot older than this is retaken before the next batch
RECENT_S = cl.RECENT_S                          # never touch anything modified within the last 10 minutes
_DFLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FFLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

# Paths that no cache rule or log root may ever point into, whatever the config or `unprotect` says.
NEVER_TOUCH = tuple(re.compile(p) for p in (
    r"^/var/lib/docker/volumes(/|$)", r"^/var/lib/libvirt(/|$)", r"^/mnt/backup(/|$)", r"^/media/(Immich|nextcloud)(/|$)",
    r"/\.config/Cursor(/|$)", r"/\.cursor(/|$)", r"^/home/[^/]+/models(/|$)", r"(^|/)ai-stack(/|$)",
    r"surreal_data|notebook_data|pgdata", r"^/usr/share/ollama|/\.ollama(/|$)|/comfyui/models",
    r"^/var/snap/plexmediaserver|^/media/SandiskSSD/plex|/Plex Media Server(/|$)", r"^/volume1/docker/plex(/|$)",
    # app state dirs hold databases and settings next to the disposable cache: only the named cache dirs are reachable
    r"^/volume1/docker/(kavita|radarr|sonarr|prowlarr|bazarr|lidarr|readarr|sabnzbd|seerr|overseerr|jellyseerr)(/(?!config/cache(/|$))|$)",
    r"/\.docker-data(/(?!tunarr/cache/)|$)"))
# Names that mean "database or its sidecar/backup": a cache tree holding any of them is not a cache, the rule is refused.
_DBISH = re.compile(r"(\.(db|sqlite\d*|vscdb|bak|ldb|mdb|rdb|kdbx|sql)|-(wal|shm|journal))$", re.I)

_ROOT_UID = 0                                  # tests cannot create root-owned files: they point this at their own uid
_euid = cl._euid
_sleep = cl._sleep
_mono = cl._mono


def _never(path: str) -> bool:
    return any(rx.search(path) for rx in NEVER_TOUCH)


def _canonical(path: Any) -> bool:
    """Absolute, already normalised (no `.`, `..`, `//`, trailing `/`) and free of symlinks: path == realpath(path)."""
    return (isinstance(path, str) and os.path.isabs(path) and "\0" not in path
            and path == os.path.normpath(path) and os.path.realpath(path) == path)


def _below(path: str, roots: list[str]) -> bool:
    """STRICTLY inside one of the roots: a rule on the root itself would reach everything the root holds."""
    return any(path.startswith(r.rstrip("/") + "/") for r in roots)


# =========================================================================== shared in-use machinery
def _open_dir(path: str) -> int:
    """fd of an absolute, normalised directory, opened one component at a time with O_NOFOLLOW."""
    parts = [p for p in path.split("/") if p]
    if not os.path.isabs(path) or any(p in (".", "..") for p in parts):
        raise ValueError(f"not a normalised absolute path: {path!r}")
    fd = os.open("/", _DFLAGS)
    try:
        for p in parts:
            nfd = os.open(p, _DFLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _proc_scan(want: set[tuple[int, int]]) -> tuple[set[tuple[int, int]], bool]:
    """(held, complete): which (st_dev, st_ino) of `want` some process has open (fd), as cwd/root, or mapped.

    os.stat on /proc/<pid>/fd/N follows the magic link to the real inode, so containers (whose paths differ) are seen.
    complete=False when any live process could not be read (not root, odd failure): silence then proves nothing.
    A process that exits mid-scan is normal and ignored."""
    held: set[tuple[int, int]] = set()
    if not want:
        return held, True
    try:
        pids = [n for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return held, False
    complete = True
    for pid in pids:
        base = f"{PROC}/{pid}"
        try:
            fds = os.listdir(f"{base}/fd")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            complete = False
            continue
        for link in [f"fd/{n}" for n in fds] + ["cwd", "root"]:
            try:
                st = os.stat(f"{base}/{link}")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ESRCH):   # a dead cwd/root link (ENOENT) is harmless
                    complete = False
                continue
            if (st.st_dev, st.st_ino) in want:
                held.add((st.st_dev, st.st_ino))
        try:
            with open(f"{base}/maps", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    p = ln.split(None, 5)
                    if len(p) >= 5 and p[4] != "0":
                        try:
                            maj, mnr = p[3].split(":")
                            k = (os.makedev(int(maj, 16), int(mnr, 16)), int(p[4]))
                        except ValueError:
                            continue
                        if k in want:
                            held.add(k)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            complete = False
    return held, complete


class _Holders:
    """Cached /proc snapshot for one set of inodes; retaken when older than HOLD_REFRESH_S (long runs)."""

    def __init__(self, want: set[tuple[int, int]]):
        self.want, self.t, self.held, self.complete = want, None, set(), True

    def get(self, force: bool = False) -> tuple[set[tuple[int, int]], bool]:
        if force or self.t is None or _mono() - self.t > HOLD_REFRESH_S:
            self.held, self.complete = _proc_scan(self.want)
            self.t = _mono()
        return self.held, self.complete


def _second_opinion(path: str, refresh: bool = False) -> tuple[str, str]:
    """The shared inuse module's PATH-based answer for one file: ('', '') = positively unused, ('used', why) or
    ('unknown', why). It complements the inode scan above (fd/cwd/exe/maps of host processes, one snapshot for all
    of them; `refresh` retakes it for a last-moment re-proof). Module missing => the inode scan is the only proof;
    module present but raising, or not root (its snapshot is then not ok) => 'unknown', which refuses apply."""
    if _inuse is None:
        return "", ""
    try:
        if refresh:
            _inuse.proc_snapshot(refresh=True)
        pr = _inuse.process_cwd_or_open_under(path, kinds=("cwd", "exe", "fd", "map"))
        if pr.unused:
            return "", ""
        return ("used" if pr.known else "unknown"), str(pr.why)[:80]
    except Exception as exc:  # noqa: BLE001
        if cl._is_timeout(exc):
            raise
        return "unknown", f"inuse probe error {type(exc).__name__}"


def _file_proof(path: str, key: tuple[int, int], held: set, complete: bool) -> tuple[str, str]:
    """('used'|'unknown'|'', why) for one file, from the inode scan and the shared module together."""
    if key in held:
        return "used", "open by a process"
    kind, why = _second_opinion(path)
    if kind == "used":
        return kind, why
    if kind == "unknown" or not complete:
        return "unknown", why or "open-file proof incomplete (not root)"
    return "", ""


def _ids_of(path: str) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _can_unlink(dirpath: str, st: os.stat_result) -> bool:
    """Root can; others need write+search on the directory, and in a sticky dir must also own the file or the dir."""
    if _euid() == 0:
        return True
    try:
        d = os.stat(dirpath)
    except OSError:
        return False
    if not os.access(dirpath, os.W_OK | os.X_OK):
        return False
    return not (d.st_mode & stat.S_ISVTX) or st.st_uid == _euid() or d.st_uid == _euid()


def _cfg_nums(ctx: Ctx, *keys: tuple[str, float, float, float]) -> list[float] | None:
    """Numeric options with (name, default, lo, hi); None when any is present but invalid."""
    out = []
    for name, default, lo, hi in keys:
        v = cl._num(ctx.opt(name, default), lo, hi)
        if v is None:
            return None
        out.append(v)
    return out


# =========================================================================== app_cache_trim
MAX_UNLINK_ERRORS = 20                          # per batch: past this the batch stops (a failing filesystem is not hammered)
STALLED_RUNS = 3                                # a rule that selects files but removes none this many runs in a row warns


@dataclass
class _Rule:
    name: str
    path: str
    max_age_s: float
    files_only: bool
    root_owned_ok: bool
    container: str | None
    health_url: str | None
    gate: str | None
    mount_srcs: frozenset = frozenset()          # every container bind-mount source (raw and resolved) at validation time


@dataclass
class _Scan:
    cands: dict[str, list[tuple]] = field(default_factory=dict)      # rel dir -> [(name, ino, dev, mtime_ns, size)]
    empty: dict[str, list[tuple]] = field(default_factory=dict)      # rel PARENT dir -> [(name, ino, dev, mtime_ns)] empty old dirs
    dbfiles: list[str] = field(default_factory=list)                 # database-looking names anywhere in the tree (any age)
    files: int = 0
    bytes: int = 0
    sel_files: int = 0
    sel_bytes: int = 0
    young: int = 0
    recent: int = 0
    skip_root: int = 0
    links: int = 0
    unreadable: int = 0
    complete: bool = True


@dataclass
class _Done:
    """Books of ONE batch, filled in place so a batch that stops half way (errors, exception) is still counted truthfully."""
    freed: int = 0
    removed: int = 0
    skipped: int = 0                             # changed / open / vanished / not regular since the scan: benign races
    n_err: int = 0                               # real per-entry OSErrors (EPERM, EROFS, immutable file, ...)
    first_err: str = ""
    gone: list[tuple] = field(default_factory=list)                  # [(rel, name, size, mtime_ns)] really unlinked


def _loopback_url(u: Any) -> bool:
    return isinstance(u, str) and re.fullmatch(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?(/[^\s]*)?", u) is not None


def _is_mount_src(path: str, srcs) -> bool:
    """True if some container bind-mounts `path` itself or something inside it (removing it would break that mount)."""
    return any(s == path or s.startswith(path + "/") for s in srcs)


def _mount_map() -> dict[str, list[str]] | None:
    """{container: [bind-mount sources]} of ALL containers (stopped ones remount on start); None = docker cannot say."""
    return cl._container_mounts()


def _check_cache_rule(ctx: Ctx, rule: Any, roots: list[str]) -> str | _Rule:
    """Validate one rule. A string is the refusal reason (nothing is selected); otherwise the parsed rule."""
    if not isinstance(rule, dict):
        return "refused: bad rule"
    name, path = str(rule.get("name") or "")[:40], rule.get("path")
    if not name:
        return "refused: rule has no name"
    if not roots:
        return "refused: no allowed_roots"
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return "refused: bad path"
    if path != os.path.normpath(path):
        return "refused: path not normalised (. .. // or trailing /)"
    if os.path.realpath(path) != path or len([p for p in path.split("/") if p]) < 4:
        return "refused: symlink in path or too shallow"
    if not cl._inside(path, roots):
        return "refused: outside allowed_roots"
    if not _below(path, roots):
        return "refused: rule path is an allowed root itself (use a cache sub-directory)"
    if _never(path):
        return "refused: never-touch path"
    if ctx.is_protected(path):
        return "refused: protected path"
    age = cl._num(rule.get("max_age_days"), 1, 3650)
    if age is None:
        return "nothing: max_age_days must be 1..3650"
    if rule.get("by", "mtime") != "mtime":
        return "refused: only by=mtime (atime is reset by readers)"
    if "files_only" in rule and not isinstance(rule["files_only"], bool):
        return "refused: files_only must be true/false"
    cont, url, gate = rule.get("container"), rule.get("health_url"), rule.get("gate")
    if cont is not None and not (isinstance(cont, str) and cl._NAME.fullmatch(cont)):
        return "refused: bad container name"
    if url is not None and not (cont and _loopback_url(url)):
        return "refused: health_url needs a container and a loopback http(s) URL"
    if gate is not None and not (isinstance(gate, str) and re.fullmatch(r"[a-z_]+", gate)):
        return "refused: bad gate"
    if cont is None and path.startswith("/volume1/docker/"):
        return "refused: an app dir under /volume1/docker needs a container"
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return "nothing: not a directory"
    except OSError:
        return "nothing: path missing"
    # an app dir with a running app must be health-guarded: any container that mounts it (or something inside it) is named
    mounts = _mount_map()
    if mounts is None:
        return "refused: docker mounts unknown (fail closed)"
    mounters = sorted(n for n, srcs in mounts.items() if any(s != "/" and cl._overlap(s, path) for s in srcs))
    if mounters and cont not in mounters:
        return f"refused: mounted by {','.join(mounters[:3])}: set container to it so its health guards the trim"
    srcs = frozenset(s for v in mounts.values() for s in v if s != "/")
    return _Rule(name, path, age * 86400, rule.get("files_only", True) is True, rule.get("root_owned_ok") is True,
                 cont, url, gate, srcs)


def _walk_cache(rule: _Rule, now: float, limit: int, budget_s: float) -> _Scan:
    """Select regular files whose MTIME is older than the rule's age (never atime: readers reset it). Symlinks are
    counted and skipped, mount points are not entered, files modified in the last 10 minutes are kept, root-owned files
    are skipped unless root_owned_ok. Empty old directories are collected only when files_only is false.
    Database-looking names (*.db, *.sqlite*, *-wal, *-shm, *.bak, *.vscdb, ...) are listed whatever their age: a tree that
    holds one is not a disposable cache and the caller refuses the whole rule.
    Hitting the entry limit or the time budget marks the scan incomplete and the rule refuses (fail closed)."""
    out, n, t0 = _Scan(), 0, _mono()
    dev = os.lstat(rule.path).st_dev
    stack: list[tuple[str, str, tuple | None]] = [("", rule.path, None)]
    while stack:
        rel, d, info = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            out.unreadable += 1
            continue
        cnt = 0
        with it:
            for e in it:
                cnt += 1
                n += 1
                if n > limit or _mono() - t0 > budget_s:
                    out.complete = False
                    return out
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                crel = f"{rel}/{e.name}" if rel else e.name
                if stat.S_ISLNK(st.st_mode):
                    out.links += 1
                elif stat.S_ISDIR(st.st_mode):
                    if st.st_dev == dev:
                        stack.append((crel, os.path.join(d, e.name), (rel, e.name, st.st_ino, st.st_dev, st.st_mtime, st.st_mtime_ns)))
                elif stat.S_ISREG(st.st_mode):
                    if _DBISH.search(e.name) and len(out.dbfiles) < 3:
                        out.dbfiles.append(crel)
                    age = now - st.st_mtime
                    out.files += 1
                    out.bytes += st.st_size
                    if age < RECENT_S:
                        out.recent += 1
                    elif age <= rule.max_age_s:
                        out.young += 1
                    elif st.st_uid == _ROOT_UID and not rule.root_owned_ok:
                        out.skip_root += 1
                    else:
                        out.cands.setdefault(rel, []).append((e.name, st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size))
                        out.sel_files += 1
                        out.sel_bytes += st.st_size
        if cnt == 0 and info and not rule.files_only and now - info[4] > rule.max_age_s and now - info[4] > RECENT_S:
            out.empty.setdefault(info[0], []).append((info[1], info[2], info[3], info[5]))
    return out


def _http_ok(url: str) -> bool:
    """GET a loopback URL: 2xx/3xx is healthy. Proxies are ignored; any error is unhealthy."""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r:
            return 200 <= r.status < 400
    except Exception as exc:  # noqa: BLE001
        if cl._is_timeout(exc):
            raise
        return False


def _container_state(name: str) -> tuple[str, str] | None:
    """(State.Status, Health.Status or 'none') of a container, None if docker cannot say."""
    r = sh(["docker", "inspect", "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", name], timeout=20)
    p = r.stdout.strip().split("|")
    return (p[0], p[1]) if r.returncode == 0 and len(p) == 2 and all(p) else None


def _healthy(container: str, url: str | None, wait_s: float) -> tuple[bool, str]:
    """Running, not unhealthy/starting, and (when a health_url is set) answering. Polls up to wait_s: an app that just
    lost a cache may need a few seconds. Unknown (docker failed) is unhealthy. This is a LIVENESS probe: an app that is
    up but misbehaves because of the missing cache files is not detected, hence the small canary batch before the bulk."""
    deadline = _mono() + wait_s
    while True:
        st = _container_state(container)
        why = ("docker inspect failed" if st is None else f"state {st[0]}" if st[0] != "running"
               else f"health {st[1]}" if st[1] not in ("healthy", "none") else "health_url failed" if url and not _http_ok(url) else "")
        if not why:
            return True, "ok"
        if _mono() >= deadline:
            return False, why
        _sleep(2)


def _pack(groups: list[tuple[str, list]], batch_n: int, first_n: int | None = None) -> list[list[tuple[str, list]]]:
    """Pack (rel dir, items) groups into batches of at most `batch_n` items (the FIRST batch at most `first_n`: the
    canary); one huge directory is split. One batch is ONE ctx.act, so a 100k-file cache costs ~50 actions (audit rows,
    per-run item cap), not 100k."""
    out, cur, n = [], [], 0
    cap = max(1, min(first_n, batch_n)) if first_n else batch_n
    for rel, items in groups:
        i = 0
        while i < len(items):
            part = items[i:i + cap - n]
            cur.append((rel, part))
            n += len(part)
            i += len(part)
            if n >= cap:
                out.append(cur)
                cur, n, cap = [], 0, batch_n
    if cur:
        out.append(cur)
    return out


def _walk_down(fd0: int, rel: str) -> int:
    """A NEW fd on root/rel, one component at a time with O_NOFOLLOW (a swapped-in symlink fails the open)."""
    fd = os.dup(fd0)
    try:
        for p in [x for x in rel.split("/") if x]:
            nfd = os.open(p, _DFLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _err(acc: _Done, what: str, exc: OSError) -> None:
    acc.n_err += 1
    acc.first_err = acc.first_err or f"{what}: {exc.strerror or exc.errno}"


def _trim_batch(root: str, root_id: tuple, kind: str, groups: list[tuple[str, list]], holders: _Holders, acc: _Done,
                keep_mounts: frozenset = frozenset()) -> None:
    """Delete one batch into `acc`: kind "f" = files [(name, ino, dev, mtime_ns, size)], kind "d" = empty old directories
    [(name, ino, dev, mtime_ns)], per rel dir. Skipped silently (a benign race, never an error): vanished or changed or
    non-regular files, a directory that is gone, became a symlink, was touched since the scan, is a container bind-mount
    source, or is held (cwd/fd) by a process; anything an open fd or mapping holds now. A per-entry OSError (EPERM, EROFS,
    an immutable file) is counted and the batch goes on, so earlier deletions are never lost from the books; past
    MAX_UNLINK_ERRORS the batch stops."""
    fd0 = _open_dir(root)
    try:
        st0 = os.fstat(fd0)
        if (st0.st_dev, st0.st_ino) != root_id:
            raise cl._Changed("root changed")
        held, complete = holders.get()
        if not complete:
            raise RuntimeError("open-file proof incomplete")
        for rel, items in groups:
            try:
                fd = _walk_down(fd0, rel)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                    acc.skipped += len(items)
                else:
                    _err(acc, rel or ".", exc)
                continue
            try:
                for it in items:
                    if acc.n_err >= MAX_UNLINK_ERRORS:
                        return
                    try:
                        st = os.stat(it[0], dir_fd=fd, follow_symlinks=False)
                        if kind == "f":
                            if stat.S_ISREG(st.st_mode) and (st.st_ino, st.st_dev, st.st_mtime_ns) == (it[1], it[2], it[3]) \
                                    and (st.st_dev, st.st_ino) not in held:
                                os.unlink(it[0], dir_fd=fd)
                                acc.freed += st.st_size
                                acc.removed += 1
                                acc.gone.append((rel, it[0], st.st_size, st.st_mtime_ns))
                            else:
                                acc.skipped += 1
                        else:
                            dpath = os.path.join(root, rel, it[0]) if rel else os.path.join(root, it[0])
                            if stat.S_ISDIR(st.st_mode) and (st.st_ino, st.st_dev, st.st_mtime_ns) == (it[1], it[2], it[3]) \
                                    and (st.st_dev, st.st_ino) not in held \
                                    and not _is_mount_src(dpath, keep_mounts):
                                os.rmdir(it[0], dir_fd=fd)       # the kernel refuses a non-empty directory
                                acc.removed += 1
                            else:
                                acc.skipped += 1
                    except OSError as exc:
                        if exc.errno in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                            acc.skipped += 1
                        else:
                            _err(acc, os.path.join(rel, it[0]), exc)
            finally:
                os.close(fd)
    finally:
        os.close(fd0)


class _Manifest:
    """Append-only JSONL of every file app_cache_trim REALLY unlinked (rule, rel dir, name, size, mtime): after a bad day
    the 131k removed names can be reconstructed. The tool's own state (STATE_DIR/manifests/app_cache_trim.<ts>.jsonl, 0600);
    manifests older than `keep_days` are dropped by the timestamp in their name."""

    def __init__(self, now: float):
        self.dir = core.STATE_DIR / "manifests"
        self.path = self.dir / f"app_cache_trim.{int(now)}.jsonl"
        self.f = None
        self.failed = False

    def open(self) -> bool:
        if self.f is None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.f = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600), "a")
            except OSError:
                self.failed = True
                return False
        return True

    def write(self, rule: str, gone: list[tuple]) -> None:
        if not gone:
            return
        try:
            for rel, name, size, mtime_ns in gone:
                self.f.write(json.dumps({"rule": rule, "dir": rel, "name": name, "size": size, "mtime": mtime_ns // 10**9}) + "\n")
            self.f.flush()
            os.fsync(self.f.fileno())
        except (OSError, AttributeError):
            self.failed = True

    def close(self, now: float, keep_days: float, trim: bool) -> None:
        if self.f is not None:
            self.f.close()
        if not trim:                                            # a dry-run changes nothing, not even its own old files
            return
        try:
            for p in self.dir.iterdir():
                m = re.fullmatch(r"app_cache_trim\.(\d+)\.jsonl", p.name)
                if m and int(m.group(1)) < now - keep_days * 86400 and p != self.path:
                    p.unlink()
        except OSError:
            pass


@task("app_cache_trim", klass="C1", tier="daily", title="App cache trim", timeout=1800, needs_root=True)
def app_cache_trim(ctx: Ctx) -> Result:
    """Trim disposable app caches by file MTIME (never atime), with a container health safeguard.

    Rule: {name, path, max_age_days, by="mtime", files_only=true, root_owned_ok=false, container, health_url, gate}.
    The path must be canonical and STRICTLY below an `allowed_roots` entry (give the exact cache dir, never an app's
    config/data dir), must not be a never-touch path, and must not hold database files (*.db, *.sqlite*, *-wal, *-shm,
    *.bak, *.vscdb): such a tree is refused as a whole. A container that bind-mounts the path (or part of it) must be named
    as `container`; an app dir under /volume1/docker needs one anyway.
    In-use proof per file: older than the cutoff by mtime, regular file (no symlink), unchanged since the scan, and
    no process holds its inode open/mapped (one /proc snapshot per rule, retaken every 60 s). Empty directories (only with
    files_only=false) must also be unchanged since the scan, not held by a process and not a container bind-mount source.
    Per rule with a `container`: it must be healthy BEFORE (else the rule is skipped); the first applied batch is a small
    CANARY (`canary_files`), after which the app gets `settle_s` and is probed; then re-probed every `check_every`
    batches or `check_s` seconds, and after the rule. If it turns unhealthy the remaining batches and ALL remaining rules
    are aborted and the run warns. The probe is liveness only (state/health/`health_url`).
    Every unlink is recorded in a manifest. A per-file OSError is counted, not fatal. A rule that is refused, skipped as
    unhealthy, scan-limited or aborted `warn_after_runs` runs in a row, or that selects files yet removes none
    `STALLED_RUNS` times in a row, makes the task warn. Needs root when the cache is root-owned (container user)."""
    rules, raw_roots = ctx.opt("rules", []), ctx.opt("allowed_roots", [])
    roots = [os.path.realpath(r) for r in raw_roots if isinstance(r, str) and os.path.isabs(r)] \
        if isinstance(raw_roots, list) else []
    roots = [r for r in roots if r != "/"]
    nums = _cfg_nums(ctx, ("scan_limit", 2_000_000, 1000, 10_000_000), ("scan_budget_s", 300, 1, 1500),
                    ("batch_files", 2000, 1, 100_000), ("check_every", 5, 1, 100_000),
                    ("settle_s", 5, 0, 120), ("health_wait_s", 30, 0, 300), ("canary_files", 200, 1, 100_000),
                    ("check_s", 10, 1, 3600), ("warn_after_runs", 2, 1, 365), ("manifest_keep_days", 30, 1, 3650))
    if nums is None:
        return cl._skipped("bad scan_limit/scan_budget_s/batch_files/check_every/settle_s/health_wait_s/canary_files/check_s/"
                           "warn_after_runs/manifest_keep_days config: nothing done")
    limit, budget, batch_n, check_every, settle, hwait = (int(nums[0]), nums[1], int(nums[2]), int(nums[3]), nums[4], nums[5])
    canary_n, check_s, warn_runs, keep_days = int(nums[6]), nums[7], int(nums[8]), nums[9]
    if not isinstance(rules, list) or not rules:
        return Result("ok", "no cache rules configured", {"mode": "report", "selected": 0, "rules": 0})
    apply = ctx.apply                                  # act() clears ctx.apply when PAUSE appears mid-run: word the result from this
    acts, rows, aborted, paused, partial = cl._Acts(ctx), [], "", False, False
    tot = dict.fromkeys(("files", "would", "dirs", "empty", "dirs_removed", "would_dirs", "needs_root", "open", "refused",
                         "skipped", "errors"), 0)
    st_rules = ctx.state.setdefault("rules", {})
    book: dict[str, dict] = {}                         # rule name -> {"bad": bool, "stalled": bool|None}: health counters
    manifest, first_err = _Manifest(ctx.now), ""
    for i, raw in enumerate(rules):
        rname = str((raw.get("name") if isinstance(raw, dict) else None) or f"rule{i}")[:40]
        row = {"name": rname, "size": "0 B", "state": ""}
        rows.append(row)
        if aborted or paused:
            row["state"] = f"skipped: {'PAUSED' if paused else 'aborted'} ({aborted or 'PAUSE file'})"
            continue
        rule = _check_cache_rule(ctx, raw, roots)
        if isinstance(rule, str):
            row["state"] = rule
            tot["refused"] += rule.startswith("refused")
            book[rname] = {"bad": True, "stalled": None}
            if rule.startswith("refused"):
                core.audit(ctx.name, "cache-rule", str(raw.get("path") if isinstance(raw, dict) else raw), 0, rule)
            continue
        if rule.gate and cl._busy(rule.gate)[0]:
            row["state"] = f"skipped: {rule.gate} busy"           # a deferral, not a fault: no counter moves
            continue
        if rule.container:                              # in-use proof 1: the app is healthy before we touch anything
            ok, why = _healthy(rule.container, rule.health_url, 0)
            if not ok:
                row["state"] = f"skipped: {rule.container} not healthy ({why})"
                book[rname] = {"bad": True, "stalled": None}
                continue
        scan = _walk_cache(rule, ctx.now, limit, budget)
        if not scan.complete:
            row["state"] = "refused: scan limit/time reached"
            tot["refused"] += 1
            book[rname] = {"bad": True, "stalled": None}
            continue
        if scan.dbfiles:                                # a tree with databases in it is not a disposable cache
            row["state"] = f"refused: tree holds database files ({', '.join(scan.dbfiles[:3])})"
            tot["refused"] += 1
            book[rname] = {"bad": True, "stalled": None}
            core.audit(ctx.name, "cache-rule", rule.path, 0, row["state"])
            continue
        want = ({(f[2], f[1]) for fl in scan.cands.values() for f in fl}          # (st_dev, st_ino) of everything we may touch
                | {(f[2], f[1]) for dl in scan.empty.values() for f in dl})
        holders = _Holders(want)
        held, complete = holders.get(force=True)
        partial = partial or (not complete and bool(want))
        if apply and not complete and want:             # in-use proof 2 needs root to see every process
            row["state"] = "refused: needs root (open-file proof incomplete)"
            tot["refused"] += 1
            book[rname] = {"bad": True, "stalled": None}
            continue
        if apply and not manifest.open():               # no trail of what is deleted, no deletion
            row["state"] = "refused: cannot write the deletion manifest"
            tot["refused"] += 1
            book[rname] = {"bad": True, "stalled": None}
            continue
        root_id = _ids_of(rule.path) or (0, 0)
        done0, open_n, mnt_n, rm_files, err_rule, stop = acts.n["done"], 0, 0, 0, "", ""
        by_kind: dict[str, list[tuple[str, list]]] = {"f": [], "d": []}
        for kind, table in (("f", scan.cands), ("d", scan.empty)):
            for rel in sorted(table):
                dpath = os.path.join(rule.path, rel) if rel else rule.path
                items = [f for f in table[rel] if (f[2], f[1]) not in held]
                open_n += len(table[rel]) - len(items)
                if kind == "d":                         # never an empty dir that a container bind-mounts (or contains a mount)
                    keep = [f for f in items if not _is_mount_src(os.path.join(dpath, f[0]), rule.mount_srcs)]
                    mnt_n += len(items) - len(keep)
                    items = keep
                if not items:
                    continue
                size = sum(f[4] for f in items) if kind == "f" else 0
                if ctx.is_protected(dpath):             # deeper components may match a pattern the root did not
                    acts._note("protected", f"{rname}: {rel or '.'}", size)
                    continue
                if _euid() != 0 and not os.access(dpath, os.W_OK | os.X_OK):
                    tot["needs_root"] += 1              # root-owned dir: apply degrades to report for it
                    if apply:
                        acts._note("refused", f"{rname}: {rel or '.'} [needs root]", size)
                        continue
                by_kind[kind].append((rel, items))
        batches = [(k, g) for k in ("f", "d") for g in _pack(by_kind[k], batch_n, canary_n if k == "f" and rule.container else None)]
        last_chk = _mono()
        for n_b, (kind, grp) in enumerate(batches):
            nfiles, ndirs = sum(len(it) for _r, it in grp), len(grp)
            size = sum(f[4] for _r, it in grp for f in it) if kind == "f" else 0
            label = (f"{rname}: {nfiles} files in {ndirs} dirs" if kind == "f"
                     else f"{rname}: {nfiles} empty dirs") + f" ({grp[0][0] or '.'}..)"
            target = os.path.join(rule.path, grp[0][0]) if grp[0][0] else rule.path    # per batch: one stuck batch must not back off the rule
            acc = _Done()

            def fn(rule=rule, kind=kind, grp=grp, holders=holders, root_id=root_id, acc=acc, rname=rname) -> int:
                keep = frozenset()
                if kind == "d":                         # containers may have started since validation: ask again
                    mm = _mount_map()
                    if mm is None:
                        raise RuntimeError("docker mounts unknown: empty dirs kept")
                    keep = frozenset(s for v in mm.values() for s in v if s != "/")
                try:
                    _trim_batch(rule.path, root_id, kind, grp, holders, acc, keep)
                finally:                                # partial batches count too
                    manifest.write(rname, acc.gone)
                    tot["files" if kind == "f" else "dirs_removed"] += acc.removed
                    tot["skipped"] += acc.skipped
                    tot["errors"] += acc.n_err
                return acc.freed

            state = acts.run("cache-trim" if kind == "f" else "cache-rmdir", target, size, fn, label=label)
            if state in ("would", "done"):
                tot["dirs" if kind == "f" else "empty"] += ndirs if kind == "f" else nfiles
                if state == "would":
                    tot["would" if kind == "f" else "would_dirs"] += nfiles
                else:
                    rm_files += acc.removed if kind == "f" else 0
                    first_err = first_err or acc.first_err
                    if acc.n_err >= MAX_UNLINK_ERRORS:
                        err_rule = f"{acc.n_err} unlink errors, stopped ({acc.first_err})"
                    if manifest.failed:
                        err_rule = err_rule or "manifest write failed, stopped"
                    n_done = acts.n["done"] - done0
                    if rule.container and (n_done == 1 or n_done % check_every == 0 or _mono() - last_chk >= check_s):
                        if n_done == 1:
                            _sleep(settle)              # canary: give the app time to trip over the missing files first
                        ok, why = _healthy(rule.container, rule.health_url, hwait)
                        last_chk = _mono()
                        if not ok:
                            aborted = f"{rule.container} {why} after {rname}"
                            acts.n["capped"] += len(batches) - n_b - 1
                            break
                    if err_rule:
                        stop = err_rule
                        break
            if apply and not ctx.apply:                 # PAUSE appeared mid-run (act() cleared apply): stop, never go on as dry-run
                paused = True
                break
        tot["open"] += open_n
        row["size"] = human(scan.sel_bytes)
        row["state"] = (f"{scan.sel_files} of {scan.files} files > {rule.max_age_s / 86400:g} d by mtime"
                        f" ({scan.young} newer, {scan.recent} recent"
                        + (f", {scan.skip_root} root-owned skipped" if scan.skip_root else "")
                        + (f", {open_n} open skipped" if open_n else "")
                        + (f", {mnt_n} mount-source dirs kept" if mnt_n else "")
                        + (f", {scan.links} symlinks skipped" if scan.links else "")
                        + (f", {scan.unreadable} dirs unreadable (need root)" if scan.unreadable else "") + ")")
        if apply:
            row["state"] += f"; {rm_files} removed"
        if paused:
            row["state"] += "; PAUSED mid-run"
        if stop:
            row["state"] += f"; STOPPED: {stop}"
        if aborted:                                     # tripped by the mid-rule check above
            row["state"] += f"; ABORTED: {aborted}"
        elif rule.container and apply and not paused and acts.n["done"] > done0:     # post-action health check
            _sleep(settle)
            ok, why = _healthy(rule.container, rule.health_url, hwait)
            row["state"] += f"; {rule.container} {'healthy' if ok else why}"
            if not ok:
                aborted = f"{rule.container} {why} after {rname}"
                row["state"] += "; ABORTED"
        elif rule.container:
            row["state"] += f"; {rule.container} healthy before"
        book[rname] = {"bad": bool(aborted or stop), "stalled": (rm_files == 0 and scan.sel_files > 0) if apply and not paused else None}
        st_rules[rname] = {**(st_rules.get(rname) if isinstance(st_rules.get(rname), dict) else {}),
                           "t": ctx.now, "files": scan.sel_files, "bytes": scan.sel_bytes, "applied": apply}
    manifest.close(ctx.now, keep_days, apply)
    # health counters across runs: a rule that silently stops working must become visible (the cache would grow back unseen)
    names = {str((r.get("name") if isinstance(r, dict) else None) or f"rule{k}")[:40] for k, r in enumerate(rules)}
    for gone in [k for k in st_rules if k not in names]:
        del st_rules[gone]
    for rname, b in book.items():
        s = st_rules.setdefault(rname, {})
        s["bad_runs"] = s.get("bad_runs", 0) + 1 if b["bad"] else 0
        if b["stalled"] is not None:
            s["stalled_runs"] = s.get("stalled_runs", 0) + 1 if b["stalled"] else 0
    sick = [f"{n} {'failing' if s.get('bad_runs', 0) >= warn_runs else 'stalled'} {max(s.get('bad_runs', 0), s.get('stalled_runs', 0))}x"
            for n, s in sorted(st_rules.items()) if s.get("bad_runs", 0) >= warn_runs or s.get("stalled_runs", 0) >= STALLED_RUNS]
    n_files, n_empty = (tot["files"], tot["dirs_removed"]) if apply else (tot["would"], tot["would_dirs"])
    res = acts.result("batches", {"rules": len(rules), "files": n_files, "dirs": tot["dirs"], "empty_dirs": n_empty,
                                  "needs_root": tot["needs_root"], "open_skipped": tot["open"], "aborted": aborted,
                                  "skipped": tot["skipped"], "unlink_errors": tot["errors"], "paused": paused,
                                  "sick_rules": len(sick), "proof": "partial (not root)" if partial else "complete",
                                  "manifest": str(manifest.path) if apply and manifest.f is not None else ""})
    res.metrics["mode"] = "apply" if apply else "report"
    s = (f"freed {human(ctx.freed)} ({n_files} files" + (f", {tot['skipped']} skipped" if tot["skipped"] else "") if apply
         else f"report: would free {human(acts.bytes['would'])} ({n_files} files in {tot['dirs']} dirs")
    s += (f", {n_empty} empty dirs)" if n_empty else ")")
    for key, word in (("refused", "refused"), ("capped", "deferred by cap"), ("failed", "failed"), ("protected", "protected"),
                      ("backoff", "in backoff")):
        cnt = tot[key] if key == "refused" else acts.n[key]
        if cnt:
            s += f", {cnt} {word}"
    if tot["errors"]:
        s += f", {tot['errors']} unlink errors ({first_err})"
    if tot["needs_root"]:
        s += f", {tot['needs_root']} dirs need root" + ("" if apply else " to apply")
    if partial and not apply and acts.bytes["would"]:
        s += "; open-file proof incomplete (not root)"
    if sick:
        s += "; WARN " + ", ".join(sick[:2])
    if paused:
        s = f"PAUSED mid-run: {s}"
    if aborted:
        s = f"ABORTED: {aborted}; {s}"
    if aborted or paused or sick or tot["errors"] or acts.n["failed"] or acts.n["backoff"]:
        res.status = "warn"
    res.summary = cl._ascii(s)
    res.items = (rows + res.items)[:12]
    return res


# =========================================================================== log_compress
_ROTATED = re.compile(r"^[^/]+?(?:\.[1-9]\d{0,2}|-20\d{6}(?:\.\d+)?|\.old)$")      # syslog.1, kern.log.2, app-20260927, x.old
_COMPRESSED = re.compile(r"\.(gz|bz2|xz|zst|lz4|lzma|zip|7z|tgz)(\.\d+)?$", re.I)
_TMP_SUFFIX = ".gz.hm-tmp"
_DEL_SUFFIX = ".hm-del"                         # the original, renamed out of the way for the final identity check
LOGROTATE_QUIET_S = 600                         # logrotate must have been idle this long before an original is removed
_boot_mono = time.monotonic                     # CLOCK_MONOTONIC: the clock systemd's *TimestampMonotonic properties use


def _logrotate_busy() -> str:
    """'' = logrotate.service is idle and has not finished a run in the last 10 minutes; otherwise the reason. A probe that
    fails or cannot be parsed is a reason too (fail closed): a rotator renaming files under us is how a live log is lost."""
    r = sh(["systemctl", "show", "logrotate.service", "-p", "ActiveState", "-p", "ExecMainExitTimestampMonotonic"], timeout=15)
    props = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
    if r.returncode != 0 or "ActiveState" not in props:
        return "cannot tell whether logrotate is running"
    if props["ActiveState"] in ("active", "activating", "reloading", "deactivating"):
        return "logrotate is running"
    last = props.get("ExecMainExitTimestampMonotonic", "")
    if not last.isdigit():
        return "logrotate last-run time unreadable"
    ago = _boot_mono() - int(last) / 1e6
    return f"logrotate finished {max(ago, 0):.0f} s ago" if int(last) and ago < LOGROTATE_QUIET_S else ""


@dataclass
class _Log:
    dirpath: str
    name: str
    ino: int
    dev: int
    size: int
    mtime_ns: int
    atime_ns: int
    uid: int
    gid: int
    mode: int


def _is_rotated_log(name: str) -> bool:
    return bool(_ROTATED.match(name)) and not _COMPRESSED.search(name) and not name.endswith(_TMP_SUFFIX)


def _scan_logs(root: str, depth_max: int, exclude: set[str], min_bytes: int, min_age_s: float,
               now: float) -> tuple[list[_Log], dict[str, int]]:
    """Rotated, uncompressed, big, old regular files below `root` (depth-limited, no symlinks, no mounts)."""
    out: list[_Log] = []
    c = {"live_large": 0, "small": 0, "young": 0, "linked": 0}
    dev = os.lstat(root).st_dev
    stack = [(root, 1)]
    while stack:
        d, depth = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if depth < depth_max and e.name not in exclude and st.st_dev == dev:
                        stack.append((os.path.join(d, e.name), depth + 1))
                    continue
                if not stat.S_ISREG(st.st_mode) or _COMPRESSED.search(e.name) or e.name.endswith(_TMP_SUFFIX):
                    continue
                if not _is_rotated_log(e.name):                  # the live log (or anything not rotated): never touched
                    c["live_large"] += st.st_size >= min_bytes
                elif st.st_size < min_bytes:
                    c["small"] += 1
                elif now - st.st_mtime < min_age_s:
                    c["young"] += 1
                elif st.st_nlink != 1:
                    c["linked"] += 1
                else:
                    out.append(_Log(d, e.name, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_atime_ns,
                                    st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)))
    return sorted(out, key=lambda x: (x.mtime_ns, x.dirpath, x.name)), c


def _gzip_log(lg: _Log, holders: _Holders) -> int:
    """gzip one rotated log next to itself: compress to a temp file, verify it with `gzip -t` (and the stored length),
    re-check that the source is unchanged and still closed (the slow /proc re-proofs come first), take the original out
    of the namespace with an atomic rename and verify its inode, publish the .gz (never overwriting) and unlink the
    original LAST. Any failure removes the temp file and puts the original back under its name. Returns bytes saved."""
    dfd = _open_dir(lg.dirpath)
    tmpname, dstname, delname = lg.name + _TMP_SUFFIX, lg.name + ".gz", lg.name + _DEL_SUFFIX
    tmp_made = False
    try:
        def same(st: os.stat_result) -> bool:
            return (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns) == (lg.ino, lg.dev, lg.size, lg.mtime_ns)

        if not same(os.stat(lg.name, dir_fd=dfd, follow_symlinks=False)):
            raise cl._Changed("changed since scan")
        full = os.path.join(lg.dirpath, lg.name)
        held, complete = holders.get(force=True)
        if (lg.dev, lg.ino) in held:
            raise cl._Changed("open by a process")
        kind, why = _second_opinion(full, refresh=True)                          # last-moment re-proof, fresh snapshots
        if kind == "used":
            raise cl._Changed(f"open: {why}")
        if kind == "unknown" or not complete:
            raise RuntimeError(f"open-file proof incomplete: {why or 'not root'}")
        sv = os.statvfs(lg.dirpath)
        if sv.f_bavail * sv.f_frsize < lg.size + 16 * cl.MIB:                  # worst case: incompressible
            raise RuntimeError("not enough free space")
        try:
            os.lstat(dstname, dir_fd=dfd)
            raise RuntimeError("target .gz exists: not overwriting")
        except FileNotFoundError:
            pass
        try:
            os.unlink(tmpname, dir_fd=dfd)                                       # stale temp of a crashed run (ours)
        except FileNotFoundError:
            pass
        sfd = os.open(lg.name, _FFLAGS, dir_fd=dfd)
        try:
            ofd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            tmp_made = True
            with os.fdopen(ofd, "wb") as out, os.fdopen(os.dup(sfd), "rb") as src:
                with gzip.GzipFile(filename=lg.name, mode="wb", fileobj=out, compresslevel=6, mtime=int(lg.mtime_ns // 10**9)) as gz:
                    while chunk := src.read(1 << 20):
                        gz.write(chunk)
                try:
                    os.fchown(out.fileno(), lg.uid, lg.gid)
                except PermissionError:
                    if (lg.uid, lg.gid) != (os.getuid(), os.getgid()):
                        raise
                os.fchmod(out.fileno(), lg.mode)
                out.flush()
                os.fsync(out.fileno())
                gz_size = os.fstat(out.fileno()).st_size
            src_after = os.fstat(sfd)
        finally:
            os.close(sfd)                                                        # we must not look like a holder ourselves
        tmp_path = os.path.join(lg.dirpath, tmpname)
        chk = sh(["gzip", "-t", "--", tmp_path], timeout=900)
        if chk.returncode != 0:
            raise RuntimeError(f"gzip -t failed rc={chk.returncode}: original kept")
        with open(tmp_path, "rb") as f:                                          # ISIZE trailer = length mod 2^32
            f.seek(-4, os.SEEK_END)
            if struct.unpack("<I", f.read(4))[0] != lg.size & 0xFFFFFFFF:
                raise RuntimeError("gzip length check failed: original kept")
        # 1. the SLOW re-proofs first (two full /proc scans take ~0.5 s): opened meanwhile? a rotator at work? then leave it
        held, complete = holders.get(force=True)
        kind, why = _second_opinion(full, refresh=True)
        if (lg.dev, lg.ino) in held or kind == "used":
            raise cl._Changed("opened by a process while compressing")
        if kind == "unknown" or not complete:
            raise RuntimeError("open-file proof incomplete after compressing")
        busy = _logrotate_busy()
        if busy:
            raise cl._Changed(busy)
        # 2. then, with nothing slow left, the identity check and the atomic take-out: the original is renamed to
        # <name>.hm-del and fstat'ed there, so what is finally unlinked is provably the inode that was compressed, never
        # whatever a rotator moved to <name> meanwhile
        if not same(src_after) or not same(os.stat(lg.name, dir_fd=dfd, follow_symlinks=False)):
            raise cl._Changed("source changed while compressing")
        try:
            os.lstat(delname, dir_fd=dfd)
            raise RuntimeError(f"stale {delname} from an earlier run: resolve by hand")
        except FileNotFoundError:
            pass
        os.rename(lg.name, delname, src_dir_fd=dfd, dst_dir_fd=dfd)
        published = False

        def put_back() -> bool:                                                  # give <name> back; never over a newer file
            try:
                os.link(delname, lg.name, src_dir_fd=dfd, dst_dir_fd=dfd)
                os.unlink(delname, dir_fd=dfd)
                return True
            except OSError:
                return False
        try:
            if not same(os.stat(delname, dir_fd=dfd, follow_symlinks=False)):
                raise cl._Changed("rotated under us (inode changed)")
            os.link(tmpname, dstname, src_dir_fd=dfd, dst_dir_fd=dfd)           # fails if dst appeared: no overwrite
            published, tmp_made = True, False
            os.unlink(tmpname, dir_fd=dfd)
            os.utime(dstname, ns=(lg.atime_ns, lg.mtime_ns), dir_fd=dfd, follow_symlinks=False)
        except BaseException as exc:
            if published:                                                        # our own .gz must not outlive a restored original
                try:
                    os.unlink(dstname, dir_fd=dfd)
                except OSError:
                    pass
            if not put_back() and isinstance(exc, Exception):                    # a benign _Changed becomes loud: data sits under delname
                raise RuntimeError(f"original kept as {delname} (put-back failed: {exc})") from exc
            raise
        os.unlink(delname, dir_fd=dfd)                                           # the original goes last
        return max(lg.size - gz_size, 0)
    finally:
        if tmp_made:
            try:
                os.unlink(tmpname, dir_fd=dfd)
            except OSError:
                pass
        os.close(dfd)


@task("log_compress", klass="C1", tier="daily", title="Compress rotated logs", timeout=1800, needs_root=True)
def log_compress(ctx: Ctx) -> Result:
    """gzip rotated, uncompressed logs bigger than `min_mib` and older than `min_age_days` (syslog.1 558 MB -> 48 MB).

    In-use proof per file: the NAME says rotated (syslog.1, kern.log.2, app-20260927, x.old; never the live
    `syslog`), mtime older than a day, a single hard link, no process holds its inode (checked before and again after
    compressing), logrotate.service is idle and has not finished a run in the last 10 minutes (checked at the start and
    again right before the original is removed), and the archive passes `gzip -t` plus a length check. The original is
    removed only after that, by an atomic rename to `<name>.hm-del` whose inode is verified (so a rotator that renamed
    files meanwhile can never cost the new live log) and never when the .gz already exists.
    Needs root: without it apply is refused, the dry-run says so."""
    raw_roots = ctx.opt("roots", ["/var/log"])
    roots = [r for r in raw_roots if isinstance(r, str) and os.path.isabs(r) and r != "/"] if isinstance(raw_roots, list) else []
    nums = _cfg_nums(ctx, ("min_mib", 50, 1, 1_000_000), ("min_age_days", 1, 0.01, 3650), ("max_depth", 2, 1, 6))
    excl = ctx.opt("exclude_dirs", ["journal"])
    if nums is None or not roots or not isinstance(excl, list):
        return cl._skipped("bad roots/min_mib/min_age_days/max_depth config: nothing done")
    min_b, age_s, depth = int(nums[0] * cl.MIB), nums[1] * 86400, int(nums[2])
    if ctx.apply and (why := _logrotate_busy()):
        return cl._skipped(f"{why}: compression deferred")          # re-checked right before every original is removed
    acts, found, counts, bad = cl._Acts(ctx), [], {"live_large": 0, "small": 0, "young": 0, "linked": 0}, 0
    for root in roots:
        if not _canonical(root) or _never(root) or not os.path.isdir(root):
            bad += 1
            continue
        logs, c = _scan_logs(root, depth, {str(x) for x in excl}, min_b, age_s, ctx.now)
        found += logs
        for k in counts:
            counts[k] += c[k]
    want = {(lg.dev, lg.ino) for lg in found}
    holders = _Holders(want)
    held, complete = holders.get(force=True)
    in_use, refused_root, partial = 0, 0, not complete
    items: list[dict] = []
    for lg in found:
        path = os.path.join(lg.dirpath, lg.name)
        kind, why = _file_proof(path, (lg.dev, lg.ino), held, complete)
        if kind == "used":
            in_use += 1
            items.append({"name": lg.name[:60], "size": human(lg.size), "state": f"kept: {why}"})
            continue
        partial = partial or kind == "unknown"
        if kind == "unknown" and ctx.apply:
            refused_root += 1
            items.append({"name": lg.name[:60], "size": human(lg.size), "state": "refused: needs root (open-file proof incomplete)"})
            continue
        acts.run("gzip-log", path, lg.size, lambda lg=lg: _gzip_log(lg, holders), label=lg.name)
    res = acts.result("logs", {"roots": len(roots), "candidates": len(found), "open_kept": in_use,
                               "live_large": counts["live_large"], "needs_root": refused_root,
                               "proof": "partial (not root)" if partial else "complete"})
    if ctx.apply:
        s = f"compressed {acts.n['done']} logs, freed {human(ctx.freed)}"
    else:
        s = f"report: would compress {acts.n['would']} logs ({human(acts.bytes['would'])} raw)"
        if partial and acts.n["would"]:
            s += "; open-file proof incomplete (not root)"
    for key, word in (("protected", "protected"), ("failed", "failed"), ("capped", "deferred by cap"), ("gone", "vanished/changed")):
        if acts.n[key]:
            s += f", {acts.n[key]} {word}"
    for cnt, word in ((in_use, "open kept"), (refused_root, "need root"), (bad, "roots refused")):
        if cnt:
            s += f", {cnt} {word}"
    if acts.errors:
        s += f"; {acts.errors[0]}"
    if not found:
        s = f"no rotated log > {nums[0]:g} MiB older than {nums[1]:g} d ({counts['live_large']} live large left alone)"
    res.summary = cl._ascii(s)
    res.items = (items + res.items)[:12]
    return res


# =========================================================================== dangling_images
def _parse_ts(s: str) -> float | None:
    """RFC3339 as docker prints it (nanoseconds, Z or an offset) -> epoch; None for unset (year 1) or garbage."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", s.strip())
    if not m:
        return None
    try:
        t = datetime.fromisoformat(f"{m.group(1)}.{(m.group(2) or '0')[:6]:0<6}{'+00:00' if m.group(3) == 'Z' else m.group(3)}")
    except ValueError:
        return None
    return t.timestamp() if t.year >= 2000 else None


def _dangling() -> list[dict] | None:
    """Dangling images (no tag at all): [{id, created, size, digests}], None when docker cannot say or output is odd.
    `size` is the VIRTUAL size (informational: layers shared with other images are counted); see _unique_sizes for freed bytes.
    Odd = any line that does not parse, or a different number of lines than ids: fail closed."""
    r = sh(["docker", "image", "ls", "--filter", "dangling=true", "--no-trunc", "-q"], timeout=30)
    if r.returncode != 0:
        return None
    ids = sorted(set(r.stdout.split()))
    if not all(cl._IMG_ID.fullmatch(i) for i in ids):
        return None
    out: list[dict] = []
    for k in range(0, len(ids), 50):
        part = ids[k:k + 50]
        q = sh(["docker", "image", "inspect", "--format",
                "{{.Id}}|{{.Created}}|{{.Size}}|{{len .RepoTags}}|{{json .RepoDigests}}", *part], timeout=60)
        lines = q.stdout.splitlines()
        if q.returncode != 0 or len(lines) != len(part):
            return None
        for ln in lines:
            f = ln.split("|", 4)
            try:
                created, size, tags, digests = _parse_ts(f[1]), int(f[2]), int(f[3]), json.loads(f[4])
            except (IndexError, ValueError):
                return None
            digests = [] if digests is None else digests              # Go prints a nil slice as null
            if f[0] not in part or created is None or tags != 0 or not isinstance(digests, list):
                return None                              # tagged (not dangling after all) or unparsable: keep it
            out.append({"id": f[0], "created": created, "size": size, "digests": [str(x) for x in digests]})
    return out


_ODD = object()


def _ledger_seen(ledger: dict, iid: str) -> Any:
    """Newest ledger last_seen for an image (keys may be full ids or 12+ char prefixes); None = never; _ODD = unreadable."""
    best, full = None, cl._norm_id(iid)
    for k, v in ledger["images"].items():
        kn = cl._norm_id(str(k))
        if len(kn) < 12 or not full.startswith(kn):
            continue
        ls = v.get("last_seen") if isinstance(v, dict) else _ODD
        if ls is _ODD or isinstance(ls, bool) or (ls is not None and not isinstance(ls, (int, float))):
            return _ODD
        if ls is not None:
            best = ls if best is None else max(best, ls)
    return best


def _unique_sizes() -> dict[str, tuple[int, bool]] | None:
    """{image id: (UniqueSize bytes, used-by-a-container)} from `docker system df -v`: UniqueSize is what removing the image
    really frees (`inspect .Size` is the virtual size and counts layers shared with images that stay). None = unknown."""
    rows = cl._image_inventory()
    if rows is None:
        return None
    out: dict[str, tuple[int, bool]] = {}
    for r in rows:
        iid = r.get("ID")
        if not isinstance(iid, str) or not cl._IMG_ID.fullmatch(iid):
            continue
        uniq = cl._parse_size(r.get("UniqueSize"))
        used = str(r.get("Containers", "")).strip() != "0"           # anything but a literal "0" counts as referenced
        p = out.get(iid)
        size = -1 if uniq is None or (p and p[0] < 0) else max(uniq, p[0] if p else 0)    # unparsable size: unknown, image kept
        out[iid] = (size, used or bool(p and p[1]))
    return out


@task("dangling_images", klass="C1", tier="daily", title="Dangling Docker images", timeout=600)
def dangling_images(ctx: Ctx) -> Result:
    """`docker image rm <id>` (never -f, never -a, never `system prune`) for DANGLING images only
    (`docker image prune -f --filter until=24h` semantics, per image so the dry-run is exact).

    In-use proof per image, ALL of: it has no tag; NO container, running or exited, references it now (and
    `docker system df` agrees); it was created more than `min_age_hours` ago; THIS task has itself seen it unreferenced for
    `min_age_hours` (first-seen clock: `Created` is the upstream build date, not the arrival date, so a just-pulled or
    just-loaded image waiting for `compose up` is never eligible on its first sighting); and the image ledger (fresh,
    not younger than the window) has not seen it in use within that window. No usable ledger = nothing selected. An image
    with no RepoDigests (a local build or `docker load`: it cannot be pulled again) needs `local_build_days` (7) instead.
    The estimate and `freed` are the image's UniqueSize. Before EVERY removal the docker_build gate and the container
    references are read again. Tagged unused images stay with `docker_images`."""
    nums = _cfg_nums(ctx, ("min_age_hours", 24, 1, 24 * 365), ("ledger_max_age_hours", 48, 1, 24 * 365),
                     ("local_build_days", 7, 1, 3650))
    if nums is None:
        return cl._skipped("bad min_age_hours/ledger_max_age_hours/local_build_days config: nothing done")
    hours, led_max, local_h = nums[0], nums[1], nums[2] * 24
    busy, why = cl._busy("docker_build")
    if busy:
        return cl._skipped(f"docker build active, image prune deferred ({why})")
    imgs, refs, uniq = _dangling(), cl._referenced_images(), _unique_sizes()
    if imgs is None or refs is None or uniq is None:
        return cl._skipped("docker unavailable or unparsable: nothing selected")
    ids = {i["id"] for i in imgs}
    old = ctx.state.get("dangling_since")
    since = {k: v for k, v in (old if isinstance(old, dict) else {}).items() if k in ids and cl._num(v) is not None}
    for i in imgs:
        if i["id"] in refs or uniq.get(i["id"], (-1, False))[1]:
            since.pop(i["id"], None)
        else:
            since.setdefault(i["id"], ctx.now)               # this task's own clock: ALWAYS required, whatever the ledger says
    ctx.state["dangling_since"] = since
    ledger, led_why = cl._read_ledger(ctx.now, led_max)
    if ledger is None:
        return cl._skipped(f"{led_why}: nothing selected", dangling=len(imgs))
    led_age_h = (ctx.now - ledger["created"]) / 3600
    acts, kept = cl._Acts(ctx), {"referenced": 0, "young": 0, "recent_use": 0, "unproven": 0}
    items = []
    for i in sorted(imgs, key=lambda x: (x["created"], x["id"])):
        iid, short = i["id"], i["id"][7:19]
        age_h = (ctx.now - i["created"]) / 3600
        size, in_use = uniq.get(iid, (-1, False))
        if iid in refs or in_use:
            kept["referenced"] += 1
            items.append({"name": f"<dangling> {short}", "size": human(max(size, 0)), "state": "kept: referenced by a container"})
            continue
        if age_h < hours:
            kept["young"] += 1
            continue
        window_h = hours if i["digests"] else max(hours, local_h)           # no digest = irrecoverable: longer window
        first = since.get(iid)
        seen_h = (ctx.now - first) / 3600 if first is not None else 0.0
        ls = _ledger_seen(ledger, iid)
        used = isinstance(ls, (int, float)) and ctx.now - ls < window_h * 3600
        proof = ("size unknown" if size < 0
                 else f"unreferenced only {seen_h:.0f} h of {window_h:g}" if first is None or seen_h < window_h
                 else f"ledger: used {(ctx.now - ls) / 3600:.0f} h ago" if used
                 else "ledger entry unreadable" if ls is _ODD
                 else "ledger too young" if ls is None and led_age_h < window_h else "")
        if proof:
            kept["recent_use" if used else "unproven"] += 1
            items.append({"name": f"<dangling> {short}", "size": human(max(size, 0)), "state": f"kept: {proof}"})
            continue

        def rm(iid=iid) -> None:
            b, why = cl._busy("docker_build")                      # re-read the world right before the delete
            if b:
                raise cl._Changed(f"docker build started ({why})")
            now_refs = cl._referenced_images()
            if now_refs is None:
                raise RuntimeError("cannot re-check container references: image kept")
            if iid in now_refs:
                raise cl._Changed("referenced by a container now")
            cl._run_ok(["docker", "image", "rm", iid], 120)       # no -f: docker itself refuses an image a container uses

        acts.run("docker-image-rm", iid, size, rm, protect=tuple(i["digests"]),
                 label=f"<dangling> {short} {age_h / 24:.0f}d old, unreferenced {seen_h / 24:.0f}d"
                       + ("" if i["digests"] else " (local build)"))
    res = acts.result("images", {"dangling": len(imgs), "referenced": kept["referenced"], "young": kept["young"],
                                 "recent_use": kept["recent_use"], "unproven": kept["unproven"], "proof": "ledger+own clock"})
    tail = (f"; {len(imgs)} dangling: {kept['referenced']} referenced, {kept['young']} < {hours:g} h old"
            + (f", {kept['recent_use'] + kept['unproven']} not proven unused" if kept["recent_use"] + kept["unproven"] else ""))
    res.summary = cl._ascii(res.summary + tail)
    res.items = (res.items + items)[:12]
    return res


# =========================================================================== crash_dumps
def _scan_flat(d: str, match, age_s: float, now: float) -> tuple[list[tuple], dict[str, int]]:
    """Regular files directly in `d` accepted by `match(name) -> epoch-or-None` (the file's own timestamp, when the name
    carries one) and older than age_s by BOTH mtime and that timestamp. Returns ([(name, ino, dev, size, mtime_ns, uid)], counts)."""
    out, c = [], {"young": 0, "other": 0}
    try:
        it = os.scandir(d)
    except OSError:
        return out, c
    with it:
        for e in it:
            ok, stamp = match(e.name)
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not ok or not stat.S_ISREG(st.st_mode):
                c["other"] += 1
                continue
            newest = max(st.st_mtime, stamp or 0.0)
            if now - newest <= max(age_s, RECENT_S):
                c["young"] += 1
            else:
                out.append((e.name, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_uid))
    return sorted(out, key=lambda x: (x[4], x[0])), c


def _match_crash(name: str) -> tuple[bool, float | None]:
    return fnmatch.fnmatchcase(name, "*.crash"), None


def _match_core(name: str) -> tuple[bool, float | None]:
    """systemd-coredump storage: core.<comm>.<uid>.<bootid>.<pid>.<usec>[.zst|.xz|.lz4]; `.partial` is a dump in progress."""
    if not name.startswith("core.") or ".partial" in name:
        return False, None
    m = re.search(r"\.(\d{16})(?:\.(?:zst|xz|lz4|gz))?$", name)
    return True, int(m.group(1)) / 1e6 if m else None


def _unlink_scanned(d: str, ent: tuple, holders: _Holders) -> int:
    name, ino, dev, size, mtime_ns, _uid = ent
    dfd = _open_dir(d)
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_dev, st.st_mtime_ns) != (ino, dev, mtime_ns):
            raise cl._Changed("changed since scan")
        held, complete = holders.get()
        kind, why = _file_proof(os.path.join(d, name), (dev, ino), held, complete)
        if kind == "unknown":
            raise RuntimeError(f"open-file proof incomplete: {why}")
        if kind == "used":
            raise cl._Changed(f"open: {why}")
        os.unlink(name, dir_fd=dfd)
        return st.st_size
    finally:
        os.close(dfd)


@task("crash_dumps", klass="C1", tier="daily", title="Crash dumps", timeout=300, needs_root=True)
def crash_dumps(ctx: Ctx) -> Result:
    """Remove apport `*.crash` reports older than `crash_max_age_days` (2) from /var/crash and systemd-coredump files
    older than `coredump_max_age_days` (7) from /var/lib/systemd/coredump.

    In-use proof per file: regular file (no symlink), older than the age by mtime (coredumps: also by the timestamp in
    their name, the younger of the two wins), not a `.partial`, no process holds it open, and the caller may unlink it
    (root, or the owner in the sticky /var/crash). `coredumpctl` has no delete verb: like systemd-tmpfiles, the storage
    files are removed and the journal metadata then shows them as missing. Whole directories are never touched."""
    nums = _cfg_nums(ctx, ("crash_max_age_days", 2, 0.05, 3650), ("coredump_max_age_days", 7, 0.05, 3650))
    cdir, kdir = ctx.opt("crash_dir", "/var/crash"), ctx.opt("coredump_dir", "/var/lib/systemd/coredump")
    if nums is None or not all(isinstance(x, str) and os.path.isabs(x) for x in (cdir, kdir)):
        return cl._skipped("bad crash_max_age_days/coredump_max_age_days/crash_dir/coredump_dir config: nothing done")
    acts, found, young, missing = cl._Acts(ctx), [], 0, 0
    for d, match, age in ((cdir, _match_crash, nums[0]), (kdir, _match_core, nums[1])):
        if not _canonical(d) or _never(d) or not os.path.isdir(d):
            missing += 1
            continue
        ents, c = _scan_flat(d, match, age * 86400, ctx.now)
        young += c["young"]
        found += [(d, e) for e in ents]
    holders = _Holders({(e[2], e[1]) for _d, e in found})
    held, complete = holders.get(force=True)
    held_n = need_root = 0
    partial = not complete
    items: list[dict] = []
    for d, e in found:
        name, ino, dev, size, _m, uid = e
        path = os.path.join(d, name)
        kind, why = _file_proof(path, (dev, ino), held, complete)
        if kind == "used":
            held_n += 1
            items.append({"name": name[:60], "size": human(size), "state": f"kept: {why}"})
            continue
        partial = partial or kind == "unknown"
        if kind == "unknown" and ctx.apply:
            need_root += 1
            items.append({"name": name[:60], "size": human(size), "state": "refused: needs root (open-file proof incomplete)"})
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not _can_unlink(d, st):
            need_root += 1
            items.append({"name": name[:60], "size": human(size), "state": "needs root to remove"})
            if ctx.apply:
                continue
        acts.run("crash-delete", path, size, lambda d=d, e=e: _unlink_scanned(d, e, holders), label=name)
    res = acts.result("dumps", {"found": len(found), "young": young, "open_kept": held_n, "needs_root": need_root,
                                "proof": "partial (not root)" if partial else "complete"})
    for cnt, word in ((held_n, "open kept"), (need_root, "need root"), (missing, "dirs absent")):
        if cnt:
            res.summary = cl._ascii(res.summary + f", {cnt} {word}")
    if partial and not ctx.apply and acts.n["would"]:
        res.summary = cl._ascii(res.summary + "; open-file proof incomplete (not root)")
    if not found:
        res.summary = cl._ascii(f"no crash dump older than {nums[0]:g}/{nums[1]:g} d ({young} newer)")
    res.items = (items + res.items)[:12]
    return res


# =========================================================================== apt_cache
@task("apt_cache", klass="C1", tier="daily", title="APT archives cache", timeout=300, needs_root=True)
def apt_cache(ctx: Ctx) -> Result:
    """`apt-get clean` when the archives cache exceeds `min_mib` and no apt/dpkg process or lock exists.

    In-use proof: gates.busy("apt") idle AND the apt/dpkg lock files probe 'free' (an unreadable lock, which is what a
    non-root user gets, refuses apply). The freed bytes are measured afterwards. Overlaps the older `apt_clean` task
    (same command): enable one of them."""
    nums = _cfg_nums(ctx, ("min_mib", 10, 0, 1_000_000))
    cache = str(ctx.opt("cache_dir", "/var/cache/apt/archives"))
    if nums is None or not os.path.isabs(cache):
        return cl._skipped("bad min_mib/cache_dir config: nothing done")
    size = cl._dir_bytes(cache)
    mode = "apply" if ctx.apply else "report"
    if size <= nums[0] * cl.MIB:
        return Result("ok", cl._ascii(f"apt cache {human(size)}, under {nums[0]:g} MiB"), {"mode": mode, "cache_h": human(size)})
    busy, why = cl._busy("apt")
    if busy:
        return cl._skipped(f"apt/dpkg busy ({why}); cache {human(size)}", cache_h=human(size))
    lock = cl._apt_lock_state()
    if lock == "busy" or (lock == "unknown" and ctx.apply):
        return cl._skipped(f"apt lock {lock}: nothing done; cache {human(size)}", cache_h=human(size))

    def clean() -> int:
        cl._run_ok(["apt-get", "clean"], 120)
        return max(size - cl._dir_bytes(cache), 0)               # measured, not assumed

    acts = cl._Acts(ctx)
    acts.run("apt-get-clean", cache, size, clean, label="apt archives")
    res = acts.result("cache", {"cache_h": human(size), "lock": lock},
                      "lock state unverifiable without root" if lock == "unknown" else "")
    return res
