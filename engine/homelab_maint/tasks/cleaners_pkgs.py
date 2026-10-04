"""cleaners_pkgs: package-level C1 cleaners that remove ONLY what is provably not in use.

stale_driver_packages  (daily)   apt packages of NVIDIA driver branches other than the LOADED kernel module
apt_autoremove_unused  (daily)   `apt autoremove --purge` candidates, minus anything not PROVABLY unused from /proc (see below)
flatpak_unused         (weekly)  `flatpak uninstall --unused`, only when every listed ref is a runtime/extension, never an app

Shared rules (the point of this module):
  * every mutation goes through `_Acts`/`ctx.act` (caps, PAUSE, protected list, audit); report mode and dry-run list exactly
    the set that apply would purge, each with its short in-use proof in `items[*].proof`;
  * a set is only purged when `apt-get -s purge <set>` removes EXACTLY that set (no extras, no installs): the closure proof;
  * "in use" is decided by inuse.py (one pass over /proc, whole host visible; non-root, partial or unreadable /proc or any
    probe error => unknown => keep) and never from "nothing maps it" alone: a package is only AUTO-selected when every
    non-documentation file it ships is a shared library/plugin under /usr/lib (what /proc can actually show). Anything with
    an executable, script, Python/Perl module, systemd/D-Bus/autostart/cron/udev/polkit/PAM file, .desktop launcher or data is
    "kept: not provable from /proc" unless the owner lists it in `allow_purge`; and a package that an installed package
    OUTSIDE the set Depends on or Recommends (apt's purge simulation ignores Recommends) is kept;
  * packages with locally edited conffiles are never purged (purge deletes them for good);
  * nothing runs while apt/dpkg is busy or locked, or while dpkg reports an inconsistent state (`dpkg --audit`);
  * after an apply the system is re-verified (nvidia-smi + dkms + the on-disk module for the NEXT boot for drivers,
    `systemctl --failed` of the system and of logged-in users for autoremove, the app list for flatpak); a regression is
    reported as crit/warn so it pages;
  * owner options are type-checked before anything runs (a bare string would be iterated per character, turning a keep
    list into a no-op): a malformed option skips the task.

Owner options ([tasks.<name>] in maint.toml; every list is type-checked, mode = "report" is the default for all three):
  stale_driver_packages  keep_branches = ["535"]     driver branches to keep installed although they are not the loaded one
  apt_autoremove_unused  keep = ["tree", "inxi"]      package globs never purged (the owner's shell/cron tools)
                         never = ["foo*"]             globs added to the built-in critical classes (kernel, boot, GPU/CUDA, JDK, ...)
                         allow_purge = ["libgl1-amber-dri"]   globs whose files may be of a kind /proc cannot speak for (config,
                                                      data, -dev): ONLY the file-class check is waived, every other proof stays
                         max_candidates = 80          simulations per run
  flatpak_unused         installations = ["system", "user:ohmz"]   (other installations' apps are cross-checked automatically)

Formats parsed here were inspected on the real machine (read only): `dpkg-query -W`, /var/lib/dpkg/status, `apt-get -s
purge|autoremove`, `dkms status`, `dpkg --verify`, `systemctl --failed --plain`, `flatpak list --columns=..`, and the table
printed by `flatpak uninstall --unused` (stdin gets an explicit "n" for its prompt; verified on a throw-away FLATPAK_USER_DIR).
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import pwd
import re
import stat
from pathlib import Path
from typing import NamedTuple

from .. import inuse
from ..core import Ctx, Result, human, sh, task
from .cleaners import _Acts, _apt_lock_state, _ascii, _busy, _Changed, _num, _parse_size, _skipped

DPKG_INFO = Path("/var/lib/dpkg/info")
DPKG_STATUS = Path("/var/lib/dpkg/status")
HOME_ROOT = "/home"               # flatpak user installations are looked for below here (consumers of system runtimes)
FS_ROOT = ""                      # test hook: a prefix stripped from listed paths before they are classified
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none", "NEEDRESTART_MODE": "l"}
_euid = os.geteuid


def _uname() -> str:
    return os.uname().release


def _run_ok(cmd: list[str], timeout: int, env: dict | None = None) -> str:
    """Run a MUTATING command; raise with a short reason unless it exits 0 (ctx.act audits the failure)."""
    r = sh(cmd, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} rc={r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-100:]}")
    return r.stdout


def _row(name: str, size: int | str, state: str, proof: str = "") -> dict:
    return {"name": _ascii(name, 60), "size": human(size) if isinstance(size, int) else size, "state": _ascii(state, 60),
            "proof": _ascii(proof, 140)}


# =========================================================================== dpkg / apt helpers
_PKG = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?")
_REMOVE = re.compile(r"^(?:Purg|Remv)\s+(\S+)")


class _Pkg(NamedTuple):
    state: str        # "i" installed | "c" config files only (rc) | "bad" half-installed/unpacked/...
    version: str
    size: int         # installed bytes (0 for config-only)
    hold: bool


def _native_arch() -> str | None:
    r = sh(["dpkg", "--print-architecture"], timeout=15)
    a = r.stdout.strip()
    return a if r.returncode == 0 and re.fullmatch(r"[a-z0-9-]+", a) else None


def _norm(name: str, native: str) -> str:
    """apt prints the native arch bare and foreign arches qualified; dpkg-query qualifies multiarch packages always."""
    base, _, arch = name.partition(":")
    return base if not arch or arch == native else name


def _installed(native: str) -> dict[str, _Pkg] | None:
    """{normalised name: _Pkg} for every package dpkg knows that is not 'not-installed'; None when unparsable."""
    r = sh(["dpkg-query", "-W", "-f", "${binary:Package}\t${db:Status-Abbrev}\t${Version}\t${Installed-Size}\n"],
           timeout=90)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    out: dict[str, _Pkg] = {}
    for ln in r.stdout.splitlines():
        f = ln.split("\t")
        if len(f) != 4 or not _PKG.fullmatch(f[0]) or len(f[1].strip()) < 2:
            continue
        ab = f[1].strip()
        if ab[1] == "n":
            continue                                              # known to dpkg but not installed
        state = "i" if ab[1] == "i" and len(ab) == 2 else "c" if ab[1] == "c" and len(ab) == 2 else "bad"
        out[_norm(f[0], native)] = _Pkg(state, f[2], int(f[3]) * 1024 if f[3].isdigit() and state == "i" else 0,
                                        ab[0] == "h")
    return out or None


def _holds(native: str) -> set[str] | None:
    r = sh(["apt-mark", "showhold"], timeout=30)
    if r.returncode != 0:
        return None
    return {_norm(x, native).split(":")[0] for x in r.stdout.split()}


_AUDIT_BENIGN = re.compile(r"missing the (?:list|md5sums) control file")


def _dpkg_clean() -> str:
    """'' when `dpkg --audit` (root only) reports nothing that matters, else why not. A package whose list/md5sums
    control file is missing (damaged metadata of one third-party .deb, seen on this host) is not a half-finished
    operation; every other section (unpacked, half-configured, triggers pending, ...) is."""
    r = sh(["dpkg", "--audit"], timeout=60)
    if r.returncode != 0:
        return f"dpkg --audit failed rc={r.returncode} (not root?)"
    for block in re.split(r"\n\s*\n", r.stdout.strip()):
        if block.strip() and not _AUDIT_BENIGN.search(" ".join(block.split("\n")[:2])):
            return "dpkg --audit: " + _ascii(" ".join(block.split())[:70], 70)
    return ""


def _apt_ready(ctx: Ctx) -> str:
    """'' when it is safe to look at / change packages, else the reason to skip (never while apt/dpkg is busy)."""
    busy, why = _busy("apt")
    if busy:
        return f"apt/dpkg busy ({why})"
    lock = _apt_lock_state()
    if lock == "busy" or (lock == "unknown" and ctx.apply):
        return f"apt lock {lock}"
    if ctx.apply and _euid() != 0:
        return "needs root to change packages"
    return ""


def _sim(args: list[str], native: str) -> tuple[list[str] | None, str]:
    """`apt-get -s <args>` => (names it would remove, why-not). Any error line, any install, any odd name => None."""
    r = sh(["apt-get", "-s", *args], timeout=240, env=APT_ENV)
    text = (r.stdout or "") + "\n" + (r.stderr or "")
    errs = [ln for ln in text.splitlines() if ln.startswith("E:")]
    if r.returncode != 0 or errs:
        return None, _ascii((errs[0] if errs else f"apt-get -s rc={r.returncode}"), 90)
    if any(ln.startswith("Inst ") for ln in r.stdout.splitlines()):
        return None, "simulation would install packages"
    names = []
    for ln in r.stdout.splitlines():
        m = _REMOVE.match(ln)
        if m:
            if not _PKG.fullmatch(m.group(1)):
                return None, "unparsable package name in simulation"
            names.append(_norm(m.group(1), native))
    return sorted(set(names)), ""


def _sim_purge(names: list[str], native: str) -> tuple[list[str] | None, str]:
    for n in names:
        if not _PKG.fullmatch(n):
            return None, "unsafe package name"
    return _sim(["purge", *names], native)


def _pkg_files(name: str, native: str) -> list[str] | None:
    """Files a package owns (/var/lib/dpkg/info/<pkg>[:arch].list, else dpkg-query -L); None when neither answers."""
    base = name.split(":")[0]
    for cand in (name, f"{name}:{native}") if ":" not in name else (name,):
        try:
            return [ln.rstrip("\n") for ln in (DPKG_INFO / f"{cand}.list").read_text(errors="replace").splitlines()]
        except OSError:
            continue
    r = sh(["dpkg-query", "-L", name if ":" in name else base], timeout=30)
    return r.stdout.splitlines() if r.returncode == 0 and r.stdout.strip() else None


_SO = re.compile(r"\.so(?:\.|$)")
_DOCS = re.compile(r"^/usr/share/(?:doc|man|info|locale|lintian|bug)(?:/|$)")
# directories under /usr/lib whose .so files are NOT checkable through /proc: interpreters' modules, helpers a service or
# a login launches on demand, plugins of the auth/transport stack
_LIB_DENY = re.compile(r"^(?:python[\d.]*|perl[\d.]*|ruby[\d.]*|node[\w.-]*|jvm|java|lua[\d.]*|php[\d.]*|tcl[\d.]*|guile[\w.-]*|"
                       r"mono|cgi-bin|systemd|udev|libexec|bin|sbin|dbus-1\.0|polkit-1|security|pam[\w.-]*|cups|sasl2|ssh|"
                       r"openssh|apt|dpkg|firmware|gconv)$")


def _rel(p: str) -> str:
    """The path as the running system sees it: FS_ROOT stripped (tests), /lib/ treated as /usr/lib/ (merged-usr)."""
    if FS_ROOT and p.startswith(FS_ROOT):
        p = p[len(FS_ROOT):]
    return "/usr" + p if p.startswith("/lib/") else p


def _odd_file(p: str) -> str:
    """'' when `p` is something /proc can speak for (a shared library/plugin under /usr/lib, outside the deny dirs), else a
    short reason. Documentation is filtered out by the caller. Executables and interpreter modules (a `python3 -s` process
    leaves no trace of the modules it imported), D-Bus/autostart/systemd/cron/udev/polkit/PAM/.desktop files, data: odd."""
    rel = _rel(p)
    if not rel.startswith("/usr/lib/") or not _SO.search(os.path.basename(rel)):
        return f"ships {rel[:70]} (not a shared library under /usr/lib)"
    bad = next((seg for seg in rel[len("/usr/lib/"):].split("/")[:-1] if _LIB_DENY.match(seg)), "")
    return f"ships {rel[:70]} ({bad} plugin dir)" if bad else ""


class _Inv(NamedTuple):
    libs: list[str]       # shared-library files that exist
    other: list[str]      # other non-documentation files that exist (not directories)
    absent: list[str]     # listed but not on disk: still matched against /proc (a deleted file can stay mapped)
    odd: str              # first file outside the allow-list ('' = every file is documentation or a library/plugin)


def _pkg_inventory(name: str, native: str) -> _Inv | None:
    files = _pkg_files(name, native)
    if files is None:
        return None
    libs, other, absent, odd = [], [], [], ""
    for p in files:
        if not p.startswith("/") or p == "/." or _DOCS.match(_rel(p)):
            continue
        try:
            if stat.S_ISDIR(os.lstat(p).st_mode):
                continue
        except OSError:
            absent.append(p)
            continue
        (libs if _SO.search(os.path.basename(p)) else other).append(p)
        odd = odd or _odd_file(p)
    return _Inv(libs, other, absent, odd)


def _pkg_in_use(name: str, native: str, inv: _Inv | None = None) -> inuse.Proof:
    """in use = any file of the package is mapped, an exe, an open file, a cwd, or named on the command line / in the
    environment of a running process (an interpreter-run script leaves its path ONLY on argv). Unknown whenever /proc was
    not fully readable, the file list cannot be read, or there is no file on disk to ask about (nothing proven is not unused)."""
    inv = inv or _pkg_inventory(name, native)
    if inv is None:
        return inuse.Proof(True, False, "unknown: file list of the package unreadable")
    if not (inv.libs or inv.other):
        return inuse.Proof(True, False, "unknown: no files on disk to prove anything about")
    res = inuse.files_in_use(inv.libs + inv.other + inv.absent, inuse.ALL_KINDS)
    bad = [r for r in res.values() if r.used]
    if bad:
        b = sorted(bad, key=lambda r: (r.known, r.why))[0]
        return inuse.Proof(True, b.known, b.why)
    return inuse.Proof(False, True, f"not mapped/open/named by any process ({len(inv.libs)} libs, "
                                     f"{len(inv.other)} files checked)")


# --- dpkg database (/var/lib/dpkg/status): dependencies, recommendations, provides, conffiles --------------------------
_REL_NAME = re.compile(r"([a-z0-9][a-z0-9+.-]*)(?::([a-z0-9-]+))?")


class _Rec(NamedTuple):
    installed: bool                          # anything but config-files/not-installed (half-configured counts: be careful)
    arch: str                                # dpkg Architecture (amd64, i386, all)
    foreign: bool                            # Multi-Arch: foreign (satisfies dependencies of every architecture)
    needs: frozenset                         # {(name, arch qualifier or "")}: Depends + Pre-Depends + Recommends, flattened
    provides: frozenset                      # virtual package names
    conffiles: tuple                         # ((path, recorded md5), ...)


def _status_text() -> str | None:
    try:
        return DPKG_STATUS.read_text(errors="replace")
    except OSError:
        return None


def _rel_names(v: str) -> set[tuple[str, str]]:
    out = set()
    for part in re.split(r"[,|]", v):
        m = _REL_NAME.match(re.sub(r"\([^)]*\)|\[[^\]]*\]|<[^>]*>", " ", part).strip())
        if m:
            out.add((m.group(1), m.group(2) or ""))
    return out


def _dpkg_db(native: str) -> dict[str, _Rec] | None:
    """{apt-form name: _Rec} from the dpkg status file; None when it cannot be read or a stanza is malformed."""
    text = _status_text()
    if not text or not text.strip():
        return None
    out: dict[str, _Rec] = {}
    for stanza in re.split(r"\n\s*\n", text.strip()):
        f: dict[str, str] = {}
        key = ""
        for ln in stanza.split("\n"):
            if ln[:1] in (" ", "\t") and key:
                f[key] += "\n" + ln
            elif ":" in ln:
                key, _, val = ln.partition(":")
                f[key] = val.strip()
            else:
                return None
        pkg, arch, st = f.get("Package", ""), f.get("Architecture", ""), f.get("Status", "").split()
        if not _PKG.fullmatch(pkg) or not arch or len(st) != 3:
            return None
        name = pkg if arch in (native, "all") else f"{pkg}:{arch}"
        conf = []
        for ln in f.get("Conffiles", "").split("\n"):
            w = ln.split()
            if w:
                conf.append((w[0], w[1] if len(w) > 1 else ""))
        needs = _rel_names(f.get("Depends", "")) | _rel_names(f.get("Pre-Depends", "")) | _rel_names(f.get("Recommends", ""))
        out[name] = _Rec(st[2] not in ("config-files", "not-installed"), arch, f.get("Multi-Arch", "") == "foreign",
                         frozenset(needs), frozenset(n for n, _ in _rel_names(f.get("Provides", ""))), tuple(conf))
    return out or None


def _referrer_index(db: dict[str, _Rec]) -> dict[str, list[tuple[str, str, str]]]:
    """{needed name: [(installed package, its architecture, arch qualifier)]}."""
    idx: dict[str, list[tuple[str, str, str]]] = {}
    for n, r in db.items():
        if r.installed:
            for dep, qual in r.needs:
                idx.setdefault(dep, []).append((n, r.arch, qual))
    return idx


def _referrers(name: str, db: dict[str, _Rec], index: dict[str, list[tuple[str, str, str]]]) -> set[str]:
    """Installed packages that Depend on / Pre-Depend on / Recommend `name` (or a virtual package it Provides) in ITS
    architecture: `libfoo` needed by an amd64 package is the amd64 libfoo, not the i386 twin (":any", Multi-Arch: foreign
    and arch:all are matched for every architecture, the careful way)."""
    rec = db.get(name)
    if rec is None:
        return set()
    out: set[str] = set()
    for nm in {name.split(":")[0]} | set(rec.provides):
        for ref, rarch, qual in index.get(nm, ()):
            if ref != name and (qual == "any" or rec.foreign or "all" in (rarch, rec.arch) or (qual or rarch) == rec.arch):
                out.add(ref)
    return out


def _edited_conf(rec: _Rec | None) -> str:
    """'' when every conffile of the package is pristine (or already deleted by the owner), else why a purge is not safe:
    `apt purge` deletes locally edited config for good. An unreadable file or an unusable recorded md5 counts as edited."""
    if rec is None:
        return "package not in the dpkg database"
    for path, md5 in rec.conffiles:
        if not path.startswith("/") or not re.fullmatch(r"[0-9a-f]{32}", md5):
            return f"conffile {os.path.basename(path)[:40]} has no usable checksum"
        h = hashlib.md5(usedforsecurity=False)
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    h.update(chunk)
        except FileNotFoundError:
            continue
        except OSError:
            return f"conffile {os.path.basename(path)[:40]} unreadable"
        if h.hexdigest() != md5:
            return f"conffile {path[-60:]} was edited (purge would delete it)"
    return ""


def _opt_list(ctx: Ctx, key: str, pattern: str = "") -> list[str] | str:
    """A list-of-strings owner option, or 'bad config: ...'. A bare string would be iterated per character and silently turn a
    keep/never list into a no-op (fail OPEN); anything that is not a list of non-empty strings (and, for `pattern`, matching
    it) is refused so the task skips instead."""
    v = ctx.opt(key, [])
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in v) \
            or (pattern and not all(re.fullmatch(pattern, x) for x in v)):
        return f"bad config: {key} must be a list of " + ("branch numbers like '535'" if pattern else "non-empty strings")
    return [x.strip() for x in v]


def _failed_units() -> set[str] | None:
    r = sh(["systemctl", "--failed", "--no-legend", "--plain"], timeout=30)
    if r.returncode != 0:
        return None
    return {ln.split()[0] for ln in r.stdout.splitlines() if ln.split()}


def _failed_user_units() -> dict[str, set[str]] | None:
    """{user: failed user units} for every logged-in/lingering user (a package removal can break a user session that the
    system-unit check cannot see); None when it cannot be read."""
    r = sh(["loginctl", "list-users", "--no-legend"], timeout=20)
    if r.returncode != 0:
        return None
    out: dict[str, set[str]] = {}
    for ln in r.stdout.splitlines():
        f = ln.split()
        if len(f) < 2 or not f[0].isdigit() or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", f[1]):
            continue
        q = sh(["systemctl", "--user", "--machine", f"{f[1]}@.host", "--failed", "--no-legend", "--plain"], timeout=30)
        if q.returncode != 0:
            return None
        out[f[1]] = {x.split()[0] for x in q.stdout.splitlines() if x.split()}
    return out


# =========================================================================== stale_driver_packages
# Driver-branch packages: the branch number is part of the NAME (nvidia-utils-580, libnvidia-gl-580:i386,
# xserver-xorg-video-nvidia-575, nvidia-firmware-580-580.173.02, nvidia-driver-580-open) or follows linux-modules-nvidia-.
_FAMILY = re.compile(r"^(?:(?:lib)?nvidia|xserver-xorg-video-nvidia)-(?:[a-z0-9+.]+-)*?(?P<br>\d{3,4})"
                     r"(?:-(?P<full>\d{3,4}\.\d+(?:\.\d+)?))?(?:-(?:open|server|server-open|no-dkms))?$")
_KMOD = re.compile(r"^linux-(?:modules|objects|signatures)-nvidia-(?P<br>\d{3,4})(?:-.+)?$")
# never part of a driver set, whatever their name looks like
_NEVER = re.compile(r"container|cuda|toolkit|settings|prime|docker|nsight|profiler|visual|opencl-dev", re.I)
_DKMS = re.compile(r"^(?P<mod>[\w.+-]+)[/,]\s*(?P<ver>[\w.+-]+),\s*(?P<kern>[^,:]+),\s*(?P<arch>[^:]+):\s*(?P<state>.+)$")


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _branch_of(name: str) -> tuple[str, str] | None:
    """(branch, full version or '') for a driver-branch package name, else None."""
    base = name.split(":")[0]
    if _NEVER.search(base):
        return None
    m = _FAMILY.match(base) or _KMOD.match(base)
    return (m.group("br"), m.groupdict().get("full") or "") if m else None


def _verify_clean(names: list[str]) -> tuple[bool, str]:
    """`dpkg --verify` on the loaded driver's packages: only config-file ('c') differences are tolerated."""
    r = sh(["dpkg", "--verify", *names], timeout=900)
    if r.returncode not in (0, 1):
        return False, f"dpkg --verify rc={r.returncode}"
    bad = []
    for ln in r.stdout.splitlines():
        f = ln.split()
        if f and not (len(f) >= 3 and f[1] == "c"):
            bad.append(f[-1])
    if bad:
        return False, f"dpkg --verify: {len(bad)} changed files e.g. {os.path.basename(bad[0])}"
    return True, f"dpkg --verify clean ({len(names)} pkgs)"


def _dkms_has(version: str, kernel: str) -> bool | None:
    """True/False from `dkms status`; None when dkms is not installed or unreadable."""
    r = sh(["dkms", "status"], timeout=60)
    if r.returncode != 0:
        return None
    for ln in r.stdout.splitlines():
        m = _DKMS.match(ln.strip())
        if m and m["mod"] == "nvidia" and m["ver"] == version and m["kern"] == kernel \
                and m["state"].strip().startswith("installed"):
            return True
    return False


def _module_problem(version: str) -> str:
    """'' when the nvidia module the NEXT boot loads (modinfo, i.e. what depmod points at for the running kernel) is
    `version` and its file exists. dkms removes the module file when the removed build was the ACTIVE one, and nvidia-smi /
    `dkms status` cannot see that because the running module is already in memory."""
    v = sh(["modinfo", "-F", "version", "nvidia"], timeout=15)
    if v.returncode != 0 or v.stdout.strip() != version:
        return f"modinfo nvidia reports {_ascii(v.stdout.strip() or 'nothing', 20)}, not {version}"
    f = sh(["modinfo", "-F", "filename", "nvidia"], timeout=15)
    fn = (f.stdout.split() or [""])[0] if f.returncode == 0 else ""
    if not fn.startswith("/") or not os.path.exists(fn):
        return "nvidia module file is missing"
    return ""


def _smi_ok(version: str) -> bool:
    r = sh(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=60)
    return r.returncode == 0 and r.stdout.split() != [] and all(x == version for x in r.stdout.split())


@task("stale_driver_packages", klass="C1", tier="daily", title="Stale NVIDIA driver packages", timeout=1800,
      needs_root=True)
def stale_driver_packages(ctx: Ctx) -> Result:
    """Purge apt packages of NVIDIA driver branches that are NOT the loaded kernel module.

    Proofs, all required: the loaded driver is known (/sys and /proc agree) and the module ON DISK for the running kernel
    is that same version (else a reboot is pending); its own packages are installed and `dpkg --verify` clean; no newer
    branch is installed; the loaded driver has a module for the running kernel (dkms status, else
    linux-modules-nvidia-<ver>-<kernel>); none of the stale packages is on apt hold, has a locally edited conffile or an
    unreadable file list; no stale library is mapped/open/named by a process; and `apt-get -s purge <stale set>` removes
    EXACTLY that set. Afterwards nvidia-smi must still report the loaded version, dkms must still list it, the loaded
    packages must still verify and `modinfo nvidia` (the module the NEXT boot loads) must still be the loaded version with its
    file present; otherwise crit with the dkms command that restores it.
    """
    keep_br = _opt_list(ctx, "keep_branches", r"\d{3,4}")
    if isinstance(keep_br, str):
        return _skipped(f"{keep_br}: nothing touched")
    drv = inuse.loaded_nvidia_driver()
    if not drv.version:
        return _skipped(f"{drv.why}: nothing touched")
    why = _apt_ready(ctx) or _dpkg_clean()
    if why:
        return _skipped(f"{why}: nothing done")
    native = _native_arch()
    pk = _installed(native) if native else None
    if pk is None:
        return _skipped("dpkg state unreadable: nothing done")
    keep_br = set(keep_br)
    L, kernel = drv.branch, _uname()
    fam = {n: (b, p) for n, p in pk.items() if (b := _branch_of(n))}      # driver-branch packages: (branch, full), pkg
    base = {"mode": "apply" if ctx.apply else "report", "loaded": drv.version, "kernel": kernel}
    if drv.ondisk != drv.version:                  # the module the next boot loads is not the one running: never purge then
        return _skipped(f"nvidia module on disk for {kernel} is {drv.ondisk or 'unreadable'} but {drv.version} is loaded "
                        f"(reboot pending?): nothing touched", **base)
    # --- anything odd in the driver packages' dpkg state, or a newer branch than the loaded one => refuse everything
    broken = sorted(n for n, (_b, p) in fam.items() if p.state == "bad")
    if broken:
        return _skipped(f"dpkg state of {broken[0]} is inconsistent: nothing touched", **base)
    newer = sorted({b[0] for n, (b, p) in fam.items() if p.state == "i" and int(b[0]) > int(L)})
    if newer:
        return _skipped(f"driver branch {newer[0]} installed but {L} loaded (reboot pending?): nothing touched", **base)
    pend = sorted(n for n, (b, p) in fam.items() if p.state == "i" and b[0] == L and not n.startswith("linux-")
                  and ((b[1] and _vtuple(b[1]) > _vtuple(drv.version))          # nvidia-firmware-580-<newer build>
                       or (not b[1] and re.match(r"\d{3,4}\.", p.version) and p.version.split("-")[0] != drv.version)))
    if pend:
        return _skipped(f"{pend[0]} is {pk[pend[0]].version.split('-')[0]} but {drv.version} is loaded "
                        f"(reboot pending?): nothing touched", **base)
    stale = sorted(n for n, (b, p) in fam.items()
                   if (b[0] != L or (b[1] and b[1] != drv.version)) and b[0] not in keep_br)
    branches = sorted({fam[n][0][0] for n in stale})
    base["stale_branches"] = branches
    if not stale:
        return Result("ok", _ascii(f"no stale NVIDIA driver packages (loaded {drv.version})"),
                      {**base, "selected": 0, "stale_pkgs": 0})
    size = sum(pk[n].size for n in stale)
    rows = [_row(n, pk[n].size, "stale", f"branch {fam[n][0][0]} != loaded {L}") for n in
            sorted(stale, key=lambda n: (-pk[n].size, n))]

    def refuse(reason: str) -> Result:
        res = _skipped(f"{len(stale)} stale pkgs kept: {reason}", **base, stale_pkgs=len(stale), selected=0,
                       refused=_ascii(reason, 100))
        res.status = "info"
        res.items = [_row("refused", "-", "kept", reason)] + [dict(r, state="kept") for r in rows][:11]
        return res

    # --- proofs ------------------------------------------------------------------------------------------------
    held = _holds(native)
    if held is None:
        return refuse("apt-mark showhold failed")
    on_hold = sorted(n for n in stale if n.split(":")[0] in held)
    if on_hold:
        return refuse(f"{on_hold[0]} is on apt hold (owner decision)")
    loaded_pkgs = sorted(n for n, (b, p) in fam.items() if p.state == "i" and b[0] == L)
    for stem in ("nvidia-utils", "libnvidia-compute"):
        if not any(n.startswith(stem + "-") and ":" not in n for n in loaded_pkgs):     # the native-arch package
            return refuse(f"loaded driver {L} has no {stem}-{L} package (not installed via dpkg?)")
    ok, v_why = _verify_clean(loaded_pkgs)
    if not ok:
        return refuse(v_why)
    dk = _dkms_has(drv.version, kernel)
    if dk is None:
        have = [n for n in loaded_pkgs if n.startswith("linux-modules-nvidia-") and kernel in n]
        if not have:
            return refuse(f"no dkms and no linux-modules-nvidia-{L} for kernel {kernel}")
        mod_why = f"{have[0]} installed for {kernel}"
    elif not dk:
        return refuse(f"dkms has no nvidia/{drv.version} installed for {kernel}")
    else:
        mod_why = f"dkms nvidia/{drv.version} installed for {kernel}"
    db = _dpkg_db(native)
    if db is None:
        return refuse("dpkg status file unreadable")
    for n in stale:                                  # purge deletes edited conffiles for good: the owner decides about those
        edited = _edited_conf(db.get(n))
        if edited:
            return refuse(f"{n}: {edited}")
    libs = _libs_of(stale, native, pk)
    if isinstance(libs, str):
        return refuse(libs)
    mapped = inuse.files_in_use(libs, inuse.ALL_KINDS)
    bad = [r for r in mapped.values() if r.used]
    if bad:
        return refuse(f"stale driver file in use: {bad[0].why}")
    removed, sim_why = _sim_purge(stale, native)
    if removed is None:
        return refuse(f"apt-get -s purge failed: {sim_why}")
    if sorted(removed) != sorted(stale):
        extra = sorted(set(removed) - set(stale))
        miss = sorted(set(stale) - set(removed))
        return refuse("purge would remove extras " + ",".join(extra[:2]) if extra
                      else f"purge would not remove {','.join(miss[:2])}")
    proof = f"{v_why}; {mod_why}; apt -s purge == stale set ({len(stale)}); no stale lib mapped"
    rows.insert(0, _row("proofs", "-", "ok", proof))

    # --- act ---------------------------------------------------------------------------------------------------
    post_failed: list[str] = []
    label = f"stale-driver-set:{'+'.join(branches)}"      # no "nvidia": protected.toml patterns must not veto it

    def purge() -> None:
        # last-moment re-proof: apt idle, the same closure, nothing mapped now (a fresh /proc snapshot)
        again = _apt_ready(ctx)
        if again:
            raise _Changed(again)
        inuse.proc_snapshot(refresh=True)
        libs2 = _libs_of(stale, native, pk)
        if isinstance(libs2, str):
            raise _Changed(libs2)
        if any(r.used for r in inuse.files_in_use(libs2, inuse.ALL_KINDS).values()):
            raise _Changed("a stale driver library became in use")
        names2, _ = _sim_purge(stale, native)
        if names2 is None or sorted(names2) != sorted(stale):
            raise _Changed("purge closure changed")
        _run_ok(["apt-get", "-y", "purge", *stale], 1500, APT_ENV)
        problems = []
        if not _smi_ok(drv.version):
            problems.append(f"nvidia-smi no longer reports {drv.version}")
        if _dkms_has(drv.version, kernel) is False:
            problems.append(f"dkms lost nvidia/{drv.version}")
        if not _verify_clean(loaded_pkgs)[0]:
            problems.append(f"loaded {L} driver files no longer verify")
        if _module_problem(drv.version):
            problems.append(f"module for the next boot is gone: dkms install nvidia/{drv.version} -k {kernel}")
        if problems:
            post_failed.extend(problems)
            raise RuntimeError("; ".join(problems))

    acts = _Acts(ctx)
    acts.run("apt-purge-stale-nvidia", label, size, purge, label=label)
    res = acts.result("sets", {**base, "stale_pkgs": len(stale), "set_h": human(size)})
    n_sel = len(stale) if (acts.n["done"] if ctx.apply else acts.n["would"]) else 0
    res.metrics["selected"] = n_sel
    if ctx.apply and acts.n["done"]:
        res.summary = _ascii(f"purged {len(stale)} stale NVIDIA pkgs ({','.join(branches)}), freed {human(size)}; "
                             f"nvidia-smi {drv.version} ok")
    elif not ctx.apply and acts.n["would"]:
        res.summary = _ascii(f"report: would purge {len(stale)} stale NVIDIA pkgs ({','.join(branches)}, "
                             f"{human(size)}); loaded {drv.version}")
    if post_failed:
        res.status, res.summary = "crit", _ascii(f"PURGED stale NVIDIA pkgs but {'; '.join(post_failed)}")
    if n_sel:
        for r in rows:
            if r["state"] == "stale":
                r["state"] = "purged" if acts.n["done"] else "would purge"
    res.items = rows[:12]
    if post_failed:                                  # the exact words of every problem, whatever the summary had room for
        res.items = [_row("post-purge check", "-", "CRIT", "; ".join(post_failed))] + rows[:11]
    return res


def _libs_of(names: list[str], native: str, pk: dict[str, _Pkg]) -> list[str] | str:
    """Shared-library paths (existing or not: a deleted file can stay mapped) of the installed stale packages, or a
    refusal text when a package's file list cannot be read or is empty (damaged dpkg metadata: nothing to check is not
    proof that nothing is in use). Config-only (rc) packages own no files."""
    out: list[str] = []
    for n in names:
        if pk[n].state != "i":
            continue
        files = _pkg_files(n, native)
        if files is None:
            return f"file list of {n} unreadable"
        if not [p for p in files if p.startswith("/") and p != "/."]:
            return f"file list of {n} is empty"
        out += [p for p in files if p.startswith("/") and _SO.search(os.path.basename(p))]
    return out


# =========================================================================== apt_autoremove_unused
_NEVER_AUTO = (
    # kernel, boot, init, libc, platform
    "linux-*", "dkms*", "grub*", "shim*", "initramfs*", "systemd*", "libc6*", "ubuntu-*", "snapd*", "docker*", "containerd*",
    "libvirt*", "qemu*", "apparmor*", "cloud-*",
    # GPU/compute stacks: dlopen()ed by ComfyUI/torch/nvcc builds only while they run, so "not mapped now" proves nothing
    "*nvidia*", "*cuda*", "libcud*", "libcub*", "libcuf*", "libcupti*", "libcurand*", "libcus*", "libcut*", "libnv*",
    "libnccl*", "nsight*", "tensorrt*",
    # JDKs (Gradle builds), firmware/microcode, virtualisation firmware, storage/crypto/network/remote-access stack
    "openjdk*", "*-jdk*", "*-jre*", "default-j*", "ovmf", "swtpm*", "*microcode*", "firmware-*", "mdadm", "lvm2", "zfs*",
    "cryptsetup*", "netplan*", "network-manager*", "openssh-*", "ufw", "nftables", "iptables")


@task("apt_autoremove_unused", klass="C1", tier="daily", title="Unused auto-installed packages", timeout=1800,
      needs_root=True)
def apt_autoremove_unused(ctx: Ctx) -> Result:
    """Purge the `apt autoremove --purge` candidates that are provably not in use.

    A candidate is kept when ANY holds: it matches the owner's `keep` list; it is on apt hold or protected.toml; it is a
    kernel/driver/boot/GPU/JDK/runtime-critical package (`never` list, extendable via `never`); a file of it is mapped, an
    exe, open, a cwd, or named on the command line/environment of a running process (inuse.py, the whole host must be
    visible and readable); it ships anything /proc cannot speak for (executables, scripts, interpreter modules, D-Bus,
    autostart, systemd, cron, udev, polkit, PAM, .desktop, data: only shared libraries/plugins under /usr/lib and
    documentation qualify, unless the owner lists the package in `allow_purge`); a conffile of it was edited by the owner;
    an installed package that is NOT itself selected Depends on or Recommends it (iterated to a fixed point; purge
    simulation ignores Recommends); or purging it alone would remove a package that is NOT itself selected (closure). The
    surviving set is finally simulated as a whole and must equal itself exactly (`apt-get -s purge`), then purged in ONE apt
    run, and neither `systemctl --failed` nor the failed user units of logged-in users may gain entries.
    """
    keep, never, allow = (_opt_list(ctx, k) for k in ("keep", "never", "allow_purge"))
    badcfg = next((v for v in (keep, never, allow) if isinstance(v, str)), "")
    if badcfg:
        return _skipped(f"{badcfg}: nothing done")
    never = [*_NEVER_AUTO, *never]
    why = _apt_ready(ctx)
    if why:
        return _skipped(f"{why}: nothing done")
    native = _native_arch()
    pk = _installed(native) if native else None
    held = _holds(native) if native else None
    if pk is None or held is None:
        return _skipped("dpkg/apt state unreadable: nothing done")
    problem = _dpkg_clean()
    if problem:
        return _skipped(f"{problem}: nothing done")
    cands, sim_why = _sim(["autoremove", "--purge"], native)
    if cands is None:
        return _skipped(f"autoremove simulation failed ({sim_why}): nothing done")
    base = {"mode": "apply" if ctx.apply else "report", "candidates": len(cands)}
    if not cands:
        return Result("ok", "no autoremove candidates", {**base, "selected": 0})
    db = _dpkg_db(native)
    if db is None:
        return _skipped("dpkg status file unreadable: nothing done")
    max_sims = int(_num(ctx.opt("max_candidates", 80), 1, 1000) or 80)
    state: dict[str, tuple[str, str]] = {}        # name -> (verdict, proof); verdict "ok" or a "kept: ..." reason
    for n in cands:
        base_n = n.split(":")[0]
        if any(fnmatch.fnmatchcase(base_n, k) or fnmatch.fnmatchcase(n, k) for k in keep):
            state[n] = ("kept: owner keep-list", "in keep list")
        elif base_n in held:
            state[n] = ("kept: on apt hold", "apt-mark hold")
        elif ctx.is_protected(base_n):
            state[n] = ("kept: protected", "matches protected.toml")
        elif any(fnmatch.fnmatchcase(base_n, k) for k in never):
            state[n] = ("kept: critical package class", "kernel/driver/boot/GPU/runtime class")
    pending = [n for n in cands if n not in state]
    for n in pending[max_sims:]:
        state[n] = ("kept: deferred (too many)", f"max_candidates={max_sims}")
    pending = pending[:max_sims]
    for n in list(pending):                                       # in-use proof: one /proc snapshot for every package
        inv = _pkg_inventory(n, native)
        pr = _pkg_in_use(n, native, inv)
        edited = _edited_conf(db.get(n)) if not pr.used else ""
        if pr.used:
            state[n] = ("kept: in use" if pr.known else "kept: unknown (cannot prove)", pr.why)
        elif inv.odd and not any(fnmatch.fnmatchcase(n.split(":")[0], a) or fnmatch.fnmatchcase(n, a) for a in allow):
            state[n] = ("kept: not provable from /proc", inv.odd)
        elif edited:
            state[n] = ("kept: edited config", edited)
        else:
            continue
        pending.remove(n)
    # closure: purge each survivor alone; it may only take other survivors with it
    removals: dict[str, set[str] | None] = {}
    for n in pending:
        r, w = _sim_purge([n], native)
        removals[n] = None if r is None else set(r)
        if r is None:
            state[n] = ("kept: closure unknown", f"apt -s purge failed: {w}")
    sel = {n for n in pending if removals[n] is not None}
    index = _referrer_index(db)
    changed = True
    while changed:
        changed = False
        for n in sorted(sel):
            extra = removals[n] - sel
            refs = sorted(_referrers(n, db, index) - sel)
            if extra:
                sel.discard(n)
                state[n] = ("kept: closure", "purge would also remove " + ",".join(sorted(extra)[:2]))
                changed = True
            elif refs:                                            # Depends/Recommends of a package that STAYS (not in the sim)
                sel.discard(n)
                state[n] = ("kept: needed by installed pkg", f"{refs[0]} depends on or recommends it")
                changed = True
    final = sorted(sel)
    if final:
        whole, w = _sim_purge(final, native)
        if whole is None or sorted(whole) != final:
            for n in final:
                state[n] = ("kept: closure", "combined purge differs from the set" + (f" ({w})" if w else ""))
            final = []
    for n in final:
        state[n] = ("ok", "not mapped/open/named by any process; shared libs/plugins only; closure ok")
    rows = [_row(n, pk[n].size if n in pk else 0, "would purge" if v == "ok" else v, p)
            for n, (v, p) in state.items()]
    rows.sort(key=lambda r: (r["state"] != "would purge", r["state"], -_size_of(r["size"]), r["name"]))
    kept = len(cands) - len(final)
    size = sum(pk[n].size for n in final if n in pk)
    metrics = {**base, "selected": len(final), "kept": kept, "set_h": human(size)}
    if not final:
        res = Result("ok", _ascii(f"no unused package to purge ({len(cands)} candidates kept: in use/keep-list/closure)"),
                     metrics, rows[:12])
        return res

    # --- act: ONE apt run for the whole verified set -------------------------------------------------------------
    before, before_u = _failed_units(), _failed_user_units()
    if ctx.apply and before is None:
        return _skipped("systemctl --failed unreadable (cannot verify afterwards): nothing done")
    regress: list[str] = []
    unverified: list[str] = []

    def purge() -> None:
        again = _apt_ready(ctx)
        if again:
            raise _Changed(again)
        inuse.proc_snapshot(refresh=True)                          # fresh snapshot for the last-moment re-proof
        for n in final:
            if not _pkg_in_use(n, native).unused:
                raise _Changed(f"{n} became in use")
        whole2, _ = _sim_purge(final, native)
        if whole2 is None or sorted(whole2) != final:
            raise _Changed("purge closure changed")
        _run_ok(["apt-get", "-y", "purge", *final], 1500, APT_ENV)
        after = _failed_units()
        if after is None:
            unverified.append("systemctl --failed unreadable after the purge")
        else:
            regress.extend(sorted(after - (before or set())))
        if before_u is not None:                                   # user sessions: only comparable when readable before
            after_u = _failed_user_units()
            if after_u is None:
                unverified.append("user units unreadable after the purge")
            else:
                regress.extend(f"{u}:{x}" for u, xs in sorted(after_u.items()) for x in sorted(xs - before_u.get(u, set())))

    acts = _Acts(ctx)
    acts.run("apt-purge-unused", "apt-autoremove-set", size, purge, label=f"{len(final)} packages")
    res = acts.result("sets", {**metrics})
    done = acts.n["done"] if ctx.apply else acts.n["would"]
    if ctx.apply and acts.n["done"]:
        res.summary = _ascii(f"purged {len(final)} unused pkgs, freed {human(size)}; {kept} kept")
    elif not ctx.apply and done:
        res.summary = _ascii(f"report: would purge {len(final)} unused pkgs ({human(size)}); {kept} kept")
    if regress:
        res.status = "crit"
        res.summary = _ascii(f"purged {len(final)} pkgs but NEW failed units: {', '.join(regress[:3])}")
    elif unverified:
        res.status, res.summary = "warn", _ascii(f"purged {len(final)} pkgs; {unverified[0]}")
    for r in rows:
        if r["state"] == "would purge" and ctx.apply and acts.n["done"]:
            r["state"] = "purged"
    res.items = rows[:12]
    return res


def _size_of(h: str) -> float:
    m = re.fullmatch(r"([\d.]+) (B|KiB|MiB|GiB|TiB)", str(h))
    return float(m.group(1)) * 1024 ** ["B", "KiB", "MiB", "GiB", "TiB"].index(m.group(2)) if m else 0.0


# =========================================================================== flatpak_unused
_FP_ROW = re.compile(r"^\s*\d+\.\s+(.*)$")
_FP_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _fp_cmd(spec: str) -> tuple[list[str], str] | None:
    """('flatpak command prefix', label) for 'system' or 'user:<name>'; None when it cannot be run from here."""
    if spec == "system":
        return ["flatpak", "--system"], "system"
    m = re.fullmatch(r"user:([a-z_][a-z0-9_-]{0,31})", spec)
    if not m:
        return None
    try:
        pw = pwd.getpwnam(m.group(1))
    except KeyError:
        return None
    if _euid() == 0 and pw.pw_uid != 0:
        return ["runuser", "-u", pw.pw_name, "--", "flatpak", "--user"], f"user {pw.pw_name}"
    if _euid() == pw.pw_uid:
        return ["flatpak", "--user"], f"user {pw.pw_name}"
    return None


def _fp_list(pre: list[str], kind: str) -> dict[tuple[str, str], tuple[int, str]] | None:
    """{(id, branch): (bytes, active commit prefix)} of installed apps or runtimes of one installation."""
    r = sh([*pre, "list", f"--{kind}", "--columns=application,branch,size,active"], timeout=60)
    if r.returncode != 0:
        return None
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for ln in r.stdout.splitlines():
        f = [x.strip() for x in ln.split("\t")]
        if len(f) >= 2 and _FP_NAME.fullmatch(f[0]) and _FP_NAME.fullmatch(f[1]):
            size = (_parse_size(re.sub(r"\s", "", f[2])) or 0) if len(f) > 2 else 0      # flatpak prints "668.9<nbsp>MB"
            commit = f[3] if len(f) > 3 and re.fullmatch(r"[0-9a-f]{6,64}", f[3]) else ""
            out[(f[0], f[1])] = (size, commit[:12])
    return out


def _fp_app_runtimes(pre: list[str]) -> set[tuple[str, str]] | None:
    """{(runtime id, branch)} the installed APPS of one installation run on (`flatpak list --app --columns=runtime`, rows like
    org.gnome.Platform/x86_64/46); None when unreadable or a row is not understood."""
    r = sh([*pre, "list", "--app", "--columns=runtime"], timeout=60)
    if r.returncode != 0:
        return None
    out: set[tuple[str, str]] = set()
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        f = ln.strip().split("/")
        if len(f) != 3 or not _FP_NAME.fullmatch(f[0]) or not _FP_NAME.fullmatch(f[2]):
            return None
        out.add((f[0], f[2]))
    return out


def _fp_consumers(specs: list) -> dict[str, set[tuple[str, str]] | None]:
    """{installation label: runtimes its apps use} for every installation whose apps could be running on a runtime installed
    in ANOTHER one: the configured ones, the system installation and every user installation under HOME_ROOT. flatpak's own
    `--unused` only sees the apps of the installation it is asked about (a user app on a system runtime is invisible to it).
    None = could not read that installation's apps (the caller must then refuse everything)."""
    want = [str(x) for x in specs] + ["system"]
    try:
        want += [f"user:{e.name}" for e in os.scandir(HOME_ROOT) if os.path.isdir(f"{e.path}/.local/share/flatpak")]
    except OSError:
        pass
    out: dict[str, set[tuple[str, str]] | None] = {}
    for spec in dict.fromkeys(want):
        got = _fp_cmd(spec)
        if got is not None and got[1] not in out:
            out[got[1]] = _fp_app_runtimes(got[0])
    return out


def _fp_unused(pre: list[str]) -> tuple[list[tuple[str, str]] | None, str]:
    """Refs `flatpak uninstall --unused` WOULD remove. This runs the MUTATING command without -y/--noninteractive and
    answers its [Y/n] prompt with an explicit "n" on stdin (never relying on what EOF means to a given flatpak version); if
    the prompt line is not in the output the output is not trusted at all (something else happened)."""
    r = sh([*pre, "uninstall", "--unused"], timeout=120, input_="n\n")
    out = r.stdout or ""
    if "Nothing unused to uninstall" in out and r.returncode == 0:
        return [], ""
    if not re.search(r"Proceed with these changes to the .*installation\? \[Y/n\]: n\s*$", out) or "Uninstalling" in out:
        return None, "unexpected flatpak output"
    rows = []
    for ln in out.splitlines():
        m = _FP_ROW.match(ln)
        if not m:
            continue
        f = [x.strip() for x in m.group(1).split("\t") if x.strip()]
        if len(f) != 3 or f[2] != "r" or not _FP_NAME.fullmatch(f[0]) or not _FP_NAME.fullmatch(f[1]):
            return None, "unexpected row in flatpak table"
        rows.append((f[0], f[1]))
    return rows, ""


def _fp_running() -> tuple[set[str], set[tuple[str, str]], set[str]] | None:
    """What running flatpak sandboxes use, from /proc/<pid>/root/.flatpak-info: (commit prefixes, (runtime id, branch),
    extension ids). None when /proc is not fully readable (not root): an unseen sandbox could be using a runtime."""
    if not inuse.proc_snapshot().ok:
        return None
    try:
        names = os.listdir(inuse.PROC)
    except OSError:
        return None
    commits: set[str] = set()
    refs: set[tuple[str, str]] = set()
    ids: set[str] = set()
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"{inuse.PROC}/{n}/root/.flatpak-info", encoding="utf-8", errors="replace") as f:
                text = f.read(1 << 20)
        except OSError:
            continue                                              # not a flatpak sandbox (the normal case) or gone
        commits |= {c[:12] for c in re.findall(r"[0-9a-f]{64}", text)}
        ids |= set(re.findall(r"(?:^|[=;])([A-Za-z0-9_][A-Za-z0-9_.-]*)=[0-9a-f]{64}", text, re.M))
        refs |= {(m[0], m[2]) for m in re.findall(r"^(?:runtime|sdk)=runtime/([^/\s]+)/([^/\s]+)/([^/\s]+)$", text, re.M)}
    return commits, refs, ids


def _fp_busy() -> bool:
    """A `flatpak` CLI process (install/update/uninstall) is running right now (daemons like flatpak-portal are not)."""
    try:
        names = os.listdir(inuse.PROC)
    except OSError:
        return True
    for n in names:
        if n.isdigit():
            try:
                if (inuse.PROC / n / "comm").read_text().strip() == "flatpak":
                    return True
            except OSError:
                continue
    return False


@task("flatpak_unused", klass="C1", tier="weekly", title="Unused Flatpak runtimes", timeout=900, needs_root=True)
def flatpak_unused(ctx: Ctx) -> Result:
    """`flatpak uninstall --unused` per configured installation, only when its whole list is runtimes/extensions.

    The list comes from flatpak itself (it knows pins, extensions and EOL rebases); every ref must be an installed
    RUNTIME and not an application, none may be used by a running sandbox (matched by commit against every
    /proc/*/root/.flatpak-info), and no `flatpak` command may be running. After an apply the installed application set
    must be unchanged (else crit).
    """
    specs = ctx.opt("installations", ["system"])
    if not isinstance(specs, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in specs):
        return _skipped("bad config: installations must be a list of non-empty strings: nothing done")
    if not specs:
        return Result("ok", "no flatpak installations configured", {"mode": "report", "selected": 0})
    if _fp_busy():
        return _skipped("a flatpak command is running: nothing done")
    running = _fp_running()
    if running is None:
        return _skipped("cannot see every process (not root): nothing done")
    if ctx.apply and _euid() != 0:
        return _skipped("needs root: nothing done")
    acts, rows, total, selected, unavailable, refused = _Acts(ctx), [], 0, 0, 0, 0
    crit: list[str] = []
    consumers = _fp_consumers(specs)
    for spec in specs:
        got = _fp_cmd(str(spec))
        if got is None:
            unavailable += 1
            rows.append(_row(str(spec), "-", "skipped: cannot run from here"))
            continue
        pre, label = got
        apps, runtimes = _fp_list(pre, "app"), _fp_list(pre, "runtime")
        unused, w = _fp_unused(pre)
        if apps is None or runtimes is None or unused is None:
            unavailable += 1
            rows.append(_row(label, "-", "skipped: flatpak unreadable", w))
            continue
        if not unused:
            rows.append(_row(label, "-", "nothing unused", "flatpak --unused lists nothing"))
            continue
        problems = [f"{i}//{b}" for i, b in unused if (i, b) not in runtimes or (i, b) in apps]
        if problems:                                              # "never an app": ANY non-runtime row refuses the lot
            refused += 1
            rows.append(_row(label, "-", "refused: not all runtimes", f"{problems[0]} is not an installed runtime"))
            continue
        other = {k: v for k, v in consumers.items() if k != label}
        if any(v is None for v in other.values()):                # cannot see every installation's apps: fail closed
            refused += 1
            rows.append(_row(label, "-", "refused: cannot cross-check", "apps of another installation unreadable"))
            continue
        cross = next((f"an app of {k} runs on {i}//{b}" for k, v in sorted(other.items()) for i, b in sorted(v or ())
                      if (i, b) in runtimes), "")
        if cross:                                                 # flatpak's list ignores other installations' apps
            refused += 1
            rows.append(_row(label, "-", "refused: used across installations", cross))
            continue
        r_commits, r_refs, r_ids = running
        busy_ref = next((f"{i}//{b}" for i, b in unused
                         if (runtimes[(i, b)][1] and runtimes[(i, b)][1] in r_commits) or (i, b) in r_refs
                         or (not runtimes[(i, b)][1] and i in r_ids)), "")
        if busy_ref:                                              # flatpak cannot remove part of the list: refuse it all
            refused += 1
            rows.append(_row(label, "-", "refused: runtime running", f"{busy_ref} is used by a running sandbox"))
            continue
        size = sum(runtimes[k][0] for k in unused)
        total += size
        selected += len(unused)
        for i, b in unused:
            rows.append(_row(f"{i}//{b} ({label})", runtimes[(i, b)][0], "would remove",
                             "unused per flatpak; runtime/extension, not an app; no sandbox or other-installation app uses it"))

        def uninstall(pre=pre, before=set(apps), want=list(unused), label=label) -> None:
            now, w2 = _fp_unused(pre)
            if now is None or sorted(now) != sorted(want):
                raise _Changed("unused list changed")
            _run_ok([*pre, "uninstall", "--unused", "-y", "--noninteractive"], 600)
            after = _fp_list(pre, "app")
            if after is None or set(after) != before:
                crit.append(f"{label}: application list changed")
                raise RuntimeError("application list changed after uninstall")

        acts.run("flatpak-uninstall-unused", f"flatpak:{label}", size, uninstall, label=f"{label} {len(unused)} refs")
    res = acts.result("installations", {"installations": len(specs), "refs": selected, "refused": refused,
                                        "unavailable": unavailable, "set_h": human(total)})
    done = acts.n["done"] if ctx.apply else acts.n["would"]
    if ctx.apply and acts.n["done"]:
        res.summary = _ascii(f"removed {selected} unused flatpak runtimes, freed {human(total)}")
    elif not ctx.apply and done:
        res.summary = _ascii(f"report: would remove {selected} unused flatpak runtimes ({human(total)})")
    elif not selected:
        res.status = "skipped" if unavailable == len(specs) else "ok"
        res.summary = _ascii("no unused flatpak runtimes" + (f"; {refused} refused" if refused else "")
                             + (f"; {unavailable} unavailable" if unavailable else ""))
    if crit:
        res.status, res.summary = "crit", _ascii("flatpak uninstall changed the application list: " + crit[0])
    res.metrics["selected"] = selected if done else 0
    res.items = rows[:12]
    return res
