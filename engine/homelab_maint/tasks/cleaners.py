"""cleaners: the C1 (safe auto-clean, daily) tasks and the C2 weekly candidate planner.

docker_cache, docker_images, apt_clean, snap_revisions, retention, trash, gradle_reaper, caps  (C1)
c2_candidates                                                                                (C2)

Safety model (the whole point of this module):
  * every mutation goes through `ctx.act`; with apply off `ctx.act` only audits "dry-run", so the dry-run
    list is exactly the list apply mode walks (`_Acts` also simulates the per-run caps in dry-run);
  * file deletion is confined to configured roots by realpath, never follows a symlink (directory walks
    use lstat; removal re-opens the path component by component with O_NOFOLLOW and dir fds and checks the
    inode it scanned), and skips anything modified in the last 10 minutes;
  * anything unparsable, missing, unknown or erroring selects NOTHING (fail closed); a protected name or
    path (protected.toml) is never acted on, even when the config asks for it;
  * an item that fails (a real error, not a benign race) is remembered in ctx.state and skipped for
    `retry_after_days` (default 3), so a few permanently stuck items cannot starve the queue behind them;
  * c2 deletions are irreversible, so apply needs root (lsof must see every process) and re-checks, at the
    last moment and with a fresh docker snapshot, that nothing running or stopped mounts the path and that
    lsof positively says "not open": a timeout, a missing tool or an error means refuse;
  * the tool's own bookkeeping (ctx.state) is the only thing a report-mode run writes.

Formats parsed here were inspected on the real machine (read only): `docker buildx ls --format json`,
`docker buildx du`, `docker system df -v --format json`, `snap list --all`, `snap get system
refresh.retain`, the Kavita log/backup directories, ~/.local/share/Trash/info/*.trashinfo, /proc/<pid>/stat.
"""
from __future__ import annotations

import errno
import fcntl
import fnmatch
import json
import math
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import core
from ..core import (GIB, CapExceeded, Ctx, Result, audit, human, plan_hash, read_history, read_json, sh,
                    task)

MIB = 1024 ** 2
RECENT_S = 600                 # never delete anything modified within the last 10 minutes
SCAN_LIMIT = 200_000           # max directory entries visited per scan; more => fail closed
SCAN_DEPTH = 16
# Paths no retention match may ever sit in, whatever the glob: a `**` or `*/*` glob under an allowed root cannot be proven against the
# unanchored ones (ai-stack, .cursor ...) statically, so each match is tested here. Equal to registry.NEVER_TOUCH (a test keeps them so).
NEVER_TOUCH = tuple(re.compile(p) for p in (
    r"^/var/lib/(docker|libvirt|containerd)(/|$)", r"^/mnt/backup(/|$)", r"^/media/(Immich|nextcloud)(/|$)", r"^/var/snap/plexmediaserver",
    r"^/media/SandiskSSD/plex", r"/Plex Media Server(/|$)", r"^/volume1/docker/plex(/|$)", r"^/usr/share/ollama", r"/\.ollama(/|$)",
    r"/comfyui/models", r"(surreal|notebook)_data|pgdata", r"/\.config/Cursor(/|$)", r"/\.cursor(/|$)", r"^/home/[^/]+/(models|\.ssh|\.gnupg)(/|$)",
    r"(^|/)ai-stack(/|$)", r"^/volume1/docker/kavita(/(?!config/(logs|backups|cache)(/|$))|$)",
    r"^/volume1/docker/(radarr|sonarr|prowlarr|bazarr|lidarr|readarr|sabnzbd|seerr|overseerr|jellyseerr)(/(?!config/cache(/|$))|$)",
    r"/\.docker-data(/(?!tunarr/cache/)|$)"))
PROC = Path("/proc")           # tests point this at a fake tree
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_IMG_ID = re.compile(r"sha256:[0-9a-f]{64}")

# Indirections so tests never sleep, signal or fork for real.
_kill = os.kill
_sleep = time.sleep
_mono = time.monotonic
_euid = os.geteuid


# =========================================================================== shared helpers
def _ascii(s: Any, n: int = 140) -> str:
    """Summaries may become an SMS: ASCII only, <= 140 chars."""
    return re.sub(r"[^\x20-\x7e]", "?", str(s))[:n]


def _num(v: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    """A finite real number within [lo, hi], else None (bools, strings, NaN and out-of-range are rejected)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "pb": 1000 ** 5,
          "kib": 1024, "mib": MIB, "gib": GIB, "tib": 1024 ** 4}


def _parse_size(s: Any) -> int | None:
    """Docker prints decimal units ("8.192kB", "22.25GB", "0B"); None when it is not a size."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", str(s))
    mult = _UNITS.get(m.group(2).lower() or "b") if m else None
    return int(float(m.group(1)) * mult) if m and mult else None


def _json_objects(text: str) -> list[dict]:
    """JSON lines, one array, or concatenated objects (buildx prints one object per builder)."""
    out: list = []
    dec, i, text = json.JSONDecoder(), 0, text.strip()
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            return []                       # unparsable => treat as "no data"
        out.extend(obj if isinstance(obj, list) else [obj])
    return [o for o in out if isinstance(o, dict)]


def _is_timeout(exc: BaseException) -> bool:
    """The runner's SIGALRM guard raises core._Timeout (an Exception): a broad `except Exception` here must
    never swallow it, or a task could outlive its timeout."""
    return isinstance(exc, getattr(core, "_Timeout", ()))


def _busy(name: str) -> tuple[bool, str]:
    """gates.busy with a fail-closed wrapper (any import/probe error means busy). Tests patch this."""
    try:
        from . import gates
        b, why = gates.busy(name)
        return bool(b), str(why)
    except Exception as exc:  # noqa: BLE001
        if _is_timeout(exc):
            raise
        return True, f"gate error: {type(exc).__name__}"


def _run_ok(cmd: list[str], timeout: int) -> str:
    """Run a mutating command; raise with a short reason unless it exits 0 (ctx.act audits the failure)."""
    r = sh(cmd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} rc={r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-100:]}")
    return r.stdout


def _tree_stats(path: str, limit: int = SCAN_LIMIT, budget_s: float = 20.0) -> tuple[int, float, bool]:
    """(bytes, newest mtime, complete) of a tree via lstat; symlinks are counted, never followed."""
    total, newest, n, t0 = 0, 0.0, 0, _mono()
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    if n > limit or _mono() - t0 > budget_s:
                        return total, newest, False
                    st = e.stat(follow_symlinks=False)
                    total += st.st_size
                    newest = max(newest, st.st_mtime)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(os.path.join(d, e.name))
        except OSError:
            continue
    return total, newest, True


class _Changed(Exception):
    """The thing to act on vanished, changed or became busy after it was scanned: skip it, it is not a failure."""


class _Acts:
    """Wraps ctx.act for a loop of candidate actions and keeps the books for the Result.

    * protected targets are reported and never reach ctx.act;
    * in dry-run, ctx.act is still called (audit "dry-run") and the per-run caps are simulated, so the
      dry-run list equals what apply mode would do;
    * the first cap hit stops the run (core semantics); an item that alone exceeds the byte cap is skipped
      ("oversize") so it cannot block everything queued behind it;
    * a file that vanished or changed after the scan (FileNotFoundError, _Changed) is a benign race: counted as
      "gone", never as a failure;
    * three real failures in a row halt the loop (a broken daemon should not be hammered), and every failed
      item is skipped for `retry_after_days` (ctx.state["fail_until"]) so the queue advances on the next run
      instead of the same stuck head-of-queue items halting it forever. Dry-run honours the same skips.
    """

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.n = dict.fromkeys(("done", "would", "protected", "capped", "oversize", "failed", "refused", "gone",
                                "backoff"), 0)
        self.bytes = {"done": 0, "would": 0}
        self.rows: list[tuple[str, str, int]] = []
        self.errors: list[str] = []
        self.capped = False
        self.pin: str | None = None              # label prefix that must survive the 12-row truncation
        self._items = self._bytes = self._fails = 0
        days = _num(ctx.opt("retry_after_days", 3), 0, 365)
        self._retry_s = (3 if days is None else days) * 86400
        old = ctx.state.get("fail_until")
        # drop expired / malformed entries; keep the table bounded (it is persisted in the task's own state)
        live = {k: v for k, v in (old if isinstance(old, dict) else {}).items()
                if isinstance(k, str) and _num(v) is not None and v > ctx.now}
        self._fail_until = dict(sorted(live.items(), key=lambda kv: kv[1])[-1000:])
        ctx.state["fail_until"] = self._fail_until

    @property
    def halted(self) -> bool:
        return self.capped or self._fails >= 3

    def run(self, what: str, target: str, size: int, fn: Callable[[], Any], protect: tuple[str, ...] = (),
            label: str | None = None) -> str:
        ctx, size = self.ctx, max(int(size or 0), 0)
        label, key = label or target, f"{what}|{target}"
        if ctx.is_protected(target, *protect):
            return self._note("protected", label, size)
        if self._fail_until.get(key, 0) > ctx.now:        # failed recently: let the queue move on
            return self._note("backoff", label, size)
        if self.halted:
            self.n["capped"] += 1
            return "capped"
        if size > ctx.cap_bytes:
            return self._note("oversize", label, size)
        if not ctx.apply:
            if self._items + 1 > ctx.cap_items or self._bytes + size > ctx.cap_bytes:
                self.capped = True
                self.n["capped"] += 1
                return "capped"
            ctx.act(what, target, size, fn, protect_names=protect)       # audits "dry-run", returns False
            self._items += 1
            self._bytes += size
            self.bytes["would"] += size
            return self._note("would", label, size)
        try:
            ok = ctx.act(what, target, size, fn, protect_names=protect)
        except CapExceeded:
            self.capped = True
            self.n["capped"] += 1
            return "capped"
        except (FileNotFoundError, _Changed):
            return self._note("gone", label, size)                       # raced with another process: not a failure
        except Exception as exc:  # noqa: BLE001  - one bad item must not abort the rest
            if _is_timeout(exc):
                raise
            self._fails += 1
            self._fail_until[key] = ctx.now + self._retry_s
            self.errors.append(f"{label}: {exc}"[:120])
            return self._note("failed", label, size)
        self._fails = 0
        if not ok:
            return self._note("refused", label, size)
        self.bytes["done"] += size
        return self._note("done", label, size)

    def _note(self, state: str, label: str, size: int) -> str:
        self.n[state] += 1
        self.rows.append((state, label, size))
        return state

    def items(self, limit: int = 12) -> list[dict]:
        order = {"failed": 0, "done": 1, "would": 1, "oversize": 2, "refused": 2, "gone": 2, "backoff": 2,
                 "protected": 3}
        pin = self.pin
        rows = sorted(self.rows, key=lambda r: (not (pin and r[1].startswith(pin)), order.get(r[0], 4), -r[2], r[1]))[:limit]
        return [{"name": lab[:60], "size": human(sz), "state": st} for st, lab, sz in rows]

    def metrics(self, **extra: Any) -> dict:
        got = self.bytes["done"] if self.ctx.apply else self.bytes["would"]
        sel = self.n["done"] if self.ctx.apply else self.n["would"]
        return {"mode": "apply" if self.ctx.apply else "report", "selected": sel, "selected_h": human(got),
                "freed_h": human(self.ctx.freed), "protected": self.n["protected"],
                "oversize": self.n["oversize"], "deferred": self.n["capped"], "failed": self.n["failed"],
                "gone": self.n["gone"], "backoff": self.n["backoff"], "capped": self.capped, **extra}

    def result(self, noun: str, extra: dict | None = None, note: str = "") -> Result:
        ctx = self.ctx
        if ctx.apply:
            s = f"freed {human(self.bytes['done'])} ({self.n['done']} {noun})"
            status = "warn" if self.n["failed"] else ("info" if self.capped else "ok")
        else:
            s = f"report: would free {human(self.bytes['would'])} ({self.n['would']} {noun})"
            status = "info" if self.n["would"] else "ok"
        for key, word in (("protected", "protected"), ("refused", "refused"), ("oversize", "over cap"),
                          ("capped", "deferred by cap"), ("gone", "vanished/changed"),
                          ("backoff", "in retry backoff"), ("failed", "failed")):
            if self.n[key]:
                s += f", {self.n[key]} {word}"
        if self.errors:
            s += f"; {self.errors[0]}"
        if note:
            s += f"; {note}"
        return Result(status, _ascii(s), self.metrics(**(extra or {})), self.items(), reclaimed_bytes=ctx.freed)


def _skipped(why: str, **metrics: Any) -> Result:
    return Result("skipped", _ascii(why), {"mode": "skipped", **metrics})


# =========================================================================== docker_cache
def _builders() -> list[str] | None:
    """Names of RUNNING buildx builders (`docker buildx ls --format json`), None if docker cannot say."""
    r = sh(["docker", "buildx", "ls", "--format", "json"], timeout=30)
    if r.returncode != 0:
        return None
    out = []
    for o in _json_objects(r.stdout):
        name = o.get("Name")
        up = any(isinstance(n, dict) and n.get("Status") == "running" for n in o.get("Nodes") or [])
        if isinstance(name, str) and _NAME.fullmatch(name) and up:
            out.append(name)
    return out


def _builder_size(name: str) -> int | None:
    """Total build-cache bytes of a builder from the `Total:` footer of `docker buildx du`."""
    r = sh(["docker", "buildx", "du", "--builder", name], timeout=120)
    if r.returncode != 0:
        return None
    for ln in reversed(r.stdout.splitlines()):
        if ln.startswith("Total:"):
            return _parse_size(ln.split(":", 1)[1])
    return None


@task("docker_cache", klass="C1", tier="daily", title="Docker build cache", timeout=1800)
def docker_cache(ctx: Ctx) -> Result:
    """Prune each builder's cache LRU-style down to `low_gib` once it exceeds `high_gib`. Never volumes."""
    high, low = _num(ctx.opt("high_gib", 15), 0.001), _num(ctx.opt("low_gib", 8), 0)
    if high is None or low is None or low >= high:
        return _skipped("bad high_gib/low_gib config: nothing done")
    busy, why = _busy("docker_build")
    if busy:
        return _skipped(f"docker build active, cache prune deferred ({why})")
    names = _builders()
    if names is None:
        return _skipped("docker buildx unavailable: nothing done")
    high_b, low_b = int(high * GIB), int(low * GIB)
    sizes: dict[str, int] = {}
    unknown = 0
    for n in names:
        s = _builder_size(n)
        if s is None:
            unknown += 1                       # unmeasurable => untouched
        else:
            sizes[n] = s
    acts, actual = _Acts(ctx), {}
    for name, size in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])):
        if size <= high_b:
            continue

        def prune(name=name, size=size) -> None:
            _run_ok(["docker", "buildx", "prune", "--builder", name, "-f", "--max-used-space", str(low_b)], 900)
            after = _builder_size(name)
            if after is not None:
                actual[name] = max(size - after, 0)

        # the estimate (before - low) is the most a prune to `low` can free: a safe upper bound for the caps
        acts.run("buildx-prune", f"builder:{name}", size - low_b, prune, label=f"{name} {human(size)}")
    total = sum(sizes.values())
    res = acts.result("builders", {"cache_h": human(total), "high_h": human(high_b), "low_h": human(low_b),
                                   "builders": len(names), "unmeasured": unknown,
                                   "actual_freed_h": human(sum(actual.values()))})
    if not acts.rows:
        res.summary = _ascii(f"build cache {human(total)} across {len(sizes)} builders, under {human(high_b)}")
    return res


# =========================================================================== docker_images
def _norm_id(s: str) -> str:
    return s.lower().removeprefix("sha256:")


def _read_ledger(now: float, max_age_h: float) -> tuple[dict | None, str]:
    """(ledger, why-not). Schema: guard.py image_ledger. Missing, stale or odd => (None, reason): fail closed."""
    p = core.STATE_DIR / "ledger" / "images.json"
    d = read_json(p, None)
    if not isinstance(d, dict) or not isinstance(d.get("images"), dict):
        return None, "image ledger missing or unreadable"
    created, updated = _num(d.get("created")), _num(d.get("updated"))
    if created is None or updated is None:
        return None, "image ledger has no created/updated time"
    if now - updated > max_age_h * 3600:
        return None, f"image ledger stale ({(now - updated) / 3600:.0f} h old)"
    return d, ""


def _image_inventory() -> list[dict] | None:
    r = sh(["docker", "system", "df", "-v", "--format", "json"], timeout=120)
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout).get("Images")
    except (ValueError, AttributeError):
        return None
    return rows if isinstance(rows, list) else None


def _referenced_images() -> set[str] | None:
    """Image IDs used by ANY container, running or exited; None if it cannot be established."""
    ps = sh(["docker", "ps", "-a", "-q", "--no-trunc"], timeout=30)
    if ps.returncode != 0:
        return None
    ids = ps.stdout.split()
    refs: set[str] = set()
    for i in range(0, len(ids), 100):
        r = sh(["docker", "container", "inspect", "--format", "{{.Image}}", *ids[i:i + 100]], timeout=60)
        lines = r.stdout.split()
        if r.returncode != 0 or len(lines) != len(ids[i:i + 100]) or not all(_IMG_ID.fullmatch(x) for x in lines):
            return None
        refs.update(lines)
    return refs


@task("docker_images", klass="C1", tier="daily", title="Unused Docker images", timeout=900)
def docker_images(ctx: Ctx) -> Result:
    """`docker image rm <id>` (never -f, never `system prune`) for images no container has used for `unused_days`.

    An image is a candidate only when ALL hold: no container (running or exited) references it now; the ledger
    is fresh, has existed >= unused_days and has not seen it within unused_days; and THIS task has itself seen it
    unreferenced for >= unused_days (so a just-pulled image waiting for `compose up` is never eligible).
    """
    days = _num(ctx.opt("unused_days", 14), 1, 3650)
    max_age_h = _num(ctx.opt("ledger_max_age_hours", 48), 1)
    if days is None or max_age_h is None:
        return _skipped("bad unused_days config: nothing done")
    rows = _image_inventory()
    refs = _referenced_images()
    if rows is None or refs is None:
        return _skipped("docker unavailable or unparsable: nothing selected")
    by_id: dict[str, dict] = {}
    for r in rows:
        iid = r.get("ID")
        if not isinstance(iid, str) or not _IMG_ID.fullmatch(iid):
            continue
        e = by_id.setdefault(iid, {"names": [], "unique": 0, "containers": 0, "size": 0})
        repo, tag = r.get("Repository"), r.get("Tag")
        if repo not in (None, "", "<none>") and tag not in (None, "", "<none>"):
            e["names"].append(f"{repo}:{tag}")
        e["unique"] = max(e["unique"], _parse_size(r.get("UniqueSize")) or 0)
        e["size"] = max(e["size"], _parse_size(r.get("Size")) or 0)
        # anything but a literal "0" (e.g. "N/A") counts as referenced: unknown means keep
        e["containers"] += 0 if str(r.get("Containers", "")).strip() == "0" else 1

    # first-sight clock per unreferenced image (this task's own state; kept even while the ledger is unusable)
    old = ctx.state.get("unref_since")
    unref: dict[str, float] = {k: v for k, v in (old if isinstance(old, dict) else {}).items()
                               if k in by_id and _num(v) is not None}
    for iid, e in by_id.items():
        if iid in refs or e["containers"]:
            unref.pop(iid, None)
        else:
            unref.setdefault(iid, ctx.now)
    ctx.state["unref_since"] = unref

    ledger, why = _read_ledger(ctx.now, max_age_h)
    if ledger is None:
        return _skipped(f"{why}: nothing selected", unreferenced=len(unref))
    ledger_days = (ctx.now - ledger["created"]) / 86400
    seen = {_norm_id(k): v.get("last_seen") for k, v in ledger["images"].items() if isinstance(v, dict)}
    cands = []
    for iid, first in unref.items():
        e = by_id[iid]
        ls = seen.get(_norm_id(iid))
        if isinstance(ls, bool) or (ls is not None and not isinstance(ls, (int, float))):
            continue                                       # odd ledger entry: keep the image
        if ls is not None and ctx.now - ls < days * 86400:
            continue
        if ctx.now - first < days * 86400 or ledger_days < days:
            continue
        cands.append((first, -(e["unique"] or e["size"]), iid, e))
    cands.sort(key=lambda c: c[:3])

    acts = _Acts(ctx)
    for _first, _neg, iid, e in cands:
        names = e["names"]
        if any(not re.fullmatch(r"[A-Za-z0-9][^\s]*", n) for n in names):
            continue                                       # odd tag text: do not pass it to docker

        def rm(iid=iid, names=names) -> None:
            # several tags => remove them all by name (a bare `rm <id>` needs -f for a multi-tagged image)
            _run_ok(["docker", "image", "rm", *(names if len(names) > 1 else [iid])], 120)

        label = f"{names[0] if names else '<dangling>'} {iid[7:19]}"
        acts.run("docker-image-rm", names[0] if names else iid, e["unique"] or e["size"], rm,
                 protect=tuple(names), label=label)
    extra = {"images": len(by_id), "unreferenced": len(unref), "candidates": len(cands),
             "ledger_days": round(ledger_days, 1)}
    res = acts.result("images", extra)
    if not cands:
        res.summary = _ascii(f"no image unused >= {days:g} d ({len(unref)} unreferenced, ledger {ledger_days:.1f} d old)")
    return res


# =========================================================================== apt_clean
_APT_LOCK_FILES = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock",
                   "/var/cache/apt/archives/lock")


def _apt_lock_state(paths: tuple[str, ...] | None = None) -> str:
    """'free' | 'busy' | 'unknown'. apt/dpkg hold POSIX (fcntl) write locks: a non-blocking SHARED probe
    conflicts with them. Opening needs root; as a normal user the answer is 'unknown'."""
    state = "free"
    for p in (_APT_LOCK_FILES if paths is None else paths):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError:
            state = "unknown"
            continue
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.lockf(fd, fcntl.LOCK_UN)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return "busy"
            state = "unknown"
        finally:
            os.close(fd)
    return state


def _dir_bytes(path: str) -> int:
    """Sum of file sizes directly in `path` (+ `partial/`, which is root-only and may be unreadable)."""
    total = 0
    for d in (path, os.path.join(path, "partial")):
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode) and e.name != "lock":
                        total += st.st_size
        except OSError:
            continue
    return total


@task("apt_clean", klass="C1", tier="daily", title="APT package cache", timeout=300, needs_root=True)
def apt_clean(ctx: Ctx) -> Result:
    """`apt-get clean` only when no apt/dpkg is running or holding a lock. Never autoremove."""
    cache = str(ctx.opt("cache_dir", "/var/cache/apt/archives"))
    size = _dir_bytes(cache)
    busy, why = _busy("apt")
    if busy:
        return _skipped(f"apt/dpkg busy ({why}); cache {human(size)}", cache_h=human(size))
    lock = _apt_lock_state()
    if lock == "busy" or (lock == "unknown" and ctx.apply):
        return _skipped(f"apt lock {lock}: nothing done; cache {human(size)}", cache_h=human(size))
    if size == 0:
        return Result("ok", "apt cache already empty", {"mode": "apply" if ctx.apply else "report", "cache_h": "0 B"})
    acts = _Acts(ctx)
    acts.run("apt-get-clean", cache, size, lambda: _run_ok(["apt-get", "clean"], 120), label="apt archives")
    note = "lock state unverifiable without root" if lock == "unknown" else ""
    return acts.result("cache", {"cache_h": human(size), "lock": lock}, note)


# =========================================================================== snap_revisions
def _snap_list() -> list[dict] | None:
    """Rows of `snap list --all`: name, rev, notes. Rows that do not have exactly 6 columns are ignored."""
    r = sh(["snap", "list", "--all"], timeout=30)
    lines = r.stdout.splitlines()
    if r.returncode != 0 or not lines or not lines[0].startswith("Name"):
        return None
    rows = []
    for ln in lines[1:]:
        f = ln.split()
        if len(f) == 6:
            rows.append({"name": f[0], "rev": f[2], "disabled": "disabled" in f[5].split(",")})
    return rows


def _snap_busy() -> bool | None:
    """True if `snap changes` shows a change that is not finished; None if it cannot be read."""
    r = sh(["snap", "changes"], timeout=30)
    if r.returncode != 0:
        return None
    done = {"Done", "Undone", "Error", "Hold"}
    for ln in r.stdout.splitlines()[-11:]:
        f = ln.split()
        if len(f) >= 2 and f[0].isdigit() and f[1] not in done:
            return True
    return False


def _snap_retain_current() -> tuple[int | None, bool]:
    """(value, readable). Unset shows up as an error naming the option; any other failure is 'unreadable'."""
    r = sh(["snap", "get", "system", "refresh.retain"], timeout=20)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return int(r.stdout.strip()), True
    if r.returncode != 0 and "has no" in r.stderr and "refresh.retain" in r.stderr:
        return None, True
    return None, False


@task("snap_revisions", klass="C1", tier="daily", title="Old snap revisions", timeout=900, needs_root=True)
def snap_revisions(ctx: Ctx) -> Result:
    """Idempotently set refresh.retain, then remove disabled revisions (never an active one).

    `keep_disabled` (default 0) disabled revisions per snap are kept for `snap revert`; the spec removes all.
    """
    retain = _num(ctx.opt("retain", 2), 2, 20)
    keep = _num(ctx.opt("keep_disabled", 0), 0, 20)
    if retain is None or keep is None:
        return _skipped("bad retain/keep_disabled config: nothing done")
    retain, keep = int(retain), int(keep)
    snap_dir = str(ctx.opt("snap_dir", "/var/lib/snapd/snaps"))
    busy = _snap_busy()
    if busy is None or busy:
        return _skipped("snap change in progress" if busy else "snap changes unreadable: nothing done")
    rows = _snap_list()
    if rows is None:
        return _skipped("snap list unreadable: nothing done")
    acts = _Acts(ctx)
    acts.pin = "refresh.retain"
    cur, readable = _snap_retain_current()
    if readable and cur != retain:
        acts.run("snap-set", f"system refresh.retain={retain}", 0,
                 lambda: _run_ok(["snap", "set", "system", f"refresh.retain={retain}"], 60),
                 label=f"refresh.retain {cur if cur is not None else 'unset'} -> {retain}")
    active = {r["name"] for r in rows if not r["disabled"]}
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        if r["disabled"] and r["name"] in active and re.fullmatch(r"[a-z0-9][a-z0-9-]*", r["name"]) \
                and r["rev"].isdigit():
            by_name.setdefault(r["name"], []).append(r)
    for name in sorted(by_name):
        revs = sorted(by_name[name], key=lambda r: -int(r["rev"]))[keep:]
        for r in revs:
            try:
                size = os.lstat(os.path.join(snap_dir, f"{name}_{r['rev']}.snap")).st_size
            except OSError:
                size = 0

            def rm(name=name, rev=r["rev"]) -> None:
                _run_ok(["snap", "remove", name, f"--revision={rev}"], 300)

            acts.run("snap-remove", f"{name} rev {r['rev']}", size, rm, protect=(name,), label=f"{name} r{r['rev']}")
    note = "" if readable else "refresh.retain unreadable"
    return acts.result("revisions", {"retain": retain, "retain_now": cur if cur is not None else -1}, note)


# =========================================================================== retention
def _seg_match(pat: list[str], name: list[str]) -> bool:
    """Glob match on path segments; a `**` segment matches zero or more directories."""
    if not pat:
        return not name
    if pat[0] == "**":
        return any(_seg_match(pat[1:], name[i:]) for i in range(len(name) + 1))
    return bool(name) and fnmatch.fnmatchcase(name[0], pat[0]) and _seg_match(pat[1:], name[1:])


@dataclass
class _Ent:
    rel: str
    path: str
    mtime_ns: int
    size: int
    ino: int
    dev: int
    kind: str                 # "file" | "dir" (only EMPTY directories are ever candidates)

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1e9


def _scan(root: str, pat: list[str], limit: int | None = None, dev: int | None = None) -> tuple[list[_Ent], int, bool]:
    """Entries under `root` whose relative path matches `pat`. Symlinks are counted and skipped; directories are
    entered only when they are real directories on the same filesystem as `root` (no crossing into mounts).
    Returns (entries, skipped_symlinks, complete)."""
    limit = SCAN_LIMIT if limit is None else limit
    if dev is None:
        dev = os.lstat(root).st_dev
    ents: list[_Ent] = []
    links = n = 0
    deep = len(pat) > 1 or "**" in pat
    max_depth = SCAN_DEPTH if "**" in pat else len(pat)
    stack = [(root, "", 1)]
    t0 = _mono()
    while stack:
        d, rel, depth = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                n += 1
                if n > limit or _mono() - t0 > 60:
                    return ents, links, False
                r = rel + e.name
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                hit = _seg_match(pat, r.split("/"))
                if stat.S_ISLNK(mode):
                    links += hit
                    continue
                kind = "file" if stat.S_ISREG(mode) else "dir" if stat.S_ISDIR(mode) else ""
                if kind == "dir" and st.st_dev != dev:
                    continue                                     # a mount point inside the root: leave it alone
                if kind == "dir" and deep and depth < max_depth:
                    stack.append((os.path.join(d, e.name), r + "/", depth + 1))
                if hit and kind:
                    if kind == "dir":
                        try:
                            with os.scandir(os.path.join(d, e.name)) as sub:
                                if next(sub, None) is not None:
                                    continue                 # non-empty dirs are never removed here
                        except OSError:
                            continue
                    ents.append(_Ent(r, os.path.join(d, e.name), st.st_mtime_ns, st.st_size, st.st_ino,
                                     st.st_dev, kind))
    return ents, links, True


def _rule_selectors(rule: dict) -> tuple[float | None, int | None, str]:
    """(max_age_s, keep_newest, error). Present-but-invalid or absent selectors mean NOTHING is selected."""
    age = keep = None
    if "max_age_days" in rule:
        a = _num(rule["max_age_days"], 0)
        if a is None or a <= 0:
            return None, None, "bad max_age_days"
        age = a * 86400
    if "keep_newest" in rule:
        k = rule["keep_newest"]
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            return None, None, "bad keep_newest"
        keep = k
    if age is None and keep is None:
        return None, None, "no selector"
    return age, keep, ""


def _select(ents: list[_Ent], now: float, max_age_s: float | None, keep: int | None) -> tuple[list[_Ent], int]:
    """Pick deletions. Newest-first order (ties: larger name = newer); the newest `keep` survive. With both
    selectors an entry must satisfy both. Entries modified < 10 min ago (or in the future) are never selected.
    Returns (selected, skipped_recent)."""
    order = sorted(ents, key=lambda e: (e.mtime_ns, e.rel), reverse=True)
    pool = order[keep:] if keep is not None else order
    sel, recent = [], 0
    for e in pool:
        if now - e.mtime < RECENT_S:
            recent += 1
        elif max_age_s is None or now - e.mtime > max_age_s:
            sel.append(e)
    return sel, recent


def _inside(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _remove_entry(root: str, ent: _Ent) -> None:
    """Delete one scanned entry without following symlinks: walk root -> parent with O_NOFOLLOW dir fds, check
    the inode is the one that was scanned, then unlink/rmdir relative to the parent fd. A directory swapped for a
    symlink after the scan makes the open fail instead of redirecting the delete."""
    if os.path.realpath(root) != root:
        raise _Changed("root moved")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parts = ent.rel.split("/")
    fd = os.open(root, flags)
    try:
        for p in parts[:-1]:
            nfd = os.open(p, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if (st.st_ino, st.st_dev, st.st_mtime_ns) != (ent.ino, ent.dev, ent.mtime_ns):
            raise _Changed("changed since scan")
        if ent.kind == "file" and stat.S_ISREG(st.st_mode):
            os.unlink(parts[-1], dir_fd=fd)
        elif ent.kind == "dir" and stat.S_ISDIR(st.st_mode):
            os.rmdir(parts[-1], dir_fd=fd)
        else:
            raise _Changed("type changed")
    finally:
        os.close(fd)


@task("retention", klass="C1", tier="daily", title="Retention rules", timeout=900, needs_root=True)
def retention(ctx: Ctx) -> Result:
    """Config rules {name,path,glob,max_age_days|keep_newest,files_only}; see module docstring for the guarantees."""
    rules = ctx.opt("rules", [])
    raw_roots = ctx.opt("allowed_roots", [])
    roots = [os.path.realpath(r) for r in raw_roots if isinstance(r, str) and os.path.isabs(r)] \
        if isinstance(raw_roots, list) else []
    roots = [r for r in roots if r != "/"]
    if not isinstance(rules, list) or not rules:
        return Result("ok", "no retention rules configured", {"mode": "report", "selected": 0, "rules": 0})
    acts, per_rule, refused = _Acts(ctx), [], 0
    for i, rule in enumerate(rules):
        rule = rule if isinstance(rule, dict) else {}
        name = str(rule.get("name") or f"rule{i}")[:40]
        row = {"name": name, "matched": 0, "size": "0 B", "state": ""}
        per_rule.append(row)
        why = _check_rule(ctx, rule, roots)
        if isinstance(why, str):
            row["state"] = why
            refused += 1
            if why.startswith("refused"):
                audit(ctx.name, "retention-rule", str(rule.get("path")), 0, why)
            continue
        root, pat, max_age_s, keep, files_only = why
        ents, links, complete = _scan(root, pat)
        if not complete:
            row["state"] = "refused: scan limit reached"
            refused += 1
            continue
        if files_only:
            ents = [e for e in ents if e.kind == "file"]
        sel, recent = _select(ents, ctx.now, max_age_s, keep)
        row["matched"], row["size"] = len(sel), human(sum(e.size for e in sel))
        row["state"] = f"{len(ents)} match, {len(sel)} selected" + (f", {recent} recent" if recent else "") \
            + (f", {links} symlinks skipped" if links else "")
        for e in sorted(sel, key=lambda e: (e.mtime_ns, e.rel)):
            if acts.halted:
                acts.n["capped"] += 1
                continue
            if any(rx.search(e.path) for rx in NEVER_TOUCH):         # the glob reached a never-touch tree: reported as protected, never offered
                audit(ctx.name, "retention-delete", e.path, e.size, "refused-never-touch")
                acts._note("protected", f"{name}: {e.rel}", e.size)
                continue
            acts.run("retention-delete", e.path, e.size if e.kind == "file" else 0,
                     lambda e=e, root=root: _remove_entry(root, e), label=f"{name}: {e.rel}")
    res = acts.result("files", {"rules": len(rules), "rules_refused": refused})
    res.items = per_rule[:12] + res.items[: max(0, 12 - len(per_rule))]
    return res


def _check_rule(ctx: Ctx, rule: dict, roots: list[str]) -> str | tuple:
    """Validate one rule. A string is the refusal reason; a tuple is (root, pattern, max_age_s, keep, files_only)."""
    path, glob = rule.get("path"), rule.get("glob")
    if not roots:
        return "refused: no allowed_roots"
    if not isinstance(path, str) or not os.path.isabs(path):
        return "refused: bad path"
    if not isinstance(glob, str) or not glob.strip():
        return "nothing: empty glob"
    parts = glob.split("/")
    if glob.startswith("/") or ".." in parts or "\0" in glob or any(p == "" for p in parts):
        return "refused: unsafe glob"
    max_age_s, keep, err = _rule_selectors(rule)
    if err:
        return f"nothing: {err}"
    root = os.path.realpath(path)
    if not _inside(root, roots):
        return "refused: outside allowed_roots"
    if ctx.is_protected(root, path):
        return "refused: protected path"
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return "nothing: not a directory"
    except OSError:
        return "nothing: path missing"
    return root, parts, max_age_s, keep, rule.get("files_only") is True


# =========================================================================== trash
def _home_of(user: str) -> tuple[str, int] | None:
    """(home dir, uid) of a configured user. Tests patch this."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        return None
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    return pw.pw_dir, pw.pw_uid


def _trash_date(info_path: str) -> tuple[float | None, str]:
    """(deletion epoch, original Path) from a .trashinfo; (None, '') if unusable. Dates are local time."""
    try:
        with open(info_path, encoding="utf-8", errors="replace") as f:
            text = f.read(4096)
    except OSError:
        return None, ""
    when, orig = None, ""
    for ln in text.splitlines():
        if ln.startswith("DeletionDate=") and when is None:
            try:
                tm = time.strptime(ln.split("=", 1)[1].strip()[:19], "%Y-%m-%dT%H:%M:%S")
                when = time.mktime(tm)
            except ValueError:
                return None, ""
        elif ln.startswith("Path="):
            orig = ln.split("=", 1)[1].strip()
    return when, orig


def _real_dir(path: str, uid: int) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid


@task("trash", klass="C1", tier="daily", title="Trash", timeout=600)
def trash(ctx: Ctx) -> Result:
    """Remove ~/.local/share/Trash entries whose DeletionDate is older than `max_age_days` (files/ + info/)."""
    days = _num(ctx.opt("max_age_days", 30), 1, 3650)
    users = ctx.opt("users", [])
    if days is None or not isinstance(users, list) or not users:
        return Result("ok" if days else "skipped", "no trash users configured" if days else "bad max_age_days",
                      {"mode": "report", "selected": 0})
    acts, skipped, kept = _Acts(ctx), 0, 0
    for user in users:
        home = _home_of(user) if isinstance(user, str) else None
        if not home:
            skipped += 1
            continue
        root = os.path.join(home[0], ".local", "share", "Trash")
        info_dir, files_dir = os.path.join(root, "info"), os.path.join(root, "files")
        # the trash must be a real directory tree owned by the user (no symlink anywhere on the way, none at the
        # end), so a symlinked ~/.local can never aim a root-run delete at somebody else's files
        if os.path.realpath(root) != root or not all(_real_dir(d, home[1]) for d in (root, info_dir, files_dir)):
            skipped += 1
            continue
        try:
            names = sorted(e.name for e in os.scandir(info_dir) if e.name.endswith(".trashinfo"))
        except OSError:
            skipped += 1
            continue
        for fname in names:
            base = fname[: -len(".trashinfo")]
            info_path = os.path.join(info_dir, fname)
            if not base or base in (".", "..") or "/" in base or "\0" in base:
                skipped += 1
                continue
            try:
                if not stat.S_ISREG(os.lstat(info_path).st_mode):
                    skipped += 1
                    continue
            except OSError:
                continue
            when, orig = _trash_date(info_path)
            if when is None:
                skipped += 1                               # unknown deletion date: fail closed, keep it
                continue
            if ctx.now - when <= days * 86400:
                kept += 1
                continue
            payload = os.path.join(files_dir, base)
            try:
                pst = os.lstat(payload)
            except FileNotFoundError:
                pst = None
            except OSError:
                skipped += 1
                continue
            size, is_dir = 0, False
            if pst is not None:
                is_dir = stat.S_ISDIR(pst.st_mode)
                if is_dir:
                    if not shutil.rmtree.avoids_symlink_attacks:
                        skipped += 1
                        continue
                    size, _, complete = _tree_stats(payload)
                    if not complete:
                        skipped += 1                       # cannot bound it: leave it
                        continue
                else:
                    size = pst.st_size

            def rm(payload=payload, info_path=info_path, pst=pst, is_dir=is_dir) -> None:
                if pst is not None:
                    now_st = os.lstat(payload)
                    if (now_st.st_ino, now_st.st_dev) != (pst.st_ino, pst.st_dev):
                        raise _Changed("payload changed since scan")
                    if is_dir:
                        shutil.rmtree(payload)             # fd-based; refuses a symlink, never follows one
                    else:
                        os.unlink(payload)                 # a symlink payload is unlinked, not followed
                os.unlink(info_path)                       # info last: a failure leaves it for the next run

            acts.run("trash-delete", payload, size, rm, protect=(orig,), label=f"{user}: {base}")
    return acts.result("entries", {"users": len(users), "kept": kept, "skipped": skipped})


# =========================================================================== gradle_reaper
@dataclass
class _P:
    pid: int
    ppid: int
    state: str
    ticks: int
    start: int
    argv: list[str]


def _read_proc(pid: int | str) -> _P | None:
    try:
        text = (PROC / str(pid) / "stat").read_text()
        raw = (PROC / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    j = text.rfind(")")
    f = text[j + 2:].split() if j > 0 else []
    try:                              # fields 3,4,14,15,22 of stat; comm (field 2) may contain spaces
        return _P(int(pid), int(f[1]), f[0], int(f[11]) + int(f[12]), int(f[19]),
                  [a for a in raw.split("\0") if a])
    except (IndexError, ValueError):
        return None


def _all_procs() -> list[_P] | None:
    try:
        names = os.listdir(PROC)
    except OSError:
        return None
    return [p for n in names if n.isdigit() and (p := _read_proc(n))]


def _is_java(p: _P) -> bool:
    return bool(p.argv) and os.path.basename(p.argv[0]).startswith("java")


_DAEMON = re.compile(r"GradleDaemon|KotlinCompileDaemon|kotlin-compiler-embeddable")


def _classify(p: _P) -> str:
    """'worker' | 'client' | 'daemon' | ''. Only real JVMs / gradle launchers count, so a shell whose command line
    merely mentions the words (pgrep, grep, editors) is ignored."""
    cmd = " ".join(p.argv)
    if p.argv and os.path.basename(p.argv[0]) in ("gradle", "gradlew"):
        return "client"
    if not _is_java(p):
        return ""
    if re.search(r"Gradle Test Executor|Gradle Worker|GradleWorkerMain", cmd):
        return "worker"
    if re.search(r"GradleWrapperMain|org\.gradle\.launcher\.GradleMain", cmd):
        return "client"
    return "daemon" if _DAEMON.search(cmd) else ""


def _idle(samples: list, need: int, idle_s: float, pct: float) -> tuple[bool, str]:
    """True when the last `need` samples span >= idle_s and CPU growth between every consecutive pair stayed
    below `pct` % of one core (a counter that went backwards means a different process: not idle)."""
    if len(samples) < need:
        return False, f"{len(samples)}/{need} samples"
    win = samples[-need:]
    if win[-1][0] - win[0][0] < idle_s:
        return False, f"observed {(win[-1][0] - win[0][0]) / 60:.0f} of {idle_s / 60:.0f} min"
    for (t0, c0), (t1, c1) in zip(win, win[1:]):
        if t1 <= t0 or c1 < c0:
            return False, "cpu counter reset"
        rate = (c1 - c0) / CLK_TCK / (t1 - t0) * 100
        if rate > pct:
            return False, f"cpu {rate:.2f}%"
    return True, "idle"


def _terminate(pid: int, start: int) -> None:
    """SIGTERM, then SIGKILL only if the same process is still alive 30 s later. Identity (pid + start time) is
    re-checked before each signal so a recycled pid is never hit."""
    def same() -> bool:
        q = _read_proc(pid)
        return q is not None and q.start == start and q.state != "Z"
    if not same():
        raise RuntimeError("process changed or gone")
    _kill(pid, signal.SIGTERM)
    deadline = _mono() + 30
    while _mono() < deadline:
        _sleep(1)
        if not same():
            return
    if same():
        _kill(pid, signal.SIGKILL)


@task("gradle_reaper", klass="C1", tier="daily", title="Idle Gradle/Kotlin daemons", timeout=600)
def gradle_reaper(ctx: Ctx) -> Result:
    """SIGTERM Gradle/Kotlin daemons that showed no CPU growth over successive samples spanning `idle_minutes`.

    Samples live in ctx.state and are recorded on every run (also report mode), keyed by pid + start time.
    `min_samples` successive samples are required: with the daily timer that is min_samples days of idleness.
    Nothing is touched while ANY Gradle client or worker process exists or the gate says a build is active.
    """
    idle_s = (_num(ctx.opt("idle_minutes", 120), 1) or 0) * 60
    need = _num(ctx.opt("min_samples", 8), 2, 1000)
    pct = _num(ctx.opt("idle_cpu_pct", 0.05), 0, 100)
    if not idle_s or need is None or pct is None:
        return _skipped("bad idle_minutes/min_samples config: nothing done")
    procs = _all_procs()
    if procs is None:
        return _skipped("/proc unreadable: nothing done")
    me = os.getpid()
    kinds = {p.pid: _classify(p) for p in procs if p.pid != me}
    daemons = [p for p in procs if kinds.get(p.pid) == "daemon"]
    blockers = [p for p in procs if kinds.get(p.pid) in ("worker", "client")]

    # record this run's sample for every live daemon; forget daemons that are gone or whose pid was reused
    old, new = ctx.state.get("gradle", {}), {}
    for p in daemons:
        e = old.get(str(p.pid))
        s = e["s"] if isinstance(e, dict) and e.get("start") == p.start and isinstance(e.get("s"), list) else []
        if not s or s[-1][0] < ctx.now:
            s = s + [[ctx.now, p.ticks]]
        new[str(p.pid)] = {"start": p.start, "s": s[-(int(need) + 8):]}
    ctx.state["gradle"] = new

    if not daemons:
        return Result("ok", "no Gradle/Kotlin daemons running", {"mode": "apply" if ctx.apply else "report", "daemons": 0})
    busy, why = _busy("gradle")
    if blockers or busy:
        return Result("info", _ascii(f"{len(daemons)} daemons left alone: build active "
                                     f"({'client/worker pid %d' % blockers[0].pid if blockers else why})"),
                      {"mode": "apply" if ctx.apply else "report", "daemons": len(daemons), "idle": 0})
    acts, idle_n, items = _Acts(ctx), 0, []
    for p in sorted(daemons, key=lambda p: p.pid):
        ok, reason = _idle(new[str(p.pid)]["s"], int(need), idle_s, pct)
        label = f"gradle pid {p.pid}"
        if not ok:
            items.append({"name": label, "size": "-", "state": f"kept: {reason}"})
            continue
        idle_n += 1
        acts.run("sigterm-daemon", label, 0, lambda p=p: _terminate(p.pid, p.start),
                 protect=(" ".join(p.argv)[:2000],), label=label)
    res = acts.result("daemons", {"daemons": len(daemons), "idle": idle_n})
    res.items = (res.items + items)[:12]
    if _nothing_done(acts):
        res.summary = _ascii(f"{len(daemons)} daemons, none idle long enough yet")
    return res


def _nothing_done(acts: _Acts) -> bool:
    return not any(acts.n[k] for k in ("done", "would", "protected", "failed", "oversize", "capped", "refused",
                                       "gone", "backoff"))


# =========================================================================== caps
def _running_containers() -> dict[str, str] | None:
    r = sh(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"], timeout=20)
    if r.returncode != 0:
        return None
    out = {}
    for ln in r.stdout.splitlines():
        cid, _, nm = ln.strip().partition(" ")
        if re.fullmatch(r"[0-9a-f]{12,64}", cid) and nm:
            out[nm.split(",")[0]] = cid
    return out


@dataclass
class _Obs:
    """What the spike sampler knows about one container."""
    n: int = 0                    # samples inside the look-back window
    first: float = 0.0            # epoch of the oldest / newest of them
    last: float = 0.0
    floor: int = 0                # highest of: any anon+swap sample, any cgroup memory.peak the sampler recorded


def _observed(name: str, days: float) -> _Obs:
    """Sampler history for a container. memory.peak is the cgroup's own high-water mark, so it also covers spikes
    between two 15-minute samples; it includes page cache, which makes it a conservative (high) floor."""
    o = _Obs()
    for rec in read_history(days * 86400, "sample"):
        c = rec.get("c", {}).get(name) if isinstance(rec.get("c"), dict) else None
        t = _num(rec.get("t"), 0)
        if not isinstance(c, dict) or _num(c.get("anon"), 0) is None or t is None:
            continue
        o.n += 1
        o.first = t if o.n == 1 else min(o.first, t)
        o.last = max(o.last, t)
        tot = int(c["anon"]) + int(_num(c.get("swap"), 0) or 0)
        o.floor = max(o.floor, tot, int(_num(c.get("peak"), 0) or 0))
    return o


@task("caps", klass="C1", tier="daily", title="Container memory ceilings", timeout=300)
def caps(ctx: Ctx) -> Result:
    """`docker update --memory C --memory-swap C*(1+ratio)` for running containers in `ceilings`.

    Idempotent (nothing when HostConfig.Memory already equals the ceiling); a stricter existing limit is kept.
    A ceiling that later OOM-kills a busy container is the risk, so it is refused unless the sampler history is
    long enough to trust: >= min_samples samples spanning >= min_span_hours (default 72), the newest one not older
    than max_sample_age_hours (default 2, i.e. the sampler is alive), and ceiling >= 1.25x the highest anon+swap
    or cgroup memory.peak seen in the look-back window (peak_days, default 14).
    """
    ceilings = ctx.opt("ceilings", {})
    ratio = _num(ctx.opt("swap_extra_ratio", 0.25), 0, 4)
    min_samples = int(_num(ctx.opt("min_samples", 8), 1, 100000) or 8)
    min_span_h = _num(ctx.opt("min_span_hours", 72), 0, 24 * 90)
    max_age_h = _num(ctx.opt("max_sample_age_hours", 2), 0.1, 24 * 90)
    peak_days = _num(ctx.opt("peak_days", 14), 0.01, 90) or 14
    if ratio is None or min_span_h is None or max_age_h is None:
        return _skipped("bad swap_extra_ratio/min_span_hours/max_sample_age_hours config: nothing done")
    if not isinstance(ceilings, dict) or not ceilings:
        return Result("ok", "no ceilings configured", {"mode": "report", "selected": 0, "containers": 0})
    running = _running_containers()
    if running is None:
        return _skipped("docker unavailable: nothing done")
    acts, items, state_n = _Acts(ctx), [], {"already": 0, "stricter": 0, "stopped": 0, "refused": 0}
    for name in sorted(ceilings):
        gib = _num(ceilings[name], 0.1, 4096)
        if not isinstance(name, str) or not _NAME.fullmatch(name) or gib is None:
            state_n["refused"] += 1
            items.append({"name": str(name)[:40], "size": "-", "state": "refused: bad ceiling config"})
            continue
        if name not in running:
            state_n["stopped"] += 1
            continue
        if ctx.is_protected(name):                  # reported as such even before there is history to judge by
            acts.n["protected"] += 1
            items.append({"name": name, "size": human(int(gib * GIB)), "state": "protected: never capped"})
            continue
        r = sh(["docker", "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}", name], timeout=20)
        f = r.stdout.split()
        if r.returncode != 0 or len(f) != 2 or not all(x.lstrip("-").isdigit() for x in f):
            state_n["refused"] += 1
            items.append({"name": name, "size": "-", "state": "refused: inspect failed"})
            continue
        cur, target = int(f[0]), int(gib * GIB)
        swap_t = int(gib * (1 + ratio) * GIB)
        if cur == target:
            state_n["already"] += 1
            continue
        if 0 < cur < target:
            state_n["stricter"] += 1
            items.append({"name": name, "size": human(cur), "state": "kept: stricter limit already set"})
            continue
        obs = _observed(name, peak_days)
        span_h = (obs.last - obs.first) / 3600
        why = (f"{obs.n}/{min_samples} samples" if obs.n < min_samples
               else f"{span_h:.0f} h of history, need {min_span_h:g}" if span_h < min_span_h
               else f"sampler silent for {(ctx.now - obs.last) / 3600:.1f} h" if ctx.now - obs.last > max_age_h * 3600
               else f"< 1.25x peak {human(obs.floor)}" if target < 1.25 * obs.floor else "")
        if why:
            state_n["refused"] += 1
            items.append({"name": name, "size": human(target), "state": f"refused: {why}"})
            continue

        def upd(name=name, target=target, swap_t=swap_t) -> None:
            _run_ok(["docker", "update", "--memory", str(target), "--memory-swap", str(swap_t), name], 60)

        acts.run("docker-update-memory", name, 0, upd,
                 label=f"{name} {human(cur) if cur else 'unlimited'} -> {human(target)}")
    res = acts.result("limits", {"containers": len(ceilings), **{k: v for k, v in state_n.items()}})
    res.summary = _ascii(_caps_summary(ctx, acts, state_n))
    res.items = (res.items + items)[:12]
    return res


def _caps_summary(ctx: Ctx, acts: _Acts, st: dict) -> str:
    n = acts.n["done"] if ctx.apply else acts.n["would"]
    s = f"{'set' if ctx.apply else 'report: would set'} {n} memory limit(s)"
    for k, word in (("already", "already set"), ("stricter", "stricter kept"), ("refused", "refused"),
                    ("stopped", "not running")):
        if st[k]:
            s += f", {st[k]} {word}"
    for k, word in (("protected", "protected"), ("failed", "failed"), ("capped", "deferred by cap"),
                    ("backoff", "in retry backoff")):
        if acts.n[k]:
            s += f", {acts.n[k]} {word}"
    return s


# =========================================================================== c2_candidates (weekly, plan + approval)
def _approved(task_name: str, h: str, ttl_h: float) -> bool:
    """An approval file STATE_DIR/approvals/<task>.<hash> (written by `homelab-maint approve`) not older than ttl."""
    p = core.STATE_DIR / "approvals" / f"{task_name}.{h}"
    try:
        return time.time() - p.stat().st_mtime <= ttl_h * 3600
    except OSError:
        return False


def _container_mounts() -> dict[str, list[str]] | None:
    """{container name: [host source paths]} of ALL containers (a stopped one mounts its data again on the next
    start), None if docker cannot say. Every source is listed both as docker prints it and fully resolved, so a
    mount reached through a symlink is still seen."""
    ps = sh(["docker", "ps", "-a", "-q", "--no-trunc"], timeout=20)
    if ps.returncode != 0:
        return None
    ids = ps.stdout.split()
    out: dict[str, list[str]] = {}
    for i in range(0, len(ids), 100):
        r = sh(["docker", "inspect", "--format", '{{.Name}}|{{range .Mounts}}{{.Source}};{{end}}', *ids[i:i + 100]],
               timeout=60)
        if r.returncode != 0:
            return None
        for ln in r.stdout.splitlines():
            nm, _, srcs = ln.partition("|")
            raw = [x for x in srcs.split(";") if x.startswith("/")]
            out[nm.lstrip("/")] = sorted(set(raw) | {os.path.realpath(x) for x in raw})
    return out


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _users_of(path: str, mounts: dict[str, list[str]] | None, is_dir: bool,
              timeout: float = 20) -> tuple[list[str] | None, str]:
    """(containers mounting it or None if unknown, open-files verdict 'none'|'in use'|'skipped').

    'none' only when lsof positively ran clean as root; a timeout (124), a missing tool (127), any other failure,
    error output, or not being root (lsof then cannot see other users' processes) is 'skipped' = unknown."""
    cont = None
    if mounts is not None:
        real = os.path.realpath(path)
        cont = sorted(n for n, srcs in mounts.items()
                      if any(s != "/" and (_overlap(s, path) or _overlap(s, real)) for s in srcs))
    r = sh(["lsof", "-w", "-F", "pft", *(["+D"] if is_dir else ["--"]), path], timeout=int(timeout))
    if r.returncode not in (0, 1):
        return cont, "skipped"
    if _lsof_holders(r.stdout):
        return cont, "in use"
    return cont, "none" if (_euid() == 0 and not (r.stderr or "").strip()) else "skipped"


def _in_use_reason(path: str, is_dir: bool, timeout: float) -> str:
    """'' only when a FRESH docker snapshot (running and stopped containers) and lsof both positively say the path
    is unused. Anything else, including 'could not find out', is a reason to leave the data alone."""
    cont, openf = _users_of(path, _container_mounts(), is_dir, timeout)
    if cont is None:
        return "container mounts unknown"
    if cont:
        return "mounted by " + ",".join(cont)
    if openf == "in use":
        return "open files"
    return "" if openf == "none" else "open-file check incomplete"


def _lsof_holders(out: str) -> list[str]:
    """Pids that really hold the tree, from `lsof -F pft` (p<pid>, then f<fd> t<type> per file). Directory handles
    (editors and file watchers keep dozens open) are ignored; cwd, executables, mapped files and open regular files
    count."""
    pids: set[str] = set()
    pid, fd, typ = None, None, None

    def flush() -> None:
        if pid and fd is not None and (fd in ("cwd", "rtd", "txt", "mem") or (fd[:1].isdigit() and typ in ("REG", "DEL"))):
            pids.add(pid)

    for ln in out.splitlines():
        k, v = ln[:1], ln[1:]
        if k == "p":
            flush()
            pid, fd, typ = v, None, None
        elif k == "f":
            flush()
            fd, typ = v, None
        elif k == "t":
            typ = v
    flush()
    return sorted(pids)


def _du(path: str, timeout: int) -> int | None:
    r = sh(["du", "-sxb", "--", path], timeout=max(timeout, 1))
    if r.returncode in (0, 1) and r.stdout.strip():                 # rc 1 = unreadable subdir, total still printed
        first = r.stdout.split()[0]
        return int(first) if first.isdigit() else None
    return None


# rsync -a plus hardlinks, sparse files, ACLs and xattrs: the copy must be a faithful one before the source goes
_RSYNC = ["rsync", "-aHSAX"]


def _archive_target_problem(arch: str, src: str) -> str:
    """'' if `arch` is a usable cold-storage target, else why not: absolute, no symlink anywhere in the path, a
    directory, and on a mounted filesystem that is neither the root filesystem nor the source's. A stub 'archive'
    directory left on / after the cold disk was unmounted would otherwise be filled until the root disk is full
    and the source removed."""
    if not os.path.isabs(arch):
        return "not an absolute path"
    if os.path.realpath(arch) != os.path.normpath(arch):
        return "symlink in the archive path"
    try:
        a, root, s = os.stat(arch), os.stat("/"), os.stat(src)
    except OSError:
        return "archive dir missing or unreadable"
    if not stat.S_ISDIR(a.st_mode):
        return "not a directory"
    if a.st_dev == root.st_dev:
        return "on the root filesystem (cold disk not mounted?)"
    if a.st_dev == s.st_dev:
        return "same filesystem as the source"
    return ""


def _archive_and_remove(src: str, dest_root: str, is_dir: bool, recheck: Callable[[], None] | None = None) -> None:
    """rsync src into dest_root/<basename>, verify the copy by CHECKSUM (a quick size+mtime check would pass a
    damaged copy), re-check that the source is still unused, then remove src. Never merges into an existing copy."""
    base = os.path.basename(src.rstrip("/"))
    dest = os.path.join(dest_root, base)
    problem = _archive_target_problem(dest_root, src)
    if problem:
        raise RuntimeError(f"archive target refused: {problem}")
    if os.path.lexists(dest):
        raise RuntimeError("archive target exists: not overwriting")
    a, b = (src.rstrip("/") + "/", dest + "/") if is_dir else (src, dest)
    _run_ok([*_RSYNC, "--", a, b], 3600)
    chk = sh([*_RSYNC, "-n", "-c", "--itemize-changes", "--", a, b], timeout=3600)
    if chk.returncode != 0 or chk.stdout.strip():
        raise RuntimeError("archive verification failed; source kept")
    if recheck:
        recheck()                      # the copy took minutes: something may have opened the source meanwhile
    _remove_path(src)


def _remove_path(path: str) -> None:
    if os.path.realpath(path) != os.path.normpath(path):
        raise RuntimeError("symlink in the path: refusing")
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError("refusing to remove a symlink candidate")
    if stat.S_ISDIR(st.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("platform rmtree is not symlink-safe")
        shutil.rmtree(path)
    else:
        os.unlink(path)


@task("c2_candidates", klass="C2", tier="weekly", title="Cleanup candidates", timeout=7200, needs_root=True)
def c2_candidates(ctx: Ctx) -> Result:
    """Weekly planner for big one-off leftovers (needs a human 'approve'). Apply needs ctx.apply AND an approval
    file for the plan hash; items flagged needs_manual_check are refused unless allow_manual_check_items is true."""
    cands = ctx.opt("candidates", [])
    if not isinstance(cands, list):
        return _skipped("bad candidates config")
    budget = _num(ctx.opt("du_budget_s", 600), 1) or 600
    lsof_s = _num(ctx.opt("lsof_timeout_s", 20), 1, 3600) or 20
    t0 = _mono()
    plan_items, items, missing, protected_n = [], [], 0, 0
    for c in cands:
        c = c if isinstance(c, dict) else {}
        name, path = str(c.get("name") or "")[:60], c.get("path")
        if not name or not isinstance(path, str) or not os.path.isabs(path):
            items.append({"name": name or "?", "size": "-", "state": "refused: bad candidate config"})
            continue
        try:
            st = os.lstat(path)
        except OSError:
            missing += 1
            continue
        why = str(c.get("why") or "")[:140]
        is_dir, is_link = stat.S_ISDIR(st.st_mode), stat.S_ISLNK(st.st_mode)
        manual = c.get("needs_manual_check") is True
        arch = c.get("archive_to")
        if "archive_to" in c and not (isinstance(arch, str) and os.path.isabs(arch)):
            # a present-but-unusable archive_to must never silently turn "archive, then remove" into "just remove"
            items.append({"name": name, "size": "-", "state": "refused: archive_to must be an absolute path"})
            continue
        bad = ("symlink" if is_link else "symlink in path" if os.path.realpath(path) != os.path.normpath(path)
               else "too shallow" if len(Path(path).parts) < 4 else "")
        if ctx.is_protected(path, name):
            protected_n += 1
            items.append({"name": name, "size": "-", "state": "protected: never auto-removed"})
            continue
        if bad:
            items.append({"name": name, "size": "-", "state": f"refused: {bad}"})
            continue
        size = _du(path, int(min(120, max(budget - (_mono() - t0), 1)))) if (_mono() - t0) < budget else None
        exact = size is not None
        if size is None:
            size = st.st_size
        # the plan holds only facts that do not depend on a time budget (its hash must be reproducible);
        # the newest-file date from a bounded walk is for the dashboard only
        mtime = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
        newest = _tree_stats(path, 100_000, 5.0)[1] if is_dir else 0.0
        newest_s = time.strftime("%Y-%m-%d", time.localtime(max(newest, st.st_mtime)))
        cont, openf = _users_of(path, _container_mounts(), is_dir, lsof_s)      # fresh snapshot per candidate
        q = shlex.quote
        if arch:
            dest = os.path.join(arch, os.path.basename(path.rstrip("/")))
            rs = " ".join(_RSYNC)
            cmd = (f"{rs} -- {q(path + '/')} {q(dest + '/')} && rm -rf -- {q(path)}" if is_dir
                   else f"{rs} -- {q(path)} {q(dest)} && rm -f -- {q(path)}")
        else:
            cmd = f"rm -rf -- {q(path)}" if is_dir else f"rm -f -- {q(path)}"
        mib = size // MIB * MIB                                    # coarse size keeps the plan hash stable
        plan_items.append({"name": name, "path": path, "why": why, "bytes": mib, "size_exact": exact,
                           "mtime": mtime, "archive_to": arch, "needs_manual_check": manual, "command": cmd})
        if manual:
            state = "manual check"
        elif cont:
            state = "in use: container " + ",".join(cont)
        elif openf == "in use":
            state = "in use: open files"
        elif cont is None:
            state = "unverified (container mounts unknown)"          # apply refuses these
        elif openf != "none":
            state = "unverified (open-file check incomplete)"
        else:
            state = "candidate"
        row = {"name": name, "size": human(size) + ("" if exact else "?"), "state": state, "mtime": newest_s,
               "open_files": openf}
        if arch and (why_not := _archive_target_problem(arch, path)):
            row["archive"] = f"apply would refuse: {why_not}"
        items.append(row)
    plan_items.sort(key=lambda i: i["name"])
    plan = {"items": plan_items, "total_bytes": sum(i["bytes"] for i in plan_items)}
    h = plan_hash(plan)
    metrics = {"mode": "apply" if ctx.apply else "report", "candidates": len(plan_items), "missing": missing,
               "protected": protected_n, "total_h": human(plan["total_bytes"]), "plan_hash": h}
    summary = (f"{len(plan_items)} candidates, {human(plan['total_bytes'])}; plan {h}" if plan_items
               else "no cleanup candidates present")
    res = Result("info" if plan_items else "ok", _ascii(summary), metrics,
                 sorted(items, key=lambda i: i["name"])[:12], plan=plan, alert=False)
    if not (ctx.apply and plan_items):
        return res
    if not _approved(ctx.name, h, float(_num(ctx.opt("approval_ttl_hours", 48), 1) or 48)):
        res.summary = _ascii(f"{summary}; awaiting approval: homelab-maint approve c2_candidates {h}")
        return res
    return _apply_plan(ctx, res, plan_items, h)


def _apply_plan(ctx: Ctx, res: Result, plan_items: list[dict], h: str) -> Result:
    if _euid() != 0:
        # lsof only sees the caller's own processes without root: its silence would prove nothing
        audit(ctx.name, "c2-apply", h, 0, "refused-not-root")
        res.status = "warn"
        res.summary = _ascii(f"{res.summary}; apply refused: needs root so lsof can see every process")
        return res
    acts = _Acts(ctx)
    allow_manual = ctx.opt("allow_manual_check_items") is True
    lsof_s = _num(ctx.opt("lsof_apply_timeout_s", 300), 1, 7200) or 300      # generous: a timeout means refuse
    for it in plan_items:
        path, label = it["path"], it["name"]
        if it["needs_manual_check"] and not allow_manual:
            acts._note("refused", f"{label} (needs manual check)", it["bytes"])
            continue
        if not it["size_exact"]:                       # an unmeasured size would slip past the byte cap
            acts._note("refused", f"{label} (size unknown)", it["bytes"])
            continue
        try:
            st = os.lstat(path)
        except OSError:
            acts._note("refused", f"{label} (gone)", 0)
            continue
        if stat.S_ISLNK(st.st_mode):
            acts._note("refused", f"{label} (is a symlink)", it["bytes"])
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        # fresh docker snapshot (stopped containers included) and lsof, right before this item
        busy = _in_use_reason(path, is_dir, lsof_s)
        if busy:
            acts._note("refused", f"{label}: {busy}", it["bytes"])             # 60-char label limit: keep it short
            continue
        arch = it["archive_to"]
        if arch:
            bad = ("archive dir protected" if ctx.is_protected(arch) else _archive_target_problem(arch, path)
                   or ("archive dir too small" if _free_bytes(arch) < it["bytes"] * 1.05 else ""))
            if bad:
                acts._note("refused", f"{label} ({bad})", it["bytes"])
                continue

        def recheck(p=path, d=is_dir) -> None:
            why = _in_use_reason(p, d, lsof_s)
            if why:
                raise _Changed(f"in use after copy: {why}")

        fn = (lambda p=path, a=arch, d=is_dir, rc=recheck: _archive_and_remove(p, a, d, rc)) if arch else \
             (lambda p=path: _remove_path(p))
        acts.run("c2-archive-remove" if arch else "c2-remove", path, it["bytes"], fn, protect=(label,), label=label)
    out = acts.result("candidates", {"plan_hash": h})
    out.plan, out.alert = res.plan, False
    if acts.n["done"]:
        for p in (core.STATE_DIR / "approvals").glob(f"{ctx.name}.*"):
            try:
                p.unlink()                       # approvals are single use
            except OSError:
                pass
    return out


def _free_bytes(path: str) -> int:
    try:
        s = os.statvfs(path)
        return s.f_bavail * s.f_frsize
    except OSError:
        return 0
